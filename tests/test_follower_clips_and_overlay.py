"""Follower-clip robustness (odd frame widths, set-up evidence) and the replay overlay guides.

Production evidence (2026-10-03..05): 7 of 11 rejected follower clips were *crashes* - MediaPipe's
segmentation aborted the engine process on frames whose width is not a multiple of 4 (478, 606,
866 px: typical of clips saved from social apps). Pose tracking was 100 % on all of them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru import diagnostics as diag
from swingguru.overlay_points import GUIDE_LANDMARKS, MIN_SCORE, overlay_points

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "src" / "swingguru" / "viewer"


# ---------------------------------------------------------------- odd frame widths

SEGMENT_ODD_WIDTHS = r"""
import sys, numpy as np
sys.path.insert(0, sys.argv[1])
from swingguru.pelvis_depth import PersonSegmenter
seg = PersonSegmenter()
for width in (606, 866, 478, 608):
    frame = np.full((1080 if width != 478 else 850, width, 3), 90, np.uint8)
    frame[300:900, width // 3: width // 3 + 120] = 200   # something person-sized to look at
    mask = seg.mask(frame)
    assert mask is None or mask.shape == frame.shape[:2], (width, None if mask is None else mask.shape)
print("ok")
"""


def test_segmentation_survives_widths_that_are_not_a_multiple_of_four() -> None:
    """Runs in a subprocess: a regression is a native abort, which would kill pytest itself."""

    pytest.importorskip("mediapipe")
    from swingguru.pose import default_model_path

    if not Path(default_model_path()).is_file():
        pytest.skip("MediaPipe pose model not present")
    done = subprocess.run([sys.executable, "-c", SEGMENT_ODD_WIDTHS, str(ROOT / "src")],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and "ok" in done.stdout, done.stderr[-800:]


# ---------------------------------------------------------------- overlay body points

def _record(index: int, points: dict[str, tuple[float, float, float]]) -> SimpleNamespace:
    pixels = {name: np.array([x, y]) for name, (x, y, _) in points.items()}
    landmarks = {name: SimpleNamespace(visibility=s, presence=s) for name, (_, _, s) in points.items()}
    return SimpleNamespace(index=index, points=SimpleNamespace(pixels=pixels, landmarks=landmarks))


def test_overlay_points_keep_only_reliable_joints_normalised_to_the_frame() -> None:
    records = [_record(10, {"left_hip": (100, 400, 0.9), "left_knee": (110, 600, 0.3),  # unreliable
                            "left_ankle": (900, 700, 0.95),   # outside a 640 px wide frame
                            "left_eye": (120, 100, 0.99)})]     # not a guide joint
    out = overlay_points(records, [{"phase": "address", "frame_index": 10}, {"phase": "top", "frame_index": None}],
                         640, 800)
    assert out["version"] == 1 and out["min_score"] == MIN_SCORE
    assert out["phases"] == {"address": {"frame": 10, "points": {"left_hip": [0.15625, 0.5]}}}


def test_overlay_points_skip_missing_frames_and_never_invent_moments() -> None:
    out = overlay_points([_record(3, {"left_hip": (1, 1, 1.0)})], [{"phase": "impact", "frame_index": 99}], 10, 10)
    assert out["phases"] == {}
    assert overlay_points([], [], 0, 0)["phases"] == {}
    assert set(GUIDE_LANDMARKS) >= {"left_shoulder", "right_hip", "left_knee", "right_ankle", "nose"}


# ---------------------------------------------------------------- set-up evidence (diagnostics only)

def test_setup_evidence_reports_the_stillest_window_before_top(monkeypatch, tmp_path) -> None:
    from swingguru import auto_moments

    times = tuple(i / 30 for i in range(40))
    series = SimpleNamespace(frames=tuple(range(40)), times_s=times)
    # Moving fast for the first 0.5 s, then a 0.4 s near-still spell (0.2 torso/s), then the swing.
    speeds = [None] + [1.5] * 14 + [0.2] * 12 + [2.0] * 13
    monkeypatch.setattr(auto_moments, "build_motion_series", lambda records, config=None: series)
    monkeypatch.setattr(auto_moments, "_smoothed", lambda s, c: [None] * len(s.frames))
    monkeypatch.setattr(auto_moments, "_speeds", lambda s, sm: speeds)
    monkeypatch.setattr("swingguru.pose_cache.load_records_any", lambda path: [])
    records = tmp_path / "pose_records.pkl"
    records.write_bytes(b"x")
    analysis = {"phases": [{"phase": "address", "frame_index": None}, {"phase": "top", "frame_index": 35}]}
    ev = diag.setup_evidence(records, analysis)
    assert ev["still_window_found"] is True and ev["quietest_window_speed"] == 0.2
    assert ev["threshold"] == 0.35 and ev["window_s"] == 0.3 and ev["pre_top_s"] > 1.0

    speeds[15:27] = [0.5] * 12   # a near-miss: never below the threshold
    ev = diag.setup_evidence(records, analysis)
    assert ev["still_window_found"] is False and ev["quietest_window_speed"] == 0.5


def test_setup_evidence_is_unavailable_without_a_swing_moment() -> None:
    ev = diag.setup_evidence(None, {"phases": []})
    assert ev["unavailable"] and ev["still_window_found"] is None


# ---------------------------------------------------------------- viewer overlay contract

def test_viewer_draws_guides_only_from_tracked_points_and_respects_reduced_motion() -> None:
    script = (VIEWER / "app.js").read_text(encoding="utf-8")
    css = (VIEWER / "app.css").read_text(encoding="utf-8")
    assert "data.overlay" in script and "function drawGuides" in script
    # A chain breaks where a joint was not tracked: guides never bridge a gap.
    assert "never bridge a gap" in script
    # Draw-on via the Web Animations API, with one choreography for guides -> line -> joints -> label.
    assert "shape.animate(" in script and "var T = {" in script
    # Animation only when not reduced: the classes and animations are applied under `animated`.
    assert "var animated = animate && !reducedMotion;" in script
    assert "prefers-reduced-motion" in css and ".overlay .pop" in css


def test_overlay_block_is_additive_and_not_part_of_the_score_inputs() -> None:
    from swingguru.scoring import score_swing

    base = {"phases": [], "hidden": []}
    with_overlay = {**base, "overlay": {"version": 1, "phases": {"address": {"points": {"left_hip": [0.1, 0.2]}}}}}
    assert score_swing(base) == score_swing(with_overlay)
    assert json.dumps(with_overlay)  # serialisable


def test_stabilised_guides_smooth_a_still_joint_and_never_lag_a_moving_one() -> None:
    """Overlay v2 (next profile): jitter on a still joint is removed; a fast joint keeps its frame."""

    records = [_record(9, {"left_hip": (100, 400, 0.9), "left_wrist": (300, 300, 0.9)}),
               _record(10, {"left_hip": (104, 402, 0.9), "left_wrist": (330, 310, 0.9)}),   # hip jitters 4 px
               _record(11, {"left_hip": (101, 400, 0.9), "left_wrist": (360, 320, 0.9)})]   # wrist moves 60 px
    phases = [{"phase": "address", "frame_index": 10}]
    raw = overlay_points(records, phases, 1000, 1000)
    stable = overlay_points(records, phases, 1000, 1000, stabilise=True)
    assert raw["version"] == 1 and stable["version"] == 2
    assert raw["phases"]["address"]["points"]["left_hip"] == [0.104, 0.402]
    assert stable["phases"]["address"]["points"]["left_hip"] == [0.101, 0.4]        # median of 100/104/101
    assert stable["phases"]["address"]["points"]["left_wrist"] == [0.33, 0.31]        # moving: exact frame
    # Default (production) output is unchanged.
    assert overlay_points(records, phases, 1000, 1000) == raw


def test_overlay_v2_is_opt_in_and_draws_from_the_same_geometry() -> None:
    script = (VIEWER / "app.js").read_text(encoding="utf-8")
    assert "var OVERLAY_V2 = /[?&]overlay=2(&|$)/.test(location.search);" in script
    assert "if (OVERLAY_V2) return drawOverlayV2(check, animate);" in script
    v2 = script[script.index("function drawOverlayV2("):script.index("// Body measurements prefer their label")]
    # Sizes follow the golfer's torso, every line has an under-line, values come from the check itself.
    assert "torsoPx(check, px)" in v2 and "stroked(" in v2
    arc = script[script.index("function angleArc("):script.index("function drawGuidesV2(")]
    assert "text.textContent = check.value;" in arc        # the engine's value, never re-measured
