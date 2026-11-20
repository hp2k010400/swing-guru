"""Swing Score V1 and V2: formula, confidence, missing checks, coverage, bounds, determinism, versioning."""

from __future__ import annotations

import copy
import json
import random

import pytest

from swingguru.scoring import SCHEMA, VERSION, points, score_swing
from swingguru.scoring import config as C
from swingguru.verdicts import CHECKPOINTS, attach_verdicts

# Raw measurement key per checkpoint, as the engine writes it.
RAW = {
    "address.torso_angle": ("raw", "torso_inclination_deg"),
    "address.trail_knee": ("raw", "knee_flex_deg"),
    "address.arm_hang": ("raw", "arm_angle_deg"),
    "mid_back.shaft": ("raw", "change_vs_address_deg"),
    "top.posture": ("raw", "torso_angle_change_deg"),
    "downswing.shaft": ("raw", "change_vs_address_deg"),
    "impact.pelvis": ("pelvis_depth", "displacement_pct_height"),
    "impact.head": ("raw", "movement_pct_height"),
}
IDEAL = {c.id: c.ideal for c in C.CHECKS}
V1 = "swing_score_v1"
V2 = "swing_score_v2"


def analysis(values: dict[str, float | None] | None = None, levels: dict[str, str] | None = None) -> dict:
    """A minimal analysis with the eight checkpoints; ``None`` leaves a check out."""

    values = {**IDEAL, **(values or {})}
    phases: dict[str, list] = {}
    for cid, value in values.items():
        if value is None:
            continue
        phase, kind = cid.split(".")
        container, key = RAW[cid]
        check = {"id": f"{phase}:{kind}", "kind": kind, "phase": phase, "title": kind, "value": str(value),
                 "level": (levels or {}).get(cid, "HIGH"), "guided": True, container: {key: value}}
        phases.setdefault(phase, []).append(check)
    data = {"phases": [{"phase": p, "label": p, "frame_index": 1, "checks": checks} for p, checks in phases.items()],
            "hidden": []}
    return attach_verdicts(data)


def check(cid: str) -> C.ScoredCheck:
    return next(c for c in C.CHECKS if c.id == cid)


# ---------------------------------------------------------------- config

def test_config_matches_the_verdict_bands_so_drift_forces_a_new_version() -> None:
    assert len(C.CHECKS) == len(CHECKPOINTS) == 8
    for scored, cp in zip(C.CHECKS, CHECKPOINTS):
        assert (scored.phase, scored.kind, scored.mapping_id) == (cp.phase, cp.mapping.kind, cp.mapping.mapping_id)
        bands = cp.mapping.bands
        good = next(b for b in bands if b.status == "good")
        assert scored.good == (good.low, good.high)
        assert scored.slight_low == next((b.low for b in bands if b.high == good.low and b.status == "attention"), None)
        assert scored.slight_high == next((b.high for b in bands if b.low == good.high and b.status == "attention"), None)
        lo, hi = scored.good
        assert scored.ideal == ((lo + hi) / 2 if lo is not None and hi is not None else 0)


def test_v1_weights_are_equal_and_documented_as_a_product_choice() -> None:
    assert set(C.MODELS[V1].weights.values()) == {1.0}
    assert "product choice" in (C.__doc__ or "")
    assert C.VERSION == VERSION == "swing_score_v3"   # v3 approved for production 2026-10-07


# ---------------------------------------------------------------- points curve

@pytest.mark.parametrize("cid", [c.id for c in C.CHECKS])
def test_points_anchor_at_ideal_range_edge_and_slight_edge(cid: str) -> None:
    c = check(cid)
    assert points(c, c.ideal) == 10.0
    lo, hi = c.good
    assert points(c, hi) == pytest.approx(9.0)
    assert points(c, c.slight_high) == pytest.approx(4.0)
    assert points(c, c.slight_high + (c.slight_high - hi)) == pytest.approx(0.0)
    assert points(c, c.slight_high + 100) == 0.0
    if lo is not None:
        assert points(c, lo) == pytest.approx(9.0)
        assert points(c, c.slight_low) == pytest.approx(4.0)
    else:  # one-sided range: the open side is fully in range
        assert points(c, -100.0) == 10.0


@pytest.mark.parametrize("cid", [c.id for c in C.CHECKS])
def test_points_are_continuous_bounded_and_fall_away_from_the_ideal(cid: str) -> None:
    c = check(cid)
    previous = 10.0
    x = c.ideal
    while x < c.ideal + 60:
        p = points(c, x)
        assert 0.0 <= p <= 10.0
        assert p <= previous + 1e-9          # never rises moving away from the ideal
        assert previous - p < 0.08           # no jump: 0.01-unit steps change points by < 0.08
        previous, x = p, x + 0.01


# ---------------------------------------------------------------- whole swing

def test_all_checks_at_their_ideal_score_ten() -> None:
    result = score_swing(analysis(), V2)
    assert result["available"] and result["total"] == 10.0
    assert (result["measured"], result["eligible"], result["coverage"]) == (8, 8, 1.0)
    assert result["excluded"] == []


def test_in_range_but_off_centre_scores_nine_to_ten() -> None:
    edgy = {c.id: (c.good[1] - 0.01) for c in C.CHECKS}
    result = score_swing(analysis(edgy), V2)
    assert 9.0 <= result["total"] < 9.1
    assert all(ch["status"] == "good" for ch in result["checks"])


def test_slight_deviations_cost_less_than_large_ones() -> None:
    slight = score_swing(analysis({"impact.head": 5.0, "top.posture": -7.0}), V1)["total"]
    large = score_swing(analysis({"impact.head": 11.0, "top.posture": -14.0}), V1)["total"]
    assert 10.0 > slight > large
    assert slight == pytest.approx((6 * 10 + points(check("impact.head"), 5.0) + points(check("top.posture"), -7.0)) / 8, abs=0.05)


def test_low_confidence_is_excluded_and_medium_counts_half() -> None:
    bad = {"mid_back.shaft": -20.0}
    full = score_swing(analysis(bad), V1)
    medium = score_swing(analysis(bad, {"mid_back.shaft": "MEDIUM"}), V1)
    low = score_swing(analysis(bad, {"mid_back.shaft": "LOW"}), V1)
    assert low["measured"] == 7 and low["total"] == 10.0
    assert any(e["id"] == "mid_back.shaft" and "reliably" in e["reason"] for e in low["excluded"])
    assert full["total"] < medium["total"] < low["total"]
    assert medium["confidence"] == {"high": 7, "medium": 1, "label": "MEDIUM"}
    share = next(c for c in medium["checks"] if c["id"] == "mid_back.shaft")["share"]
    assert share == pytest.approx(0.5 / 7.5, abs=0.001)


def test_missing_checks_are_excluded_not_counted_as_zero_or_ten() -> None:
    values = {"impact.pelvis": None, "impact.head": None, "top.posture": -8.0}
    result = score_swing(analysis(values), V1)
    assert result["measured"] == 6 and result["coverage"] == 0.75
    assert {e["id"] for e in result["excluded"]} == {"impact.pelvis", "impact.head"}
    assert result["total"] == pytest.approx((5 * 10 + points(check("top.posture"), -8.0)) / 6, abs=0.05)


def test_too_few_reliable_checks_gives_no_score() -> None:
    gone = {cid: None for cid in ("mid_back.shaft", "downswing.shaft", "impact.pelvis", "impact.head")}
    result = score_swing(analysis(gone), V2)
    assert result["measured"] == 4 and not result["available"]
    assert result["total"] is None and result["reason"] == C.NOT_ENOUGH == "Not enough reliable measurements for a Swing Score."
    assert score_swing(analysis({**gone, "impact.head": 1.0}), V2)["available"]  # 5 of 8 is enough


def test_score_is_always_between_zero_and_ten_on_random_swings() -> None:
    rng = random.Random(7)
    for _ in range(300):
        values = {c.id: (None if rng.random() < 0.15 else rng.uniform(-60, 80)) for c in C.CHECKS}
        levels = {c.id: rng.choice(["HIGH", "MEDIUM", "LOW"]) for c in C.CHECKS}
        result = score_swing(analysis(values, levels), V2)
        if result["available"]:
            assert 0.0 <= result["total"] <= 10.0
            assert result["total"] == round(result["total"], 1)


def test_scoring_is_deterministic_and_independent_of_phase_order() -> None:
    values = {"address.arm_hang": 13.3, "downswing.shaft": 7.7, "impact.head": 6.1}
    a = analysis(values)
    shuffled = copy.deepcopy(a)
    shuffled["phases"].reverse()
    assert score_swing(a) == score_swing(copy.deepcopy(a)) == score_swing(shuffled)


def test_result_carries_version_and_round_trips_as_json() -> None:
    result = score_swing(analysis({"impact.head": 6.0}), V2)
    assert result["version"] == "swing_score_v2" and result["schema"] == SCHEMA
    assert score_swing(analysis({"impact.head": 6.0}))["version"] == "swing_score_v3"   # the production default
    assert score_swing(analysis({"impact.head": 6.0}), V1)["version"] == V1
    stored = json.loads(json.dumps(result))
    assert stored == result
    for key in ("total", "measured", "eligible", "coverage", "confidence", "checks", "excluded", "version"):
        assert key in stored
    assert all({"id", "points", "weight", "measurement", "confidence"} <= set(c) for c in stored["checks"])


def test_unrelated_fields_do_not_change_the_score() -> None:
    a = analysis({"top.posture": -6.5})
    base = score_swing(a)
    b = copy.deepcopy(a)
    b["summary"] = {"text": "anything"}
    b["video"] = "other.mp4"
    for phase in b["phases"]:
        phase["label"] = "Renamed"
        for c in phase["checks"]:
            c["title"], c["summary"], c["value"] = "New title", "New copy", "shown differently"
    assert score_swing(b) == base


def test_the_wording_never_claims_validation() -> None:
    text = (C.METHOD + C.V1_METHOD + C.NOT_ENOUGH).lower()
    for banned in ("pga", "tour", "validated", "scientific", "biomechanical accuracy"):
        assert banned not in text
    assert "—" not in text and "–" not in text   # no em or en dashes in user-facing copy
    assert C.METHOD.startswith("Based on this swing's measured mechanics.")
    assert "Swing Score evaluates this individual swing, not your handicap or overall playing ability." in C.METHOD


# ---------------------------------------------------------------- V2 (2026-10-05 audit)

def test_v2_weights_by_phase_set_up_20_backswing_25_downswing_and_impact_55() -> None:
    w = C.MODELS["swing_score_v2"].weights
    assert set(w) == {c.id for c in C.CHECKS} and sum(w.values()) == 100
    assert sum(v for k, v in w.items() if k.startswith("address.")) == 20
    assert w["mid_back.shaft"] + w["top.posture"] == 25
    assert w["downswing.shaft"] + w["impact.pelvis"] + w["impact.head"] == 55
    # The plane is one measurement taken twice: together it is under a quarter of the score.
    assert w["mid_back.shaft"] + w["downswing.shaft"] < 25


def test_v2_set_up_alone_cannot_carry_a_poor_swing() -> None:
    poor_motion = {"mid_back.shaft": 14.0, "top.posture": -12.0, "downswing.shaft": 15.0,
                   "impact.pelvis": 6.5, "impact.head": 10.0}
    v1, v2 = score_swing(analysis(poor_motion), V1)["total"], score_swing(analysis(poor_motion), V2)["total"]
    assert v2 < v1 and v2 < 4.0


def test_v2_missing_checks_no_longer_raise_the_score() -> None:
    """V1's worst flaw: a swing scored on its easy checks alone beat the same swing fully measured."""

    values = {"address.arm_hang": 4.0, "top.posture": -3.0, "downswing.shaft": 3.0,
              "mid_back.shaft": 9.0, "impact.pelvis": 4.0, "impact.head": 7.0}
    full = score_swing(analysis(values), V2)
    partial = score_swing(analysis({**values, "mid_back.shaft": None, "impact.pelvis": None, "impact.head": None}), V2)
    assert score_swing(analysis({**values, "mid_back.shaft": None, "impact.pelvis": None, "impact.head": None}), V1)["total"]         > score_swing(analysis(values), V1)["total"]                     # V1: fewer measured -> higher
    assert partial["measured"] == 5 and partial["total"] < 9.0          # V2: pulled toward neutral
    # The neutral term is reported with its weight and share, so the sheet can explain it.
    gone = {e["id"]: e for e in partial["excluded"]}
    assert gone["impact.pelvis"]["points"] == 6.0 and gone["impact.pelvis"]["weight"] == 11.0
    assert partial["unmeasured"] == {"points": 6.0, "weight": 0.5}
    assert full["available"]


def test_v2_exact_formula_with_a_neutral_term() -> None:
    values = {"impact.pelvis": None, "impact.head": None, "top.posture": -8.0}
    w = C.MODELS["swing_score_v2"].weights
    result = score_swing(analysis(values), V2)
    measured = sum(w[k] for k in w if k not in ("impact.pelvis", "impact.head", "top.posture"))
    num = measured * 10 + w["top.posture"] * points(check("top.posture"), -8.0) + (w["impact.pelvis"] + w["impact.head"]) * 0.5 * 6.0
    den = measured + w["top.posture"] + (w["impact.pelvis"] + w["impact.head"]) * 0.5
    assert result["total"] == pytest.approx(num / den, abs=0.05)


def test_v2_needs_a_downswing_or_impact_check() -> None:
    no_finish = {"downswing.shaft": None, "impact.pelvis": None, "impact.head": None}
    v2 = score_swing(analysis(no_finish), V2)
    assert v2["measured"] == 5 and not v2["available"] and v2["total"] is None
    assert score_swing(analysis(no_finish), V1)["available"]          # V1 would have scored it


def test_v2_all_ideal_is_still_ten_and_every_check_at_its_range_edge_is_nine() -> None:
    assert score_swing(analysis(), V2)["total"] == 10.0
    edgy = {c.id: (c.good[1] - 0.01) for c in C.CHECKS}
    assert 9.0 <= score_swing(analysis(edgy), V2)["total"] < 9.1


def test_scoring_never_reads_a_player_identity() -> None:
    """No player-specific exceptions: names, uploader and club never change the score."""

    a = analysis({"impact.head": 6.0})
    b = copy.deepcopy(a)
    b.update({"player": "Any Tour Player", "user_id": "x", "club": "Driver", "title": "Pro swing"})
    assert score_swing(a) == score_swing(b)
    import inspect
    from swingguru.scoring import model as M
    source = (inspect.getsource(M) + inspect.getsource(C)).lower()
    for word in ("player", "pro_", "professional", "name ==", "user_id"):
        assert word not in source


def test_stored_v1_scores_are_returned_unchanged() -> None:
    from swingguru.app_model import swing_score

    a = analysis({"impact.head": 6.0})
    stored = score_swing(a, V1)
    assert swing_score({**a, "swing_score": stored}) == stored          # history is never rescored
    assert swing_score(a)["version"] == "swing_score_v3"                  # unscored swings get the current model
