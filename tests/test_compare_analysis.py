"""The acceptance guard must fail closed on anything the viewer can see.

``scripts/compare_analysis.py`` is what decides whether a performance change
kept the analysis materially identical, so it is tested against the ways a
bundle can drift: missing, extra or reordered phases, changed overlay
metadata, missing anchors, changed confidence and changed guided flags - plus
the two numeric tolerances it is allowed to grant.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import compare_analysis  # noqa: E402  (scripts/compare_analysis.py)

WIDTH, HEIGHT = 400, 600


def _check(*, kind: str = "shaft", level: str = "HIGH") -> dict:
    return {
        "id": f"address:{kind}",
        "kind": kind,
        "phase": "address",
        "frame_index": 10,
        "title": "Shaft angle",
        "value": "45 deg",
        "qualifier": "",
        "summary": "Measured against the horizontal.",
        "callout": "45 deg",
        "detail": "Measured against the horizontal, as the camera sees it.",
        "explain": "The angle of the club shaft against the horizontal.",
        "level": level,
        "estimated": level == "MEDIUM",
        "guided": True,
        "source": "classical",
        "confidence": 0.812,
        "time_s": 0.3333,
        "raw": {
            "shaft_direction_deg": 45.0,
            "shaft_from_horizontal_deg": 45.0,
            "change_vs_address_deg": 0.0,
        },
        "geometry": [
            {"kind": "line", "role": "reference", "points": [[0.1, 0.2], [0.3, 0.4]]},
            {"kind": "line", "role": "shaft", "points": [[0.1, 0.2], [0.5, 0.6]]},
            {"kind": "dot", "role": "primary", "points": [[0.1, 0.2]]},
        ],
        "anchor": [0.3, 0.4],
    }


def _phase(name: str, frame: int) -> dict:
    check = _check()
    check["id"] = f"{name}:shaft"
    check["phase"] = name
    check["frame_index"] = frame
    return {
        "phase": name,
        "label": name.title(),
        "frame_index": frame,
        "time_s": round(frame / 30.0, 4),
        "source": "estimated",
        "checks": [check],
    }


def _bundle() -> dict:
    return {
        "version": 1,
        "source": "clip.mp4",
        "camera_view": "down-the-line",
        "video": "swing.mp4",
        "fps": 30.0,
        "width": WIDTH,
        "height": HEIGHT,
        "duration_s": 2.0,
        "segment_start_s": 0.5,
        "segment_end_s": 1.5,
        "phases": [_phase("address", 10), _phase("impact", 40)],
        "summary": {"reliable_checks": 2, "guided_checks": 2},
        "focus": None,
        "hidden": [],
        "engine": {"timings": {"total_s": 1.0}},
    }


def _mutated(mutate) -> tuple[dict, dict]:
    before = _bundle()
    after = copy.deepcopy(before)
    mutate(after)
    return before, after


def test_an_identical_bundle_is_equivalent_and_exact() -> None:
    before = _bundle()
    result = compare_analysis.compare(before, copy.deepcopy(before))
    assert result["equivalent"] is True
    assert result["exact_payload_except_engine"] is True
    assert result["differences"] == []
    assert result["max_angle_delta_deg"] == 0.0
    assert result["max_geometry_delta_px"] == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["angle", "point"])
def test_nonfinite_analysis_geometry_fails_comparison(value, field) -> None:
    before = _bundle()
    after = copy.deepcopy(before)
    check = after["phases"][0]["checks"][0]
    if field == "angle":
        check["raw"]["shaft_direction_deg"] = value
    else:
        check["geometry"][0]["points"][0][0] = value
    assert not compare_analysis.compare(before, after)["equivalent"]


def test_an_engine_only_difference_is_still_exact() -> None:
    before, after = _mutated(lambda payload: payload["engine"].update({"timings": {"total_s": 99.0}}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is True
    assert result["exact_payload_except_engine"] is True


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_a_missing_phase_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"].pop())
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("phase count" in item for item in result["differences"])


def test_an_extra_phase_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"].append(_phase("top", 30)))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("present only in the second bundle" in item for item in result["differences"])


def test_reordered_phases_fail_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"].reverse())
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("identity" in item for item in result["differences"])


def test_a_missing_phase_frame_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0].update({"frame_index": 11}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("frame_index" in item for item in result["differences"])


def test_a_missing_check_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"].clear())
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("checks count" in item for item in result["differences"])


def test_a_missing_hidden_list_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload.pop("hidden"))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False


def test_an_unknown_top_level_key_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload.update({"telemetry": 1}))
    assert compare_analysis.compare(before, after)["equivalent"] is False


def test_an_unknown_check_key_fails_closed() -> None:
    def mutate(payload):
        payload["phases"][0]["checks"][0]["verdict"] = "good"

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("unexpected key" in item for item in result["differences"])


# ---------------------------------------------------------------------------
# Consumer metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["guided", "estimated", "level", "source", "value", "callout", "detail", "explain"])
def test_changed_check_metadata_fails_closed(key: str) -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0].update({key: "changed"}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any(key in item for item in result["differences"])


def test_changed_confidence_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0].update({"confidence": 0.9}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("confidence" in item for item in result["differences"])


def test_a_dropped_confidence_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0].update({"confidence": None}))
    assert compare_analysis.compare(before, after)["equivalent"] is False


def test_a_changed_focus_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload.update({"focus": {"check_id": "impact:posture"}}))
    assert compare_analysis.compare(before, after)["equivalent"] is False


def test_a_changed_summary_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["summary"].update({"guided_checks": 1}))
    assert compare_analysis.compare(before, after)["equivalent"] is False


# ---------------------------------------------------------------------------
# Overlay geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key,value", [("kind", "arc"), ("role", "accent"), ("radius", 0.5), ("start_deg", 10.0)])
def test_changed_geometry_metadata_fails_closed(key: str, value: object) -> None:
    def mutate(payload):
        payload["phases"][0]["checks"][0]["geometry"][0][key] = value

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any(key in item for item in result["differences"])


def test_a_reordered_geometry_list_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0]["geometry"].reverse())
    assert compare_analysis.compare(before, after)["equivalent"] is False


def test_a_missing_anchor_on_one_side_fails_closed() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0].update({"anchor": None}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("anchor" in item for item in result["differences"])


def test_a_deep_copy_of_geometry_points_is_equivalent() -> None:
    before, after = _mutated(lambda payload: None)
    assert compare_analysis.compare(before, after)["equivalent"] is True


# ---------------------------------------------------------------------------
# The two allowed tolerances
# ---------------------------------------------------------------------------


def test_a_half_degree_shaft_change_is_within_tolerance() -> None:
    def mutate(payload):
        raw = payload["phases"][0]["checks"][0]["raw"]
        raw["shaft_direction_deg"] = 45.5
        raw["shaft_from_horizontal_deg"] = 45.5
        raw["change_vs_address_deg"] = 0.5

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is True
    assert result["max_angle_delta_deg"] == pytest.approx(0.5, abs=1e-6)
    assert result["exact_payload_except_engine"] is False


def test_a_two_degree_shaft_change_breaks_tolerance() -> None:
    before, after = _mutated(lambda payload: payload["phases"][0]["checks"][0]["raw"].update({"shaft_direction_deg": 47.0}))
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("shaft_direction_deg" in item for item in result["differences"])


def test_a_two_pixel_geometry_move_is_within_tolerance() -> None:
    def mutate(payload):
        payload["phases"][0]["checks"][0]["geometry"][1]["points"] = [
            [0.1 + 1.9 / WIDTH, 0.2],
            [0.5, 0.6],
        ]

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is True
    assert result["max_geometry_delta_px"] == pytest.approx(1.9, abs=1e-6)


def test_a_four_pixel_geometry_move_breaks_tolerance() -> None:
    def mutate(payload):
        payload["phases"][0]["checks"][0]["geometry"][1]["points"] = [
            [0.1 + 4.0 / WIDTH, 0.2],
            [0.5, 0.6],
        ]

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert result["max_geometry_delta_px"] > compare_analysis.GEOMETRY_TOLERANCE_PX


def test_a_four_pixel_anchor_move_breaks_tolerance() -> None:
    def mutate(payload):
        payload["phases"][0]["checks"][0]["anchor"] = [0.3 + 4.0 / WIDTH, 0.4]

    before, after = _mutated(mutate)
    result = compare_analysis.compare(before, after)
    assert result["equivalent"] is False
    assert any("anchor" in item for item in result["differences"])


def test_an_unparallel_bundle_is_reported_not_compared() -> None:
    result = compare_analysis.compare(_bundle(), {"engine": {}})
    assert result["equivalent"] is False
    assert result["exact_payload_except_engine"] is False


def test_non_object_payloads_fail_closed() -> None:
    assert compare_analysis.compare([], {})["equivalent"] is False
    assert compare_analysis.compare({}, [])["equivalent"] is False
