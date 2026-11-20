"""Deterministic, versioned, fail-closed interpretation rules.

Each rule reads :class:`MeasurementFact` values and returns an
:class:`InterpretationResult`. A rule never guesses:

* missing facts, low confidence, the wrong camera view or a missing Address
  reference -> ``status = "blocked"`` with the reason;
* a descriptive rule whose facts are present -> ``status = "described"``: a
  factual sentence, never a verdict and never eligible for main focus;
* a candidate pattern whose facts line up but whose thresholds are not
  calibrated -> ``status = "observed"`` with ``calibration = NEEDS_CALIBRATION``:
  no classification, no severity, not eligible for main focus;
* only a rule with ``calibration = VALIDATED`` can return ``status = "fired"``
  with a classification. No rule is validated today.

Evidence strength codes follow ``docs/research/golf-interpretation-rules.md``:
P peer-reviewed, I industry definition, V vendor benchmark, O observational.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .facts import FactSet, MeasurementFact

VALIDATED = "VALIDATED"
NEEDS_CALIBRATION = "NEEDS_CALIBRATION"
DESCRIPTIVE_ONLY = "DESCRIPTIVE_ONLY"
NOT_SUPPORTED = "NOT_SUPPORTED"

STATUS_FIRED = "fired"
STATUS_OBSERVED = "observed"
STATUS_DESCRIBED = "described"
STATUS_NOT_FIRED = "not_fired"
STATUS_BLOCKED = "blocked"

PHASE_LABELS = {
    "address": "address",
    "takeaway": "takeaway",
    "mid_back": "mid-backswing",
    "top": "the top",
    "downswing": "mid-downswing",
    "impact": "impact",
}


@dataclass(frozen=True)
class Evidence:
    source: str
    url: str
    strength: str  # P / I / V / O
    note: str = ""


@dataclass(frozen=True)
class InterpretationResult:
    rule_id: str
    rule_version: int
    phase: str
    status: str
    calibration: str
    required_measurements: tuple[str, ...]
    required_confidence: str
    observed: Mapping[str, float] = field(default_factory=dict)
    observed_confidence: Mapping[str, str] = field(default_factory=dict)
    classification: str | None = None
    severity: str | None = None
    user_text: str | None = None
    why_it_matters: str | None = None
    reason: str = ""
    evidence: tuple[Evidence, ...] = ()
    priority: int = 100
    coaching_key: str | None = None  # future: rule_id -> explanation -> drill_id

    @property
    def eligible_for_focus(self) -> bool:
        return (
            self.status == STATUS_FIRED
            and self.calibration == VALIDATED
            and self.classification is not None
            and self.severity is not None
        )

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "phase": self.phase,
            "status": self.status,
            "calibration": self.calibration,
            "required_measurements": list(self.required_measurements),
            "required_confidence": self.required_confidence,
            "observed": {key: round(float(value), 3) for key, value in self.observed.items()},
            "observed_confidence": dict(self.observed_confidence),
            "classification": self.classification,
            "severity": self.severity,
            "user_text": self.user_text,
            "why_it_matters": self.why_it_matters,
            "reason": self.reason,
            "evidence": [e.__dict__ for e in self.evidence],
            "priority": self.priority,
            "coaching_key": self.coaching_key,
            "eligible_for_focus": self.eligible_for_focus,
        }


@dataclass(frozen=True)
class InterpretationRule:
    rule_id: str
    version: int
    phase: str
    requires: tuple[tuple[str, str], ...]  # (fact key, phase)
    required_confidence: str
    calibration: str
    evidence: tuple[Evidence, ...]
    describe: Callable[[Mapping[str, MeasurementFact]], tuple[str, str | None, str]]
    views: tuple[str, ...] = ("down-the-line",)
    needs_toward_ball: bool = False
    priority: int = 100
    confidence: Mapping[str, str] = field(default_factory=dict)  # per "key@phase" override
    camera: tuple[str, ...] | None = None  # accepted DTL camera-check statuses; None = not required

    def evaluate(self, facts: FactSet) -> InterpretationResult:
        def result(status: str, reason: str, *, found=None, text=None, why=None, classification=None, severity=None):
            found = found or {}
            return InterpretationResult(
                rule_id=self.rule_id,
                rule_version=self.version,
                phase=self.phase,
                status=status,
                calibration=self.calibration,
                required_measurements=tuple(f"{key}@{phase}" for key, phase in self.requires),
                required_confidence=self.required_confidence,
                observed={name: fact.value for name, fact in found.items()},
                observed_confidence={name: fact.confidence for name, fact in found.items()},
                classification=classification,
                severity=severity,
                user_text=text,
                why_it_matters=why,
                reason=reason,
                evidence=self.evidence,
                priority=self.priority,
                coaching_key=self.rule_id if status == STATUS_FIRED else None,
            )

        if self.calibration == NOT_SUPPORTED:
            return result(STATUS_BLOCKED, "not supported by current vision")
        if facts.camera_view not in self.views:
            return result(STATUS_BLOCKED, f"camera view '{facts.camera_view}' is not supported by this rule")
        if not facts.address_available:
            return result(STATUS_BLOCKED, "no Address moment to compare with")
        if self.needs_toward_ball and not facts.toward_ball_known:
            return result(STATUS_BLOCKED, "direction toward the ball could not be established from Address")
        if self.camera is not None and facts.camera_validity not in self.camera:
            return result(STATUS_BLOCKED, f"camera check is {facts.camera_validity}; needs {'/'.join(self.camera)}")
        found: dict[str, MeasurementFact] = {}
        for key, phase in self.requires:
            fact = facts.get(key, phase)
            if fact is None:
                return result(STATUS_BLOCKED, f"missing {key} at {phase}", found=found)
            found[f"{key}@{phase}"] = fact
            needed = self.confidence.get(f"{key}@{phase}", self.required_confidence)
            if not fact.meets(needed):
                return result(STATUS_BLOCKED, f"{key} at {phase} is {fact.confidence}, needs {needed}", found=found)
        outcome, text, why = self.describe(found)
        if outcome == STATUS_NOT_FIRED:
            return result(STATUS_NOT_FIRED, text or "pattern not present", found=found)
        if outcome == STATUS_FIRED and self.calibration != VALIDATED:
            # Fail closed: an uncalibrated rule can only ever be an observation.
            outcome = STATUS_OBSERVED
        reason = {
            STATUS_DESCRIBED: "descriptive fact; no verdict",
            STATUS_OBSERVED: "signals line up, but thresholds are not calibrated; no verdict",
            STATUS_FIRED: "validated rule fired",
        }.get(outcome, "")
        return result(outcome, reason, found=found, text=text, why=why)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

TPI_EE = Evidence("TPI - Early Extension swing characteristic",
                  "https://www.mytpi.com/articles/swing/early-extension-swing-characteristic", "I",
                  "Defined by forward pelvis movement toward the ball; no public threshold.")
TPI_LOP = Evidence("TPI - Loss of Posture", "https://www.mytpi.com/improve-my-game/swing-characteristics/loss-of-posture",
                   "I", "Any significant alteration from set-up angles; 'significant' undefined.")
SPORTSBOX_PT = Evidence("Sportsbox AI - Pelvis Thrust at Impact", "https://help.sportsbox.ai/what-is-early-extension",
                        "V", "3D, body-scaled; tour range example -0.9 in to +1.7 in (negative = toward ball).")
SPORTSBOX_BEND = Evidence("Sportsbox tour data via Golf Digest", "https://www.golfdigest.com/story/useful-golf-swingfacts-sportsbox-ai-graph",
                          "V", "Tour chest bend ~30 deg at Address and Impact (3D chest segment).")
YOUNG_EE = Evidence("Adam Young - Early Extension", "https://www.adamyounggolf.com/early-extension/", "O",
                    "Tour players extend through impact after a transition squat.")
HEAD_O = Evidence("Head movement instruction", "https://rotaryswing.com/golf-instruction/swingfundamentals/golf-head-behind-ball",
                  "O", "Lateral and vertical head movement is normal; no agreed threshold.")
TUSH_LINE = Evidence("RotarySwing - Early extension and the tush line",
                     "https://rotaryswing.com/golf-instruction/golfbiomechanics/early-extension-in-golf", "O",
                     "A vertical line down from the rear at address; the rear moving forward off it is the cue.")
P2_O = Evidence("Takeaway P2 checkpoint", "https://www.golfdistillery.com/swing-tips/takeaway/club-position/", "O",
                "Inside/outside judged at shaft parallel with the camera on the hand line.")


# ---------------------------------------------------------------------------
# Descriptions (plain language, no verdict terms)
# ---------------------------------------------------------------------------


def _describe_posture(found):
    change = found["posture_change_deg@impact"].value
    if abs(change) < 1.0:
        return STATUS_DESCRIBED, "Your upper body angle at impact matches your set-up.", None
    direction = "more upright" if change < 0 else "more bent over"
    return (
        STATUS_DESCRIBED,
        f"Your upper body is {abs(change):.0f}° {direction} at impact than at address.",
        "Your upper-body angle is measured from your hips to your shoulders, compared with your own set-up.",
    )


def _describe_pelvis(found):
    toward = found["pelvis_toward_ball_pct@impact"].value
    if abs(toward) < 0.5:
        return STATUS_DESCRIBED, "Your hips are about the same distance from the ball at impact as at address.", None
    where = "closer to" if toward > 0 else "further from"
    return (
        STATUS_DESCRIBED,
        f"At impact your hips are {abs(toward):.1f}% of your height {where} the ball than at address.",
        "Measured from the middle of your hips, compared with your own set-up.",
    )


def _describe_head(found):
    fact = found["head_movement_pct@impact"]
    return STATUS_DESCRIBED, f"Your head moved {fact.value:.1f}% of your height between address and impact.", \
        "Some head movement is normal in a golf swing; this is a measurement, not a target."


def _describe_setup(found):
    torso = found["torso_angle_deg@address"].value
    return STATUS_DESCRIBED, f"At address your upper body leans {torso:.0f}° forward from upright.", \
        "Saved as your personal set-up reference; there is no single right set-up angle for everyone."


def _describe_knees(found):
    lead = found["lead_knee_flex_deg@address"].value
    trail = found["trail_knee_flex_deg@address"].value
    return STATUS_DESCRIBED, f"At address your knees are bent {lead:.0f}° (front) and {trail:.0f}° (back).", \
        "Saved as your personal set-up reference; knee bend depends on club, stance and build."


def _describe_shaft_mid_down(found):
    change = found["shaft_vs_address_ref_deg@downswing"].value
    if abs(change) < 1.0:
        return STATUS_DESCRIBED, "Mid-downswing, the shaft matches the Address shaft reference.", None
    return STATUS_DESCRIBED, (
        f"Mid-downswing, the shaft is {abs(change):.0f}° {'more upright' if change > 0 else 'flatter'} "
        "than the Address shaft reference."
    ), "The reference is where your shaft sat at address, as the camera sees it."


# Measurement-noise floors, shared with the bundle's "stayed put" / "same as
# Address" wording. They say "this moved at all", not "this moved too much":
# they are not golf thresholds and do not calibrate any verdict.
NOISE_FLOOR_PCT = 0.5
NOISE_FLOOR_DEG = 1.0


def _pattern_pelvis_and_posture(found):
    """Candidate multi-signal pattern (v2): the REAR of the pelvis moves toward
    the ball off the Address tush line AND the torso becomes more upright at impact."""

    toward = found["pelvis_rear_toward_ball_pct@impact"].value
    posture = found["posture_change_deg@impact"].value
    if toward >= NOISE_FLOOR_PCT and posture <= -NOISE_FLOOR_DEG:
        return (
            STATUS_FIRED,  # downgraded to "observed" while NEEDS_CALIBRATION
            "Your pelvis moves toward the ball while your upper body becomes more upright through impact.",
            "This combination is worth watching; we have not yet calibrated how much of it matters for your swing.",
        )
    return STATUS_NOT_FIRED, "the rear of the pelvis did not move toward the ball while the upper body became more upright", None


def _describe_rear_pelvis(found):
    toward = found["pelvis_rear_toward_ball_pct@impact"].value
    if abs(toward) < NOISE_FLOOR_PCT:
        return STATUS_DESCRIBED, "At impact the back of your pelvis is on the line it set at address.", None
    where = "toward" if toward > 0 else "away from"
    return (
        STATUS_DESCRIBED,
        f"At impact the back of your pelvis is {abs(toward):.1f}% of your height {where} the ball from its address line.",
        "The line is a vertical line at the back of your pelvis at address, as the camera sees it.",
    )


def _unsupported(_found):
    return STATUS_NOT_FIRED, "not supported", None


RULES: tuple[InterpretationRule, ...] = (
    InterpretationRule(
        "impact.hips_toward_ball_with_upright_torso", 2, "impact",
        (("pelvis_rear_toward_ball_pct", "impact"), ("posture_change_deg", "impact")),
        "HIGH", NEEDS_CALIBRATION, (TPI_EE, TUSH_LINE, SPORTSBOX_PT, YOUNG_EE), _pattern_pelvis_and_posture,
        needs_toward_ball=True, priority=10,
        confidence={"pelvis_rear_toward_ball_pct@impact": "MEDIUM", "posture_change_deg@impact": "HIGH"},
        camera=("VALID",),
    ),
    InterpretationRule(
        "impact.rear_pelvis_vs_tush_line", 1, "impact", (("pelvis_rear_toward_ball_pct", "impact"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (TUSH_LINE, TPI_EE), _describe_rear_pelvis,
        needs_toward_ball=True, priority=25, camera=("VALID", "UNCERTAIN"),
    ),
    InterpretationRule(
        "impact.posture_change", 1, "impact", (("posture_change_deg", "impact"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (TPI_LOP, SPORTSBOX_BEND, YOUNG_EE), _describe_posture, priority=20,
    ),
    # v2: position changes from Address are camera motion when the phone moved
    # (swing 08), so they need a camera that is not INVALID. Angles do not.
    InterpretationRule(
        "impact.pelvis_vs_address", 2, "impact", (("pelvis_toward_ball_pct", "impact"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (TPI_EE, SPORTSBOX_PT), _describe_pelvis, needs_toward_ball=True, priority=30,
        camera=("VALID", "UNCERTAIN"),
    ),
    InterpretationRule(
        "impact.head_movement", 2, "impact", (("head_movement_pct", "impact"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (HEAD_O,), _describe_head, priority=60, camera=("VALID", "UNCERTAIN"),
    ),
    InterpretationRule(
        "address.torso_reference", 1, "address", (("torso_angle_deg", "address"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (SPORTSBOX_BEND,), _describe_setup, priority=80,
    ),
    InterpretationRule(
        "address.knee_reference", 1, "address",
        (("lead_knee_flex_deg", "address"), ("trail_knee_flex_deg", "address")),
        "MEDIUM", DESCRIPTIVE_ONLY, (), _describe_knees, priority=90,
    ),
    InterpretationRule(
        "mid_down.shaft_vs_address_reference", 1, "downswing", (("shaft_vs_address_ref_deg", "downswing"),),
        "MEDIUM", DESCRIPTIVE_ONLY, (), _describe_shaft_mid_down, priority=50,
    ),
    InterpretationRule(
        "takeaway.club_inside_outside", 1, "takeaway", (("shaft_angle_deg", "takeaway"),),
        "HIGH", NOT_SUPPORTED, (P2_O,), _unsupported, priority=100,
    ),
    InterpretationRule(
        "top.posture_change", 1, "top", (("posture_change_deg", "top"),),
        "HIGH", NOT_SUPPORTED, (TPI_LOP,), _unsupported, priority=100,
    ),
)


def rules_by_id() -> Mapping[str, InterpretationRule]:
    return {rule.rule_id: rule for rule in RULES}


def evaluate_rules(facts: FactSet, rules: Sequence[InterpretationRule] = RULES) -> list[InterpretationResult]:
    return [rule.evaluate(facts) for rule in rules]


__all__ = [
    "DESCRIPTIVE_ONLY",
    "NEEDS_CALIBRATION",
    "NOT_SUPPORTED",
    "RULES",
    "STATUS_BLOCKED",
    "STATUS_DESCRIBED",
    "STATUS_FIRED",
    "STATUS_NOT_FIRED",
    "STATUS_OBSERVED",
    "VALIDATED",
    "Evidence",
    "InterpretationResult",
    "InterpretationRule",
    "evaluate_rules",
    "rules_by_id",
]
