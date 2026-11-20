"""Golfer-facing checkpoints and verdicts: a presentation layer over measurements.

This module never measures anything and never changes a measurement. It:

1. fixes the product walkthrough to a small set of named checkpoints per phase
   (``CHECKPOINTS``) - Address: hip hinge, knee bend, arm hang; Mid-Back and
   Mid-Down: plane; Top: posture; Impact: extension, head;
2. maps the measured value behind each checkpoint to a short golf verdict with
   a colour status (``good`` / ``attention`` / ``problem``);
3. records exactly how: mapping id, input value, bands, basis and
   ``validation`` - ``provisional_ui_mapping`` (a stated rule that has not been
   calibrated against reference data) or ``validated_rule`` (none yet).

A checkpoint is shown only when its measurement exists and a mapping produced a
verdict. Checkpoints the engine cannot yet judge are listed in
``UNSUPPORTED_CHECKPOINTS`` with the reason and are never shown to a golfer.
Every other measurement stays on the results screen as a plain, grey,
information row (its own title and value, no invented verdict).

It is separate from ``swingguru.interpretation`` (the fail-closed rule engine,
which still bans these golf terms and has no validated rule): nothing there
reads this module, and nothing here feeds the main focus.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

GOOD = "good"            # green: clearly acceptable
ATTENTION = "attention"  # amber: slight issue
PROBLEM = "problem"      # orange-red: clear issue
INFO = "info"            # grey: information only, no verdict

PROVISIONAL = "provisional_ui_mapping"
VALIDATED = "validated_rule"
VERDICT_VERSION = 2

# Claims the engine cannot back at all (no club face, ball, target line or 3D
# club position). A verdict containing one is a bug (tested).
UNSUPPORTED_CLAIMS: tuple[str, ...] = (
    "face", "path", "laid off", "across the line", "over the top", "attack angle",
)


@dataclass(frozen=True)
class Band:
    """``low <= value < high`` (None = open) maps to a status and wording."""

    low: float | None
    high: float | None
    status: str
    label: str
    support: str

    def contains(self, value: float) -> bool:
        return (self.low is None or value >= self.low) and (self.high is None or value < self.high)

    def describe(self) -> dict:
        return {"from": self.low, "to": self.high, "status": self.status, "label": self.label}


@dataclass(frozen=True)
class VerdictMapping:
    mapping_id: str
    kind: str
    phases: tuple[str, ...]
    measurement: str
    unit: str
    extract: Callable[[dict], float | None]
    bands: tuple[Band, ...]
    basis: str
    approximation: str  # how our 2D measure differs from the golf concept named in the labels
    validation: str = PROVISIONAL
    version: int = 1
    formatter: Callable[[float], tuple[str, str]] | None = None  # secondary value = the number used
    # Inputs outside this range are treated as measurement errors, not verdicts.
    plausible: tuple[float, float] | None = None

    def classify(self, value: float) -> Band | None:
        return next((band for band in self.bands if band.contains(value)), None)


def _raw(key: str) -> Callable[[dict], float | None]:
    return lambda check: check.get("raw", {}).get(key)


def _rear_pelvis(check: dict) -> float | None:
    value = (check.get("pelvis_depth") or {}).get("displacement_pct_height")
    return None if value is None else float(value)


def _pct_toward(value: float) -> tuple[str, str]:
    return f"{abs(value):.1f}%", "toward the ball" if value > 0 else "away from the ball" if value < 0 else "on the line"


# ---------------------------------------------------------------------------
# Mappings. All bands are stated presentation rules, not calibrated
# thresholds; ``basis`` gives their origin and ``approximation`` the gap.
# ---------------------------------------------------------------------------

HIP_HINGE = VerdictMapping(
    "address.hip_hinge", "torso_angle", ("address",),
    "forward tilt of the hip-to-shoulder line from vertical", "deg", _raw("torso_inclination_deg"),
    (
        Band(None, 20, PROBLEM, "Too upright", "You're standing tall - tilt more from the hips."),
        Band(20, 30, ATTENTION, "Slightly upright", "Tilt a little more from the hips."),
        Band(30, 50, GOOD, "Hip hinge good", "An athletic tilt from the hips."),
        Band(50, 58, ATTENTION, "Slightly bent over", "A touch too much forward tilt."),
        Band(58, None, PROBLEM, "Too bent over", "Stand a little taller over the ball."),
    ),
    "Coaching convention for down-the-line set-up tilt (tour chest bend ~30-45 deg in 3D).",
    "2D hip-to-shoulder line, not 3D spine or hip-flexion angle.",
)

KNEE_BEND = VerdictMapping(
    "address.knee_bend", "trail_knee", ("address",),
    "trail knee bend from a straight leg", "deg", _raw("knee_flex_deg"),
    (
        Band(None, 5, PROBLEM, "Too straight", "Your legs are close to locked - add knee flex."),
        Band(5, 10, ATTENTION, "Slightly straight", "Add a touch more knee flex."),
        Band(10, 30, GOOD, "Knee bend good", "Soft, athletic knee flex."),
        Band(30, 40, ATTENTION, "Slightly flexed", "A little more knee bend than you need."),
        Band(40, None, PROBLEM, "Too much knee bend", "You're sitting down - straighten the legs a little."),
    ),
    "Coaching convention of a soft, athletic knee flex (~15-25 deg).",
    "2D hip-knee-ankle angle of the near leg.",
)

ARM_HANG = VerdictMapping(
    "address.arm_hang", "arm_hang", ("address",),
    "near-arm line from shoulder to hands vs straight down (+ = hands out toward the ball)", "deg",
    _raw("arm_angle_deg"),
    (
        Band(None, -20, PROBLEM, "Too crowded", "Your hands are tight to your body - give them room."),
        Band(-20, -10, ATTENTION, "Slightly crowded", "Your hands are a little close to your body."),
        Band(-10, 10, GOOD, "Arm hang good", "Your arms hang naturally from your shoulders."),
        Band(10, 20, ATTENTION, "Slightly reaching", "Your hands are a little far from your body."),
        Band(20, None, PROBLEM, "Reaching", "Your hands are pushed out - let the arms hang."),
    ),
    "Coaching convention that the arms hang roughly straight down from the shoulders at address. "
    "Robustness batch (12 swings): -13 to +22 deg.",
    "2D near arm only; hands position also depends on club length.",
)

PLANE = VerdictMapping(
    "swing.plane_vs_address_shaft", "shaft", ("mid_back", "downswing"),
    "shaft angle minus the Address shaft angle (+ = steeper, - = flatter)", "deg", _raw("change_vs_address_deg"),
    (
        Band(None, -12, PROBLEM, "Too shallow", "Your shaft is noticeably flatter than your Address reference."),
        Band(-12, -5, ATTENTION, "Slightly shallow", "Your shaft is a little flatter than your Address reference."),
        Band(-5, 5, GOOD, "On plane", "Your shaft matches your Address reference."),
        Band(5, 12, ATTENTION, "Slightly steep", "Your shaft is a little steeper than your Address reference."),
        Band(12, None, PROBLEM, "Too steep", "Your shaft is noticeably steeper than your Address reference."),
    ),
    "Down-the-line checkpoint: at mid-backswing and mid-downswing the shaft is compared with the "
    "Address shaft line (parallel = on plane, flatter = shallow, steeper = steep).",
    "Angle only: parallel is tested, not whether the shaft sits on, above or below the line; assumes the "
    "camera is on the hand line, which is not verified.",
    # Batch range -15..+11 deg; -57 / -60 deg were the tracker's false flat lines on swings 13 and 14.
    plausible=(-25.0, 25.0),
)

POSTURE_TOP = VerdictMapping(
    "top.posture", "posture", ("top",),
    "change of the hip-to-shoulder line from Address (- = more upright)", "deg", _raw("torso_angle_change_deg"),
    (
        Band(None, -10, PROBLEM, "Loss of posture", "You've stood up out of your set-up tilt."),
        Band(-10, -5, ATTENTION, "Slight loss of posture", "You've risen a little from your set-up tilt."),
        Band(-5, 5, GOOD, "Posture maintained", "You've held your set-up tilt."),
        Band(5, 10, ATTENTION, "Slight loss of posture", "You've bent a little further over than at set-up."),
        Band(10, None, PROBLEM, "Loss of posture", "You've bent well over from your set-up tilt."),
    ),
    "TPI: loss of posture is any significant change from the set-up angles, either way (so bending further "
    "over also counts). The shoulder midpoint stays on the spine axis as the shoulders turn, so the 2D "
    "hip-to-shoulder line follows forward tilt.",
    "The far shoulder is often hidden at the Top, so the midpoint is partly estimated by the pose model.",
)

EXTENSION = VerdictMapping(
    "impact.early_extension", "pelvis", ("impact",),
    "back of the pelvis vs its Address line (tush line), % of height (+ = toward the ball)", "%", _rear_pelvis,
    (
        Band(None, 2, GOOD, "Posture maintained", "Your hips stayed back on their Address line."),
        Band(2, 5, ATTENTION, "Slight early extension", "Your hips moved a little toward the ball."),
        Band(5, None, PROBLEM, "Early extension", "Your hips pushed toward the ball through impact."),
    ),
    "TPI definition: early extension is the pelvis moving toward the ball in the downswing; judged on "
    "the tush line. Tour pelvis thrust at impact reaches ~0.9 in (~1.3 % of height), so under 2 % is held.",
    "2D rear-pelvis silhouette; only when the camera stayed still (otherwise the check is not shown).",
    formatter=_pct_toward,
)

HEAD = VerdictMapping(
    "impact.head", "head", ("impact",),
    "head movement from Address, % of body height", "%", _raw("movement_pct_height"),
    (
        Band(None, 4, GOOD, "Head steady", "Your head held its set-up position."),
        Band(4, 8, ATTENTION, "Slight head movement", "Your head moved a little from set-up."),
        Band(8, None, PROBLEM, "Excessive head movement", "Your head moved a long way from set-up."),
    ),
    "Some head movement is normal; instructors allow ~1.5 in (~2 % of height). Bands are wider on purpose.",
    "2D nose position; only when the camera stayed still.",
)

# Plain measurement rows for the results screen (not in the walkthrough).
SUPPORTING: tuple[VerdictMapping, ...] = (
    VerdictMapping(
        "support.posture", "posture", ("downswing", "impact"),
        "change of the hip-to-shoulder line from Address", "deg", _raw("torso_angle_change_deg"), POSTURE_TOP.bands,
        POSTURE_TOP.basis, POSTURE_TOP.approximation,
    ),
    VerdictMapping(
        "support.head", "head", ("takeaway", "mid_back", "top"),
        HEAD.measurement, "%", HEAD.extract, HEAD.bands, HEAD.basis, HEAD.approximation,
    ),
    VerdictMapping(
        "support.lead_knee", "lead_knee", ("address",),
        "lead knee bend from a straight leg", "deg", _raw("knee_flex_deg"), KNEE_BEND.bands,
        KNEE_BEND.basis, KNEE_BEND.approximation,
    ),
)

MAPPINGS: tuple[VerdictMapping, ...] = (HIP_HINGE, KNEE_BEND, ARM_HANG, PLANE, POSTURE_TOP, EXTENSION, HEAD, *SUPPORTING)


@dataclass(frozen=True)
class Checkpoint:
    phase: str
    name: str
    mapping: VerdictMapping


# The product walkthrough, in order. Nothing else pauses the video.
CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint("address", "Hip hinge", HIP_HINGE),
    Checkpoint("address", "Knee bend", KNEE_BEND),
    Checkpoint("address", "Arm hang", ARM_HANG),
    Checkpoint("mid_back", "Plane", PLANE),
    Checkpoint("top", "Posture", POSTURE_TOP),
    Checkpoint("downswing", "Plane", PLANE),
    Checkpoint("impact", "Extension", EXTENSION),
    Checkpoint("impact", "Head", HEAD),
)

# Checkpoints a golf app would show that the engine cannot yet judge honestly.
UNSUPPORTED_CHECKPOINTS: Mapping[str, str] = {
    "takeaway.plane": "Our Takeaway is the hands leaving set-up, not shaft-parallel. A shaft-parallel (P2) "
                      "detector exists (swingguru/takeaway_p2.py) but at 30 fps the club crosses parallel between "
                      "frames and is usually lost to motion blur: on the batch it found 3 of 12 swings, one of them "
                      "a background edge. Diagnostic only until the club is tracked through the takeaway.",
    "top.hand_depth": "Measured (check kind hand_depth: hands vs trail shoulder, % of height, same frame so "
                      "camera drift cancels). Stable (frame jitter 0.2-2.0 points, duplicate swing within 0.8) "
                      "but every batch swing sits 9-17 % behind the trail shoulder, so any good/deep/shallow band "
                      "would be fitted to our own data. Descriptive only; needs coach- or 3D-labelled swings.",
    "top.across_the_line": "Needs the target line and the club's 3D direction.",
    "impact.face_and_path": "No club-face or ball tracking.",
}


# The rest of the locked vocabulary (docs/product/VERDICT_LANGUAGE.md): wording
# agreed for checks the engine cannot judge yet. ``validation="unsupported"``;
# never shown to a golfer.
UNSUPPORTED_VERDICTS: tuple[dict, ...] = (
    {"phase": "takeaway", "check": "Takeaway plane",
     "verdicts": ["On plane", "Slightly inside", "Inside", "Slightly outside", "Outside"],
     "source": "none", "validation": "unsupported", "reason": UNSUPPORTED_CHECKPOINTS["takeaway.plane"] +
     " Inside / outside also needs the target line and clubhead depth, which a single 2D view does not give."},
    {"phase": "top", "check": "Hand depth",
     "verdicts": ["Hand depth good", "Slightly shallow hand depth", "Too shallow", "Slightly deep", "Too deep"],
     "source": "hand_depth: hands vs trail shoulder, % of height (measured, descriptive only)", "validation": "unsupported",
     "reason": UNSUPPORTED_CHECKPOINTS["top.hand_depth"]},
)


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _verdict(mapping: VerdictMapping, check: dict, value: float, band: Band) -> dict:
    shown, qualifier = mapping.formatter(value) if mapping.formatter else (check.get("value"), check.get("qualifier") or "")
    return {
        "verdict_id": f"{mapping.mapping_id}.{_slug(band.label)}",
        "phase": check.get("phase"),
        "sources": [f"{check.get('id', check.get('kind'))}:{mapping.measurement}"],
        "band": band.describe(),
        "label": band.label,
        "status": band.status,
        "support": band.support,
        "value": shown,
        "qualifier": qualifier,
        "confidence": check.get("level"),
        "mapping": {
            "id": mapping.mapping_id,
            "version": mapping.version,
            "validation": mapping.validation,
            "measurement": mapping.measurement,
            "input": round(float(value), 3),
            "unit": mapping.unit,
            "bands": [b.describe() for b in mapping.bands],
            "basis": mapping.basis,
            "approximation": mapping.approximation,
        },
    }


def _information(check: dict) -> dict:
    """A measurement without a verdict: its own title and value, grey."""

    return {
        "label": check.get("title", ""),
        "status": INFO,
        "support": check.get("summary") or check.get("detail") or "",
        "value": check.get("value"),
        "qualifier": check.get("qualifier") or "",
        "confidence": check.get("level"),
        "mapping": {"id": f"{check.get('phase')}.{check.get('kind')}.information", "validation": PROVISIONAL,
                    "kind": "information", "reason": "no defensible verdict for this measurement here"},
    }


def verdict_for(check: dict, context: Mapping | None = None) -> dict:
    """The verdict for one check; ``info`` when no mapping applies or the input is missing."""

    for mapping in MAPPINGS:
        if mapping.kind != check.get("kind") or check.get("phase") not in mapping.phases:
            continue
        value = mapping.extract(check)
        if value is None:
            continue
        if mapping.plausible and not mapping.plausible[0] <= float(value) <= mapping.plausible[1]:
            info = _information(check)
            info["mapping"].update({"id": mapping.mapping_id, "input": round(float(value), 3),
                                    "reason": f"input outside the plausible range {mapping.plausible}; "
                                              "probably a tracking error, so no verdict"})
            return info
        band = mapping.classify(float(value))
        if band is not None:
            return _verdict(mapping, check, float(value), band)
    return _information(check)


STATUS_RANK = {PROBLEM: 3, ATTENTION: 2, GOOD: 1, INFO: 0}


def attach_verdicts(analysis: dict) -> dict:
    """Verdicts on every check; the walkthrough becomes the fixed checkpoint list.

    Sets ``check["verdict"]``, ``check["checkpoint"]`` (name, or None) and
    ``check["guided"]`` (only supported checkpoints), keeps the engine's own
    choice in ``check["guided_engine"]``, orders each phase checkpoints first,
    and gives each phase a ``status`` (worst guided verdict). Idempotent; never
    touches a measured value.
    """

    plan: dict[tuple[str, str], Checkpoint] = {(cp.phase, cp.mapping.kind): cp for cp in CHECKPOINTS}
    order = {(cp.phase, cp.mapping.kind): i for i, cp in enumerate(CHECKPOINTS)}
    for phase in analysis.get("phases", []):
        statuses = []
        for check in phase.get("checks", []):
            check.setdefault("guided_engine", bool(check.get("guided")))
            verdict = verdict_for(check)
            check["verdict"] = verdict
            checkpoint = plan.get((phase["phase"], check.get("kind")))
            supported = (checkpoint is not None and verdict["status"] != INFO
                         and verdict["mapping"].get("id") == checkpoint.mapping.mapping_id)
            check["checkpoint"] = checkpoint.name if supported else None
            check["guided"] = supported
            if supported:
                statuses.append(verdict["status"])
        phase["checks"].sort(key=lambda c: order.get((phase["phase"], c.get("kind")), 99))
        phase["status"] = max(statuses, key=lambda s: STATUS_RANK[s]) if statuses else INFO
    if "summary" in analysis:
        shown = [c for p in analysis.get("phases", []) for c in p.get("checks", [])]
        analysis["summary"]["guided_checks"] = sum(1 for c in shown if c["guided"])
    analysis["verdicts"] = {
        "version": VERDICT_VERSION,
        "checkpoints": [{"phase": cp.phase, "name": cp.name, "mapping": cp.mapping.mapping_id,
                         "validation": cp.mapping.validation} for cp in CHECKPOINTS],
        "unsupported_checkpoints": dict(UNSUPPORTED_CHECKPOINTS),
        "unsupported_verdicts": [dict(v) for v in UNSUPPORTED_VERDICTS],
        "validated_rules": 0,
        "note": "Golfer-facing verdicts are provisional presentation rules over measured values; none is "
                "a validated rule yet. See swingguru/verdicts.py.",
    }
    return analysis


def unsupported_claims(text: str | None) -> list[str]:
    import re

    lowered = (text or "").lower()
    return [term for term in UNSUPPORTED_CLAIMS if re.search(r"\b" + re.escape(term) + r"\b", lowered)]


def all_mappings() -> Sequence[VerdictMapping]:
    return MAPPINGS


STATUS_COLOUR = {GOOD: "green", ATTENTION: "amber", PROBLEM: "coral", INFO: "neutral"}


def _band_text(band: Band, unit: str) -> str:
    u = "°" if unit == "deg" else unit
    if band.low is None:
        return f"< {band.high:g}{u}"
    if band.high is None:
        return f">= {band.low:g}{u}"
    return f"{band.low:g} to {band.high:g}{u}"


def verdict_matrix() -> str:
    """The golfer-facing vocabulary as Markdown, generated from the code (no drift)."""

    rows = ["| Phase | Check | Verdict | Colour | Source measurement | Band | Validation |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    names = {"address": "Address", "takeaway": "Takeaway", "mid_back": "Mid-Back", "top": "Top",
             "downswing": "Mid-Down", "impact": "Impact"}
    for cp in CHECKPOINTS:
        m = cp.mapping
        for band in m.bands:
            rows.append(f"| {names[cp.phase]} | {cp.name} | {band.label} | "
                        f"{STATUS_COLOUR[band.status]} | {m.measurement} | {_band_text(band, m.unit)} | {m.validation} |")
    for u in UNSUPPORTED_VERDICTS:
        rows.append(f"| {names[u['phase']]} | {u['check']} | {' / '.join(u['verdicts'])} | - | {u['source']} | - | "
                    f"unsupported (not shown) |")
    return "\n".join(rows)


__all__ = [
    "ATTENTION", "GOOD", "INFO", "PROBLEM", "PROVISIONAL", "VALIDATED", "MAPPINGS", "CHECKPOINTS",
    "UNSUPPORTED_CHECKPOINTS", "UNSUPPORTED_CLAIMS", "UNSUPPORTED_VERDICTS", "verdict_matrix", "Band", "Checkpoint", "VerdictMapping",
    "attach_verdicts", "verdict_for", "unsupported_claims", "all_mappings",
]
