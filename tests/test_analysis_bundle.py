"""The consumer analysis bundle: gating, wording, geometry and the viewer contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from swingguru import analysis_bundle as bundle
from swingguru.auto_moments import estimate_moments
from swingguru.club.geometry import unit_from_angle
from swingguru.club.tracker import ClubConfig, ClubTrack, FrameShaft
from swingguru.measurements import MeasurementConfig
from swingguru.phases import Phase
from swingguru.pipeline import apply_reference, build_record
from swingguru.synthetic import joints_to_observation
from swingguru.ui.guided_fixture import GuidedSwingClip, guided_joints

VIEWER = Path(bundle.__file__).resolve().parent / "viewer"
CLIP = GuidedSwingClip(width=360, height=640)


@pytest.fixture(scope="module")
def swing():
    config = MeasurementConfig()
    records = [
        build_record(
            i, i * 1000.0 / CLIP.fps, np.zeros((CLIP.height, CLIP.width, 3), np.uint8),
            joints_to_observation(guided_joints(i, clip=CLIP)), config=config,
        )
        for i in range(CLIP.frame_count)
    ]
    moments = estimate_moments(records)
    apply_reference(records, moments.frames()[Phase.ADDRESS], config=config)
    return records, moments


def _track(records, levels: dict[int, str]) -> ClubTrack:
    frames = []
    for record in records:
        level = levels.get(record.index)
        if level is None:
            frames.append(FrameShaft(record.index, record.timestamp_ms, "unavailable", "UNAVAILABLE", 0.0))
            continue
        grip = np.array([150.0, 400.0])
        frames.append(FrameShaft(record.index, record.timestamp_ms, "detected", level, 0.8,
                                 angle_deg=-56.0, grip=grip, tip=grip + unit_from_angle(-56.0) * 200))
    return ClubTrack(tuple(frames), (), "test", ClubConfig())


def test_only_six_phases_and_finish_is_never_analysed(swing) -> None:
    records, moments = swing
    analysis = bundle.build_analysis(records, moments, None, fps=CLIP.fps, video="v.mp4")
    assert [p["phase"] for p in analysis["phases"]] == [
        "address", "takeaway", "mid_back", "top", "downswing", "impact",
    ]
    json.dumps(analysis)  # serialisable


def test_every_shown_check_is_high_or_medium_and_explains_itself(swing) -> None:
    records, moments = swing
    analysis = bundle.build_analysis(records, moments, None, fps=CLIP.fps, video="v.mp4")
    shown = [c for p in analysis["phases"] for c in p["checks"]]
    assert shown
    for check in shown:
        assert check["level"] in ("HIGH", "MEDIUM")
        assert check["estimated"] == (check["level"] == "MEDIUM")
        assert check["explain"] and check["title"] and check["value"] and check["callout"]
        assert check["geometry"] and check["anchor"]
        assert any(g["role"] in ("reference",) for g in check["geometry"]), check["id"]
        # The measurement itself never carries a verdict word. Golfer-facing
        # labels live only in the separate, provisional ``verdict`` block
        # (swingguru/verdicts.py), which has its own capability guard.
        measurement = {k: v for k, v in check.items() if k != "verdict"}
        assert not re.search(r"\b(good|bad|steep|shallow|on plane|early extension)\b", json.dumps(measurement).lower())
        if "verdict" in check:
            from swingguru.verdicts import unsupported_claims

            assert unsupported_claims(check["verdict"]["label"] + " " + check["verdict"]["support"]) == []
    # Without a club track every shaft check is hidden with a reason, never shown.
    assert all(c["kind"] != "shaft" for c in shown)
    assert any(h["kind"] == "shaft" and h["reason"] for h in analysis["hidden"])
    assert analysis["summary"]["reliable_checks"] == len(shown)


def test_confidence_gates_are_per_pose_model() -> None:
    assert bundle.body_level(0.9, "mediapipe") == "HIGH"
    assert bundle.body_level(0.65, "mediapipe") == "MEDIUM"
    assert bundle.body_level(0.5, "mediapipe") is None
    assert bundle.body_level(0.65, "rtmw") == "HIGH"
    assert bundle.body_level(0.5, "rtmw") == "MEDIUM"
    assert bundle.body_level(0.3, "rtmw") is None
    assert bundle.body_level(None) is None


def test_low_scored_joints_hide_the_check(swing) -> None:
    records, moments = swing
    context = bundle.swing_context(records, moments.frames()[Phase.ADDRESS], "mediapipe")
    record = records[moments.frames()[Phase.TOP]]
    for landmark in record.points.landmarks.values():
        object.__setattr__(landmark, "visibility", 0.2)
    check, reason = bundle.build_body_check("head", Phase.TOP, record, context)
    assert check is None and "confidence" in reason


def test_posture_and_movement_wording_is_factual() -> None:
    assert bundle.posture_wording(-10.4)[0] == "10° more upright"
    assert bundle.posture_wording(6.0)[0] == "6° more bent over"
    assert bundle.posture_wording(0.4)[0] == "Same as Address"
    assert bundle.movement_direction(np.array([10.0, 0.0]), 1.0) == "toward the ball"
    assert bundle.movement_direction(np.array([10.0, 0.0]), -1.0) == "away from the ball"
    assert bundle.movement_direction(np.array([6.0, 8.0]), 1.0) == "down and toward the ball"
    assert bundle.movement_direction(np.array([0.0, -5.0]), 1.0) == "up"
    callout, sentence = bundle.movement_wording("head", 2.14, "down")
    assert callout == "2.1% of height" and "down" in sentence
    assert bundle.movement_wording("pelvis", 0.2, "")[0] == "Stayed put"


def test_shaft_line_angle_is_folded_to_0_90() -> None:
    assert bundle.shaft_line_angle(-56.0) == pytest.approx(56.0)
    assert bundle.shaft_line_angle(-163.0) == pytest.approx(17.0)
    assert bundle.shaft_line_angle(126.0) == pytest.approx(54.0)
    assert bundle.shaft_line_angle(90.0) == pytest.approx(90.0)


def test_shaft_checks_follow_club_confidence(swing) -> None:
    records, moments = swing
    frames = moments.frames()
    levels = {frames[Phase.ADDRESS]: "HIGH", frames[Phase.IMPACT]: "MEDIUM", frames[Phase.TOP]: "LOW"}
    analysis = bundle.build_analysis(records, moments, _track(records, levels), fps=CLIP.fps, video="v.mp4")
    shafts = {c["phase"]: c for p in analysis["phases"] for c in p["checks"] if c["kind"] == "shaft"}
    assert set(shafts) == {"address", "impact"}
    assert shafts["impact"]["estimated"] is True
    assert "Address" in shafts["impact"]["detail"]
    hidden = {(h["phase"], h["kind"]) for h in analysis["hidden"]}
    assert ("top", "shaft") in hidden and ("mid_back", "shaft") in hidden


def test_no_largest_number_focus_remains(swing) -> None:
    """Main focus now only comes from validated interpretation rules (none yet)."""

    records, moments = swing
    analysis = bundle.build_analysis(records, moments, None, fps=CLIP.fps, video="v.mp4")
    assert not hasattr(bundle, "main_focus")
    assert analysis["focus"] is None
    assert analysis["interpretations"]["focus_fallback"] == "Coaching priorities are coming next."
    assert "toward_ball_known" in analysis["geometry_context"]


def test_viewer_contract_frame_true_holds_and_no_seek_between_same_moment_checks() -> None:
    script = (VIEWER / "app.js").read_text(encoding="utf-8")
    assert "requestVideoFrameCallback" in script and "mediaTime" in script
    advance = script[script.index("function advance()"): script.index("function fade(")]
    assert "seek(" not in advance.replace("state.stats.seeks", "")
    # Standalone: no host page reruns. The one message is the Swing Guru app
    # integration (?embed=1): it only says "ready"/"close"/"ended", from notifyApp, and
    # notifyApp is only called when embedded.
    # (Two call sites, both inside notifyApp: the browser parent frame, or the
    # mobile app's React Native WebView bridge.)
    notify = script[script.index("function notifyApp("): script.index("var els = {};")]
    assert script.count("postMessage") == notify.count("postMessage") == 2
    calls = [i for i in range(len(script)) if script.startswith("notifyApp(", i)
             and not script[max(0, i - 9): i].endswith("function ")]
    assert len(calls) == 3 and all("if (EMBED)" in script[max(0, i - 400): i] for i in calls)
    html = (VIEWER / "index.html").read_text(encoding="utf-8")
    for element in ("video", "overlay", "card", "screen-results", "groups", "focus"):
        assert f'id="{element}"' in html


def test_viewer_review_options_and_consumer_overlays() -> None:
    import re

    script = (VIEWER / "app.js").read_text(encoding="utf-8")
    hold = int(re.search(r"var HOLD_MS = (\d+);", script).group(1))
    assert 1500 <= hold <= 2000  # a guided pause per check, not a slideshow
    assert "autoplay=1" in script and "review=1" in script
    # The debug skeleton is never part of the viewer; the hip check may add the
    # tush line only when the rear pelvis was measured.
    assert "skeleton" not in script.lower()
    assert 'check.kind === "pelvis" && check.pelvis_depth' in script
    css = (VIEWER / "app.css").read_text(encoding="utf-8")
    assert "html.review" in css
