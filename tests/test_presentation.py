"""Tests for the golfer-facing presentation layer.

The presentation module is the seam between the engine's measured records and
what a golfer reads on screen. These tests pin the two guarantees that matter
most there: the checkpoint mapping only uses measurements the engine really
produces, and the hero still shows green geometry for nothing but a valid
measurement.
"""

from __future__ import annotations

import re

import numpy as np
import pytest

from swingguru.landmarks import PoseObservation
from swingguru.measurements import (
    MEASUREMENT_KEYS,
    W_DEGENERATE,
    W_LANDMARK_MISSING,
    W_LOW_VISIBILITY,
    W_NO_POSE,
    W_NO_REFERENCE,
    W_OUT_OF_FRAME,
    W_PRESENCE_MISSING,
    W_SINGLE_FRAME,
    W_VIEWPOINT_UNKNOWN,
    MeasurementConfig,
    MeasurementResult,
)
from swingguru.phases import PHASE_ORDER
from swingguru.pipeline import apply_reference, build_record
from swingguru import overlays as engine_overlays
from swingguru.synthetic import (
    ScriptedPoseProvider,
    SyntheticClip,
    draw_synthetic_frame,
)
from swingguru.ui import presentation

CLIP = SyntheticClip(width=320, height=180, fps=24.0, frame_count=24)


def _records(count: int = 6, config: MeasurementConfig | None = None):
    settings = config or MeasurementConfig()
    provider = ScriptedPoseProvider(clip=CLIP)
    records = []
    for index in range(count):
        frame = draw_synthetic_frame(index, clip=CLIP)
        timestamp = index * 1000.0 / CLIP.fps
        observation = provider.detect(frame, timestamp)
        records.append(
            build_record(index, timestamp, frame, observation, config=settings)
        )
    return records


def _lost_record(config: MeasurementConfig | None = None):
    settings = config or MeasurementConfig()
    frame = draw_synthetic_frame(0, clip=CLIP)
    return build_record(
        0,
        0.0,
        frame,
        PoseObservation.missing("scripted tracking loss"),
        config=settings,
    )


def _result(key: str, warnings: tuple[str, ...], *, valid: bool) -> MeasurementResult:
    spec = presentation.SPECS_BY_KEY[key]
    return MeasurementResult(
        key=key,
        label=spec.label,
        value=0.0 if valid else None,
        unit=spec.unit,
        valid=valid,
        warnings=warnings,
    )


def test_checkpoints_cover_the_ordered_phases_once() -> None:
    assert [checkpoint.phase for checkpoint in presentation.CHECKPOINTS] == list(
        PHASE_ORDER
    )
    titles = [checkpoint.title for checkpoint in presentation.CHECKPOINTS]
    assert titles == [
        "Address",
        "Takeaway",
        "Mid-Back",
        "Top",
        "Mid-Down",
        "Impact",
        "Finish",
    ]
    for index, title in enumerate(titles):
        assert presentation.progress_caption(index) == f"Checkpoint {index + 1} of 7"


def test_checkpoint_measurements_exist_in_the_engine_and_stay_between_one_and_three() -> (
    None
):
    for checkpoint in presentation.CHECKPOINTS:
        keys = [item.key for item in checkpoint.measurements]
        assert 1 <= len(keys) <= 3, f"{checkpoint.title} shows {len(keys)} rows"
        assert len(set(keys)) == len(keys)
        for item in checkpoint.measurements:
            assert item.key in MEASUREMENT_KEYS
            assert "_" not in item.label, "labels are golfer-facing words"
            assert item.unit == presentation.SPECS_BY_KEY[item.key].unit
            assert presentation.unit_label(item.unit) in {
                "\u00b0",
                "px",
                "% of frame height",
            }


def test_valid_measurements_format_with_explicit_units() -> None:
    records = _records()
    apply_reference(records, 0, config=MeasurementConfig())
    rows = presentation.measurement_rows(records[4], presentation.CHECKPOINTS[0])
    assert [row.label for row in rows] == [
        "Torso inclination",
        "Lead knee flex",
        "Trail knee flex",
    ]
    for row in rows:
        assert row.valid and not row.needs_review
        assert re.fullmatch(r"-?\d+\.\d", row.value_text), row.value_text
        assert row.unit == "\u00b0"
        assert row.reason is None


def test_unavailable_measurements_show_an_em_dash_and_never_zero() -> None:
    record = _lost_record()
    rows = presentation.measurement_rows(record, presentation.CHECKPOINTS[0])
    assert len(rows) == 3
    for row in rows:
        assert row.needs_review and not row.valid
        assert row.value_text == presentation.EM_DASH
        assert "0" not in row.value_text
        assert row.reason == "We could not find the golfer in this frame."
        assert any(W_NO_POSE in warning for warning in row.technical)


def test_movement_needs_an_address_frame_and_says_so() -> None:
    records = _records()
    apply_reference(records, None, config=MeasurementConfig())
    checkpoint = presentation.CHECKPOINT_BY_PHASE[PHASE_ORDER[2]]
    rows = presentation.measurement_rows(records[4], checkpoint)
    movement = next(row for row in rows if row.label == "Head movement")
    assert movement.needs_review
    assert movement.value_text == presentation.EM_DASH
    assert movement.reason == "Choose Address to compare movement."

    apply_reference(records, 0, config=MeasurementConfig())
    rows = presentation.measurement_rows(records[4], checkpoint)
    movement = next(row for row in rows if row.label == "Head movement")
    assert movement.valid
    assert movement.unit == "% of frame height"


def test_clean_still_draws_only_measurement_geometry() -> None:
    records = _records()
    apply_reference(records, 0, config=MeasurementConfig())
    frame = np.full((180, 320, 3), 40, dtype=np.uint8)
    still = presentation.render_checkpoint_still(
        frame, records[4], presentation.CHECKPOINTS[0]
    )
    assert still.shape == frame.shape
    colours = {tuple(int(channel) for channel in row) for row in np.unique(
        still.reshape(-1, 3), axis=0
    )}
    debug_colours = {
        engine_overlays.COLOUR_GOOD,
        engine_overlays.COLOUR_FAIR,
        engine_overlays.COLOUR_POOR,
        engine_overlays.COLOUR_JOINT,
        engine_overlays.COLOUR_TORSO,
        engine_overlays.COLOUR_HEAD,
        engine_overlays.COLOUR_PELVIS,
        engine_overlays.COLOUR_ALERT,
    }
    assert not (colours & debug_colours), colours & debug_colours
    assert presentation.COLOUR_MEASUREMENT in colours
    assert presentation.COLOUR_MEASUREMENT_DARK not in colours
    green = int(np.count_nonzero(np.all(still == presentation.COLOUR_MEASUREMENT, axis=2)))
    assert green > 0, "valid measurements should draw green geometry"


def test_clean_still_draws_nothing_when_the_frame_has_no_pose() -> None:
    record = _lost_record()
    frame = np.full((180, 320, 3), 40, dtype=np.uint8)
    still = presentation.render_checkpoint_still(
        frame, record, presentation.CHECKPOINTS[0]
    )
    assert np.array_equal(still, frame)
    assert still is not frame


def test_clean_still_adds_movement_geometry_only_with_a_reference() -> None:
    records = _records()
    checkpoint = presentation.CHECKPOINT_BY_PHASE[PHASE_ORDER[1]]
    frame = np.full((180, 320, 3), 40, dtype=np.uint8)

    apply_reference(records, None, config=MeasurementConfig())
    without = presentation.render_checkpoint_still(frame, records[4], checkpoint)
    apply_reference(records, 0, config=MeasurementConfig())
    with_reference = presentation.render_checkpoint_still(
        frame,
        records[4],
        checkpoint,
        config=MeasurementConfig(),
    )

    def green(image: np.ndarray) -> int:
        return int(
            np.count_nonzero(
                np.all(image == presentation.COLOUR_MEASUREMENT, axis=2)
            )
        )

    assert green(without) > 0, "the torso angle is still valid without a reference"
    assert green(with_reference) > green(without)
    assert np.count_nonzero(
        np.all(with_reference == presentation.COLOUR_REFERENCE, axis=2)
    ) > 0, "the reference position is marked separately"


def test_clean_still_rejects_an_empty_frame() -> None:
    records = _records()
    with pytest.raises(ValueError):
        presentation.render_checkpoint_still(
            np.zeros((0, 0, 3), dtype=np.uint8),
            records[0],
            presentation.CHECKPOINTS[0],
        )


def test_friendly_reasons_cover_the_engine_failure_warnings() -> None:
    cases = {
        W_NO_POSE: "We could not find the golfer in this frame.",
        W_NO_REFERENCE: "Choose Address to compare movement.",
        W_LANDMARK_MISSING: "Part of the body was hidden in this frame.",
        W_OUT_OF_FRAME: "The golfer is partly outside the frame.",
        W_LOW_VISIBILITY: "Too much of the body is hidden to measure this.",
        W_PRESENCE_MISSING: "This frame's body position was not reliable enough.",
        W_DEGENERATE: "The body position in this frame was too collapsed to measure.",
    }
    for warning, message in cases.items():
        result = _result("torso_inclination_deg", (warning,), valid=False)
        assert presentation.friendly_reason(result) == message
        assert presentation.technical_reasons(result) == (warning,)


def test_viewpoint_context_is_not_treated_as_a_failure() -> None:
    result = _result(
        "torso_inclination_deg",
        (W_VIEWPOINT_UNKNOWN, W_SINGLE_FRAME),
        valid=True,
    )
    assert presentation.friendly_reason(result) is None
    assert presentation.measurement_warnings(result) == ()
    assert presentation.is_context_warning(W_VIEWPOINT_UNKNOWN)
    assert not presentation.is_context_warning(W_NO_POSE)


def test_unknown_warnings_still_produce_a_plain_language_reason() -> None:
    result = _result("torso_inclination_deg", ("something new: detail",), valid=False)
    reason = presentation.friendly_reason(result)
    assert reason is not None and "could not be completed" in reason


def test_selection_wording_is_honest_about_manual_choices() -> None:
    unselected = presentation.selection_caption("address", None)
    assert "chosen yet" in unselected
    assert "app has not picked one for you" in unselected
    selected = presentation.selection_caption("address", 12)
    assert "Chosen manually at frame 12" in selected
    for caption in (unselected, selected):
        assert "detected" not in caption.lower()
        assert "automatic" not in caption.lower()


def test_normal_flow_wording_avoids_engine_jargon() -> None:
    banned = (
        "landmark",
        "visibility",
        "presence",
        "mediapipe",
        "provider",
        "inference",
        "skeleton",
        "threshold",
        "confidence",
    )
    strings: list[str] = [presentation.CAMERA_NOTE]
    for checkpoint in presentation.CHECKPOINTS:
        strings.extend(
            [checkpoint.title, checkpoint.prompt, checkpoint.description]
        )
        strings.extend(item.label for item in checkpoint.measurements)
    strings.append(presentation.selection_caption("address", None))
    strings.append(presentation.selection_caption("address", 3))
    strings.append(presentation.progress_caption(0))
    for warning in (
        W_NO_POSE,
        W_NO_REFERENCE,
        W_LANDMARK_MISSING,
        W_OUT_OF_FRAME,
        W_LOW_VISIBILITY,
        W_PRESENCE_MISSING,
        W_DEGENERATE,
        W_VIEWPOINT_UNKNOWN,
    ):
        reason = presentation.friendly_reason(
            _result("torso_inclination_deg", (warning,), valid=False)
        )
        if reason is not None:
            strings.append(reason)

    for text in strings:
        lowered = text.lower()
        for term in banned:
            assert term not in lowered, f"{text!r} contains {term!r}"


def test_checkpoint_index_helpers_clamp_and_do_not_wrap() -> None:
    assert presentation.clamp_checkpoint_index(-3) == 0
    assert presentation.clamp_checkpoint_index(99) == 6
    assert presentation.clamp_checkpoint_index("broken") == 0
    assert not presentation.has_previous(0)
    assert presentation.has_next(0)
    assert presentation.has_previous(6)
    assert not presentation.has_next(6)
    assert presentation.checkpoint_index("impact") == 5


def test_palette_colour_conversion_matches_the_visual_direction() -> None:
    assert presentation.PALETTE["page"] == "#F7F7F2"
    assert presentation.PALETTE["card"] == "#FFFFFF"
    assert presentation.PALETTE["green"] == "#4F7F5F"
    assert presentation.PALETTE["green_dark"] == "#365B43"
    assert presentation.PALETTE["text"] == "#292929"
    assert presentation.hex_to_bgr("#4F7F5F") == (95, 127, 79)
    assert presentation.COLOUR_MEASUREMENT == presentation.hex_to_bgr(
        presentation.PALETTE["green"]
    )
    with pytest.raises(ValueError):
        presentation.hex_to_bgr("#FFF")
