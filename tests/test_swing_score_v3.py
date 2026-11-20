"""Swing Score v3 (development model): noise-aware, smooth, grouped, compounding. v2 stays production."""

from __future__ import annotations

import copy
import json
import random

import pytest

from swingguru.scoring import VERSION, score_swing
from swingguru.scoring import config as C

from test_swing_score import analysis, check

V2, V3 = "swing_score_v2", "swing_score_v3"


def out_by(check_id: str, d: float) -> float:
    """A reading exactly ``d`` "slightly out" band widths beyond the high edge of the target band."""

    c = check(check_id)
    value = c.good[1] + d * (c.slight_high - c.good[1])
    if check_id.endswith(".shaft"):
        # Plane readings beyond +-25 deg are refused by the verdict layer's plausibility gate (a
        # false club detection); keep test values inside it.
        value = min(value, 24.9)
    return value


def v3(values: dict | None = None, levels: dict | None = None) -> dict:
    return score_swing(analysis(values, levels), V3)


def total(values: dict | None = None) -> float:
    return v3(values)["total"]


def test_production_default_is_v3_and_v2_stays_reproducible() -> None:
    assert VERSION == C.VERSION == V3                 # approved for production 2026-10-07
    assert score_swing(analysis())["version"] == V3
    assert score_swing(analysis(), V2)["version"] == V2


# ---------------------------------------------------------------- the scenarios


def test_all_checks_in_range() -> None:
    assert total() == 10.0
    edge = {c.id: c.good[1] - 0.01 for c in C.CHECKS}
    assert 9.4 <= total(edge) <= 9.6                      # in range everywhere: barely touched
    assert all(c["class"] == "in_range" for c in v3(edge)["checks"])


def test_one_slightly_out_costs_little() -> None:
    for cid in ("impact.head", "impact.pelvis", "top.posture", "downswing.shaft", "address.arm_hang"):
        drop = 10.0 - total({cid: out_by(cid, 0.5)})
        assert 0.0 < drop <= 0.5, cid
        assert next(c for c in v3({cid: out_by(cid, 0.5)})["checks"] if c["id"] == cid)["class"] in (
            "slightly_out", "within_noise")


def test_one_clearly_out_costs_about_one_point() -> None:
    for cid, weight in C.MODELS[V3].weights.items():
        drop = 10.0 - total({cid: out_by(cid, 2.0)})
        assert 0.2 <= drop <= 1.6, (cid, drop)
        if weight >= 15:
            assert drop >= 0.8, (cid, drop)               # a heavy check clearly out is not shrugged off


def test_multiple_slightly_out_degrade_progressively() -> None:
    ids = ["address.arm_hang", "downswing.shaft", "impact.head"]
    scores = [total({i: out_by(i, 0.5) for i in ids[:n]}) for n in range(0, 4)]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]
    assert scores[-1] >= 9.0                              # several small deviations are still a good swing


def test_multiple_clearly_out_compound() -> None:
    one = 10.0 - total({"impact.head": out_by("impact.head", 2.0)})
    two = 10.0 - total({"impact.head": out_by("impact.head", 2.0), "downswing.shaft": out_by("downswing.shaft", 2.0)})
    one_plane = 10.0 - total({"downswing.shaft": out_by("downswing.shaft", 2.0)})
    assert two > one + one_plane                           # separate significant faults cost more together
    assert v3({c.id: out_by(c.id, 2.0) for c in C.CHECKS})["total"] <= 3.0
    assert v3({c.id: out_by(c.id, 3.0) for c in C.CHECKS})["total"] <= 2.5
    assert v3({c.id: out_by(c.id, 2.0) for c in C.CHECKS})["deductions"]["significant_families"] == 3


def test_correlated_faults_are_not_double_penalised() -> None:
    head = out_by("impact.head", 2.0)
    pelvis = out_by("impact.pelvis", 2.0)
    both = v3({"impact.head": head, "impact.pelvis": pelvis})
    family = next(f for f in both["families"] if f["id"] == "posture")
    assert family["loss"] < family["independent_sum"]      # grouped relief
    drop_both = 10.0 - both["total"]
    drop_head, drop_pelvis = 10.0 - total({"impact.head": head}), 10.0 - total({"impact.pelvis": pelvis})
    assert max(drop_head, drop_pelvis) < drop_both < drop_head + drop_pelvis
    assert both["deductions"]["multi_fault_penalty"] == 0  # one family: no compounding


def test_borderline_readings_within_measurement_noise_are_not_penalised() -> None:
    # Head band ends at 4 %; head noise is 1.55 %. 5.2 % is within noise of the edge.
    result = v3({"impact.head": 5.2})
    head = next(c for c in result["checks"] if c["id"] == "impact.head")
    v2_head = next(c for c in score_swing(analysis({"impact.head": 5.2}), V2)["checks"] if c["id"] == "impact.head")
    assert head["class"] == "within_noise"
    assert head["points"] > v2_head["points"] and head["points"] >= 8.5     # softened, not ignored
    assert result["total"] >= score_swing(analysis({"impact.head": 5.2}), V2)["total"]
    # Far outside the band, noise gives no free allowance: same reading, same deduction as without noise.
    far = next(c for c in v3({"impact.head": 14.0})["checks"] if c["id"] == "impact.head")
    assert far["class"] == "clearly_out" and far["points"] <= 3.5
    # Noise never costs an in-range reading anything.
    assert next(c for c in v3({"downswing.shaft": 0.0})["checks"] if c["id"] == "downswing.shaft")["points"] == 10.0
    # MEDIUM confidence widens the allowance: a reading just past the HIGH allowance is still noise.
    medium = v3({"impact.head": 4 + 1.55 * 1.4}, {"impact.head": "MEDIUM"})   # 2.17 out; noise 2.33
    assert next(c for c in medium["checks"] if c["id"] == "impact.head")["class"] == "within_noise"


def test_missing_measurements_count_neutral_not_as_perfect() -> None:
    full = total()
    missing = v3({"impact.head": None, "impact.pelvis": None})
    assert missing["measured"] == 6 and missing["total"] < full
    assert {e["id"] for e in missing["excluded"]} == {"impact.head", "impact.pelvis"}
    assert all(e["points"] == 6.0 and e["weight"] > 0 for e in missing["excluded"])
    # Missing data never lifts a swing with real faults above the same swing fully measured.
    faulty = {"top.posture": out_by("top.posture", 2.0), "impact.head": out_by("impact.head", 0.2),
              "impact.pelvis": out_by("impact.pelvis", 0.2)}
    assert total({**faulty, "impact.head": None, "impact.pelvis": None}) <= total(faulty) + 0.6


def test_low_coverage_gives_no_score() -> None:
    gone = {cid: None for cid in ("mid_back.shaft", "downswing.shaft", "impact.pelvis", "impact.head")}
    result = v3(gone)
    assert result["measured"] == 4 and not result["available"] and result["total"] is None
    assert result["reason"] == C.NOT_ENOUGH
    no_finish = v3({"downswing.shaft": None, "impact.pelvis": None, "impact.head": None})
    assert no_finish["measured"] == 5 and not no_finish["available"]


# ---------------------------------------------------------------- properties


def test_monotone_bounded_and_deterministic() -> None:
    for cid in ("impact.head", "top.posture", "address.trail_knee", "mid_back.shaft"):
        previous = 11.0
        for d in [0, 0.25, 0.5, 1, 1.5, 2, 3, 5]:
            now = total({cid: out_by(cid, d)})
            assert now <= previous + 1e-9, (cid, d)
            previous = now
    rng = random.Random(3)
    for _ in range(300):
        values = {c.id: (None if rng.random() < 0.15 else rng.uniform(-60, 80)) for c in C.CHECKS}
        levels = {c.id: rng.choice(["HIGH", "MEDIUM", "LOW"]) for c in C.CHECKS}
        result = v3(values, levels)
        if result["available"]:
            assert 0.0 <= result["total"] <= 10.0
    a = analysis({"impact.head": 6.3})
    shuffled = copy.deepcopy(a)
    shuffled["phases"].reverse()
    assert score_swing(a, V3) == score_swing(shuffled, V3)
    assert json.loads(json.dumps(score_swing(a, V3))) == score_swing(a, V3)


def test_v3_never_reads_who_the_golfer_is_and_explains_itself() -> None:
    a = analysis({"impact.head": 6.0})
    b = copy.deepcopy(a)
    b.update({"player": "Any Tour Player", "club": "Driver", "user_id": "x"})
    assert score_swing(a, V3) == score_swing(b, V3)
    result = score_swing(a, V3)
    assert result["method"].startswith("Based on this swing's measured mechanics.")
    assert all({"noise", "deviation", "d", "class", "loss"} <= set(c) for c in result["checks"])
    assert "—" not in result["method"] and "–" not in result["method"]


def test_noise_settings_match_the_measured_evidence() -> None:
    """Every v3 noise figure comes from measurement_noise.json (no borrowed values)."""

    from pathlib import Path

    evidence = json.loads((Path(__file__).resolve().parents[1] / "docs" / "agent-work" / "analysis-quality"
                           / "measurement_noise.json").read_text(encoding="utf-8"))["summary"]
    for check_id, noise in C.V3_NOISE.items():
        assert check_id in evidence, check_id
        assert noise == pytest.approx(evidence[check_id]["sigma_median"], abs=0.1), check_id
