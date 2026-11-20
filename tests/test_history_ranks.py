"""Comparisons only claim real change; ranks reward quality and practice, never re-uploads."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from swingguru.ranks import DEFAULT_CONFIG, compute, load_config, rank_name
from swingguru.swing_history import compare, near_duplicate


def rec(total, **checks):
    """A record; checks given as id=(status, value)."""

    items = [{"id": k.replace("__", "."), "status": s, "value": v, "unit": "%" if k.startswith("impact") else "deg",
              "confidence": "HIGH"} for k, (s, v) in checks.items()]
    from swingguru.swing_history import fingerprint
    return {"score": {"total": total, "version": "swing_score_v3", "available": total is not None},
            "checks": items, "fingerprint": fingerprint(total, items)}


def swings(records, start=datetime(2026, 10, 1, 9), step=timedelta(days=1), club="7 Iron"):
    return [{"id": str(i), "created_at": (start + i * step).isoformat(), "club": club, "record": r}
            for i, r in enumerate(records)]


# ---------------------------------------------------------------- comparison

def test_small_change_is_about_the_same_and_big_change_counts():
    a = rec(7.5, top__posture=("attention", -8.0), address__torso_angle=("attention", 26.0))
    b = rec(8.4, top__posture=("good", -1.0), address__torso_angle=("attention", 27.0))
    out = compare(a, b, club_a="7 Iron", club_b="8 Iron")
    rows = {r["id"]: r for r in out["rows"]}
    assert rows["top.posture"]["result"] == "improved"
    assert rows["address.torso_angle"]["result"] == "same"          # 1 deg < 3 deg practical minimum
    assert out["score"]["result"] == "improved" and out["score"]["delta"] == 0.9


def test_mid_back_plane_needs_more_than_its_noise():
    a = rec(8.0, mid_back__shaft=("problem", -15.0))
    b = rec(8.0, mid_back__shaft=("attention", -8.0))
    row = next(r for r in compare(a, b, club_a="7 Iron", club_b="7 Iron")["rows"] if r["id"] == "mid_back.shaft")
    assert row["result"] == "same" and row["threshold"] == 14.0     # 7 deg noise, two recordings


def test_plane_not_compared_across_club_types_and_missing_checks_explained():
    a = rec(8.0, downswing__shaft=("attention", 8.0))
    b = rec(8.0, downswing__shaft=("good", 0.0), impact__head=("good", 2.0))
    out = compare(a, b, club_a="Driver", club_b="Pitching Wedge")
    rows = {r["id"]: r for r in out["rows"]}
    assert rows["downswing.shaft"]["result"] == "not_comparable" and rows["downswing.shaft"]["reason"] == "Different club types"
    assert rows["impact.head"]["reason"] == "Not measured on the first swing"
    assert out["notes"]


def test_scores_from_different_versions_are_not_compared():
    a, b = rec(7.0), rec(8.0)
    b["score"]["version"] = "swing_score_v2"
    assert compare(a, b)["score"]["result"] == "not_comparable"


def test_near_duplicate():
    a = rec(8.2, top__posture=("good", 1.0), address__torso_angle=("good", 40.0), address__trail_knee=("good", 20.0),
            address__arm_hang=("good", 2.0))
    retrim = json.loads(json.dumps(a))
    retrim["fingerprint"] = "different"
    retrim["checks"][0]["value"] = 1.3
    other = rec(8.2, top__posture=("good", 4.0), address__torso_angle=("good", 33.0), address__trail_knee=("good", 15.0),
                address__arm_hang=("good", -4.0))
    assert near_duplicate(a, a) and near_duplicate(a, retrim) and not near_duplicate(a, other)
