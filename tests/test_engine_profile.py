"""Engine profiles (1.0.1 coverage fixes, audit section C). ``current`` must stay production behaviour."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from swingguru import camera_correction as cc
from swingguru.analysis_bundle import build_body_check, swing_context
from swingguru.engine_profile import (
    FIXES, infer_handedness, physical_side, profile, reason_code, rebind_sides, stature_px,
)
from swingguru.phases import Phase


def _record(pixels: dict, index: int = 0, score: dict | None = None, measurements: dict | None = None):
    landmarks = {name: SimpleNamespace(visibility=(score or {}).get(name, 0.95), presence=(score or {}).get(name, 0.95))
                 for name in pixels}
    return SimpleNamespace(index=index, width=1000, height=1000,
                           points=SimpleNamespace(pixels={k: np.asarray(v, float) for k, v in pixels.items()},
                                                  landmarks=landmarks),
                           measurements=measurements or {})


# ---------------------------------------------------------------- profiles


def test_default_is_current_with_no_fixes(monkeypatch) -> None:
    monkeypatch.delenv("SG_ENGINE_PROFILE", raising=False)
    assert profile().name == "current" and not profile().fixes
    monkeypatch.setenv("SG_ENGINE_PROFILE", "next")
    assert profile().fixes == frozenset(FIXES)
    assert profile("next", only=["stature"]).fixes == {"stature"}
    with pytest.raises(ValueError):
        profile("experimental")
    with pytest.raises(ValueError):
        profile("next", only=["make_scores_higher"])


# ---------------------------------------------------------------- C4 handedness


def test_handedness_is_inferred_from_the_down_the_line_ball_side() -> None:
    assert infer_handedness(1.0, True) == ("right", "inferred_ball_side")
    assert infer_handedness(-1.0, True) == ("left", "inferred_ball_side")
    assert infer_handedness(-1.0, False) == ("right", "default")     # unknown -> production default
    assert physical_side("trail", "right") == "right" and physical_side("trail", "left") == "left"
    assert physical_side("lead", "right") == "left" and physical_side("lead", "left") == "right"


def test_rebinding_points_trail_at_the_left_leg_for_a_left_handed_golfer() -> None:
    record = _record({"left_knee": (1, 1), "right_knee": (2, 2), "left_hip": (3, 3), "right_hip": (4, 4),
                      "trail_knee": (2, 2), "lead_knee": (1, 1)})
    rebind_sides([record], "left")
    px = record.points.pixels
    assert tuple(px["trail_knee"]) == (1, 1) and tuple(px["lead_knee"]) == (2, 2)
    assert tuple(px["trail_hip"]) == (3, 3) and tuple(px["lead_hip"]) == (4, 4)
    rebind_sides([record], "right")
    assert tuple(px["trail_knee"]) == (2, 2)


# ---------------------------------------------------------------- C5 one-sided confidence


def test_knee_confidence_uses_only_the_measured_leg_under_side_confidence() -> None:
    from swingguru.measurements import MeasurementResult  # noqa: F401  (shape reference only)

    pixels = {"left_hip": (500, 500), "right_hip": (520, 500), "left_knee": (510, 700), "right_knee": (540, 700),
              "left_ankle": (500, 900), "right_ankle": (530, 900), "trail_hip": (520, 500), "trail_knee": (540, 700),
              "trail_ankle": (530, 900), "nose": (520, 200), "left_wrist": (600, 650), "right_wrist": (610, 650),
              "hip_mid": (510, 500), "shoulder_mid": (515, 300)}
    knee = SimpleNamespace(valid=True, value=18.0)
    far_knee_hidden = {"left_knee": 0.2}          # the far (lead) knee is occluded
    record = _record(pixels, score=far_knee_hidden, measurements={"trail_knee_flex_deg": knee})
    plain = swing_context([record], 0, "rtmw")
    fixed = swing_context([record], 0, "rtmw", profile("next", only=["side_confidence"]))
    assert build_body_check("trail_knee", Phase.ADDRESS, record, plain)[0] is None      # both legs: refused
    check, _ = build_body_check("trail_knee", Phase.ADDRESS, record, fixed)
    assert check is not None and check["level"] == "HIGH"


# ---------------------------------------------------------------- C6 stature


def test_stature_does_not_shrink_when_the_golfer_bends_over() -> None:
    upright = _record({"nose": (500, 100), "shoulder_mid": (500, 250), "hip_mid": (500, 500),
                       "left_hip": (500, 500), "left_knee": (500, 720), "left_ankle": (500, 940)})
    # The same body (head 150, torso 250, thigh 220, shin 220) hinged 40 deg with soft knees.
    bent = _record({"nose": (707.1, 213.6), "shoulder_mid": (610.7, 328.5), "hip_mid": (450, 520),
                    "left_hip": (450, 520), "left_knee": (506.9, 732.5), "left_ankle": (487.7, 951.7)})
    assert stature_px(upright) == pytest.approx(840, rel=0.01)
    # Nose-to-ankle (the current normaliser) shrinks with the posture; the chain does not.
    assert np.linalg.norm(np.array([707.1, 213.6]) - np.array([487.7, 951.7])) < 0.93 * 840
    assert stature_px(bent) == pytest.approx(840, rel=0.01)
    assert stature_px(_record({"nose": (0, 0)})) is None


# ---------------------------------------------------------------- C2 camera correction


def _comparison(shift_px: float) -> dict:
    return {"status": "INVALID", "reliable": True, "matrix": [[1, 0, shift_px], [0, 1, 0]], "residual_px": 0.5,
            "rotation_deg": 0.0, "scale": 1.0, "shift_px": shift_px}


def _feet(dx_ankle: float, dx_toe: float = 0.0):
    address = _record({"left_ankle": (400, 900), "right_ankle": (600, 900),
                       "left_foot_index": (380, 930), "right_foot_index": (620, 930)})
    later = _record({"left_ankle": (400 + dx_ankle, 900), "right_ankle": (600 + dx_ankle, 900),
                     "left_foot_index": (380 + dx_toe, 930), "right_foot_index": (620 + dx_toe, 930)})
    return address, later


def test_production_correction_is_unchanged_a_moved_foot_still_vetoes() -> None:
    address, later = _feet(dx_ankle=30)             # ankles 3 % of an 1000 px body, after correction too
    result = cc.assess(_comparison(4.0), address, later, frame_height=1000, body_height_px=1000,
                       motion_status="INVALID")
    assert result["status"] == cc.UNCERTAIN and "accepted_by" not in result


def test_small_camera_shift_is_trusted_when_the_foot_cannot_decide() -> None:
    config = cc.CorrectionConfig(small_shift_pct=1.0, foot_parts=("ankle", "heel", "foot_index"))
    address, later = _feet(dx_ankle=34, dx_toe=34)   # every foot point moved: the witness says no
    small = cc.assess(_comparison(4.0), address, later, frame_height=1000, body_height_px=1000,
                      motion_status="INVALID", config=config)
    assert small["status"] == cc.CORRECTED_VALID and small["accepted_by"] == "small_shift"
    assert small["shift_pct_body"] == pytest.approx(0.4)
    address, later = _feet(dx_ankle=60, dx_toe=60)   # camera 25 px, feet 35 px further: genuinely unclear
    large = cc.assess(_comparison(25.0), address, later, frame_height=1000, body_height_px=1000,
                      motion_status="INVALID", config=config)
    assert large["status"] == cc.UNCERTAIN            # a real camera move still needs the witness


def test_a_planted_toe_confirms_the_correction_when_the_ankle_rolled() -> None:
    config = cc.CorrectionConfig(foot_parts=("ankle", "heel", "foot_index"))
    address, later = _feet(dx_ankle=40, dx_toe=25)   # camera moved 25 px; the ankle also rolled
    result = cc.assess(_comparison(25.0), address, later, frame_height=1000, body_height_px=1000,
                       motion_status="INVALID", config=config)
    assert result["status"] == cc.CORRECTED_VALID and "accepted_by" not in result


def test_a_bad_registration_is_never_rescued() -> None:
    config = cc.CorrectionConfig(small_shift_pct=1.0)
    address, later = _feet(dx_ankle=40)
    bad = {**_comparison(2.0), "residual_px": 3.0}
    assert cc.assess(bad, address, later, frame_height=1000, body_height_px=1000, motion_status="INVALID",
                     config=config)["status"] == cc.INVALID


# ---------------------------------------------------------------- reason codes


@pytest.mark.parametrize("reason,code", [
    ("the camera moved during the swing, so movement from Address cannot be measured", "camera_uncorrected"),
    ("club not detected in this frame", "club_not_detected"),
    ("club detection confidence LOW", "club_low_confidence"),
    ("moment not found", "moment_missing"),
    ("pose confidence 0.31 too low", "pose_low_confidence"),
    ("something new", "other"),
])
def test_every_unavailable_reason_has_a_stable_code(reason, code) -> None:
    assert reason_code(reason) == code
