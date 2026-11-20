"""Tests for the experimental automatic moment estimates.

The detector is tested on physically motivated movement series that are built
for each case, with cue positions that are deliberately *not* evenly spaced and
that change between cases, so an implementation that inferred phases from video
percentages would fail here. The guided fixture is used for end-to-end sanity
only; it is not evidence about real golf footage.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.auto_moments import (
    SOURCE_ESTIMATED,
    SOURCE_MANUAL,
    SOURCE_UNAVAILABLE,
    MomentConfig,
    apply_manual_overrides,
    build_motion_series,
    estimate_moments,
    moment_summary,
    to_phase_selection,
)
from swingguru.landmarks import Landmark, PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.phases import PHASE_ORDER, Phase
from swingguru.pipeline import build_record
from swingguru.synthetic import ScriptedPoseProvider, SyntheticClip, draw_synthetic_frame
from swingguru.ui.guided_fixture import (
    GuidedPoseProvider,
    GuidedSwingClip,
    draw_guided_frame,
)

SIZE = (640, 360)

# Default timetable: still set-up, takeaway, top, downswing, impact, finish.
DEFAULT_CUES = (0.20, 0.30, 0.46, 0.62, 0.72, 0.88)
HAND_PATH = (
    (-0.10, 0.55),
    (-0.40, 0.45),
    (0.60, -0.60),
    (0.15, 0.05),
    (-0.05, 0.55),
    (0.25, -0.45),
)


def _smoothstep(value: float) -> float:
    clamped = min(1.0, max(0.0, value))
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _hand_offset(
    t: float, cues: tuple[float, ...], path: tuple[tuple[float, float], ...] = HAND_PATH
) -> tuple[float, float]:
    """Hand offset from the pelvis in torso lengths, for a scripted timetable."""

    hold_end, takeaway, top, downswing, impact, finish = cues
    keys = [
        (0.0, path[0]),
        (hold_end, path[0]),
        (takeaway, path[1]),
        (top, path[2]),
        (downswing, path[3]),
        (impact, path[4]),
        (finish, path[5]),
        (1.0, path[5]),
    ]
    if t <= keys[0][0]:
        return keys[0][1]
    for (start_t, start_v), (end_t, end_v) in zip(keys, keys[1:]):
        if t <= end_t:
            span = max(end_t - start_t, 1e-9)
            weight = _smoothstep((t - start_t) / span)
            return (
                start_v[0] + (end_v[0] - start_v[0]) * weight,
                start_v[1] + (end_v[1] - start_v[1]) * weight,
            )
    return keys[-1][1]


def _observation(
    hands: tuple[float, float] | None,
    *,
    torso_units: float = 0.20,
    wrist_visibility: float = 0.95,
) -> PoseObservation:
    """A pose observation with a fixed body and scripted hands."""

    pelvis_x, pelvis_y = 0.50, 0.60
    shoulder_x, shoulder_y = 0.52, 0.44
    rows = {
        "nose": (0.53, 0.40),
        "left_shoulder": (shoulder_x - 0.04, shoulder_y),
        "right_shoulder": (shoulder_x + 0.04, shoulder_y),
        "left_hip": (pelvis_x - 0.035, pelvis_y),
        "right_hip": (pelvis_x + 0.035, pelvis_y),
        "left_knee": (pelvis_x - 0.03, pelvis_y + 0.16),
        "right_knee": (pelvis_x + 0.03, pelvis_y + 0.16),
        "left_ankle": (pelvis_x - 0.03, pelvis_y + 0.31),
        "right_ankle": (pelvis_x + 0.03, pelvis_y + 0.31),
    }
    if hands is not None:
        hand_x = pelvis_x + hands[0] * torso_units
        hand_y = pelvis_y + hands[1] * torso_units
        rows["left_wrist"] = (hand_x - 0.012, hand_y)
        rows["right_wrist"] = (hand_x + 0.012, hand_y)

    index_by_name = {
        "nose": 0,
        "left_shoulder": 11,
        "right_shoulder": 12,
        "left_wrist": 15,
        "right_wrist": 16,
        "left_hip": 23,
        "right_hip": 24,
        "left_knee": 25,
        "right_knee": 26,
        "left_ankle": 27,
        "right_ankle": 28,
    }
    landmarks = []
    for name, index in index_by_name.items():
        entry = rows.get(name)
        if entry is None:
            continue
        x, y = entry
        visibility = wrist_visibility if "wrist" in name else 0.95
        landmarks.append(
            Landmark(
                index=index,
                name=name,
                x=float(x),
                y=float(y),
                z=0.0,
                visibility=visibility,
                presence=visibility,
            )
        )
    return PoseObservation(detected=True, landmarks=tuple(landmarks))


def _records(
    cues: tuple[float, ...] = DEFAULT_CUES,
    *,
    frame_count: int = 90,
    fps: float = 30.0,
    size: tuple[int, int] = SIZE,
    gap: tuple[int, int] | None = None,
    nonfinite_frames: tuple[int, ...] = (),
    start_fraction: float = 0.0,
    path: tuple[tuple[float, float], ...] = HAND_PATH,
    **config_kwargs: object,
):
    """FrameRecords whose hands follow the timetable of the given cues."""

    config = MeasurementConfig(**config_kwargs)  # type: ignore[arg-type]
    records = []
    first = int(round(start_fraction * (frame_count - 1)))
    for index in range(first, frame_count):
        t = index / (frame_count - 1)
        hands = _hand_offset(t, cues, path)
        if gap is not None and gap[0] <= index < gap[1]:
            hands = None
        if index in nonfinite_frames and hands is not None:
            hands = (math.nan, hands[1])
        frame = np.zeros((size[1], size[0], 3), dtype=np.uint8)
        records.append(
            build_record(
                index,
                index * 1000.0 / fps,
                frame,
                _observation(hands),
                config=config,
            )
        )
    return records


def _estimated_frames(records, **kwargs) -> dict[Phase, int | None]:
    moment_set = estimate_moments(records, **kwargs)
    return {estimate.phase: estimate.frame_index for estimate in moment_set.estimates}


@pytest.mark.parametrize(
    "cues,frame_count,fps,size",
    [
        (DEFAULT_CUES, 90, 30.0, (640, 360)),
        ((0.35, 0.45, 0.58, 0.70, 0.78, 0.92), 150, 60.0, (720, 1280)),
        ((0.12, 0.22, 0.40, 0.55, 0.66, 0.80), 120, 24.0, (1280, 720)),
    ],
)
def test_moments_follow_the_scripted_timetable(
    cues: tuple[float, ...], frame_count: int, fps: float, size: tuple[int, int]
) -> None:
    records = _records(cues, frame_count=frame_count, fps=fps, size=size)
    frames = _estimated_frames(records)
    span = frame_count - 1
    # Geometry-pinned cues must land within a couple of frames of the scripted
    # moment, at every frame rate and aspect.
    for phase, fraction in (
        (Phase.TOP, cues[2]),
        (Phase.IMPACT, cues[4]),
    ):
        expected = fraction * span
        assert frames[phase] is not None, phase
        assert abs(frames[phase] - expected) <= 3, (phase, frames[phase], expected)
    # Address is the last still frame before the departure; takeaway follows it.
    assert frames[Phase.ADDRESS] is not None
    assert frames[Phase.TAKEAWAY] is not None
    assert cues[0] * span - 2 <= frames[Phase.ADDRESS] <= cues[1] * span
    assert frames[Phase.ADDRESS] < frames[Phase.TAKEAWAY] < frames[Phase.MID_BACK]
    assert frames[Phase.MID_BACK] < frames[Phase.TOP]
    # The remaining cues must be ordered and inside their scripted windows.
    assert frames[Phase.TOP] < frames[Phase.DOWNSWING] < frames[Phase.IMPACT]
    assert frames[Phase.IMPACT] < frames[Phase.FINISH] <= span
    assert abs(frames[Phase.DOWNSWING] - cues[3] * span) <= 6
    assert abs(frames[Phase.FINISH] - cues[5] * span) <= 8


def test_estimates_are_not_video_percentages() -> None:
    """The same shape at different times must move with the motion."""

    early = _estimated_frames(_records((0.10, 0.20, 0.35, 0.50, 0.60, 0.75)))
    late = _estimated_frames(_records((0.45, 0.55, 0.68, 0.80, 0.88, 0.96)))
    for phase in (Phase.TOP, Phase.DOWNSWING, Phase.IMPACT, Phase.FINISH):
        assert early[phase] is not None and late[phase] is not None
        assert late[phase] - early[phase] > 15, (phase, early[phase], late[phase])


def test_handedness_does_not_change_the_recovered_moments() -> None:
    right = _estimated_frames(_records(handedness="right"))
    left = _estimated_frames(_records(handedness="left"))
    for phase in PHASE_ORDER:
        assert right[phase] is not None
        assert abs(right[phase] - left[phase]) <= 1, phase


def test_a_still_clip_returns_no_moments() -> None:
    # Constant hands: no departure, so there is no swing to estimate.
    constant = []
    for index in range(60):
        frame = np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8)
        constant.append(
            build_record(
                index,
                index * 1000.0 / 30.0,
                frame,
                _observation(HAND_PATH[0]),
                config=MeasurementConfig(),
            )
        )
    moment_set = estimate_moments(constant)
    assert all(estimate.frame_index is None for estimate in moment_set.estimates)
    assert not moment_set.ambiguous
    assert not moment_set.has_estimates
    assert "No moments could be estimated" in moment_summary(moment_set)


def test_a_tracking_gap_blocks_the_estimate() -> None:
    records = _records(gap=(30, 60))
    moment_set = estimate_moments(records)
    assert all(estimate.frame_index is None for estimate in moment_set.estimates)
    assert any("gap" in reason for reason in moment_set.sequence["gates"])


def test_low_coverage_blocks_the_estimate() -> None:
    records = _records(gap=(0, 60))
    moment_set = estimate_moments(records)
    assert all(estimate.frame_index is None for estimate in moment_set.estimates)
    assert any(
        "tracking coverage" in reason for reason in moment_set.sequence["gates"]
    )


def test_nonfinite_landmarks_are_never_used() -> None:
    records = _records(nonfinite_frames=(20, 21, 22))
    moment_set = estimate_moments(records)
    series = build_motion_series(records, config=MomentConfig())
    usable_frames = {
        frame for frame, flag in zip(series.frames, series.usable) if flag
    }
    for estimate in moment_set.estimates:
        if estimate.frame_index is not None:
            assert estimate.frame_index in usable_frames


def test_a_clip_that_starts_mid_swing_keeps_partial_results() -> None:
    records = _records(start_fraction=0.25)
    moment_set = estimate_moments(records)
    assert moment_set.get(Phase.ADDRESS).frame_index is None
    assert "stable set-up window" in moment_set.get(Phase.ADDRESS).evidence[0]
    assert moment_set.get(Phase.TOP).frame_index is not None
    assert moment_set.get(Phase.TOP).source == SOURCE_ESTIMATED


def test_two_swings_are_reported_as_ambiguous() -> None:
    # Two identical swing episodes back to back.
    frame_count = 180
    records = []
    for index in range(frame_count):
        local = (index % 90) / 89.0
        hands = _hand_offset(local, DEFAULT_CUES)
        if 0.88 <= local <= 1.0:
            hands = HAND_PATH[5]
        frame = np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8)
        records.append(
            build_record(
                index,
                index * 1000.0 / 30.0,
                frame,
                _observation(hands),
                config=MeasurementConfig(),
            )
        )
    moment_set = estimate_moments(records)
    assert moment_set.ambiguous
    assert all(estimate.frame_index is None for estimate in moment_set.estimates)
    assert "Two swing-like movement episodes" in moment_summary(moment_set)
    assert any("more than one swing" in reason for reason in moment_set.sequence["gates"])


def test_too_few_frames_is_unavailable_not_guessed() -> None:
    records = _records(frame_count=4)
    moment_set = estimate_moments(records)
    assert all(estimate.source == SOURCE_UNAVAILABLE for estimate in moment_set.estimates)
    assert any("too few frames" in reason for reason in moment_set.sequence["gates"])


def test_manual_choices_win_and_survive_re_estimation() -> None:
    records = _records()
    first = estimate_moments(records)
    edited = apply_manual_overrides(
        first,
        {Phase.TOP: 33, Phase.IMPACT: None},
        timestamps_ms={33: 1100.0},
    )
    assert edited.get(Phase.TOP).source == SOURCE_MANUAL
    assert edited.get(Phase.TOP).frame_index == 33
    assert edited.get(Phase.TOP).provenance_label == "Selected manually"
    assert edited.get(Phase.IMPACT).source == SOURCE_UNAVAILABLE
    assert edited.get(Phase.TOP).timestamp_ms == 1100.0

    again = estimate_moments(records, previous=edited)
    assert again.get(Phase.TOP).source == SOURCE_MANUAL
    assert again.get(Phase.TOP).frame_index == 33
    assert again.get(Phase.IMPACT).frame_index is None
    assert again.get(Phase.ADDRESS).source == SOURCE_ESTIMATED


def test_phase_selection_adapter_keeps_the_address_reference() -> None:
    records = _records()
    moment_set = estimate_moments(records)
    selection = to_phase_selection(moment_set)
    assert selection.reference_frame == moment_set.get(Phase.ADDRESS).frame_index
    assert set(selection.frames) == set(PHASE_ORDER)
    assert Phase.ADDRESS in selection.assigned() or (
        moment_set.get(Phase.ADDRESS).frame_index is None
    )


def test_guided_fixture_yields_ordered_estimates() -> None:
    clip = GuidedSwingClip()
    provider = GuidedPoseProvider(clip=clip)
    records = []
    for index in range(clip.frame_count):
        frame = draw_guided_frame(index, clip=clip)
        records.append(
            build_record(
                index,
                index * 1000.0 / clip.fps,
                frame,
                provider.detect(frame, index * 1000.0 / clip.fps),
                config=MeasurementConfig(),
            )
        )
    moment_set = estimate_moments(records)
    frames = moment_set.frames()
    assert all(frames[phase] is not None for phase in PHASE_ORDER), frames
    ordered = [frames[phase] for phase in PHASE_ORDER]
    assert ordered == sorted(ordered)
    for phase in PHASE_ORDER:
        expected = clip.cue_frame(phase)
        tolerance = 6 if phase is Phase.ADDRESS else 8
        assert abs(frames[phase] - expected) <= tolerance, (phase, frames[phase], expected)
    assert moment_set.sequence["coverage"] == 1.0


def test_guidance_is_labelled_as_estimated() -> None:
    records = _records()
    moment_set = estimate_moments(records)
    for estimate in moment_set.estimates:
        assert estimate.source == SOURCE_ESTIMATED
        assert estimate.provenance_label == "Estimated moment"
        assert estimate.review_required is True
        assert estimate.evidence


# ---------------------------------------------------------------------------
# Real footage and real-swing shapes
# ---------------------------------------------------------------------------

REAL_FIXTURE = Path(__file__).parent / "fixtures" / "real_swing_hand_path.json"


def _real_records(frames: list[dict] | None = None) -> list[SimpleNamespace]:
    """Lightweight records from the real swing's derived body points.

    The estimator only reads ``index``, ``timestamp_ms`` and
    ``points.pixels``, so the fixture stores just those points (no video).
    """

    rows = frames if frames is not None else json.loads(REAL_FIXTURE.read_text())["frames"]
    records = []
    for row in rows:
        pixels = {
            name: np.asarray(row[name], dtype=float)
            for name in ("left_wrist", "right_wrist", "hip_mid", "shoulder_mid")
            if row.get(name) is not None
        }
        records.append(
            SimpleNamespace(
                index=int(row["index"]),
                timestamp_ms=float(row["timestamp_ms"]),
                points=SimpleNamespace(pixels=pixels) if pixels else None,
            )
        )
    return records


def _real_rows() -> list[dict]:
    return json.loads(REAL_FIXTURE.read_text())["frames"]


def test_real_swing_recovers_all_seven_moments_near_the_human_check() -> None:
    """One real phone-video driver swing, checked frame by frame by a human.

    One swing is a sanity check, not an accuracy figure.
    """

    payload = json.loads(REAL_FIXTURE.read_text())
    checked = {Phase(key): value for key, value in payload["human_checked_moments"].items()}
    moment_set = estimate_moments(_real_records())
    assert not moment_set.ambiguous
    assert moment_set.sequence["swing_episodes"] == 1
    frames = moment_set.frames()
    tolerance = {Phase.ADDRESS: 8, Phase.FINISH: 12}
    for phase in PHASE_ORDER:
        assert frames[phase] is not None, (phase, moment_set.get(phase).evidence)
        assert abs(frames[phase] - checked[phase]) <= tolerance.get(phase, 3), (
            phase,
            frames[phase],
            checked[phase],
        )
    ordered = [frames[phase] for phase in PHASE_ORDER]
    assert ordered == sorted(ordered) and len(set(ordered)) == len(ordered)
    assert all(item.source == SOURCE_ESTIMATED for item in moment_set.estimates)
    assert "7 of 7 moments available" in moment_summary(moment_set)


def test_lowering_the_club_after_the_finish_is_not_a_second_swing() -> None:
    """Holding the finish, then bringing the hands down, then standing still.

    The old detector counted any two hand-height peaks as two swings and
    withheld every moment for this very common real-world clip ending.
    """

    rows = _real_rows()
    last = rows[-1]
    address = rows[108]

    def hands(row: dict) -> np.ndarray:
        wrists = [row[name] for name in ("left_wrist", "right_wrist") if row.get(name)]
        return np.mean(np.asarray(wrists, dtype=float), axis=0)

    start, end = hands(last), hands(address)
    extra = []
    for step in range(1, 61):
        weight = min(1.0, step / 30.0)
        point = list(start + (end - start) * weight)
        extra.append(
            {
                "index": last["index"] + step,
                "timestamp_ms": last["timestamp_ms"] + step * 33.28,
                "left_wrist": point,
                "right_wrist": point,
                "hip_mid": last["hip_mid"],
                "shoulder_mid": last["shoulder_mid"],
            }
        )
    original = estimate_moments(_real_records()).frames()
    extended = estimate_moments(_real_records(rows + extra))
    assert not extended.ambiguous
    after = extended.frames()
    for phase in PHASE_ORDER:
        assert after[phase] is not None, phase
        # Finish is the middle of its settled window, which can grow by a frame
        # when the clip continues; every other moment must not move at all.
        allowed = 2 if phase is Phase.FINISH else 0
        assert abs(after[phase] - original[phase]) <= allowed, phase


def test_a_practice_swing_then_the_real_swing_is_ambiguous() -> None:
    rows = _real_rows()
    second = [
        {**row, "index": row["index"] + len(rows), "timestamp_ms": row["timestamp_ms"] + len(rows) * 33.28}
        for row in rows
    ]
    moment_set = estimate_moments(_real_records(rows + second))
    assert moment_set.ambiguous
    assert moment_set.sequence["swing_episodes"] == 2
    assert all(estimate.frame_index is None for estimate in moment_set.estimates)


def test_a_clip_that_stops_before_the_top_keeps_only_the_set_up() -> None:
    rows = _real_rows()[:124]
    moment_set = estimate_moments(_real_records(rows))
    assert moment_set.get(Phase.ADDRESS).frame_index is not None
    assert moment_set.get(Phase.TAKEAWAY).frame_index is not None
    for phase in (Phase.MID_BACK, Phase.TOP, Phase.DOWNSWING, Phase.IMPACT, Phase.FINISH):
        assert moment_set.get(phase).frame_index is None, phase
        assert moment_set.get(phase).evidence


def test_a_finish_higher_than_the_top_does_not_hide_the_top() -> None:
    """On real swings the hands usually finish higher than they were at the top."""

    frames = _estimated_frames(_records(path=(*HAND_PATH[:5], (0.25, -1.40))))
    span = 89
    assert frames[Phase.TOP] is not None
    assert abs(frames[Phase.TOP] - DEFAULT_CUES[2] * span) <= 3
    assert all(frames[phase] is not None for phase in PHASE_ORDER), frames


def test_mid_back_sits_between_takeaway_and_top() -> None:
    frames = _estimated_frames(_records())
    assert frames[Phase.TAKEAWAY] < frames[Phase.MID_BACK] < frames[Phase.TOP]


def test_plumbing_fixture_is_not_treated_as_a_swing() -> None:
    """A monotonic arm sweep must not produce six moments."""

    clip = SyntheticClip(width=320, height=180, fps=24.0, frame_count=24)
    provider = ScriptedPoseProvider(clip=clip)
    records = []
    for index in range(clip.frame_count):
        frame = draw_synthetic_frame(index, clip=clip)
        records.append(
            build_record(
                index,
                index * 1000.0 / clip.fps,
                frame,
                provider.detect(frame, index * 1000.0 / clip.fps),
                config=MeasurementConfig(),
            )
        )
    moment_set = estimate_moments(records)
    assert not moment_set.has_estimates
