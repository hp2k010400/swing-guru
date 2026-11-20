"""Run the interpretation rules over an analysis and pick a main focus.

Main focus may only come from results that are eligible (a VALIDATED rule that
fired with a classification and severity). Different measurement units are
never compared: candidates are ordered by severity, then rule priority, then
phase importance, then confidence - never by the size of a number.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from .facts import CONFIDENCE_ORDER, facts_from_analysis
from .rules import RULES, InterpretationResult, InterpretationRule, evaluate_rules

INTERPRETATION_VERSION = 1
NO_FOCUS_TEXT = "Coaching priorities are coming next."

# Words that must not appear in golfer-facing interpretation text until a
# validated rule supports them (docs/research/golf-interpretation-rules.md, D).
BANNED_TERMS: tuple[str, ...] = (
    "early extension", "loss of posture", "stood up", "standing up", "on plane", "swing plane",
    "ideal plane", "correct plane", "steep", "shallow", "laid off", "across the line",
    "over the top", "inside", "outside", "good", "bad", "correct", "ideal", "perfect",
    "fault", "error", "keep your head still",
)

SEVERITY_RANK = {"major": 3, "moderate": 2, "minor": 1}
PHASE_IMPORTANCE = {"impact": 6, "downswing": 5, "top": 4, "mid_back": 3, "takeaway": 2, "address": 1}

# Deterministic future mapping: rule_id -> drill_id. Empty until drills exist
# and a rule is validated; never chosen by a language model.
DRILLS: Mapping[str, str] = {}


def terminology_violations(text: str | None) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    return [term for term in BANNED_TERMS if re.search(r"\b" + re.escape(term) + r"\b", lowered)]


def select_focus(results: Sequence[InterpretationResult]) -> InterpretationResult | None:
    """The single main focus, from eligible results only; otherwise ``None``."""

    eligible = [result for result in results if result.eligible_for_focus]
    if not eligible:
        return None

    def key(result: InterpretationResult):
        confidence = min((CONFIDENCE_ORDER.get(c, 0) for c in result.observed_confidence.values()), default=0)
        return (
            -SEVERITY_RANK.get(result.severity or "", 0),
            result.priority,
            -PHASE_IMPORTANCE.get(result.phase, 0),
            -confidence,
            result.rule_id,
        )

    return sorted(eligible, key=key)[0]


def interpret(analysis: Mapping, rules: Sequence[InterpretationRule] = RULES) -> dict:
    """Interpretations for one analysis, JSON-ready and deterministic."""

    facts = facts_from_analysis(analysis)
    results = evaluate_rules(facts, rules)
    for result in results:
        bad = terminology_violations(result.user_text) + terminology_violations(result.why_it_matters)
        if bad:  # A rule must never ship banned wording; fail loudly in development.
            raise ValueError(f"{result.rule_id} uses unsupported golf terms: {bad}")
    focus = select_focus(results)
    return {
        "version": INTERPRETATION_VERSION,
        "results": [result.to_dict() for result in results],
        "focus": None if focus is None else {
            "rule_id": focus.rule_id,
            "rule_version": focus.rule_version,
            "phase": focus.phase,
            "text": focus.user_text,
            "why_it_matters": focus.why_it_matters,
            "drill_id": DRILLS.get(focus.rule_id),
        },
        "focus_fallback": NO_FOCUS_TEXT,
        "facts": [
            {"key": f.key, "phase": f.phase, "value": round(f.value, 3), "unit": f.unit,
             "reference": f.reference, "confidence": f.confidence, "source": f.source}
            for f in facts.facts
        ],
    }


__all__ = [
    "BANNED_TERMS",
    "DRILLS",
    "INTERPRETATION_VERSION",
    "NO_FOCUS_TEXT",
    "interpret",
    "select_focus",
    "terminology_violations",
]
