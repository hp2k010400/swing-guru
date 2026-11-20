"""Rear-of-pelvis (tush line) measurement and the DTL camera check."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.camera_check import INVALID, UNCERTAIN, VALID, background_shift, check_camera
from swingguru.pelvis_depth import (
    LEVEL_HIGH,
    LEVEL_UNAVAILABLE,
    RearPelvis,
    displacement_from_line,
    measure_pelvis_depth,
    rear_pelvis,
)

H, W = 600, 400
TORSO = 100.0
HIP = np.array([200.0, 300.0])


def _mask(rear_x: float = 150.0, *, top: int = 150, bottom: int = 560, front: int = 320, soft: float = 0.0) -> np.ndarray:
    """A golfer-shaped blob: body from ``rear_x`` to ``front`` between rows."""

    xs = np.arange(W)[None, :].repeat(H, 0).astype(float)
    mask = np.zeros((H, W), np.float32)
    rows = slice(top, bottom)
    if soft <= 0:
        mask[rows] = ((xs[rows] >= rear_x) & (xs[rows] <= front)).astype(np.float32)
    else:  # a soft edge: probability ramps over ``soft`` pixels
        ramp = np.clip((xs[rows] - rear_x) / soft + 0.5, 0, 1)
        mask[rows] = ramp * (xs[rows] <= front)
    return mask


def test_rear_point_is_found_on_the_silhouette_behind_the_hips() -> None:
    result = rear_pelvis(_mask(150.0), HIP, toward_ball=1, torso_px=TORSO, frame_index=7)
    assert result.available and result.level == LEVEL_HIGH
    assert result.point[0] == pytest.approx(149.5, abs=0.6)  # sub-pixel crossing of the 0.5 level
    assert result.band[0] <= result.point[1] <= result.band[1]
    assert result.depth_torso == pytest.approx(0.5, abs=0.02)


def test_direction_away_from_the_ball_is_respected() -> None:
    mirrored = _mask(150.0)[:, ::-1].copy()  # ball on the left now
    result = rear_pelvis(mirrored, np.array([W - 1 - 200.0, 300.0]), toward_ball=-1, torso_px=TORSO)
    assert result.available and result.point[0] == pytest.approx(W - 1 - 149.5, abs=0.6)


@pytest.mark.parametrize("hip, mask, kwargs, reason", [
    (None, _mask(), {}, "hips not tracked"),
    (HIP, None, {}, "no silhouette"),
    (HIP, _mask(), {"toward_ball": 0}, "direction"),
    (HIP, _mask(), {"hip_score": 0.2}, "confidence"),
    (np.array([380.0, 300.0]), _mask(front=320), {}, "outline"),  # hip midpoint outside the body
])
def test_missing_or_occluded_pelvis_fails_closed(hip, mask, kwargs, reason) -> None:
    args = {"toward_ball": 1, "torso_px": TORSO, **kwargs}
    result = rear_pelvis(mask, hip, **args)
    assert not result.available and result.level == LEVEL_UNAVAILABLE
    assert reason in result.reason


def test_outline_touching_the_frame_edge_is_not_a_tush_line() -> None:
    hip = np.array([60.0, 300.0])  # golfer framed tight against the left edge
    clipped = rear_pelvis(_mask(0.0), hip, toward_ball=1, torso_px=TORSO)
    assert not clipped.available and "frame edge" in clipped.reason
    near = rear_pelvis(_mask(2.0), hip, toward_ball=1, torso_px=TORSO)
    assert not near.available and "frame edge" in near.reason


def test_missing_camera_motion_evidence_is_never_valid() -> None:
    result = check_camera(_record(), _record(), frame_size=(W, H), body_height_px=390.0, torso_px=TORSO)
    assert result["signals"]["camera_motion"]["status"] == UNCERTAIN
    assert result["status"] == UNCERTAIN


def test_a_soft_ambiguous_edge_is_rejected() -> None:
    result = rear_pelvis(_mask(150.0, soft=40.0), HIP, toward_ball=1, torso_px=TORSO)
    assert not result.available and "soft" in result.reason


def test_implausible_depth_is_rejected() -> None:
    result = rear_pelvis(_mask(20.0), HIP, toward_ball=1, torso_px=TORSO)  # 1.8 torso lengths behind the hips
    assert not result.available and "implausible" in result.reason


def test_displacement_is_normalised_and_signed_toward_the_ball() -> None:
    address = rear_pelvis(_mask(150.0), HIP, toward_ball=1, torso_px=TORSO)
    impact = rear_pelvis(_mask(165.0), HIP, toward_ball=1, torso_px=TORSO)
    result = displacement_from_line("impact", address, impact, toward_ball=1, body_height_px=300.0)
    assert result.available
    assert result.displacement_px == pytest.approx(15.0, abs=0.1)
    assert result.displacement_pct_height == pytest.approx(5.0, abs=0.05)
    assert result.direction == "toward the ball"
    back = rear_pelvis(_mask(140.0), HIP, toward_ball=1, torso_px=TORSO)
    assert displacement_from_line("top", address, back, toward_ball=1, body_height_px=300.0).direction == "away from the ball"
    still = rear_pelvis(_mask(150.5), HIP, toward_ball=1, torso_px=TORSO)
    assert displacement_from_line("top", address, still, toward_ball=1, body_height_px=300.0).direction == "on the Address line"


def test_no_address_line_or_body_scale_means_no_displacement() -> None:
    missing = RearPelvis(0, False, LEVEL_UNAVAILABLE, reason="hips not tracked")
    current = rear_pelvis(_mask(160.0), HIP, toward_ball=1, torso_px=TORSO)
    assert not displacement_from_line("impact", missing, current, toward_ball=1, body_height_px=300).available
    address = rear_pelvis(_mask(150.0), HIP, toward_ball=1, torso_px=TORSO)
    assert not displacement_from_line("impact", address, current, toward_ball=1, body_height_px=None).available


def _record(hip=HIP, *, shoulders_apart=10.0, hands_ahead=60.0, include=("nose", "left_ankle", "right_ankle")):
    pixels = {
        "hip_mid": np.asarray(hip, float),
        "left_hip": hip + [-shoulders_apart / 2, 0], "right_hip": hip + [shoulders_apart / 2, 0],
        "left_shoulder": hip + [-shoulders_apart / 2, -100], "right_shoulder": hip + [shoulders_apart / 2, -100],
        "left_wrist": hip + [hands_ahead, 60], "right_wrist": hip + [hands_ahead, 60],
    }
    extra = {"nose": hip + [20, -150], "left_ankle": hip + [0, 240], "right_ankle": hip + [5, 240]}
    pixels.update({k: v for k, v in extra.items() if k in include})
    return SimpleNamespace(index=0, points=SimpleNamespace(pixels=pixels, landmarks={}))


def test_measure_pelvis_depth_is_deterministic_and_complete() -> None:
    frames = {"address": (99, None, _record()), "impact": (134, None, _record())}
    masks = {"address": _mask(150.0), "impact": _mask(165.0)}
    first = measure_pelvis_depth(frames, masks, toward_ball=1, torso_px=TORSO, body_height_px=300.0)
    second = measure_pelvis_depth(frames, masks, toward_ball=1, torso_px=TORSO, body_height_px=300.0)
    assert first == second
    assert first["tush_line_x"] == pytest.approx(149.5, abs=0.6)
    assert first["phases"]["impact"]["displacement_pct_height"] == pytest.approx(5.0, abs=0.05)
    assert first["phases"]["top"]["available"] is False  # moment not supplied
    unknown = measure_pelvis_depth(frames, masks, toward_ball=None, torso_px=TORSO, body_height_px=300.0)
    assert unknown["address"]["available"] is False


def test_camera_states_valid_uncertain_invalid() -> None:
    still = (np.random.default_rng(2).random((H, W, 3)) * 255).astype(np.uint8)
    kwargs = {"frame_size": (W, H), "body_height_px": 390.0, "torso_px": TORSO,
              "address_frame": still, "impact_frame": still}
    dtl = check_camera(_record(), _record(), **kwargs)
    assert dtl["status"] == VALID
    face_on = check_camera(_record(shoulders_apart=90.0, hands_ahead=5.0), _record(), **kwargs)
    assert face_on["status"] == INVALID
    assert face_on["signals"]["orientation_width"]["status"] == INVALID
    assert face_on["signals"]["hands_in_front"]["status"] == INVALID
    angled = check_camera(_record(shoulders_apart=60.0), _record(), **kwargs)
    assert angled["status"] == UNCERTAIN
    cut_off = check_camera(_record(include=("nose", "left_ankle")), _record(), **kwargs)
    assert cut_off["status"] == INVALID and cut_off["signals"]["full_body"]["status"] == INVALID
    small = check_camera(_record(), _record(), **{**kwargs, "body_height_px": 150.0})
    assert small["signals"]["golfer_size"]["status"] == UNCERTAIN
    assert check_camera(None, None, **kwargs)["status"] == INVALID


def test_camera_motion_is_detected_from_the_background() -> None:
    rng = np.random.default_rng(1)
    base = (rng.random((H, W, 3)) * 255).astype(np.uint8)
    shifted = np.roll(base, 25, axis=1)
    assert background_shift(base, base, None, None) == pytest.approx(0.0, abs=0.5)
    assert background_shift(base, shifted, None, None) == pytest.approx(25.0, abs=1.0)
    moving = check_camera(_record(), _record(), frame_size=(W, H), body_height_px=390.0, torso_px=TORSO,
                          address_frame=base, impact_frame=shifted)
    assert moving["signals"]["camera_motion"]["status"] == INVALID
