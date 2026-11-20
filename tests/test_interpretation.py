"""Golf interpretation layer: fail-closed, deterministic, versioned rules."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace

import pytest

from swingguru.interpretation import BANNED_TERMS, RULES, interpret, select_focus, terminology_violations
from swingguru.interpretation.facts import facts_from_analysis
from swingguru.interpretation.rules import (
    DESCRIPTIVE_ONLY,
    NEEDS_CALIBRATION,
    NOT_SUPPORTED,
    STATUS_BLOCKED,
    STATUS_DESCRIBED,
    STATUS_FIRED,
    STATUS_NOT_FIRED,
    STATUS_OBSERVED,
    VALIDATED,
    InterpretationResult,
    rules_by_id,
)

PATTERN = "impact.hips_toward_ball_with_upright_torso"


def _check(kind, level="HIGH", **raw):
    return {"kind": kind, "level": level, "raw": raw}


def _analysis(*, pelvis_dx=15.0, pelvis_dy=7.0, pelvis_pct=6.5, posture=-20.0, level="HIGH",
              view="down-the-line", toward=1, known=True, address=True, drop=(),
              rear_pct=6.7, rear_level="HIGH", camera="VALID"):
    impact = [
        _check("posture", level, torso_angle_change_deg=posture),
        _check("pelvis", level, movement_pct_height=pelvis_pct, dx_px=pelvis_dx, dy_px=pelvis_dy),
        _check("head", level, movement_pct_height=3.0, dx_px=-5.0, dy_px=-1.0),
        _check("shaft", level, shaft_from_horizontal_deg=70.0, change_vs_address_deg=14.0),
    ]
    impact = [c for c in impact if c["kind"] not in drop]
    rear = {"available": "rear" not in drop, "level": rear_level,
            "displacement_pct_height": None if "rear" in drop else rear_pct}
    return {
        "camera_view": view,
        "camera_check": {"status": camera},
        "pelvis_depth": {"phases": {"impact": rear}},
        "geometry_context": {"toward_ball": toward, "toward_ball_known": known},
        "phases": [
            {"phase": "address", "frame_index": 99 if address else None, "checks": [
                _check("torso_angle", torso_inclination_deg=46.0),
                _check("lead_knee", knee_flex_deg=16.0),
                _check("trail_knee", knee_flex_deg=18.0),
                _check("shaft", shaft_from_horizontal_deg=56.0),
            ]},
            {"phase": "takeaway", "frame_index": 116, "checks": [_check("shaft", shaft_from_horizontal_deg=15.0, change_vs_address_deg=-41.0)]},
            {"phase": "top", "frame_index": 127, "checks": [_check("posture", torso_angle_change_deg=-10.0)]},
            {"phase": "downswing", "frame_index": 132, "checks": [_check("shaft", shaft_from_horizontal_deg=54.0, change_vs_address_deg=-2.0)]},
            {"phase": "impact", "frame_index": 134, "checks": impact},
        ],
    }


def _result(analysis, rule_id):
    return next(r for r in interpret(analysis)["results"] if r["rule_id"] == rule_id)


def test_pattern_is_observed_but_never_a_verdict_while_uncalibrated() -> None:
    result = _result(_analysis(), PATTERN)
    assert result["status"] == STATUS_OBSERVED
    assert result["calibration"] == NEEDS_CALIBRATION
    assert result["classification"] is None and result["severity"] is None
    assert result["eligible_for_focus"] is False
    assert result["observed"]["posture_change_deg@impact"] == pytest.approx(-20.0)
    assert result["observed"]["pelvis_rear_toward_ball_pct@impact"] == pytest.approx(6.7)
    assert result["rule_version"] == 2


def test_every_dependency_is_required_before_a_rule_can_fire() -> None:
    for missing in ("rear", "posture"):
        result = _result(_analysis(drop=(missing,)), PATTERN)
        assert result["status"] == STATUS_BLOCKED and "missing" in result["reason"]
        assert result["user_text"] is None


@pytest.mark.parametrize("level", ["LOW", "UNAVAILABLE"])
def test_low_confidence_cannot_create_an_interpretation(level) -> None:
    result = _result(_analysis(level=level, rear_level=level), PATTERN)
    assert result["status"] == STATUS_BLOCKED and result["user_text"] is None
    assert _result(_analysis(level=level), "impact.posture_change")["status"] == STATUS_BLOCKED


def test_medium_is_enough_to_describe_but_not_for_the_pattern() -> None:
    assert _result(_analysis(level="MEDIUM"), PATTERN)["status"] == STATUS_BLOCKED
    assert _result(_analysis(level="MEDIUM"), "impact.posture_change")["status"] == STATUS_DESCRIBED


@pytest.mark.parametrize("kwargs, reason", [
    ({"view": "face-on"}, "camera view"),
    ({"address": False}, "Address"),
    ({"known": False}, "direction toward the ball"),
])
def test_fail_closed_on_wrong_view_missing_address_or_unknown_direction(kwargs, reason) -> None:
    result = _result(_analysis(**kwargs), PATTERN)
    assert result["status"] == STATUS_BLOCKED and reason in result["reason"]


def test_pattern_needs_both_signals_beyond_measurement_noise() -> None:
    away = _result(_analysis(rear_pct=-3.0), PATTERN)
    assert away["status"] == STATUS_NOT_FIRED
    bent_over = _result(_analysis(posture=6.0), PATTERN)
    assert bent_over["status"] == STATUS_NOT_FIRED
    tiny = _result(_analysis(rear_pct=0.3), PATTERN)
    assert tiny["status"] == STATUS_NOT_FIRED


@pytest.mark.parametrize("camera", ["UNCERTAIN", "INVALID"])
def test_pattern_needs_a_valid_dtl_camera(camera) -> None:
    result = _result(_analysis(camera=camera), PATTERN)
    assert result["status"] == STATUS_BLOCKED and "camera check" in result["reason"]
    described = _result(_analysis(camera=camera), "impact.rear_pelvis_vs_tush_line")
    assert described["status"] == (STATUS_DESCRIBED if camera == "UNCERTAIN" else STATUS_BLOCKED)


def test_pattern_uses_the_rear_pelvis_not_the_hip_midpoint() -> None:
    # Hip midpoint toward the ball but the rear of the pelvis stays on its line.
    result = _result(_analysis(pelvis_dx=15.0, rear_pct=0.2), PATTERN)
    assert result["status"] == STATUS_NOT_FIRED
    # Rear moves toward the ball even though the midpoint moved away.
    result = _result(_analysis(pelvis_dx=-15.0, rear_pct=5.0), PATTERN)
    assert result["status"] == STATUS_OBSERVED


def test_medium_rear_pelvis_is_enough_but_posture_must_be_high() -> None:
    assert _result(_analysis(rear_level="MEDIUM"), PATTERN)["status"] == STATUS_OBSERVED
    assert _result(_analysis(level="MEDIUM", rear_level="HIGH"), PATTERN)["status"] == STATUS_BLOCKED


def test_output_is_deterministic_and_versioned() -> None:
    first = interpret(_analysis())
    second = interpret(copy.deepcopy(_analysis()))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["version"] >= 1
    for result in first["results"]:
        assert isinstance(result["rule_version"], int) and result["rule_version"] >= 1
    assert len({rule.rule_id for rule in RULES}) == len(RULES)


def test_no_unsupported_golf_terminology_in_any_golfer_text() -> None:
    for variant in (_analysis(), _analysis(posture=6.0), _analysis(pelvis_dx=-15.0), _analysis(rear_pct=-4.0)):
        for result in interpret(variant)["results"]:
            for text in (result["user_text"], result["why_it_matters"]):
                assert terminology_violations(text) == [], (result["rule_id"], text)
    assert terminology_violations("That was early extension and a steep plane") == ["early extension", "steep"]
    assert "early extension" in BANNED_TERMS and "on plane" in BANNED_TERMS


def test_unsupported_concepts_are_blocked() -> None:
    results = {r["rule_id"]: r for r in interpret(_analysis())["results"]}
    assert results["takeaway.club_inside_outside"]["status"] == STATUS_BLOCKED
    assert results["takeaway.club_inside_outside"]["calibration"] == NOT_SUPPORTED
    assert results["top.posture_change"]["status"] == STATUS_BLOCKED


def test_main_focus_only_from_eligible_rules_and_none_today() -> None:
    report = interpret(_analysis())
    assert report["focus"] is None
    assert report["focus_fallback"] == "Coaching priorities are coming next."
    assert not any(r["eligible_for_focus"] for r in report["results"])


def _eligible(rule_id, phase, severity, priority, observed):
    return InterpretationResult(rule_id, 1, phase, STATUS_FIRED, VALIDATED, ("x",), "HIGH",
                                observed=observed, observed_confidence={k: "HIGH" for k in observed},
                                classification="c", severity=severity, user_text="t", priority=priority)


def test_focus_never_compares_unlike_units_only_severity_and_priority() -> None:
    degrees = _eligible("a.deg", "impact", "moderate", 20, {"posture_change_deg@impact": -40.0})
    percent = _eligible("b.pct", "top", "major", 50, {"pelvis_toward_ball_pct@impact": 0.6})
    # The tiny percentage wins on severity, however large the degree value.
    assert select_focus([degrees, percent]).rule_id == "b.pct"
    # Same severity: rule priority decides, not magnitudes.
    small = _eligible("c.pct", "impact", "moderate", 10, {"pelvis_toward_ball_pct@impact": 0.1})
    assert select_focus([degrees, small]).rule_id == "c.pct"
    # Descriptive / observed / blocked results are never focus candidates.
    descriptive = replace(degrees, status=STATUS_DESCRIBED, calibration=DESCRIPTIVE_ONLY)
    assert select_focus([descriptive]) is None


def test_an_uncalibrated_rule_that_fires_is_downgraded_to_observed() -> None:
    rule = rules_by_id()[PATTERN]
    assert rule.calibration == NEEDS_CALIBRATION
    result = rule.evaluate(facts_from_analysis(_analysis()))
    assert result.status == STATUS_OBSERVED and not result.eligible_for_focus
    assert result.coaching_key is None  # drills map only from fired, validated rules


def test_facts_split_movement_into_toward_ball_and_vertical() -> None:
    facts = facts_from_analysis(_analysis(pelvis_dx=15.0, pelvis_dy=0.0, pelvis_pct=6.0))
    assert facts.get("pelvis_toward_ball_pct", "impact").value == pytest.approx(6.0)
    flipped = facts_from_analysis(_analysis(pelvis_dx=15.0, pelvis_dy=0.0, pelvis_pct=6.0, toward=-1))
    assert flipped.get("pelvis_toward_ball_pct", "impact").value == pytest.approx(-6.0)
    unknown = facts_from_analysis(_analysis(known=False))
    assert unknown.get("pelvis_toward_ball_pct", "impact") is None
    assert unknown.get("pelvis_movement_pct", "impact") is not None


def test_research_doc_lists_every_banned_term_and_the_four_sections() -> None:
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "research" / "golf-interpretation-rules.md").read_text(encoding="utf-8")
    for heading in ("A. SUPPORTED NOW", "B. POSSIBLE BUT NEEDS CALIBRATION", "C. NOT SUPPORTED BY CURRENT VISION", "D. DO NOT SHIP YET"):
        assert heading in doc
    for term in ("early extension", "on plane", "steep", "shallow", "laid off", "across the line"):
        assert re.search(re.escape(term), doc, re.IGNORECASE)
