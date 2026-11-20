"""Camera-motion check: background-only registration that fails closed.

Regression for the first version, which blanked the golfer with the same flat
patch in both frames; the patch edges dominated phase correlation and a
panning phone (swing 08, ~55-60 px) read as 0.3 px and VALID.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from swingguru.camera_check import (
    INVALID,
    UNCERTAIN,
    VALID,
    CameraCheckConfig,
    background_motion,
    check_camera,
    motion_signal,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "camera_motion"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(swing: str, phase: str) -> tuple[np.ndarray, np.ndarray]:
    image = cv2.imread(str(FIXTURES / f"swing{swing}_{phase}.jpg"))
    mask = cv2.imread(str(FIXTURES / f"swing{swing}_{phase}_mask.png"), cv2.IMREAD_GRAYSCALE)
    assert image is not None and mask is not None, "camera-motion fixture missing"
    return image, mask.astype(np.float32) / 255.0


def _texture(height=600, width=400, seed=3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = (rng.random((height // 4, width // 4, 3)) * 255).astype(np.uint8)
    return cv2.resize(base, (width, height), interpolation=cv2.INTER_NEAREST)


def _shift(image: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(np.roll(image, dy, axis=0), dx, axis=1)


# --- swing 08 regression -------------------------------------------------------


@pytest.mark.parametrize("later", ["top", "impact"])
def test_swing08_real_pan_is_measured(later) -> None:
    address, a_mask = _load("08", "address")
    frame, mask = _load("08", later)
    estimate = background_motion(address, frame, a_mask, mask)
    assert estimate.reliable, estimate.reason
    # The pan measured independently on background patches was ~45-60 px; the
    # check must see tens of pixels, not the 0.3 px of the old method.
    assert 40.0 <= estimate.shift_px <= 75.0
    assert estimate.shift_px / address.shape[0] > CameraCheckConfig().motion_invalid


def _record(width, height):
    cx, cy = width * 0.35, height * 0.5
    pixels = {
        "hip_mid": np.array([cx, cy]), "left_hip": np.array([cx - 5, cy]), "right_hip": np.array([cx + 5, cy]),
        "left_shoulder": np.array([cx - 5, cy - 150]), "right_shoulder": np.array([cx + 5, cy - 150]),
        "left_wrist": np.array([cx + 100, cy + 60]), "right_wrist": np.array([cx + 100, cy + 60]),
        "nose": np.array([cx + 20, cy - 220]), "left_ankle": np.array([cx, cy + 330]),
        "right_ankle": np.array([cx + 5, cy + 330]),
    }
    return SimpleNamespace(index=0, points=SimpleNamespace(pixels=pixels, landmarks={}))


def _camera(swing: str) -> dict:
    address, a_mask = _load(swing, "address")
    top, t_mask = _load(swing, "top")
    impact, i_mask = _load(swing, "impact")
    height, width = address.shape[:2]
    record = _record(width, height)
    return check_camera(record, record, frame_size=(width, height), body_height_px=550.0, torso_px=150.0,
                        address_frame=address, impact_frame=impact, address_mask=a_mask, impact_mask=i_mask,
                        motion_frames={"top": (top, t_mask)})


def test_swing08_camera_is_invalid_and_blocks_pelvis_interpretation() -> None:
    result = _camera("08")
    motion = result["signals"]["camera_motion"]
    assert motion["status"] == INVALID and result["status"] == INVALID
    assert set(motion["comparisons"]) == {"address->top", "address->impact"}
    assert all(c["status"] == INVALID for c in motion["comparisons"].values())

    from test_interpretation import _analysis  # the shared interpretation fixture

    from swingguru.interpretation import interpret

    results = {r["rule_id"]: r for r in interpret(_analysis(camera=result["status"], rear_pct=19.5))["results"]}
    pattern = results["impact.hips_toward_ball_with_upright_torso"]
    assert pattern["status"] == "blocked" and "camera check" in pattern["reason"]
    assert results["impact.rear_pelvis_vs_tush_line"]["status"] == "blocked"
    assert pattern["user_text"] is None


def test_still_tripod_clip_stays_valid() -> None:
    address, a_mask = _load("04", "address")
    impact, i_mask = _load("04", "impact")
    estimate = background_motion(address, impact, a_mask, i_mask)
    assert estimate.reliable and estimate.shift_px < 2.0
    assert _camera("04")["signals"]["camera_motion"]["status"] == VALID


# --- the old failure mode -------------------------------------------------------


def test_identical_golfer_patches_do_not_hide_camera_motion() -> None:
    """The first version's bug: a flat patch at the same place in both frames."""

    a = _texture()
    b = _shift(a, 30, 12)
    person = np.zeros(a.shape[:2], np.float32)
    person[150:500, 120:260] = 1.0  # the golfer, same place in both frames
    a[person > 0] = 128
    b[person > 0] = 128
    estimate = background_motion(a, b, person, person)
    assert estimate.reliable
    assert estimate.dx_px == pytest.approx(30, abs=1.5) and estimate.dy_px == pytest.approx(12, abs=1.5)


def test_moving_golfer_on_a_still_background_is_still() -> None:
    a = _texture()
    b = a.copy()
    golfer_a = np.zeros(a.shape[:2], np.float32)
    golfer_b = np.zeros(a.shape[:2], np.float32)
    golfer_a[150:500, 120:220] = 1
    golfer_b[150:500, 180:280] = 1  # golfer moved, camera did not
    b[golfer_b > 0] = _texture(seed=9)[golfer_b > 0]
    estimate = background_motion(a, b, golfer_a, golfer_b)
    assert estimate.reliable and estimate.shift_px < 1.0


# --- insufficient evidence -> UNCERTAIN, never VALID -----------------------------


def test_insufficient_background_is_uncertain() -> None:
    a = _texture()
    everything = np.ones(a.shape[:2], np.float32)
    everything[:20, :20] = 0
    estimate = background_motion(a, a, everything, everything)
    assert not estimate.reliable and "background" in estimate.reason


def test_textureless_background_is_uncertain() -> None:
    flat = np.full((600, 400, 3), 120, np.uint8)
    estimate = background_motion(flat, flat, None, None)
    assert not estimate.reliable


def test_clustered_background_evidence_is_uncertain() -> None:
    # Texture only in one small corner: points too clustered to trust.
    a = np.full((600, 400, 3), 120, np.uint8)
    a[20:100, 20:100] = _texture(80, 80)
    estimate = background_motion(a, a, None, None)
    assert not estimate.reliable


def test_unmeasured_motion_is_never_valid() -> None:
    settings = CameraCheckConfig()
    flat = np.full((600, 400, 3), 120, np.uint8)
    signal = motion_signal((flat, None), {"top": (flat, None), "impact": (flat, None)}, 600, settings)
    assert signal["status"] == UNCERTAIN and signal["value"] is None
    assert motion_signal((flat, None), {}, 600, settings)["status"] == UNCERTAIN


def test_any_trusted_large_shift_fails_closed() -> None:
    settings = CameraCheckConfig()
    a = _texture()
    flat = np.full_like(a, 120)
    moved = _shift(a, 0, 40)  # 40 / 600 = 6.7 % of frame height
    signal = motion_signal((a, None), {"top": (a, None), "downswing": (flat, None), "impact": (moved, None)}, 600,
                           settings)
    assert signal["comparisons"]["address->top"]["status"] == VALID
    assert signal["comparisons"]["address->downswing"]["status"] == UNCERTAIN
    assert signal["status"] == INVALID


def test_drift_is_judged_against_the_position_noise_floor() -> None:
    """Handheld drift of a few px is small for the frame but large for the tush line."""

    settings = CameraCheckConfig()
    a = _texture()
    drift = _shift(a, 0, 5)  # 0.8 % of a 600 px frame: VALID by frame height alone
    by_frame = motion_signal((a, None), {"impact": (drift, None)}, 600, settings)
    assert by_frame["status"] == VALID
    # For a 344 px golfer, 5 px is 1.5 % of body height - three times the
    # 0.5 % noise floor of every Address-relative position: INVALID.
    by_body = motion_signal((a, None), {"impact": (drift, None)}, 600, settings, body_height_px=344.0)
    assert by_body["status"] == INVALID and by_body["unit"] == "share of body height"
    still = motion_signal((a, None), {"impact": (a.copy(), None)}, 600, settings, body_height_px=344.0)
    assert still["status"] == VALID


def test_moving_camera_hides_position_checks_but_keeps_angles() -> None:
    from swingguru.analysis_bundle import hide_position_checks

    def check(kind, guided=True):
        return {"kind": kind, "estimated": False, "guided": guided}

    analysis = {
        "phases": [{"phase": "impact", "checks": [check("posture"), check("pelvis"), check("shaft"), check("head")]}],
        "hidden": [],
        "summary": {"reliable_checks": 4, "estimated_checks": 0, "hidden_checks": 0, "guided_checks": 4},
    }
    hide_position_checks(analysis)
    kept = [c["kind"] for c in analysis["phases"][0]["checks"]]
    assert "pelvis" not in kept and "head" not in kept and {"posture", "shaft"} <= set(kept)
    assert {h["kind"] for h in analysis["hidden"]} == {"pelvis", "head"}
    assert all("camera moved" in h["reason"] for h in analysis["hidden"])
    assert analysis["summary"]["reliable_checks"] == 2 and analysis["summary"]["hidden_checks"] == 2


def test_all_reliable_and_still_is_valid() -> None:
    a = _texture()
    signal = motion_signal((a, None), {"top": (a, None), "impact": (a.copy(), None)}, 600, CameraCheckConfig())
    assert signal["status"] == VALID and signal["value"] == pytest.approx(0.0, abs=1e-3)
