"""Golfer-facing checkpoints and verdicts: provisional, explicit, honest about capability."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from swingguru.verdicts import (
    ATTENTION,
    CHECKPOINTS,
    GOOD,
    INFO,
    MAPPINGS,
    PROBLEM,
    PROVISIONAL,
    UNSUPPORTED_CHECKPOINTS,
    attach_verdicts,
    unsupported_claims,
    verdict_for,
)


def _check(kind, phase, raw=None, level="HIGH", **extra):
    return {"kind": kind, "phase": phase, "raw": raw or {}, "level": level, "value": "x", "qualifier": "",
            "title": kind.title(), "summary": "", "guided": True, **extra}


@pytest.mark.parametrize("kind, phase, raw, status, label", [
    ("torso_angle", "address", {"torso_inclination_deg": 46.0}, GOOD, "Hip hinge good"),
    ("torso_angle", "address", {"torso_inclination_deg": 25.0}, ATTENTION, "Slightly upright"),
    ("trail_knee", "address", {"knee_flex_deg": 18.0}, GOOD, "Knee bend good"),
    ("trail_knee", "address", {"knee_flex_deg": 3.0}, PROBLEM, "Too straight"),
    ("arm_hang", "address", {"arm_angle_deg": -5.3}, GOOD, "Arm hang good"),
    ("arm_hang", "address", {"arm_angle_deg": 14.7}, ATTENTION, "Slightly reaching"),
    ("arm_hang", "address", {"arm_angle_deg": 22.1}, PROBLEM, "Reaching"),
    ("arm_hang", "address", {"arm_angle_deg": -13.4}, ATTENTION, "Slightly crowded"),
    ("shaft", "mid_back", {"change_vs_address_deg": -2.0}, GOOD, "On plane"),
    ("shaft", "mid_back", {"change_vs_address_deg": -11.8}, ATTENTION, "Slightly shallow"),
    ("shaft", "downswing", {"change_vs_address_deg": 8.8}, ATTENTION, "Slightly steep"),
    ("shaft", "downswing", {"change_vs_address_deg": -15.2}, PROBLEM, "Too shallow"),
    ("posture", "top", {"torso_angle_change_deg": -8.8}, ATTENTION, "Slight loss of posture"),
    ("posture", "top", {"torso_angle_change_deg": -1.7}, GOOD, "Posture maintained"),
    ("head", "impact", {"movement_pct_height": 3.7}, GOOD, "Head steady"),
    ("head", "impact", {"movement_pct_height": 7.1}, ATTENTION, "Slight head movement"),
])
def test_bands_map_measurements_to_golf_wording(kind, phase, raw, status, label) -> None:
    verdict = verdict_for(_check(kind, phase, raw))
    assert (verdict["status"], verdict["label"]) == (status, label)
    assert verdict["mapping"]["validation"] == PROVISIONAL
    assert verdict["mapping"]["input"] == pytest.approx(next(iter(raw.values())))


@pytest.mark.parametrize("rear, status, label", [
    (1.7, GOOD, "Posture maintained"), (4.8, ATTENTION, "Slight early extension"), (6.4, PROBLEM, "Early extension"),
])
def test_early_extension_uses_the_tush_line(rear, status, label) -> None:
    check = _check("pelvis", "impact", {"movement_pct_height": 1.0},
                   pelvis_depth={"displacement_pct_height": rear, "direction": "toward the ball", "level": "HIGH"})
    verdict = verdict_for(check)
    assert (verdict["status"], verdict["label"]) == (status, label)
    assert verdict["value"] == f"{rear:.1f}%"  # the secondary value is the number the verdict used


def test_no_tush_line_means_no_extension_verdict() -> None:
    # e.g. the camera moved: the rear pelvis was not measured, the hip midpoint is not used.
    assert verdict_for(_check("pelvis", "impact", {"movement_pct_height": 9.0}))["status"] == INFO


def test_implausible_plane_input_is_a_tracking_error_not_a_verdict() -> None:
    verdict = verdict_for(_check("shaft", "mid_back", {"change_vs_address_deg": -57.0}))
    assert verdict["status"] == INFO and "tracking error" in verdict["mapping"]["reason"]


@pytest.mark.parametrize("kind, phase", [
    ("shaft", "address"), ("shaft", "takeaway"), ("shaft", "top"), ("shaft", "impact"), ("posture", "mid_back"),
])
def test_no_invented_verdicts_outside_the_checkpoints(kind, phase) -> None:
    verdict = verdict_for(_check(kind, phase, {"change_vs_address_deg": 3.0, "torso_angle_change_deg": 1.0}))
    assert verdict["status"] == INFO
    assert verdict["label"] == kind.title()  # the plain measurement name, never a made-up phrase


def test_band_edges_are_contiguous_and_open_ended() -> None:
    for mapping in MAPPINGS:
        assert mapping.bands[0].low is None and mapping.bands[-1].high is None, mapping.mapping_id
        for a, b in zip(mapping.bands, mapping.bands[1:]):
            assert a.high == b.low, mapping.mapping_id


def test_every_mapping_states_its_basis_and_approximation() -> None:
    for mapping in MAPPINGS:
        assert mapping.basis and mapping.approximation and mapping.validation == PROVISIONAL


def test_the_walkthrough_is_the_fixed_checkpoint_list() -> None:
    names = [(cp.phase, cp.name) for cp in CHECKPOINTS]
    assert names == [("address", "Hip hinge"), ("address", "Knee bend"), ("address", "Arm hang"),
                     ("mid_back", "Plane"), ("top", "Posture"), ("downswing", "Plane"),
                     ("impact", "Extension"), ("impact", "Head")]
    assert {"takeaway.plane", "top.hand_depth"} <= set(UNSUPPORTED_CHECKPOINTS)


def test_no_label_claims_capability_the_engine_lacks() -> None:
    for mapping in MAPPINGS:
        for band in mapping.bands:
            assert unsupported_claims(band.label + " " + band.support) == [], band.label
    assert unsupported_claims("Face open and path out to in") == ["face", "path"]


def _analysis():
    return {"summary": {"guided_checks": 0}, "phases": [
        {"phase": "address", "checks": [
            _check("trail_knee", "address", {"knee_flex_deg": 18.0}),
            _check("shaft", "address", {"shaft_from_horizontal_deg": 55.0}),
            _check("torso_angle", "address", {"torso_inclination_deg": 46.0}, guided=False),
        ]},
        {"phase": "takeaway", "checks": [_check("shaft", "takeaway", {"change_vs_address_deg": -40.0})]},
        {"phase": "impact", "checks": [
            _check("posture", "impact", {"torso_angle_change_deg": -19.7}),
            _check("head", "impact", {"movement_pct_height": 7.1}, guided=False),
        ]},
    ]}


def test_attach_selects_checkpoints_orders_them_and_never_changes_measurements() -> None:
    analysis = _analysis()
    before = copy.deepcopy(analysis)
    out = attach_verdicts(analysis)
    address = out["phases"][0]["checks"]
    assert [c["kind"] for c in address[:2]] == ["torso_angle", "trail_knee"]  # checkpoint order
    assert [c["checkpoint"] for c in address] == ["Hip hinge", "Knee bend", None]
    assert [c["guided"] for c in address] == [True, True, False]  # the address shaft is not a checkpoint
    assert not any(c["guided"] for c in out["phases"][1]["checks"])  # no takeaway checkpoint
    impact = {c["kind"]: c for c in out["phases"][2]["checks"]}
    assert impact["head"]["guided"] and not impact["posture"]["guided"]
    assert impact["posture"]["guided_engine"] is True  # the engine's own choice is kept
    assert out["phases"][2]["status"] == ATTENTION
    assert out["summary"]["guided_checks"] == 3
    measured = lambda a: [{k: v for k, v in c.items() if k not in ("verdict", "checkpoint", "guided", "guided_engine")}
                          for p in a["phases"] for c in sorted(p["checks"], key=lambda c: c["kind"])]
    assert measured(out) == measured(before)
    assert attach_verdicts(copy.deepcopy(out)) == out  # idempotent


def test_verdicts_stay_out_of_the_interpretation_layer() -> None:
    for path in (Path(__file__).resolve().parents[1] / "src" / "swingguru" / "interpretation").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "swingguru.verdicts" not in text and "from ..verdicts" not in text, path


LOCKED = {
    "Hip hinge": {"Hip hinge good", "Slightly upright", "Too upright", "Slightly bent over", "Too bent over"},
    "Knee bend": {"Knee bend good", "Slightly straight", "Too straight", "Slightly flexed", "Too much knee bend"},
    "Arm hang": {"Arm hang good", "Slightly reaching", "Reaching", "Slightly crowded", "Too crowded"},
    "Plane": {"On plane", "Slightly shallow", "Too shallow", "Slightly steep", "Too steep"},
    "Posture": {"Posture maintained", "Slight loss of posture", "Loss of posture"},
    "Extension": {"Posture maintained", "Slight early extension", "Early extension"},
    "Head": {"Head steady", "Slight head movement", "Excessive head movement"},
}


def test_the_vocabulary_is_locked() -> None:
    for cp in CHECKPOINTS:
        assert {band.label for band in cp.mapping.bands} == LOCKED[cp.name], cp.name


def test_each_shown_verdict_records_id_phase_sources_band_confidence_and_status() -> None:
    check = _check("shaft", "mid_back", {"change_vs_address_deg": -15.2}, id="mid_back:shaft", level="MEDIUM")
    verdict = verdict_for(check)
    assert verdict["verdict_id"] == "swing.plane_vs_address_shaft.too_shallow"
    assert verdict["phase"] == "mid_back" and verdict["sources"][0].startswith("mid_back:shaft")
    assert verdict["band"] == {"from": None, "to": -12, "status": PROBLEM, "label": "Too shallow"}
    assert verdict["confidence"] == "MEDIUM" and verdict["mapping"]["validation"] == PROVISIONAL


def test_unsupported_vocabulary_is_recorded_but_never_a_checkpoint() -> None:
    from swingguru.verdicts import UNSUPPORTED_VERDICTS

    names = {u["check"] for u in UNSUPPORTED_VERDICTS}
    assert names == {"Takeaway plane", "Hand depth"}
    assert all(u["validation"] == "unsupported" for u in UNSUPPORTED_VERDICTS)
    shown = {band.label for cp in CHECKPOINTS for band in cp.mapping.bands}
    assert not ({"Slightly inside", "Inside", "Outside", "Hand depth good", "Too deep"} & shown)


def test_the_product_doc_matches_the_code() -> None:
    import sys

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import verdict_matrix

    doc = (root / "docs" / "product" / "VERDICT_LANGUAGE.md").read_text(encoding="utf-8")
    assert doc == verdict_matrix.render(), "run scripts/verdict_matrix.py after changing verdicts.py"
