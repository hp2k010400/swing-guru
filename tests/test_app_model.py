"""App product layer: score and focus interfaces stay honest; drills stay drafts."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from swingguru import app_api, app_drills
from swingguru.app_model import (
    NEUTRAL_FOCUS, SCORE_SCHEMA, SOURCE_MODEL, SOURCE_NONE, SOURCE_STUB, StubScorer, UncalibratedScorer,
    checkpoints, focus_for, status_counts, swing_datetime,
)


def _analysis(statuses=("good", "attention", "problem"), focus=None) -> dict:
    kinds = ("torso_angle", "trail_knee", "pelvis")
    phases = ("address", "address", "impact")
    out = {"phases": [], "interpretations": {"focus": focus, "results": []}}
    for phase in ("address", "impact"):
        out["phases"].append({"phase": phase, "checks": []})
    for kind, phase, status in zip(kinds, phases, statuses):
        target = next(p for p in out["phases"] if p["phase"] == phase)
        target["checks"].append({
            "kind": kind, "guided": True, "checkpoint": kind.title(), "value": "1", "time_s": 1.0,
            "verdict": {"status": status, "label": f"{kind} {status}", "support": "why",
                        "verdict_id": f"x.{kind}.{status}", "mapping": {"id": f"x.{kind}", "validation": "provisional_ui_mapping"}},
        })
    # An info check and an unguided one never count as checkpoints.
    out["phases"][0]["checks"].append({"kind": "shaft", "guided": False, "verdict": {"status": "good"}})
    out["phases"][0]["checks"].append({"kind": "hand_depth", "guided": True, "verdict": {"status": "info"}})
    return out


def test_checkpoints_are_guided_supported_verdicts_in_swing_order() -> None:
    items = checkpoints(_analysis())
    assert [c["id"] for c in items] == ["address.torso_angle", "address.trail_knee", "impact.pelvis"]
    assert status_counts(items) == {"good": 1, "attention": 1, "problem": 1, "total": 3}


def test_production_score_is_never_calculated_from_provisional_bands() -> None:
    result = UncalibratedScorer().score(_analysis(), swing_id="a").to_dict()
    assert result["available"] is False and result["total"] is None and result["source"] == SOURCE_NONE
    assert result["reason"] and result["supported_count"] == 3 and result["schema"] == SCORE_SCHEMA
    for key in ("total", "scale", "contributions", "confidence", "version", "supported_count", "reason"):
        assert key in result


def test_stub_score_is_flagged_and_independent_of_the_measurements() -> None:
    a = StubScorer().score(_analysis(("good",) * 3), swing_id="swing-1").to_dict()
    b = StubScorer().score(_analysis(("problem",) * 3), swing_id="swing-1").to_dict()
    assert a["source"] == SOURCE_STUB and a["version"] == "stub-0"
    assert a["total"] == b["total"]  # not derived from verdicts
    assert 0 <= a["total"] <= 10


def test_start_here_is_neutral_without_an_eligible_rule() -> None:
    result = focus_for(_analysis(), demo=False)
    assert not result.available and result.source == SOURCE_NONE and result.message == NEUTRAL_FOCUS


def test_start_here_uses_an_eligible_interpretation_rule() -> None:
    analysis = _analysis(focus={"rule_id": "impact.rule", "phase": "impact", "user_text": "Keep your hips back."})
    analysis["interpretations"]["results"] = [{"rule_id": "impact.rule", "priority": 10, "phase": "impact"}]
    result = focus_for(analysis, demo=False)
    assert result.available and result.source == SOURCE_MODEL and result.rule_id == "impact.rule"
    assert result.checkpoint == "impact.pelvis" and result.priority.rule_priority == 10


def test_demo_start_here_is_flagged_stub() -> None:
    result = focus_for(_analysis(), demo=True)
    assert result.source == SOURCE_STUB and result.available and "not a coaching priority" in result.reason


def test_every_drill_mapping_is_a_draft_needing_coach_review() -> None:
    assert all(v["status"] == app_drills.DRAFT == "draft_needs_coach_review" for v in app_drills.MAPPINGS.values())
    assert all(d.status == app_drills.DRAFT for d in app_drills.DRILLS)
    text = json.dumps(app_drills.catalogue()).lower()
    assert "pga" not in text and "reviewed by" not in text and "certified" not in text
    for mapping in app_drills.MAPPINGS.values():
        assert all(app_drills.drill(d) for d in mapping["drills"])


def test_every_supported_checkpoint_has_a_drill() -> None:
    from swingguru.verdicts import CHECKPOINTS

    for cp in CHECKPOINTS:
        assert app_drills.drills_for(cp.mapping.mapping_id, cp.phase), f"{cp.phase} {cp.name}"


def test_capture_time_comes_from_the_file_name() -> None:
    fallback = datetime(2000, 1, 1)
    assert swing_datetime("WhatsApp Video 2026-09-29 at 14.55.01.mp4", fallback=fallback) == datetime(2026, 9, 29, 14, 55, 1)
    assert swing_datetime("window.mp4", "10_2026-09-29_14-54-56", fallback=fallback) == datetime(2026, 9, 29, 14, 54, 56)
    assert swing_datetime("clip.mp4", fallback=fallback) == fallback


def test_week_streak_counts_back_from_this_or_last_week() -> None:
    days = [datetime(2026, 9, 29), datetime(2026, 9, 22), datetime(2026, 9, 8)]
    assert app_api.week_streak(days, date(2026, 9, 30)) == 2
    assert app_api.week_streak(days, date(2026, 10, 6)) == 2   # nothing this week yet: counts from last week
    assert app_api.week_streak(days, date(2026, 10, 20)) == 0


@pytest.mark.skipif(not app_api.bundle_dirs(), reason="no analysed swings in var/")
def test_real_library_marks_every_stub_and_keeps_verdicts_real() -> None:
    items = app_api.library(demo=True)
    assert items and all(s["score"]["source"] == SOURCE_STUB for s in items)
    assert all(s["focus"]["source"] in (SOURCE_STUB, SOURCE_MODEL, SOURCE_NONE) for s in items)
    assert all(s["pr"]["source"] == SOURCE_STUB for s in items)
    production = app_api.library(demo=False)
    # Production: the versioned Swing Score model (never the stub), and no PR badge from it yet.
    assert all(s["score"]["source"] == SOURCE_MODEL and s["score"]["version"] == "swing_score_v3" for s in production)
    assert all(s["pr"]["value"] is False for s in production)
    assert all(s["focus"]["source"] != SOURCE_STUB for s in production)
    assert production[0]["counts"] == items[0]["counts"]  # real data is the same in both modes
