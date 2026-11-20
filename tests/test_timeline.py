"""Tests for the guided playback timeline and its payload contract."""

from __future__ import annotations

import json

import numpy as np
import pytest

from swingguru.auto_moments import (
    SOURCE_MANUAL,
    SOURCE_UNAVAILABLE,
    apply_manual_overrides,
    estimate_moments,
    to_phase_selection,
)
from swingguru.export import write_json
from swingguru.landmarks import PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.phases import PHASE_ORDER, Phase
from swingguru.pipeline import build_record
from swingguru.synthetic import joints_to_observation
from swingguru.ui import presentation
from swingguru.ui.guided_fixture import (
    GuidedSwingClip,
    guided_joints,
)
from swingguru.ui.timeline import build_timeline, timeline_payload

CLIP = GuidedSwingClip()


def _records(*, missing_frame: int | None = None):
    config = MeasurementConfig()
    records = []
    for index in range(CLIP.frame_count):
        frame = np.zeros((CLIP.height, CLIP.width, 3), dtype=np.uint8)
        if index == missing_frame:
            observation = PoseObservation.missing("scripted tracking loss")
        else:
            observation = joints_to_observation(guided_joints(index, clip=CLIP))
        records.append(
            build_record(
                index,
                index * 1000.0 / CLIP.fps,
                frame,
                observation,
                config=config,
            )
        )
    return records


def _timeline(records=None, moments=None, **kwargs):
    records = records if records is not None else _records()
    if moments is None:
        moments = estimate_moments(records)
    reference = moments.get(Phase.ADDRESS).frame_index
    if "reference_frame" in kwargs:
        reference = kwargs.pop("reference_frame")
    return build_timeline(
        records,
        moments,
        fps=CLIP.fps,
        measurement_config=MeasurementConfig(),
        reference_frame=reference,
        **kwargs,
    )


def test_every_moment_maps_to_ordered_checks_at_one_time() -> None:
    records = _records()
    moments = estimate_moments(records)
    timeline = _timeline(records, moments)
    assert timeline.events
    assert len(timeline.events) == len(moments.available_frames())
    assert timeline.check_ids() == tuple(sorted(set(timeline.check_ids()), key=timeline.check_ids().index))

    times = [event.time_s for event in timeline.events]
    assert times == sorted(times)
    for event in timeline.events:
        assert event.checks, event.phase
        assert {check.time_s for check in event.checks} == {event.time_s}
        assert {check.frame_index for check in event.checks} == {event.frame_index}
        assert {check.phase for check in event.checks} == {event.phase}


def test_address_shows_three_checks_with_the_human_labels() -> None:
    timeline = _timeline()
    address = timeline.events[0]
    assert address.phase is Phase.ADDRESS
    assert [check.label for check in address.checks] == [
        "Torso inclination",
        "Lead knee flex",
        "Trail knee flex",
    ]
    assert all(check.unit == "\u00b0" for check in address.checks)
    assert all(check.value_text != presentation.EM_DASH for check in address.checks)


def test_movement_checks_are_grouped_with_their_moment() -> None:
    timeline = _timeline()
    by_phase = {event.phase: event for event in timeline.events}
    top = by_phase[Phase.TOP]
    assert [check.label for check in top.checks] == [
        "Posture change",
        "Head movement",
        "Pelvis movement",
    ]
    assert top.checks[1].unit == "% of frame height"
    mid_back = by_phase[Phase.MID_BACK]
    assert [check.label for check in mid_back.checks] == [
        "Posture change",
        "Head movement",
    ]
    assert len({check.time_s for check in mid_back.checks}) == 1
    impact = by_phase[Phase.IMPACT]
    assert [check.label for check in impact.checks] == [
        "Posture change",
        "Head movement",
        "Pelvis movement",
    ]


def test_posture_change_draws_a_neutral_reference_line() -> None:
    timeline = _timeline()
    downswing = next(
        event for event in timeline.events if event.phase is Phase.DOWNSWING
    )
    posture = next(
        check
        for check in downswing.checks
        if check.measurement_key == "torso_angle_change_deg"
    )
    roles = {item.role for item in posture.geometry}
    assert presentation.ROLE_PRIMARY in roles
    assert presentation.ROLE_REFERENCE in roles
    assert all(
        0.0 <= value <= 1.0
        for item in posture.geometry
        for point in item.points
        for value in point
    )


def test_movement_without_a_reference_keeps_the_card_and_drops_geometry() -> None:
    timeline = _timeline(reference_frame=None)
    takeaway = next(
        event for event in timeline.events if event.phase is Phase.TAKEAWAY
    )
    movement = next(
        check
        for check in takeaway.checks
        if check.measurement_key.startswith("head_")
    )
    assert movement.valid is False
    assert movement.geometry == ()
    assert movement.value_text == presentation.EM_DASH
    assert movement.reason == "Choose Address to compare movement."
    torso = next(
        check for check in takeaway.checks if check.label == "Torso inclination"
    )
    assert torso.valid is True and torso.geometry


def test_a_lost_frame_moment_is_reported_and_drawn_without_geometry() -> None:
    records = _records(missing_frame=41)
    moments = estimate_moments(records)
    timeline = _timeline(records, moments, reference_frame=None)
    top = next(
        (event for event in timeline.events if event.phase is Phase.TOP), None
    )
    if top is not None:
        for check in top.checks:
            if not check.valid:
                assert check.geometry == ()
                assert check.reason


def test_manual_choice_flows_into_the_timeline_provenance() -> None:
    records = _records()
    moments = estimate_moments(records)
    manual_frame = int(moments.get(Phase.TOP).frame_index) + 3
    edited = apply_manual_overrides(moments, {Phase.TOP: manual_frame})
    timeline = _timeline(records, edited)
    top = next(event for event in timeline.events if event.phase is Phase.TOP)
    assert top.source == SOURCE_MANUAL
    assert top.provenance == "Selected manually"
    assert top.frame_index == manual_frame
    assert all(check.moment_source == SOURCE_MANUAL for check in top.checks)


def test_unavailable_moment_is_absent_from_the_timeline() -> None:
    records = _records()
    moments = estimate_moments(records)
    cleared = apply_manual_overrides(moments, {Phase.FINISH: None})
    timeline = _timeline(records, cleared)
    assert Phase.FINISH not in {event.phase for event in timeline.events}
    assert all(check.phase is not Phase.FINISH for check in timeline.checks)
    assert cleared.get(Phase.FINISH).source == SOURCE_UNAVAILABLE


def test_a_moment_outside_the_analysed_range_is_skipped_with_a_note() -> None:
    records = _records()
    moments = estimate_moments(records)
    edited = apply_manual_overrides(moments, {Phase.TOP: 5000})
    timeline = _timeline(records, edited)
    assert Phase.TOP not in {event.phase for event in timeline.events}
    assert any("not in the analysed range" in note for note in timeline.review_notes)


def test_payload_is_json_serialisable_for_the_component() -> None:
    timeline = _timeline()
    payload = timeline_payload(
        timeline,
        run_key="run",
        video_src="generated/preview.mp4",
        hold_seconds=1.8,
        poster=None,
        labels={"note": "Moments are estimates."},
    )
    encoded = json.dumps(payload)
    restored = json.loads(encoded)
    assert restored["events"]
    first = restored["events"][0]["checks"][0]
    assert isinstance(first["time_s"], float)
    assert first["geometry"]
    assert all(
        isinstance(point, list)
        for item in first["geometry"]
        for point in item["points"]
    )
    assert {
        "run_key",
        "video_src",
        "fps",
        "first_index",
        "last_index",
        "hold_seconds",
        "events",
    } <= set(restored)


def test_export_sidecar_preserves_mixed_provenance(tmp_path) -> None:
    records = _records()
    moments = estimate_moments(records)
    edited = apply_manual_overrides(
        moments, {Phase.IMPACT: int(moments.get(Phase.IMPACT).frame_index)}
    )
    path = write_json(edited.to_dict(), tmp_path / "moments.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources = {
        item["phase"]: item["source"] for item in payload["moments"]
    }
    assert sources["impact"] == "manual"
    assert sources["top"] == "estimated"
    assert payload["sequence"]["estimated_count"] >= 1
    assert payload["sequence"]["manual_count"] == 1

    selection = to_phase_selection(edited)
    assert selection.reference_frame == edited.get(Phase.ADDRESS).frame_index


def test_timeline_records_the_reference_source() -> None:
    records = _records()
    moments = estimate_moments(records)
    estimated = _timeline(records, moments)
    assert estimated.reference_source == "estimated"
    manual = apply_manual_overrides(
        moments, {Phase.ADDRESS: int(moments.get(Phase.ADDRESS).frame_index)}
    )
    edited = _timeline(records, manual)
    assert edited.reference_source == "manual"
