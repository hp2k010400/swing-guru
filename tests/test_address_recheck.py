"""Address re-check (candidate fix address_recheck): only a suspiciously early set-up moves."""

from __future__ import annotations

from pathlib import Path

import pytest

from swingguru.auto_moments import ADDRESS_RECHECK_GAP_S, MomentConfig, estimate_moments
from swingguru.pose_cache import load_records_any

ROOT = Path(__file__).resolve().parents[1] / "var" / "qa" / "validation_prod" / "candidate"
pytestmark = pytest.mark.skipif(not (ROOT / "02" / "pose_records.pkl").is_file(), reason="validation pose caches not present")


def _times(records, recheck: bool) -> dict:
    m = estimate_moments(records, config=MomentConfig(address_recheck=recheck))
    return {getattr(e.phase, "value", e.phase): (None if e.timestamp_ms is None else e.timestamp_ms / 1000) for e in m.estimates}, m


def test_early_setup_is_moved_to_the_final_still_window():
    records = load_records_any(ROOT / "02" / "pose_records.pkl")
    before, _ = _times(records, False)
    after, moments = _times(records, True)
    assert before["takeaway"] - before["address"] > 2.0                    # 0.69 s vs takeaway 2.89 s
    assert 0 < after["takeaway"] - after["address"] <= ADDRESS_RECHECK_GAP_S
    for phase in ("takeaway", "mid_back", "top", "downswing", "impact", "finish"):
        assert after[phase] == before[phase], phase                        # only Address moves
    assert moments.sequence["address_recheck"]["hand_speed_address_s"] == pytest.approx(before["address"], abs=0.01)


def test_normal_setup_is_untouched():
    records = load_records_any(ROOT / "01" / "pose_records.pkl")
    before, _ = _times(records, False)
    after, moments = _times(records, True)
    assert before == after and "address_recheck" not in moments.sequence


def _rows(lines, usable=True):
    return {"address_reference": {"frames": [{"line_deg": v, "usable": usable} for v in lines]}}


def test_club_steadiness_guard_on_the_regression_cases():
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_analysis", Path(__file__).resolve().parents[1] / "scripts" / "build_analysis.py")
    ba = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ba)
    # The real club readings at the re-checked Address (FIX_address_recheck.md).
    assert ba.club_steady_at_address(_rows([123, 121, 117, 113, 109]))[0] is False     # prod/02: moving
    assert ba.club_steady_at_address(_rows([114, 108, 100, 87, 51]))[0] is False      # 1_0_1/01: moving fast
    assert ba.club_steady_at_address(_rows([135, 137, 137, 137, 137]))[0] is True     # prod/46: steady
    assert ba.club_steady_at_address(_rows([135, 135, 135, 135, 135], usable=False))[0] is False   # unclear club
    assert ba.club_steady_at_address({})[0] is False


def test_off_by_default_and_not_in_the_candidate_profile():
    from swingguru.engine_profile import CANDIDATE_FIXES, profile

    assert MomentConfig().address_recheck is False
    assert "address_recheck" not in CANDIDATE_FIXES and not profile("candidate").on("address_recheck")
    assert profile("candidate", only=["address_recheck"]).on("address_recheck")     # harness only
