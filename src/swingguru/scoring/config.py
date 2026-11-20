"""Swing Score model configuration. Every number the score uses lives here, versioned.

Changing anything below changes scores, so it needs a new ``VERSION`` (stored
with every swing, so historical results stay traceable). A test fails if the
verdict bands in ``swingguru.verdicts`` drift from the ranges recorded here.

These are **product choices**, not golf science: the target ranges are the
app's provisional verdict bands (coaching conventions, not calibrated against
reference swings). Every released model stays in ``MODELS`` so a stored score
can always be reproduced from its version.

V1 (``swing_score_v1``) weighted the eight checks equally and simply left out
the ones it could not measure. The 2026-10-05 audit of 45 production swings
found three problems with that:

* the three static set-up checks were 37.5 % of the score and averaged 8.8
  points against 6.7 for the five motion checks, so set-up carried most swings;
* the checks that go missing are almost always motion checks (impact needs a
  still camera, plane needs the club), so a swing with 5 of 8 measured averaged
  9.45 against 7.4 for 8 of 8: fewer measurements meant a *higher* score;
* the two plane checks (one measurement taken twice) were 25 %.

V2 (``swing_score_v2``) keeps the same checks, target ranges and points curve
and changes how they combine: weights by swing phase (set-up 20 %, backswing
25 %, downswing and impact 55 %), and a check that could not be measured counts
at a neutral 6.0 with half its weight, so a partly measured swing is pulled
toward the middle instead of being judged only on what was easy to see. A score
also needs at least one downswing or impact check.
"""

from __future__ import annotations

from dataclasses import dataclass

VERSION = "swing_score_v3"   # the model new analyses are scored with (v3 approved for production 2026-10-07)
SCALE = 10.0


@dataclass(frozen=True)
class ScoredCheck:
    """One score-eligible checkpoint and how its measurement turns into points.

    ``good`` is the target range (the verdict's "in range" band) and
    ``slight_low`` / ``slight_high`` the "slightly out" band edges on each side
    (None = that side has no band, i.e. the range is open there). ``ideal`` is
    the point inside the range that earns the full 10: the middle of a two-sided
    range, or 0 (no movement) for a one-sided one.
    """

    id: str                     # "<phase>.<kind>", as in the Summary
    phase: str
    kind: str
    name: str
    mapping_id: str
    unit: str
    good: tuple[float | None, float | None]
    slight_low: float | None
    slight_high: float | None
    ideal: float


# The eight walkthrough checkpoints (swingguru.verdicts.CHECKPOINTS); weights live in each model.
CHECKS: tuple[ScoredCheck, ...] = (
    ScoredCheck("address.torso_angle", "address", "torso_angle", "Hip hinge", "address.hip_hinge", "deg",
                good=(30, 50), slight_low=20, slight_high=58, ideal=40),
    ScoredCheck("address.trail_knee", "address", "trail_knee", "Knee bend", "address.knee_bend", "deg",
                good=(10, 30), slight_low=5, slight_high=40, ideal=20),
    ScoredCheck("address.arm_hang", "address", "arm_hang", "Arm hang", "address.arm_hang", "deg",
                good=(-10, 10), slight_low=-20, slight_high=20, ideal=0),
    ScoredCheck("mid_back.shaft", "mid_back", "shaft", "Plane", "swing.plane_vs_address_shaft", "deg",
                good=(-5, 5), slight_low=-12, slight_high=12, ideal=0),
    ScoredCheck("top.posture", "top", "posture", "Posture", "top.posture", "deg",
                good=(-5, 5), slight_low=-10, slight_high=10, ideal=0),
    ScoredCheck("downswing.shaft", "downswing", "shaft", "Plane", "swing.plane_vs_address_shaft", "deg",
                good=(-5, 5), slight_low=-12, slight_high=12, ideal=0),
    ScoredCheck("impact.pelvis", "impact", "pelvis", "Extension", "impact.early_extension", "%",
                good=(None, 2), slight_low=None, slight_high=5, ideal=0),
    ScoredCheck("impact.head", "impact", "head", "Head", "impact.head", "%",
                good=(None, 4), slight_low=None, slight_high=8, ideal=0),
)

# Points curve (per check, 0-10), continuous and piecewise linear in the measurement:
#   at the ideal ............................. 10
#   at the edge of the target range ........... 9   ("in range" = 9-10)
#   at the slightly-out / clearly-out boundary  4   ("slightly out" = 4-9)
#   one more slightly-out band width further .. 0   ("clearly out" = 0-4)
POINTS_IDEAL = 10.0
POINTS_RANGE_EDGE = 9.0
POINTS_SLIGHT_EDGE = 4.0
ZERO_AFTER_WIDTHS = 1.0     # how many slightly-out widths past the boundary the points reach 0


@dataclass(frozen=True)
class ScoreModel:
    """How the per-check points combine into one score. Never edit a released model: add a version."""

    version: str
    weights: dict                       # check id -> weight
    method: str
    unmeasured_points: float | None     # None = leave unmeasured checks out (V1)
    unmeasured_weight: float = 0.0      # fraction of a missing check's weight that counts at unmeasured_points
    needs_motion: tuple[str, ...] = ()  # at least one of these checks must be measured


V1_METHOD = ("Your Swing Score combines the swing positions Swing Guru could reliably measure in this video. "
             "Each measured position contributes to the score based on how close it was to its current target "
             "range. Checks the app could not measure reliably are excluded.")
METHOD = ("Based on this swing's measured mechanics. Each position Swing Guru measured earns points for how close "
          "it was to its target range. Set-up counts for 20%, the backswing for 25% and the downswing and impact "
          "for 55%. A position we could not measure reliably counts as neutral at half its weight, so a partly "
          "measured swing is pulled toward the middle. Positions we could only estimate count half. "
          "Swing Score evaluates this individual swing, not your handicap or overall playing ability.")

MOTION_FINISH = ("downswing.shaft", "impact.pelvis", "impact.head")

MODELS: dict[str, ScoreModel] = {
    "swing_score_v1": ScoreModel("swing_score_v1", {c.id: 1.0 for c in CHECKS}, V1_METHOD, None),
    "swing_score_v2": ScoreModel(
        "swing_score_v2",
        {   # set-up 20 %
            "address.torso_angle": 7.0, "address.trail_knee": 6.0, "address.arm_hang": 7.0,
            # backswing 25 %: the plane is half-weight here because the downswing plane repeats the measurement
            "mid_back.shaft": 8.0, "top.posture": 17.0,
            # downswing and impact 55 %
            "downswing.shaft": 15.0, "impact.pelvis": 22.0, "impact.head": 18.0,
        },
        METHOD, unmeasured_points=6.0, unmeasured_weight=0.5, needs_motion=MOTION_FINISH,
    ),
}
# v3 keeps v2's check weights, neutral handling and coverage rule; its combination is in scoring/v3.py.
MODELS["swing_score_v3"] = ScoreModel("swing_score_v3", dict(MODELS["swing_score_v2"].weights), "",
                                      unmeasured_points=6.0, unmeasured_weight=0.5, needs_motion=MOTION_FINISH)

# Confidence: the engine's own level on each measurement.
#   HIGH   -> counts fully
#   MEDIUM -> counts half (in practice: estimated, e.g. the club recovered between tracked frames)
#   LOW / missing -> excluded (fail closed: never reward or punish what the vision system doesn't trust)
CONFIDENCE_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.5}

# Coverage: at least 5 of the 8 checks must contribute. Address has only 3 checks, so 5 guarantees at
# least 2 swing-motion checks - a score is never just the set-up. Below it: no score, only the checks.
MIN_MEASURED = 5

NOT_ENOUGH = "Not enough reliable measurements for a Swing Score."

# ---------------------------------------------------------------------------- Swing Score v3
# Development only (engine profile ``next``): ``VERSION`` above stays the production model, and
# v3 is computed only when asked for explicitly (validation harness, tests). See scoring/v3.py.

V3_VERSION = "swing_score_v3"

# Measurement noise per check (check units), from scripts/estimate_measurement_noise.py on the
# reference swings with the C6 stature normaliser (docs/agent-work/analysis-quality/measurement_noise.json):
# the half-range of the reading at the moment frame +-1, median over swings. A check scores the higher
# of its quality at the reading and its *expected* quality given a Gaussian measurement error of this
# size: borderline readings are not penalised as if certain, readings far outside the band get no free
# allowance, and noise never costs an in-range reading anything. MEDIUM confidence widens it by 1.5x.
#   Plane checks: the one-frame change of the club angle at the moment (engine.plane_frames,
#   next profile): Mid-Back 7.0 deg (9 swings), Mid-Down 4.0 deg (10 swings), both measured.
#   impact.pelvis: hip-centre proxy (the silhouette is key-frame only).
V3_NOISE: dict[str, float] = {
    "address.torso_angle": 0.4, "address.trail_knee": 0.6, "address.arm_hang": 0.5,
    "mid_back.shaft": 7.0, "top.posture": 0.5, "downswing.shaft": 4.0,
    "impact.pelvis": 0.6, "impact.head": 1.55,
}
V3_MEDIUM_NOISE_FACTOR = 1.5

# Per-check quality q (0-1) at a value, from its deviation d beyond the target band in units of that
# side's "slightly out" band width (the expected value of this over the measurement noise is scored):
#   inside the target band ......... 1.00 at the ideal, 0.95 at the edge (mild preference for centre)
#   outside: q = FLOOR + (0.95 - FLOOR) * exp(-K * d ** P)
#            d = 0.5 -> 0.80, d = 1 (slight/clear boundary) -> 0.62, d = 2 -> 0.33, d = 3 -> 0.18
# (v2 for comparison: 6.5, 4, 0 points at d = 0.5, 1, 2.) A "clearly out" reading costs most of the check.
V3_EDGE_QUALITY = 0.95
V3_FLOOR = 0.10
V3_K = 0.5
V3_P = 1.4
V3_NOISE_SAMPLES = 41      # deterministic Gaussian quadrature over +-4 sigma
V3_SIGNIFICANT_D = 1.0   # beyond the "slightly out" band after noise: a genuinely significant fault

# Families of related checks. Within a family, the largest deduction counts in full and the others
# at RHO, so one root cause (for example standing up out of posture, which also moves the hips and
# head) is not charged three times. The two plane checks are one measurement taken twice.
V3_FAMILIES: dict[str, dict] = {
    "setup": {"label": "Set-up", "checks": ("address.torso_angle", "address.trail_knee", "address.arm_hang"),
              "rho": 1.0},
    "plane": {"label": "Swing plane", "checks": ("mid_back.shaft", "downswing.shaft"), "rho": 0.5},
    # Related but distinct: losing posture, early extension and head movement often share a cause,
    # so the second and third are discounted, not ignored.
    "posture": {"label": "Posture and stability", "checks": ("top.posture", "impact.pelvis", "impact.head"),
                "rho": 0.7},
}
# Compounding: each family beyond the first with a genuinely significant fault costs this much more.
V3_MULTI_FAULT = 1.0
V3_METHOD = ("Based on this swing's measured mechanics. Each position Swing Guru measured is compared with "
             "its target range, allowing for how precisely that position can be measured from a phone video. "
             "Small deviations cost a little, clear ones more. Related positions are grouped, so one fault is "
             "not counted several times, while several separate clear faults cost more together. A position we "
             "could not measure counts as neutral at half its weight. "
             "Swing Score evaluates this individual swing, not your handicap or overall playing ability.")

__all__ = ["V3_EDGE_QUALITY", "V3_FAMILIES", "V3_FLOOR", "V3_K", "V3_MEDIUM_NOISE_FACTOR", "V3_METHOD",
           "V3_MULTI_FAULT", "V3_NOISE", "V3_NOISE_SAMPLES", "V3_P", "V3_SIGNIFICANT_D", "V3_VERSION",
           "CHECKS", "CONFIDENCE_WEIGHT", "METHOD", "MIN_MEASURED", "MODELS", "MOTION_FINISH", "NOT_ENOUGH", "ScoreModel",
           "V1_METHOD", "POINTS_IDEAL", "POINTS_RANGE_EDGE",
           "POINTS_SLIGHT_EDGE", "SCALE", "ScoredCheck", "VERSION", "ZERO_AFTER_WIDTHS"]
