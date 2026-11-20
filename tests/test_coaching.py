"""Coaching copy: wording only, picked by the side of the good band; never changes a verdict."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swingguru.app_model import checkpoints
import re

from swingguru.coaching import COPY, LOW_TRUST_NOTES, SEVERITY_HEADLINES, SEVERITY_NOTES, coaching_for, practise_this
from swingguru.verdicts import CHECKPOINTS, UNSUPPORTED_CLAIMS, unsupported_claims, verdict_for

ROOT = Path(__file__).resolve().parents[1]
SETS = [ROOT / "var" / "qa" / s / "candidate" for s in ("validation_prod", "validation_new", "validation_1_0_1")]


def _check(kind: str, phase: str, raw: dict) -> dict:
    check = {"kind": kind, "phase": phase, "raw": raw, "value": "x", "level": "HIGH"}
    check["verdict"] = verdict_for(check)
    return check


def test_every_side_of_every_checkpoint_has_copy():
    for cp in CHECKPOINTS:
        key = cp.mapping.mapping_id + ("@" + cp.phase if cp.mapping.mapping_id.startswith("swing.") else "")
        assert (key, "good") in COPY, key
        good = next(b for b in cp.mapping.bands if b.status == "good")
        for band in cp.mapping.bands:
            if band.status == "good":
                continue
            side = "low" if good.low is not None and band.high is not None and band.high <= good.low else "high"
            assert (key, side) in COPY, (key, side)


def test_copy_never_makes_an_unsupported_claim():
    for entry in COPY.values():
        for text in entry:
            assert not unsupported_claims(text), (text, UNSUPPORTED_CLAIMS)


@pytest.mark.parametrize("raw,side,headline", [
    ({"torso_angle_change_deg": -6.2}, "low", "Rose a little in the backswing"),
    ({"torso_angle_change_deg": -12.0}, "low", "Stood up in the backswing"),
    ({"torso_angle_change_deg": 13.0}, "high", "Bent further over at the top"),
    ({"torso_angle_change_deg": 1.0}, "good", "Held your posture"),
])
def test_top_posture_direction(raw, side, headline):
    card = coaching_for(_check("posture", "top", raw), "top")
    assert card["side"] == side and card["headline"] == headline


def test_plane_is_phase_specific():
    mid = coaching_for(_check("shaft", "mid_back", {"change_vs_address_deg": 14.0}), "mid_back")
    down = coaching_for(_check("shaft", "downswing", {"change_vs_address_deg": 14.0}), "downswing")
    assert mid["headline"] == "Steep halfway back" and down["headline"] == "Steep coming down"
    assert "14°" in mid["what"]


def test_good_cards_have_no_fix_and_bad_cards_do():
    good = coaching_for(_check("torso_angle", "address", {"torso_inclination_deg": 40}), "address")
    bad = coaching_for(_check("torso_angle", "address", {"torso_inclination_deg": 15}), "address")
    assert good["fix"] is None and bad["fix"]
    assert "30–50°" in bad["what"]


def test_severity_headlines_and_extension_away_from_ball():
    slight = coaching_for(_check("head", "impact", {"movement_pct_height": 5.0}), "impact")
    clear = coaching_for(_check("head", "impact", {"movement_pct_height": 9.0}), "impact")
    assert slight["headline"] == "Some head movement" and clear["headline"] == "Head moved a lot"
    check = {"kind": "pelvis", "phase": "impact", "pelvis_depth": {"displacement_pct_height": -2.4}, "level": "HIGH"}
    check["verdict"] = verdict_for(check)
    away = coaching_for(check, "impact")
    assert away["side"] == "good" and "away from the ball" in away["what"] and "2.4%" in away["what"]


def test_severity_headlines_are_safe():
    for text in SEVERITY_HEADLINES.values():
        assert not unsupported_claims(text)


def test_severity_notes_keep_small_differences_small():
    slight = coaching_for(_check("posture", "top", {"torso_angle_change_deg": -6.0}), "top")
    clear = coaching_for(_check("posture", "top", {"torso_angle_change_deg": -12.0}), "top")
    good = coaching_for(_check("posture", "top", {"torso_angle_change_deg": 1.0}), "top")
    assert "small difference" in slight["note"] and "not a major fault" in slight["note"]
    assert "clear difference" in clear["note"] and good["note"] is None
    head = coaching_for(_check("head", "impact", {"movement_pct_height": 5.0}), "impact")
    assert "normal" in head["note"] and head["low_trust"] is True
    for notes in [SEVERITY_NOTES, *LOW_TRUST_NOTES.values()]:
        for text in notes.values():
            assert not unsupported_claims(text) and not re.search(r"\d", text)


def test_drills_only_pushed_where_the_finding_can_be_trusted():
    assert practise_this("top.posture", "attention") and practise_this("top.posture", "problem")
    assert not practise_this("impact.head", "attention") and practise_this("impact.head", "problem")
    assert not practise_this("swing.plane_vs_address_shaft@mid_back", "attention")
    assert practise_this("swing.plane_vs_address_shaft@downswing", "attention")
    assert not practise_this("address.hip_hinge", "good")


def test_no_verdict_no_card():
    assert coaching_for({"kind": "posture", "phase": "top", "verdict": {"status": "info", "mapping": {}}}, "top") is None


def _bundles():
    for root in SETS:
        if root.is_dir():
            yield from sorted(root.glob("*/analysis.json"))


@pytest.mark.skipif(not any(r.is_dir() for r in SETS), reason="validation set not present")
def test_validation_set_every_shown_checkpoint_gets_a_card_consistent_with_its_status():
    seen = 0
    for path in _bundles():
        analysis = json.loads(path.read_text(encoding="utf-8"))
        for c in checkpoints(analysis):
            card = c["coaching"]
            assert card is not None, (path, c["id"])
            assert (card["side"] == "good") == (c["status"] == "good"), (path, c["id"], c["status"], card["side"])
            assert "{" not in card["what"], card["what"]
            seen += 1
    assert seen > 300
