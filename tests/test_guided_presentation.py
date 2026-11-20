"""Measurement-specific overlays, card data and the player's playback contract.

These pin the behaviour the guided viewer depends on: every overlay draws what
its number is measured *against*, the callout anchor sits on the measured
geometry, a failed measurement carries nothing to draw, the walkthrough is
trimmed to the swing, and the browser player never reruns Streamlit while
playing.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pytest

from swingguru.auto_moments import estimate_moments
from swingguru.measurements import MeasurementConfig
from swingguru.phases import Phase
from swingguru.pipeline import build_record
from swingguru.synthetic import joints_to_observation
from swingguru.ui import media, presentation
from swingguru.ui.guided_fixture import GuidedSwingClip, guided_joints
from swingguru.ui.timeline import LEAD_IN_S, LEAD_OUT_S, build_timeline, timeline_payload

CLIP = GuidedSwingClip()
ASSETS = Path(presentation.__file__).resolve().parent / "player_assets"


@pytest.fixture(scope="module")
def records():
    config = MeasurementConfig()
    return [
        build_record(
            index,
            index * 1000.0 / CLIP.fps,
            np.zeros((CLIP.height, CLIP.width, 3), dtype=np.uint8),
            joints_to_observation(guided_joints(index, clip=CLIP)),
            config=config,
        )
        for index in range(CLIP.frame_count)
    ]


@pytest.fixture(scope="module")
def timeline(records):
    moments = estimate_moments(records)
    return build_timeline(
        records,
        moments,
        fps=CLIP.fps,
        measurement_config=MeasurementConfig(),
        reference_frame=moments.get(Phase.ADDRESS).frame_index,
    )


def _check(timeline, check_id):
    return next(check for check in timeline.checks if check.id == check_id)


def test_torso_inclination_draws_the_vertical_it_is_measured_against(timeline) -> None:
    check = _check(timeline, "address:torso_inclination_deg")
    references = [item for item in check.geometry if item.role == presentation.ROLE_REFERENCE]
    assert len(references) == 1
    (x0, y0), (x1, y1) = references[0].points
    assert x0 == pytest.approx(x1, abs=1e-6), "reference must be vertical"
    assert y1 < y0, "reference must point up from the hips"
    primary = [item for item in check.geometry if item.kind == "line" and item.role == "primary"]
    assert primary and primary[0].points[0] == references[0].points[0], "both start at the hips"


def test_knee_flex_draws_the_leg_chain_and_a_straight_leg_reference(timeline) -> None:
    check = _check(timeline, "address:lead_knee_flex_deg")
    chain = [item for item in check.geometry if item.kind == "polyline"]
    assert len(chain) == 1 and len(chain[0].points) == 3
    references = [item for item in check.geometry if item.role == presentation.ROLE_REFERENCE]
    assert len(references) == 1
    knee = np.array(chain[0].points[1])
    hip = np.array(chain[0].points[0])
    straight_end = np.array(references[0].points[1])
    assert references[0].points[0] == chain[0].points[1], "reference starts at the knee"
    # The straight-leg line continues the thigh direction (in pixels).
    size = np.array([CLIP.width, CLIP.height], dtype=float)
    thigh = (knee - hip) * size
    extension = (straight_end - knee) * size
    cosine = float(np.dot(thigh, extension) / (np.linalg.norm(thigh) * np.linalg.norm(extension)))
    assert cosine == pytest.approx(1.0, abs=1e-6)


def test_movement_draws_current_and_address_rings(timeline) -> None:
    check = _check(timeline, "impact:head_displacement_pct_height")
    rings = [item for item in check.geometry if item.kind == "ring"]
    assert {item.role for item in rings} == {"primary", "reference"}
    assert all(item.radius and 0 < item.radius < 0.5 for item in rings)
    assert all(not item.filled for item in rings)


def test_no_skeleton_is_ever_sent(timeline) -> None:
    for check in timeline.checks:
        points = sum(len(item.points) for item in check.geometry)
        assert points <= 9, (check.id, points)


def test_valid_checks_carry_anchor_callout_and_tracking(timeline) -> None:
    for check in timeline.checks:
        payload = check.to_dict()
        if not check.valid:
            continue
        assert check.anchor is not None, check.id
        assert all(0.0 <= value <= 1.0 for value in check.anchor)
        assert payload["callout"].startswith(check.label + " · ")
        assert check.value_text in payload["callout"]
        assert payload["measured_against"]
        assert payload["tracking"] in {"clear", "uncertain", "unknown"}
        if check.confidence is not None:
            assert 0.0 <= check.confidence <= 1.0


def test_anchor_sits_on_the_measured_geometry(timeline) -> None:
    knee = _check(timeline, "address:lead_knee_flex_deg")
    chain = next(item for item in knee.geometry if item.kind == "polyline")
    assert knee.anchor == pytest.approx(chain.points[1])
    torso = _check(timeline, "address:torso_inclination_deg")
    line = next(item for item in torso.geometry if item.kind == "line" and item.role == "primary")
    midpoint = tuple((a + b) / 2 for a, b in zip(*line.points))
    assert torso.anchor == pytest.approx(midpoint)


def test_an_unmeasurable_check_has_nothing_to_draw_or_point_at(records) -> None:
    moments = estimate_moments(records)
    timeline = build_timeline(
        records, moments, fps=CLIP.fps, measurement_config=MeasurementConfig(), reference_frame=None
    )
    check = _check(timeline, "takeaway:head_displacement_pct_height")
    payload = check.to_dict()
    assert check.valid is False
    assert payload["geometry"] == []
    assert payload["anchor"] is None
    assert payload["tracking"] == "unmeasured"
    assert payload["callout"].endswith("can't measure")


def test_walkthrough_is_trimmed_to_the_swing(timeline) -> None:
    payload = timeline_payload(timeline, run_key="k", video_src="v.mp4", hold_seconds=1.8)
    times = [event.time_s for event in timeline.events]
    assert payload["segment_start_s"] == pytest.approx(max(0.0, min(times) - LEAD_IN_S), abs=1e-3)
    assert payload["segment_end_s"] == pytest.approx(
        min(payload["duration_s"], max(times) + LEAD_OUT_S), abs=1e-3
    )
    assert payload["segment_start_s"] < min(times) <= max(times) < payload["segment_end_s"] + 1e-9


def test_preview_uses_short_keyframe_interval_and_versioned_cache(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(args, *, timeout_s):
        captured["args"] = args
        return True, ""

    monkeypatch.setattr(media, "_run_ffmpeg", fake_run)
    media._encode(
        Path("in.mp4"), tmp_path / "out.mp4", first_index=0, frame_count=10,
        fps=30.0, max_height=1080, crf=20, exact=False, timeout_s=5,
    )
    args = captured["args"]
    assert args[args.index("-g") + 1] == str(round(30.0 * media.KEYFRAME_INTERVAL_S))
    assert args[args.index("-crf") + 1] == "20"
    assert media.DEFAULT_MAX_HEIGHT >= 1080
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    key = media.preview_cache_key(source, first_index=0, frame_count=10, fps=30.0)
    monkeypatch.setattr(media, "ENCODER_VERSION", "different")
    assert media.preview_cache_key(source, first_index=0, frame_count=10, fps=30.0) != key


def _function_body(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(name)


def test_player_posts_to_streamlit_only_on_error() -> None:
    script = (ASSETS / "player.js").read_text(encoding="utf-8")
    calls = [match.start() for match in re.finditer(r"postMessage\(SET_COMPONENT_VALUE", script)]
    assert len(calls) == 1, "one value-posting call site"
    body = _function_body(script, "reportError")
    assert "SET_COMPONENT_VALUE" in body
    for name in ("advanceCheck", "enterHold", "finishEvent", "tick", "render"):
        assert "reportError" not in _function_body(script, name), name


def test_player_stops_on_the_frame_clock_and_never_reloads_in_place() -> None:
    script = (ASSETS / "player.js").read_text(encoding="utf-8")
    assert "requestVideoFrameCallback" in script
    assert "mediaTime" in script
    same_run = _function_body(script, "applyData")
    # Data for the same run and video only refreshes text.
    assert ".load()" not in same_run and "loadVideoSource" not in same_run
    advance = _function_body(script, "advanceCheck")
    assert "setCurrentTime" not in advance, "same-moment checks must not seek"
