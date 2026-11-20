"""Product-layer view of an analysis for the Swing Guru app: checkpoints, score, focus.

Reads ``analysis.json`` only; never changes the engine or its outputs.

Two things the app shows are deliberately *not* computed from the current
provisional verdict bands:

* **Swing Score** - production uses the deterministic, versioned model in
  :mod:`swingguru.scoring` (:func:`swing_score`), stored with each swing so a
  later model never rewrites old results. :class:`StubScorer` still produces
  clearly flagged demo numbers (``source="stub"``) for UI review.
  :class:`UncalibratedScorer` is kept for callers that must not show a score.
* **Start here** - :class:`FocusResult` is filled in production only from an
  interpretation rule that is explicitly eligible for priority
  (``analysis["interpretations"]["focus"]``). Otherwise it is a neutral
  state. A flagged stub exists for demos. :class:`PriorityInputs` is the
  shape a future deterministic priority model will rank on - it compares
  normalised issue properties, never unlike raw measurements.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

SCORE_SCHEMA = "swing_score/1"
FOCUS_SCHEMA = "focus/1"

SOURCE_MODEL = "model"      # a calibrated scoring model / an eligible interpretation rule
SOURCE_STUB = "stub"        # demo data, never derived from the swing's measurements
SOURCE_NONE = "none"        # nothing to show yet

PHASE_LABELS = {"address": "Address", "takeaway": "Takeaway", "mid_back": "Mid-Back", "top": "Top",
                "downswing": "Mid-Down", "impact": "Impact"}
PHASE_ORDER = tuple(PHASE_LABELS)
STATUSES = ("good", "attention", "problem")

NEUTRAL_FOCUS = "Your focus will appear here as Swing Guru learns your swing."
SCORE_UNAVAILABLE = ("The Swing Score is still being calibrated, so it isn't shown yet. "
                     "Every checkpoint below is measured from your swing.")


# ---------------------------------------------------------------- checkpoints

def checkpoints(analysis: Mapping) -> list[dict]:
    """The guided, supported checkpoints of one swing, in swing order (real data)."""

    from .coaching import coaching_for

    out = []
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            verdict = check.get("verdict") or {}
            if not check.get("guided") or verdict.get("status") not in STATUSES:
                continue
            mapping = (verdict.get("mapping") or {})
            out.append({
                "id": f"{phase['phase']}.{check['kind']}",
                "phase": phase["phase"],
                "phase_label": PHASE_LABELS.get(phase["phase"], phase["phase"]),
                "name": check.get("checkpoint") or check.get("title"),
                "mapping": mapping.get("id"),
                "verdict_id": verdict.get("verdict_id"),
                "label": verdict.get("label"),
                "status": verdict.get("status"),
                "support": verdict.get("support"),
                "value": verdict.get("value") or check.get("value"),
                "estimated": bool(check.get("estimated")),
                "confidence": verdict.get("confidence"),
                "validation": mapping.get("validation", "provisional_ui_mapping"),
                "time_s": check.get("time_s"),
                "coaching": coaching_for(check, phase["phase"]),
            })
    return out


def status_counts(items: Sequence[Mapping]) -> dict:
    counts = {status: sum(1 for c in items if c["status"] == status) for status in STATUSES}
    counts["total"] = len(items)
    return counts


def withheld(analysis: Mapping) -> list[dict]:
    """Checkpoints the product would show that the engine withheld for this swing, with a plain reason."""

    from .verdicts import CHECKPOINTS

    shown = {c["id"] for c in checkpoints(analysis)}
    out, seen = [], set()
    for item in analysis.get("hidden", []):
        match = next((cp for cp in CHECKPOINTS if cp.phase == item.get("phase") and cp.mapping.kind == item.get("kind")), None)
        key = f"{item.get('phase')}.{item.get('kind')}"
        if match is None or key in shown or key in seen:
            continue
        seen.add(key)
        out.append({"id": key, "phase": match.phase, "phase_label": PHASE_LABELS.get(match.phase, match.phase),
                    "name": match.name, "status": "withheld", "reason": friendly_reason(item.get("reason", ""))})
    return out


def friendly_reason(reason: str) -> str:
    text = (reason or "").lower()
    if "camera moved" in text:
        return "Not measured: your phone moved during the swing"
    if "club" in text or "shaft" in text:
        return "Not measured: the club wasn't clear enough"
    if "moment not found" in text:
        return "Not measured: this moment wasn't found"
    if "confidence" in text or "tracking" in text or "visible" in text:
        return "Not measured: this part of the body wasn't clear enough"
    return "Not measured on this video"


def _check_name(c: Mapping) -> str:
    name = {"Head": "head movement"}.get(c["name"], (c["name"] or "").lower())
    return f"{c['phase_label']} {name}"


def _join(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def summary_text(items: Sequence[Mapping], missing: Sequence[Mapping] = ()) -> str:
    """A short, deterministic description of the verdicts. It names what was in and
    out of range; it never ranks issues or gives coaching advice."""

    if not items:
        return "We couldn't measure enough of this swing to show checks." if missing else ""
    sentences = []
    setup = [c for c in items if c["phase"] == "address"]
    if setup:
        good = sum(1 for c in setup if c["status"] == "good")
        sentences.append(f"Your set-up was solid: all {len(setup)} set-up checks were in range."
                         if good == len(setup) else
                         f"{good} of {len(setup)} set-up checks {'was' if good == 1 else 'were'} in range.")
    clearly = [_check_name(c) for c in items if c["status"] == "problem"]
    slightly = [_check_name(c) for c in items if c["status"] == "attention"]
    if not clearly and not slightly:
        sentences.append("Every check we could measure was in range.")
    else:
        parts = []
        if clearly:
            parts.append(f"{_join(clearly)} {'was' if len(clearly) == 1 else 'were'} clearly out of range")
        if slightly:
            parts.append(f"{_join(slightly)} {'was' if len(slightly) == 1 else 'were'} slightly out")
        text = "; ".join(parts)
        sentences.append(text[0].upper() + text[1:] + ".")
    if missing:
        sentences.append(f"{len(missing)} check{'s' if len(missing) > 1 else ''} couldn't be measured on this video.")
    return " ".join(sentences)


# ---------------------------------------------------------------- score

@dataclass
class ScoreContribution:
    checkpoint: str
    phase: str
    label: str | None
    weight: float
    points: float


@dataclass
class ScoreResult:
    """What any Swing Score model returns. ``total`` is out of ``scale``."""

    available: bool
    source: str                         # SOURCE_MODEL | SOURCE_STUB | SOURCE_NONE
    version: str | None
    total: float | None = None
    scale: int = 10
    confidence: str | None = None       # HIGH | MEDIUM | LOW once a model exists
    supported_count: int = 0            # supported measurements available to score
    contributions: list[ScoreContribution] = field(default_factory=list)
    reason: str | None = None
    schema: str = SCORE_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)


class Scorer(Protocol):
    name: str
    version: str | None

    def score(self, analysis: Mapping, *, swing_id: str) -> ScoreResult: ...


class UncalibratedScorer:
    """Production scorer until a calibrated model exists: never invents a number."""

    name = "uncalibrated"
    version = None

    def score(self, analysis: Mapping, *, swing_id: str) -> ScoreResult:
        return ScoreResult(available=False, source=SOURCE_NONE, version=None,
                           supported_count=len(checkpoints(analysis)), reason=SCORE_UNAVAILABLE)


def swing_score(analysis: Mapping) -> dict:
    """The swing's stored Swing Score (scored when the analysis finished), else scored now.

    Old results keep the model version they were scored with; a swing analysed
    before scoring existed is scored with the current version, which it reports."""

    from .scoring import SCHEMA, score_swing

    stored = analysis.get("swing_score")
    if isinstance(stored, Mapping) and stored.get("schema") == SCHEMA:
        return dict(stored)
    return score_swing(analysis)


class StubScorer:
    """Demo numbers for building the score UI. Derived from the swing id only -
    *not* from its measurements - and always flagged ``source="stub"``."""

    name = "stub"
    version = "stub-0"

    def score(self, analysis: Mapping, *, swing_id: str) -> ScoreResult:
        digest = int(hashlib.sha1(swing_id.encode("utf-8")).hexdigest()[:8], 16)
        total = round(4.6 + (digest % 43) / 10.0, 1)  # 4.6 - 8.8
        return ScoreResult(available=True, source=SOURCE_STUB, version=self.version, total=total,
                           confidence=None, supported_count=len(checkpoints(analysis)),
                           reason="Demo value - not calculated from this swing.")


# ---------------------------------------------------------------- focus ("Start here")

@dataclass
class PriorityInputs:
    """Normalised properties a future priority model ranks on (all 0-1 or counts).

    Issues are compared through these, never through their raw measurements
    (degrees against percentages of body height would be meaningless).
    """

    severity: float | None = None          # rule severity, normalised
    confidence: float | None = None        # measurement confidence, normalised
    phase_importance: float | None = None  # coaching weight of the phase
    rule_priority: float | None = None     # the rule's declared priority, normalised
    repeat_count: int | None = None        # swings in recent history with the same issue


@dataclass
class FocusResult:
    available: bool
    source: str                           # SOURCE_MODEL (eligible rule) | SOURCE_STUB | SOURCE_NONE
    message: str
    checkpoint: str | None = None         # "impact.pelvis"
    phase: str | None = None
    title: str | None = None              # "Impact · Extension"
    label: str | None = None              # the verdict label
    status: str | None = None
    rule_id: str | None = None
    drill_mapping: str | None = None      # verdict mapping id used to pick a drill
    priority: PriorityInputs = field(default_factory=PriorityInputs)
    reason: str | None = None
    schema: str = FOCUS_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)


def focus_for(analysis: Mapping, *, demo: bool = False) -> FocusResult:
    """Production: only an interpretation rule eligible for priority. Demo: a flagged stub."""

    interpretations = analysis.get("interpretations") or {}
    rule_focus = interpretations.get("focus")
    if rule_focus:
        return _from_rule(analysis, rule_focus, interpretations)
    if demo:
        return _stub_focus(analysis)
    return FocusResult(available=False, source=SOURCE_NONE, message=NEUTRAL_FOCUS,
                       reason="no interpretation rule is eligible for priority yet")


def _from_rule(analysis: Mapping, focus: Mapping, interpretations: Mapping) -> FocusResult:
    rule_id = focus.get("rule_id")
    result = next((r for r in interpretations.get("results", []) if r.get("rule_id") == rule_id), {})
    phase = focus.get("phase") or result.get("phase")
    match = next((c for c in checkpoints(analysis) if c["phase"] == phase), None)
    return FocusResult(
        available=True, source=SOURCE_MODEL,
        message=focus.get("user_text") or result.get("user_text") or "",
        checkpoint=match["id"] if match else None, phase=phase,
        title=f"{PHASE_LABELS.get(phase, phase)} · {match['name']}" if match else PHASE_LABELS.get(phase, phase),
        label=match["label"] if match else None, status=match["status"] if match else None,
        rule_id=rule_id, drill_mapping=(focus.get("coaching_key") or (match or {}).get("mapping")),
        priority=PriorityInputs(rule_priority=result.get("priority")),
    )


def _stub_focus(analysis: Mapping) -> FocusResult:
    """Demo only: the first checkpoint in swing order that isn't in range. Not a coaching rule."""

    items = checkpoints(analysis)
    pick = next((c for c in items if c["status"] != "good"), None)
    if pick is None:
        return FocusResult(available=False, source=SOURCE_STUB, message=NEUTRAL_FOCUS,
                           reason="demo: every checkpoint in range")
    return FocusResult(
        available=True, source=SOURCE_STUB, message=pick["support"] or "",
        checkpoint=pick["id"], phase=pick["phase"], title=f"{pick['phase_label']} · {pick['name']}",
        label=pick["label"], status=pick["status"], drill_mapping=pick["mapping"],
        reason="Demo pick (first checkpoint out of range in swing order) - not a coaching priority.")


# ---------------------------------------------------------------- swing date

_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})\D+(\d{1,2})[.:-](\d{2})[.:-](\d{2})")


def swing_datetime(*names: str, fallback: datetime) -> datetime:
    """Capture time from the first name carrying one (phone / WhatsApp naming,
    or a batch folder like ``10_2026-09-29_14-54-56``), else the fallback."""

    for name in names:
        match = _STAMP.search(name or "")
        if match:
            y, mo, d, h, mi, s = (int(v) for v in match.groups())
            try:
                return datetime(y, mo, d, h, mi, s)
            except ValueError:
                continue
    return fallback


__all__ = [
    "FOCUS_SCHEMA", "FocusResult", "NEUTRAL_FOCUS", "PriorityInputs", "SCORE_SCHEMA", "SCORE_UNAVAILABLE",
    "SOURCE_MODEL", "SOURCE_NONE", "SOURCE_STUB", "ScoreContribution", "ScoreResult", "Scorer", "StubScorer",
    "UncalibratedScorer", "swing_score", "checkpoints", "focus_for", "friendly_reason", "status_counts", "summary_text",
    "swing_datetime", "withheld",
]
