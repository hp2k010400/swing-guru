"""Manual phase assignment and the interface reserved for auto detection."""

from __future__ import annotations

import pytest

from swingguru.phases import (
    PHASE_ORDER,
    ManualPhaseProvider,
    Phase,
    PhaseSelection,
    UnavailableAutoPhaseProvider,
    coerce_phase,
    parse_phase_assignments,
)


def test_coerce_phase() -> None:
    assert coerce_phase("Top") is Phase.TOP
    assert coerce_phase(Phase.IMPACT) is Phase.IMPACT
    with pytest.raises(ValueError):
        coerce_phase("backswing")


def test_parse_phase_assignments() -> None:
    parsed = parse_phase_assignments(["address=12", "top = 40", ""])
    assert parsed == {Phase.ADDRESS: 12, Phase.TOP: 40}
    with pytest.raises(ValueError):
        parse_phase_assignments(["address"])
    with pytest.raises(ValueError):
        parse_phase_assignments(["address=top"])
    with pytest.raises(ValueError):
        parse_phase_assignments(["address=-3"])


def test_manual_provider_derives_reference_from_address() -> None:
    provider = ManualPhaseProvider(frames={Phase.ADDRESS: 4, Phase.TOP: 30})
    selection = provider.select()
    assert selection.reference_frame == 4
    assert selection.source == "manual"
    assert any("derived" in warning for warning in selection.warnings)


def test_manual_provider_can_leave_the_reference_unset() -> None:
    provider = ManualPhaseProvider(
        frames={Phase.ADDRESS: 4}, derive_reference_from_address=False
    )
    selection = provider.select()
    assert selection.reference_frame is None
    assert any("no reference frame" in warning for warning in selection.warnings)


def test_explicit_reference_wins_over_the_address_frame() -> None:
    provider = ManualPhaseProvider(
        frames={Phase.ADDRESS: 4}, reference_frame=9
    )
    assert provider.select().reference_frame == 9


def test_validation_flags_out_of_range_duplicate_and_order() -> None:
    selection = PhaseSelection(
        frames={
            Phase.ADDRESS: 20,
            Phase.TOP: 5,
            Phase.DOWNSWING: 5,
        },
        reference_frame=99,
    )
    issues = selection.validate(range(10))
    text = " | ".join(issues)
    assert "outside the processed frames" in text
    assert "share frame" in text
    assert "is before" in text


def test_validation_is_quiet_for_a_sane_selection() -> None:
    selection = PhaseSelection(
        frames={Phase.ADDRESS: 0, Phase.TOP: 10, Phase.IMPACT: 20},
        reference_frame=0,
    )
    assert selection.validate(range(30)) == ()


def test_validation_rejects_negative_indices() -> None:
    selection = PhaseSelection(
        frames={Phase.ADDRESS: -1}, reference_frame=-2
    )
    issues = selection.validate(range(5))
    assert any("address frame -1 is negative" in issue for issue in issues)
    assert any("reference frame -2 is negative" in issue for issue in issues)


def test_validation_uses_the_frames_actually_processed() -> None:
    # A run that started at frame 5 holds 5..12, so frame 7 is available and
    # frame 0 is not, even though 0 exists in the source video.
    available = list(range(5, 13))
    good = PhaseSelection(frames={Phase.ADDRESS: 7}, reference_frame=7)
    assert good.validate(available) == ()

    unavailable = PhaseSelection(frames={}, reference_frame=0)
    issues = unavailable.validate(available)
    assert any(
        "reference frame 0 is outside the processed frames (5..12)" in issue
        for issue in issues
    )


def test_selection_json_round_trip() -> None:
    selection = PhaseSelection(
        frames={Phase.ADDRESS: 0, Phase.FINISH: 40},
        reference_frame=0,
        warnings=("something",),
    )
    payload = selection.to_dict()
    restored = PhaseSelection.from_dict(payload)
    assert restored.reference_frame == 0
    assert restored.phase_frame(Phase.ADDRESS) == 0
    assert restored.phase_frame(Phase.TOP) is None
    assert restored.warnings == ("something",)
    assert list(payload["frames"]) == [phase.value for phase in PHASE_ORDER]


def test_ordered_frames_follow_swing_order() -> None:
    selection = PhaseSelection(
        frames={Phase.FINISH: 40, Phase.ADDRESS: 0, Phase.TOP: 20}
    )
    assert [phase for phase, _ in selection.ordered_frames()] == [
        Phase.ADDRESS,
        Phase.TOP,
        Phase.FINISH,
    ]


def test_automatic_phase_detection_is_explicitly_unavailable() -> None:
    provider = UnavailableAutoPhaseProvider()
    assert provider.requires_records
    with pytest.raises(NotImplementedError) as info:
        provider.select([])
    assert "not implemented" in str(info.value)


def test_mid_back_and_mid_down_names_are_accepted() -> None:
    from swingguru.phases import PHASE_LABELS, coerce_phase

    assert coerce_phase("mid-back") is Phase.MID_BACK
    assert coerce_phase("mid_back") is Phase.MID_BACK
    assert coerce_phase("mid-down") is Phase.DOWNSWING
    assert coerce_phase("downswing") is Phase.DOWNSWING
    assert PHASE_LABELS[Phase.DOWNSWING] == "Mid-Down"
    assert PHASE_ORDER.index(Phase.TAKEAWAY) < PHASE_ORDER.index(Phase.MID_BACK)
    assert PHASE_ORDER.index(Phase.MID_BACK) < PHASE_ORDER.index(Phase.TOP)
