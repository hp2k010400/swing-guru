"""Golf interpretation layer: measured facts -> deterministic, fail-closed rules.

Separate from vision, measurements, UI and any future generated copy. See
``docs/research/golf-interpretation-rules.md`` for the evidence behind every
rule and its calibration status.
"""

from .engine import BANNED_TERMS, NO_FOCUS_TEXT, interpret, select_focus, terminology_violations
from .facts import FactSet, MeasurementFact, facts_from_analysis
from .rules import RULES, InterpretationResult, InterpretationRule, evaluate_rules

__all__ = [
    "BANNED_TERMS",
    "NO_FOCUS_TEXT",
    "RULES",
    "FactSet",
    "InterpretationResult",
    "InterpretationRule",
    "MeasurementFact",
    "evaluate_rules",
    "facts_from_analysis",
    "interpret",
    "select_focus",
    "terminology_violations",
]
