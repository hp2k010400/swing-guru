"""Rank rules v2: consistent quality over time, practice not volume, eligibility by measurement quality."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from swingguru.ranks import DEFAULT_CONFIG, compute, eligibility, load_config, rank_name
from swingguru.swing_history import fingerprint

START = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)          # a Monday


def rec(total, n=0, measured=8, long_side=1920, status="good"):
    """A distinct record (value varies with n) with a given score and measurement quality."""

    items = [{"id": "top.posture", "status": status, "value": round(0.37 * n, 2), "unit": "deg", "confidence": "HIGH"}]
    return {"score": {"total": total, "version": "swing_score_v3", "available": total is not None, "measured": measured,
                      "eligible": 8}, "video": {"long_side": long_side}, "checks": items,
            "fingerprint": fingerprint(total, items) + f"#{n}"}


def daily(scores, per_day=1, start=START, **kw):
    out, i = [], 0
    for d, s in enumerate(scores):
        for k in range(per_day):
            out.append({"id": f"{d}-{k}", "created_at": (start + timedelta(days=d, minutes=10 * k)).isoformat(),
                        "club": "7 Iron", "record": rec(s, i, **kw)})
            i += 1
    return out


def at(days: int) -> datetime:
    return START + timedelta(days=days)


def test_reuploads_earn_nothing():
    one = rec(8.0)
    swings = [{"id": str(i), "created_at": (START + timedelta(minutes=i)).isoformat(), "club": "7 Iron", "record": one}
              for i in range(10)]
    out = compute(swings, now=at(1))
    assert out["eligible_swings"] == 1 and out["rp"] == 10 and out["eligibility"]["duplicate"] == 9


def test_practice_points_are_capped_per_day_volume_earns_nothing_extra():
    out = compute(daily([7.5], per_day=8), now=at(1))
    assert out["rp"] == 10 + 5 + 5 and out["eligible_swings"] == 8      # 8 counted, only 3 earn points


def test_low_resolution_and_thin_measurement_are_practice_only():
    assert eligibility(rec(8.0, long_side=540), DEFAULT_CONFIG) == "practice_only:low_resolution"
    assert eligibility(rec(8.0, measured=4), DEFAULT_CONFIG) == "practice_only:insufficient_measurements"
    assert eligibility(rec(8.0, long_side=1024), DEFAULT_CONFIG) == "eligible"     # TikTok-size export: fine
    out = compute(daily([8.0] * 12, long_side=540), now=at(30))
    assert out["rp"] > 0 and out["rating"] is None and out["rating_swings"] == 0  # points yes, typical score no


def test_volume_alone_cannot_open_a_tier():
    out = compute(daily([6.0] * 120, per_day=5), now=at(121))
    assert out["rank"]["tier"] == "Bronze"
    assert out["next"]["rating_required"] == DEFAULT_CONFIG["tier_rating"]["Silver"]


def gates_only():
    """Points never limit the rank, so the score gates and tenure are tested on their own."""

    config = load_config()
    config["division_rp"] = [0] * 15
    return config


def test_a_tier_opens_only_after_two_consecutive_qualifying_weekly_evaluations():
    # 8.0 every day: the typical score clears Gold (7.8) once 10 eligible swings exist (day 10).
    swings = daily([8.0] * 30)
    once = compute(swings[:14], gates_only(), now=at(14))         # evaluations at day 7 (no rating) and 14 (met once)
    assert once["rank"]["tier"] == "Bronze" and once["next"]["weeks_met"] == 1
    twice = compute(swings[:21], gates_only(), now=at(21))         # second consecutive qualifying evaluation
    assert twice["rank"]["tier"] == "Gold" and twice["evaluations"][-1]["allowed_tier"] == "Gold"
    # With real point thresholds the same golfer is limited by points (practice), not by the gate.
    assert compute(swings[:21], now=at(21))["rank"]["tier"] in ("Bronze", "Silver")


def TIERS_INDEX(out):
    return ["Bronze", "Silver", "Gold", "Platinum", "Diamond"].index(out["rank"]["tier"])


def test_one_lucky_week_does_not_lock_in_a_tier():
    # 10 days of 9.5, then poor swings: the gate is met at most once before the window turns.
    scores = [9.5] * 10 + [6.0] * 4 + [6.0] * 40
    out = compute(daily(scores), now=at(60))
    assert out["rank"]["tier"] in ("Bronze", "Silver")


def test_tenure_holds_back_platinum():
    # Excellent, consistent swings from day one: Platinum still needs 6 weeks and 40 eligible swings.
    swings = daily([9.3] * 60)
    early = compute(swings[:30], gates_only(), now=at(35))          # gate met for weeks, but 5 weeks / 30 swings
    assert early["rank"]["tier"] == "Gold"
    assert early["next"]["tenure"]["met"] is False and early["next"]["tenure"]["swings_required"] == 40
    later = compute(swings, gates_only(), now=at(63))                # 9 weeks, 60 swings: Platinum, not yet Diamond
    assert later["rank"]["tier"] == "Platinum"


def test_back_in_range_needs_confirmation_on_the_next_swing():
    s = [rec(7.0, 0, status="problem"), rec(7.5, 1, status="good"), rec(7.6, 2, status="good")]
    swings = [{"id": str(i), "created_at": (START + timedelta(days=i)).isoformat(), "club": "7 Iron", "record": r}
              for i, r in enumerate(s)]
    out = compute(swings, now=at(3))
    counted = [e for e in out["events"] if e["kind"] == "counted"]
    assert "back_in_range" not in counted[1]["reasons"] and "back_in_range" in counted[2]["reasons"]


def test_rank_never_drops_and_next_reports_weeks_and_tenure():
    out = compute(daily([8.2] * 25 + [6.0] * 25), now=at(60))
    names = [t["rank"] for t in out["timeline"]]
    order = [rank_name(i) for i in range(15)]
    assert [order.index(n) for n in names] == sorted(order.index(n) for n in names)
    nxt = out["next"]
    assert {"weeks_required", "weeks_met", "tier_open", "tenure", "next_evaluation"} <= set(nxt)
    assert nxt["progress"] <= 1.0


def test_provisional_until_five_counted_swings():
    assert compute(daily([7.0] * 3), now=at(3))["provisional_remaining"] == 2
    assert compute(daily([7.0] * 5), now=at(5))["provisional"] is False


def test_config_override(monkeypatch):
    monkeypatch.setenv("SG_RANK_CONFIG", json.dumps({"gate_weeks": 3, "tier_rating": {"Gold": 7.5}}))
    config = load_config()
    assert config["gate_weeks"] == 3 and config["tier_rating"]["Gold"] == 7.5 and config["tier_rating"]["Silver"] == 7.0
    monkeypatch.setenv("SG_RANK_CONFIG", json.dumps({"division_rp": [0, 1]}))
    with pytest.raises(ValueError):
        load_config()
