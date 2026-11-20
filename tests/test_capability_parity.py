"""Capability parity: camera-drift correction, P2 detection, hand depth at the Top."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru import analysis_bundle as bundle
from swingguru import camera_correction as cc
from swingguru import takeaway_p2
from swingguru.auto_moments import estimate_moments
from swingguru.measurements import MeasurementConfig
from swingguru.phases import Phase
from swingguru.pipeline import apply_reference, build_record
from swingguru.synthetic import joints_to_observation
from swingguru.ui.guided_fixture import GuidedSwingClip, guided_joints

CLIP = GuidedSwingClip(width=360, height=640)
SHIFT = np.array([[1.0, 0.0, 40.0], [0.0, 1.0, -12.0]])  # a pure camera pan


@pytest.fixture(scope="module")
def swing():
    config = MeasurementConfig()
    records = [
        build_record(i, i * 1000.0 / CLIP.fps, np.zeros((CLIP.height, CLIP.width, 3), np.uint8),
                     joints_to_observation(guided_joints(i, clip=CLIP)), config=config)
        for i in range(CLIP.frame_count)
    ]
    moments = estimate_moments(records)
    apply_reference(records, moments.frames()[Phase.ADDRESS], config=config)
    return records, moments


def _pose(pixels: dict) -> SimpleNamespace:
    return SimpleNamespace(points=SimpleNamespace(pixels={k: np.asarray(v, float) for k, v in pixels.items()}))


def _panned(record, matrix=SHIFT):
    """The same body seen by a camera that moved by ``matrix`` (Address -> later)."""

    pixels = {name: cc.to_later(p, matrix) for name, p in record.points.pixels.items()}
    return SimpleNamespace(index=record.index, timestamp_ms=record.timestamp_ms,
                           points=SimpleNamespace(pixels=pixels, landmarks=record.points.landmarks),
                           measurements=getattr(record, "measurements", {}))


def _comparison(matrix=SHIFT, **overrides):
    m = np.asarray(matrix, float)
    base = {"status": "INVALID", "reliable": True, "matrix": m.tolist(), "residual_px": 0.8,
            "rotation_deg": 0.0, "scale": 1.0, "shift_px": float(np.hypot(*m[:, 2]))}
    return {**base, **overrides}


# --- camera correction -------------------------------------------------------

def test_inverse_and_forward_transforms_round_trip() -> None:
    angle = np.radians(1.5)
    m = np.array([[1.02 * np.cos(angle), -1.02 * np.sin(angle), 30.0],
                  [1.02 * np.sin(angle), 1.02 * np.cos(angle), -8.0]])
    p = np.array([123.0, 456.0])
    assert np.allclose(cc.to_address(cc.to_later(p, m), m), p)


def test_tripod_clip_is_left_as_measured() -> None:
    feet = {"left_ankle": (100, 600), "right_ankle": (160, 600)}
    result = cc.assess({"status": "VALID", "reliable": True}, _pose(feet), _pose(feet), frame_height=640,
                       body_height_px=500, motion_status="VALID")
    assert result["status"] == cc.VALID and result["matrix"] is None


def test_camera_pan_is_corrected_when_the_planted_foot_agrees() -> None:
    feet = {"left_ankle": (100.0, 600.0), "right_ankle": (160.0, 600.0)}
    later = {k: cc.to_later(v, SHIFT) for k, v in feet.items()}
    result = cc.assess(_comparison(), _pose(feet), _pose(later), frame_height=640, body_height_px=500,
                       motion_status="INVALID")
    assert result["status"] == cc.CORRECTED_VALID
    assert result["foot_residual_pct"] == pytest.approx(0.0, abs=1e-6)
    assert result["foot_raw_pct"] > 8  # the raw feet "moved" by the pan


def test_correction_fails_closed_when_the_feet_disagree() -> None:
    feet = {"left_ankle": (100.0, 600.0), "right_ankle": (160.0, 600.0)}
    # Parallax / a wrong transform: after correction both feet are still ~4 % away.
    later = {k: cc.to_later(np.asarray(v) + (20.0, 0.0), SHIFT) for k, v in feet.items()}
    result = cc.assess(_comparison(), _pose(feet), _pose(later), frame_height=640, body_height_px=500,
                       motion_status="INVALID")
    assert result["status"] == cc.UNCERTAIN and not cc.usable(result)


def test_correction_needs_ankles() -> None:
    result = cc.assess(_comparison(), _pose({}), _pose({}), frame_height=640, body_height_px=500,
                       motion_status="INVALID")
    assert result["status"] == cc.UNCERTAIN


@pytest.mark.parametrize("overrides", [
    {"reliable": False}, {"residual_px": 3.0}, {"rotation_deg": 6.0}, {"scale": 1.2}, {"shift_px": 200.0},
])
def test_unreliable_or_implausible_transforms_are_invalid(overrides) -> None:
    feet = {"left_ankle": (100.0, 600.0), "right_ankle": (160.0, 600.0)}
    result = cc.assess(_comparison(**overrides), _pose(feet), _pose(feet), frame_height=640, body_height_px=500,
                       motion_status="INVALID")
    assert result["status"] == cc.INVALID and not cc.usable(result)


def test_corrected_head_matches_the_tripod_value(swing) -> None:
    """The regression behind swing 08: a pan must not read as head movement."""

    records, moments = swing
    context = bundle.swing_context(records, moments.frames()[Phase.ADDRESS], "rtmw")
    top = records[moments.frames()[Phase.TOP]]
    truth, _ = bundle.build_body_check("head", Phase.TOP, top, context)
    moved = _panned(top)
    raw, _ = bundle.build_body_check("head", Phase.TOP, moved, context)
    assert truth and raw
    assert raw["raw"]["movement_pct_height"] > truth["raw"]["movement_pct_height"] + 5
    analysis = {"phases": [{"phase": "top", "checks": [raw]}]}
    bundle.apply_position_correction(analysis, {"top": moved},
                                     {"top": {"status": cc.CORRECTED_VALID, "matrix": SHIFT.tolist()}}, context)
    fixed = analysis["phases"][0]["checks"][0]
    assert fixed["camera"] == cc.CORRECTED_VALID
    assert fixed["raw"]["movement_pct_height"] == pytest.approx(truth["raw"]["movement_pct_height"], abs=1e-6)
    assert fixed["raw"]["uncorrected_movement_pct_height"] == raw["raw"]["movement_pct_height"]
    # Geometry is drawn where the body is in the moved frame.
    assert fixed["anchor"] != truth["anchor"]


def test_uncorrected_phases_keep_the_raw_check(swing) -> None:
    records, moments = swing
    context = bundle.swing_context(records, moments.frames()[Phase.ADDRESS], "rtmw")
    top = records[moments.frames()[Phase.TOP]]
    check, _ = bundle.build_body_check("head", Phase.TOP, top, context)
    analysis = {"phases": [{"phase": "top", "checks": [check]}]}
    bundle.apply_position_correction(analysis, {"top": top}, {"top": {"status": cc.UNCERTAIN}}, context)
    assert analysis["phases"][0]["checks"][0] is check


def _depth(rear_x: float) -> dict:
    return {"tush_line_x": 100.0, "phases": {"impact": {
        "available": True, "rear": {"point": [rear_x, 300.0]}, "displacement_pct_height": 99.0, "level": "HIGH"}}}


def test_rear_pelvis_is_corrected_or_withheld(swing) -> None:
    records, moments = swing
    context = replace(bundle.swing_context(records, moments.frames()[Phase.ADDRESS], "rtmw"),
                      body_height_px=500.0, toward_ball=1.0, toward_ball_known=True)
    # Rear pelvis 5 px toward the ball in Address coordinates, seen through the pan.
    seen = float(cc.to_later((105.0, 300.0), SHIFT)[0])
    fixed = bundle.correct_pelvis_depth(_depth(seen), {"impact": {"status": cc.CORRECTED_VALID,
                                                                  "matrix": SHIFT.tolist()}},
                                        context, camera_moved=True)["phases"]["impact"]
    assert fixed["displacement_pct_height"] == pytest.approx(1.0)
    assert fixed["uncorrected_displacement_pct_height"] == 99.0
    withheld = bundle.correct_pelvis_depth(_depth(seen), {"impact": {"status": cc.UNCERTAIN}},
                                           context, camera_moved=True)["phases"]["impact"]
    assert withheld["available"] is False and withheld["displacement_pct_height"] is None
    tripod = bundle.correct_pelvis_depth(_depth(seen), {}, context, camera_moved=False)["phases"]["impact"]
    assert tripod["displacement_pct_height"] == 99.0  # untouched


# --- plane reference placement -------------------------------------------------

def _plane_analysis(motion_status: str) -> dict:
    geometry = [{"kind": "line", "role": "shaft", "points": [[0.4, 0.5], [0.2, 0.3]]},
                {"kind": "line", "role": "reference", "points": [[0.4, 0.6], [0.7, 0.9]]},
                {"kind": "dot", "role": "origin", "points": [[0.7, 0.9]]}]
    check = {"kind": "shaft", "phase": "mid_back", "geometry": geometry}
    return {"phases": [{"phase": "mid_back", "checks": [check]}], "camera_correction": {},
            "camera_check": {"signals": {"camera_motion": {"status": motion_status}}}}


def test_plane_reference_follows_a_confirmed_camera_move(swing) -> None:
    records, moments = swing
    frames = moments.frames()
    context = bundle.swing_context(records, frames[Phase.ADDRESS], "rtmw")
    address, mid_back = records[frames[Phase.ADDRESS]], _panned(records[frames[Phase.MID_BACK]])
    analysis = _plane_analysis("INVALID")
    bundle.align_plane_references(analysis, [address, mid_back], frames, {}, _comparison(), context, camera_moved=True)
    check = analysis["phases"][0]["checks"][0]
    assert check["reference_frame"] == "corrected"
    origin = next(g for g in check["geometry"] if g["role"] == "origin")["points"][0]
    expected = cc.to_later((0.7 * context.width, 0.9 * context.height), SHIFT)
    assert origin == pytest.approx([expected[0] / context.width, expected[1] / context.height], abs=1e-4)
    shaft = next(g for g in check["geometry"] if g["role"] == "shaft")
    assert shaft["points"] == [[0.4, 0.5], [0.2, 0.3]]  # the measurement is untouched


def test_plane_reference_is_not_extended_when_the_move_is_unconfirmed(swing) -> None:
    records, moments = swing
    frames = moments.frames()
    context = bundle.swing_context(records, frames[Phase.ADDRESS], "rtmw")
    analysis = _plane_analysis("INVALID")
    bundle.align_plane_references(analysis, records, frames, {}, _comparison(residual_px=4.0), context,
                                  camera_moved=True)
    check = analysis["phases"][0]["checks"][0]
    assert check["reference_frame"] == "unaligned"
    assert all(g["role"] != "origin" for g in check["geometry"])
    tripod = _plane_analysis("VALID")
    bundle.align_plane_references(tripod, records, frames, {}, None, context, camera_moved=False)
    assert tripod["phases"][0]["checks"][0]["reference_frame"] == "address"


# --- hand depth at the Top -----------------------------------------------------

def test_hand_depth_is_measured_in_one_frame_so_a_pan_does_not_change_it(swing) -> None:
    records, moments = swing
    context = bundle.swing_context(records, moments.frames()[Phase.ADDRESS], "rtmw")
    top = records[moments.frames()[Phase.TOP]]
    check, reason = bundle.build_body_check("hand_depth", Phase.TOP, top, context)
    assert check is not None, reason
    assert check["raw"]["hands_used"] >= 1
    assert any(g["role"] == "reference" for g in check["geometry"])
    moved, _ = bundle.build_body_check("hand_depth", Phase.TOP, _panned(top), context)
    assert moved["raw"]["hand_offset_pct_height"] == pytest.approx(check["raw"]["hand_offset_pct_height"], abs=1e-6)


def test_hand_depth_has_no_verdict_vocabulary() -> None:
    from swingguru import verdicts

    assert "top.hand_depth" in verdicts.UNSUPPORTED_CHECKPOINTS


# --- P2 (shaft parallel in the backswing) ----------------------------------------

def _frame(frame: int, elevation: float, level: str = "HIGH", **extra) -> dict:
    grip = np.array([200.0, 400.0])
    a = np.radians(elevation)
    tip = grip + 150.0 * np.array([-np.cos(a), -np.sin(a)])
    return {"frame": frame, "available": True, "interpolated": False, "level": level,
            "grip": grip.tolist(), "tip": tip.tolist(), **extra}


def test_elevation_sign() -> None:
    assert takeaway_p2.elevation_deg((0, 100), (100, 50)) > 0   # clubhead above the hands
    assert takeaway_p2.elevation_deg((0, 100), (100, 150)) < 0
    assert takeaway_p2.elevation_deg((0, 0), (0, 0)) is None


def test_p2_is_the_detected_frame_nearest_level_on_the_rise() -> None:
    path = [_frame(10, -50), _frame(11, -20), _frame(12, 6), _frame(13, 40)]
    result = takeaway_p2.detect(path, takeaway_frame=10, mid_back_frame=14, toward_ball=-1.0, address_shaft_px=150)
    assert result["detected"] and result["frame"] == 12 and result["bracket"] == [11, 12]
    assert result["level"] == "HIGH" and result["projected_length"] == pytest.approx(1.0)


def test_p2_fails_closed() -> None:
    kw = dict(takeaway_frame=10, mid_back_frame=14, toward_ball=1.0, address_shaft_px=150)
    # Never rises through level.
    assert not takeaway_p2.detect([_frame(10, -50), _frame(11, -30)], **kw)["detected"]
    # The tracker lost the club through level: nearest detected frame is too far off.
    assert not takeaway_p2.detect([_frame(10, -40), _frame(13, 35)], **kw)["detected"]
    # Interpolated or LOW frames never count.
    assert not takeaway_p2.detect([_frame(10, -10), _frame(11, 5, interpolated=True)], **kw)["detected"]
    assert not takeaway_p2.detect([_frame(10, -10), _frame(11, 5, level="LOW")], **kw)["detected"]
    # Outside the Takeaway..Mid-Back window.
    assert not takeaway_p2.detect([_frame(30, -10), _frame(31, 5)], **kw)["detected"]
    assert not takeaway_p2.detect([_frame(10, -10), _frame(11, 5)], takeaway_frame=None, mid_back_frame=14,
                                  toward_ball=1.0, address_shaft_px=150)["detected"]


def test_p2_needs_a_witnessed_rise_and_a_foreshortened_line() -> None:
    kw = dict(takeaway_frame=10, mid_back_frame=14, toward_ball=1.0, address_shaft_px=150)
    # A near-level line with nothing seen before it (the horizon at Mid-Back).
    assert "approach" in takeaway_p2.detect([_frame(12, -1), _frame(13, 50)], **kw)["reason"]
    # Longer in the image than the club at Address: a background line.
    long_line = _frame(12, 3)
    long_line["tip"] = [200.0 - 300.0, 400.0 - 15.0]
    result = takeaway_p2.detect([_frame(10, -40), _frame(11, -20), long_line], **kw)
    assert not result["detected"] and "background" in result["reason"]
    # The frame after the crossing may be after Mid-Back; P2 itself may not.
    late = takeaway_p2.detect([_frame(12, -40), _frame(14, -5), _frame(15, 30)], **kw)
    assert late["detected"] and late["frame"] == 14


def test_p2_from_recovered_frames_is_never_high() -> None:
    path = [_frame(10, -50), _frame(11, -20, status="recovered"), _frame(12, 3), _frame(13, 40)]
    result = takeaway_p2.detect(path, takeaway_frame=10, mid_back_frame=14, toward_ball=-1.0, address_shaft_px=150,
                                fps=30.0, first_frame=0)
    assert result["detected"] and result["frame"] == 12 and result["level"] == "MEDIUM"
    assert result["source"] == "direct" and result["bracket_sources"] == ["temporal recovery", "direct"]
    assert result["time_s"] == pytest.approx(0.4)


def test_p2_is_not_a_guided_check(swing) -> None:
    records, moments = swing
    analysis = bundle.build_analysis(records, moments, None, fps=CLIP.fps, video="v.mp4",
                                     takeaway_p2={"detected": True, "frame": 5})
    assert analysis["takeaway_p2"]["status"] == "diagnostic_only"
    takeaway = next(p for p in analysis["phases"] if p["phase"] == "takeaway")
    assert all("p2" not in c["kind"] for c in takeaway["checks"])
