"""Direction-aware head feedback (engine profile candidate, fix head_direction): wording only."""

from __future__ import annotations

import copy

from swingguru.engine_profile import profile
from swingguru.head_direction import DEPTH_NOTE, components, describe_head_direction
from swingguru.verdicts import attach_verdicts


def head(phase, pct, dx, dy, **extra):
    return {"id": f"{phase}:head", "kind": "head", "phase": phase, "title": "Head movement", "value": f"{pct:.1f}%",
            "detail": "old", "summary": "", "raw": {"movement_pct_height": pct, "dx_px": dx, "dy_px": dy}, **extra}


def analysis(*checks):
    a = {"phases": [{"phase": c["phase"], "checks": [c]} for c in checks]}
    return attach_verdicts(a)


def test_candidate_only() -> None:
    assert "head_direction" in profile("candidate").fixes and "head_direction" not in profile("next").fixes


def test_components_split_the_same_movement() -> None:
    c = components(head("impact", 5.0, 30.0, 40.0), toward_ball=-1.0)   # 3-4-5 triangle
    assert c == {"down_pct": 4.0, "toward_ball_pct": -3.0, "total_pct": 5.0}
    assert components(head("impact", 5.0, 30.0, 40.0), toward_ball=None)["toward_ball_pct"] is None


def test_score_inputs_and_verdict_label_are_unchanged() -> None:
    a = analysis(head("top", 1.0, 0.0, 10.0), head("impact", 7.4, -10.0, 73.0))
    before = copy.deepcopy(a)
    describe_head_direction(a, toward_ball=1.0)
    for p0, p1 in zip(before["phases"], a["phases"]):
        c0, c1 = p0["checks"][0], p1["checks"][0]
        assert c0["raw"] == c1["raw"] and c0["value"] == c1["value"]
        for key in ("label", "status", "value", "mapping"):
            assert c0["verdict"][key] == c1["verdict"][key]


def test_tour_style_drop_is_named_with_its_timing() -> None:
    a = analysis(head("top", 0.8, 0.0, 8.0), head("impact", 7.0, -10.0, 70.0))
    describe_head_direction(a, toward_ball=1.0)
    impact = a["phases"][1]["checks"][0]
    d = impact["head_direction"]
    assert d["down_pct"] > 6.9 and d["timing"]["segment"] == "downswing" and d["toward_target_pct"] is None
    assert d["toward_target_note"] == DEPTH_NOTE
    assert impact["verdict"]["support"].startswith("By Impact your head dropped 6.9% of your height, compared")
    assert "most of the drop came between the Top and Impact" in impact["verdict"]["support"]


def test_small_parts_are_not_named_and_unclear_timing_is_left_out() -> None:
    a = analysis(head("top", 2.0, -20.0, 0.0), head("impact", 4.0, -40.0, 5.0))
    describe_head_direction(a, toward_ball=1.0)
    text = a["phases"][1]["checks"][0]["verdict"]["support"]
    assert "moved 4.0% of your height away from the ball" in text and "dropped" not in text and "most of" not in text


def test_no_ball_side_means_only_vertical_is_reported() -> None:
    a = analysis(head("impact", 3.0, -30.0, 0.0))
    describe_head_direction(a, toward_ball=None)
    assert "stayed within 1% of your height" in a["phases"][0]["checks"][0]["verdict"]["support"]


def test_timing_needs_the_same_camera_state() -> None:
    a = analysis(head("top", 0.8, 0.0, 8.0), head("impact", 7.0, 0.0, 70.0, camera="CORRECTED_VALID"))
    describe_head_direction(a, toward_ball=1.0)
    assert a["phases"][1]["checks"][0]["head_direction"]["timing"] is None
