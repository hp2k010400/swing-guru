"""Batch robustness tooling: conservative status rules and pose usability."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import batch_validate as bv  # noqa: E402

ALL_RIGHT = {k: True for k, _ in bv.PHASES}


def _row(**overrides):
    row = {
        "build_ok": True, "camera": {"status": "VALID"}, "phases_found": 6, "pose_usable": True, "pose_gaps": [],
        "shaft_shown": 6, "pelvis_valid": True, "pelvis_reason": "", "interpretation_ok": True,
        "interpretation_note": "", "window": {"status": "SINGLE_SWING", "confidence": "HIGH"},
        "review": {"phases_correct": dict(ALL_RIGHT), "shaft_errors": [], "pelvis_plausible": True},
    }
    row.update(overrides)
    return row


def test_everything_good_and_reviewed_is_pass() -> None:
    assert bv.classify(_row()) == ("PASS", [])


def test_unreviewed_can_never_pass() -> None:
    status, reasons = bv.classify(_row(review=None))
    assert status == "PARTIAL" and "not visually reviewed" in reasons


def test_invalid_camera_or_crash_or_wrong_anchor_frame_is_fail() -> None:
    assert bv.classify(_row(camera={"status": "INVALID"}))[0] == "FAIL"
    assert bv.classify(_row(build_ok=False, error="exit 1"))[0] == "FAIL"
    assert bv.classify(_row(phases_found=3))[0] == "FAIL"
    wrong_top = {**ALL_RIGHT, "top": False}
    assert bv.classify(_row(review={"phases_correct": wrong_top}))[0] == "FAIL"


def test_camera_motion_alone_is_partial_other_invalid_camera_is_fail() -> None:
    moved = {"status": "INVALID", "signals": {"camera_motion": {"status": "INVALID"},
                                              "full_body": {"status": "VALID"}}}
    status, reasons = bv.classify(_row(camera=moved, pelvis_valid=False, pelvis_reason="camera moved"))
    assert status == "PARTIAL" and any("camera moved" in r for r in reasons)
    face_on = {"status": "INVALID", "signals": {"camera_motion": {"status": "VALID"},
                                                "orientation_width": {"status": "INVALID"}}}
    assert bv.classify(_row(camera=face_on))[0] == "FAIL"


def test_partial_for_each_weaker_element() -> None:
    for overrides in (
        {"camera": {"status": "UNCERTAIN"}},
        {"shaft_shown": 4},
        {"pose_usable": False, "pose_gaps": ["Top wrists"]},
        {"pelvis_valid": False, "pelvis_reason": "no tush line"},
        {"window": {"status": "MULTIPLE_SWINGS", "confidence": "LOW"}},
        {"review": {"phases_correct": {**ALL_RIGHT, "takeaway": False}}},
        {"review": {"phases_correct": dict(ALL_RIGHT), "shaft_errors": ["top"]}},
    ):
        assert bv.classify(_row(**overrides))[0] == "PARTIAL", overrides


def _record(score=0.9, point=(10.0, 10.0)):
    names = [n for group in bv.JOINT_GROUPS.values() for n in group]
    landmark = SimpleNamespace(visibility=score, presence=score)
    return SimpleNamespace(points=SimpleNamespace(landmarks={n: landmark for n in names},
                                                  pixels={n: np.array(point) for n in names}))


def test_pose_usability_uses_the_engine_gate_and_the_frame() -> None:
    assert all(bv.pose_table(_record(0.9), 100, 100).values())
    assert not any(bv.pose_table(_record(0.3), 100, 100).values())  # below the MEDIUM gate
    assert not any(bv.pose_table(_record(0.9, (150.0, 10.0)), 100, 100).values())  # outside the frame
    assert all(v is None for v in bv.pose_table(None, 100, 100).values())
