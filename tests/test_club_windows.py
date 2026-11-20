"""Event-window generation, merging and per-window tracking (performance bundle).

The classical tracker only needs the frames around the six moments. These
tests pin the window arithmetic, and then pin the behaviour that matters for
accuracy: disjoint windows are selected independently, no frame outside a
window is ever reported, and no gap between windows is bridged.
"""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from swingguru.club import ClassicalClubTracker, ClubConfig
from swingguru.club.geometry import angle_difference, unit_from_angle
from swingguru.club.metrics import reportable
from swingguru.club.tracker import (
    as_frame_source,
    build_references,
    frame_accessor,
    reference_hands,
    reference_picks,
    torso_length_px,
)
from swingguru.club.windows import (
    FrameWindow,
    frames_in,
    merge_windows,
    phase_windows,
    windows_around,
)

W, H = 400, 600
HANDS = np.array([200.0, 300.0])
TORSO = 100.0


# ---------------------------------------------------------------------------
# Window arithmetic
# ---------------------------------------------------------------------------


def test_window_rejects_an_inverted_range() -> None:
    with pytest.raises(ValueError):
        FrameWindow(9, 4)


def test_merge_joins_overlaps_and_touching_windows_only() -> None:
    merged = merge_windows(
        [
            FrameWindow(20, 24),
            FrameWindow(0, 4),
            FrameWindow(4, 6),
            FrameWindow(22, 30),
            FrameWindow(40, 41),
        ]
    )
    assert [(item.start, item.end) for item in merged] == [(0, 6), (20, 30), (40, 41)]


def test_merge_clips_to_bounds_and_drops_impossible_windows() -> None:
    merged = merge_windows(
        [FrameWindow(0, 5), FrameWindow(9, 12), FrameWindow(20, 25)],
        lower=2,
        upper=10,
    )
    assert [(item.start, item.end) for item in merged] == [(2, 5), (9, 10)]
    assert merge_windows([FrameWindow(30, 40)], lower=0, upper=10) == ()


def test_merge_of_an_empty_set_is_empty() -> None:
    assert merge_windows([]) == ()


def test_windows_around_skips_missing_centres_and_merges_neighbours() -> None:
    windows = windows_around([10, None, 14, 40], 3, lower=0, upper=100)
    assert [(item.start, item.end) for item in windows] == [(7, 17), (37, 43)]


def test_phase_windows_uses_only_the_requested_phases() -> None:
    frames = {"address": 10, "takeaway": 20, "mid_back": 30}
    windows = phase_windows(
        frames, 2, lower=0, upper=100, phases=("address", "mid_back")
    )
    assert [(item.start, item.end) for item in windows] == [(8, 12), (28, 32)]


def test_frames_in_is_sorted_and_deduplicated() -> None:
    frames = frames_in([FrameWindow(0, 2), FrameWindow(2, 3), FrameWindow(10, 11)])
    assert frames == (0, 1, 2, 3, 10, 11)


def test_windows_are_clipped_to_the_analysed_range() -> None:
    # A moment near the first or last frame must not ask for frames that were
    # never analysed.
    windows = windows_around([1, 195], 5, lower=0, upper=195)
    assert [(item.start, item.end) for item in windows] == [(0, 6), (190, 195)]


# ---------------------------------------------------------------------------
# Frame access for a partially decoded clip
# ---------------------------------------------------------------------------


def _record(index: int, *, hands=HANDS) -> SimpleNamespace:
    pixels = {
        "right_wrist": np.asarray(hands, float),
        "hip_mid": np.array([150.0, 330.0]),
        "shoulder_mid": np.array([150.0, 230.0]),
    }
    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=W,
        height=H,
        points=SimpleNamespace(pixels=pixels, landmarks={}),
    )


def test_frame_accessor_reads_a_mapping_keyed_by_frame_index() -> None:
    records = [_record(4), _record(5)]
    frames = {5: np.zeros((H, W, 3), np.uint8)}
    accessor = frame_accessor(frames, records)
    assert accessor(0) is None
    assert accessor(1) is frames[5]


def test_frame_accessor_reads_a_positional_sequence() -> None:
    first = np.zeros((H, W, 3), np.uint8)
    second = np.ones((H, W, 3), np.uint8)
    accessor = frame_accessor([first, second], [_record(0), _record(1)])
    assert accessor(0) is first and accessor(1) is second and accessor(2) is None


def test_reference_picks_are_evenly_spaced_and_within_range() -> None:
    picks = reference_picks(196, reference_frames=16)
    assert len(picks) == 16
    assert picks[0] == 0 and picks[-1] == 195
    assert list(picks) == sorted(set(picks))
    assert reference_picks(0, reference_frames=16) == ()


def test_reference_hands_needs_no_decoded_image() -> None:
    records = [_record(index) for index in range(20)]
    found = reference_hands(records, torso_px=TORSO, config=ClubConfig())
    picks = reference_picks(20, reference_frames=ClubConfig().reference_frames)
    assert [index for index, _ in found] == list(picks)
    assert all(hands is not None for _, hands in found)


def test_build_references_uses_the_same_picks_with_and_without_a_mapping() -> None:
    records = [_record(index) for index in range(20)]
    frames = [_canvas() for _ in records]
    as_list = build_references(frames, records, torso_px=TORSO, config=ClubConfig())
    by_index = {record.index: frame for record, frame in zip(records, frames)}
    as_map = build_references(by_index, records, torso_px=TORSO, config=ClubConfig())
    assert [item.frame_index for item in as_list] == [item.frame_index for item in as_map]
    assert as_list and len(as_list) == len(reference_picks(20, reference_frames=ClubConfig().reference_frames))
    assert [item.hands.tolist() for item in as_list] == [item.hands.tolist() for item in as_map]
    for listed, mapped in zip(as_list, as_map):
        assert np.array_equal(listed.maps[0][0], mapped.maps[0][0])


def test_a_generator_frame_source_is_materialised_once_trimmed_to_the_records() -> None:
    records = [_record(index) for index in range(3)]
    frames = [_canvas(), _canvas(), _canvas(), _canvas()]
    materialised = as_frame_source((frame for frame in frames), records)
    assert isinstance(materialised, list) and len(materialised) == 3
    mapping = {0: frames[0]}
    assert as_frame_source(mapping, records) is mapping


def test_build_references_accepts_a_generator_of_frames() -> None:
    records = [_record(index) for index in range(20)]
    frames = [_canvas() for _ in records]
    as_list = build_references(frames, records, torso_px=TORSO, config=ClubConfig())
    as_generator = build_references((frame for frame in frames), records, torso_px=TORSO, config=ClubConfig())
    assert [item.frame_index for item in as_list] == [item.frame_index for item in as_generator]


def test_track_sequence_accepts_a_generator_a_list_and_a_mapping() -> None:
    # `track_sequence` used to materialise its frame iterable; keep generators
    # working now that the reference builder length-checks the source.
    angles = {index: -70.0 + 4.0 * index for index in range(12)}
    frames, records = _clip(angles, total=12)
    tracker = ClassicalClubTracker(ClubConfig())
    by_index = {index: frame for index, frame in enumerate(frames)}

    from_list = tracker.track_sequence(list(frames), records)
    from_generator = tracker.track_sequence((frame for frame in frames), records)
    from_mapping = tracker.track_sequence(by_index, records)

    assert len(from_list.frames) == len(from_generator.frames) == len(from_mapping.frames) == 12
    for index in range(12):
        listed, generated, mapped = from_list.at(index), from_generator.at(index), from_mapping.at(index)
        assert listed is not None and generated is not None and mapped is not None
        assert generated.level == listed.level and mapped.level == listed.level
        assert generated.status == listed.status and mapped.status == listed.status
        if listed.angle_deg is not None:
            assert generated.angle_deg == pytest.approx(listed.angle_deg)
            assert mapped.angle_deg == pytest.approx(listed.angle_deg)


# ---------------------------------------------------------------------------
# Windowed tracking
# ---------------------------------------------------------------------------


def _canvas(value: int = 150) -> np.ndarray:
    rng = np.random.default_rng(11)
    image = np.full((H, W, 3), value, np.uint8)
    noise = rng.integers(-4, 5, size=(H, W, 1))
    return np.clip(image.astype(int) + noise, 0, 255).astype(np.uint8)


def _shaft(image: np.ndarray, angle_deg: float, *, start=HANDS, length=220.0) -> np.ndarray:
    tip = np.asarray(start, float) + unit_from_angle(angle_deg) * length
    cv2.line(
        image,
        tuple(int(v) for v in start),
        tuple(int(v) for v in tip),
        (40, 40, 40),
        2,
        cv2.LINE_AA,
    )
    return image


def _clip(angles: dict[int, float], *, total: int = 40):
    records, frames = [], []
    for index in range(total):
        records.append(_record(index))
        image = _canvas()
        if index in angles:
            _shaft(image, angles[index])
        frames.append(image)
    return frames, records


def test_windowed_tracking_reproduces_the_full_sequence_phase_result() -> None:
    angles = {index: -70.0 + 3.0 * index for index in range(40)}
    frames, records = _clip(angles)
    config = ClubConfig()
    tracker = ClassicalClubTracker(config)
    full = tracker.track_sequence(frames, records)
    windows = merge_windows([FrameWindow(8, 12), FrameWindow(24, 28)])
    windowed = tracker.track_windows({index: frame for index, frame in enumerate(frames)}, records, windows)

    for frame_index in (10, 26):
        expected = full.at(frame_index)
        found = windowed.at(frame_index)
        assert found is not None and expected is not None
        assert found.level == expected.level
        assert reportable(found) == reportable(expected)
        if reportable(expected):
            assert abs(angle_difference(found.angle_deg, expected.angle_deg)) < 1.0


def test_windowed_tracking_only_reports_frames_inside_the_windows() -> None:
    angles = {index: -70.0 + 3.0 * index for index in range(40)}
    frames, records = _clip(angles)
    windows = merge_windows([FrameWindow(8, 12), FrameWindow(24, 28)])
    track = ClassicalClubTracker(ClubConfig()).track_windows(
        {index: frame for index, frame in enumerate(frames)}, records, windows
    )
    covered = set(frames_in(windows))
    assert {frame.frame_index for frame in track.frames} == covered
    assert track.at(20) is None


def test_windows_are_never_bridged_as_adjacent_temporal_evidence() -> None:
    # Two windows whose shaft angles are far apart. A single whole-clip pass
    # could carry evidence between them; a per-window pass cannot, and no
    # frame between the windows is interpolated or reported.
    angles = dict.fromkeys(range(40), -60.0)
    angles.update({index: 60.0 for index in range(24, 29)})
    frames, records = _clip(angles)
    windows = merge_windows([FrameWindow(8, 12), FrameWindow(24, 28)])
    track = ClassicalClubTracker(ClubConfig()).track_windows(
        {index: frame for index, frame in enumerate(frames)}, records, windows
    )

    early = track.at(10)
    assert early is not None and reportable(early)
    assert abs(angle_difference(early.angle_deg, -60.0)) < 8.0
    for frame_index in range(13, 24):
        assert track.at(frame_index) is None
        assert all(frame.frame_index != frame_index for frame in track.frames)


def test_overlapping_windows_detect_each_frame_exactly_once() -> None:
    angles = {index: -70.0 + 3.0 * index for index in range(40)}
    frames, records = _clip(angles)
    overlapping = [FrameWindow(8, 14), FrameWindow(12, 18), FrameWindow(30, 34)]
    windows = merge_windows(overlapping)
    tracker = ClassicalClubTracker(ClubConfig())
    track = tracker.track_windows(
        {index: frame for index, frame in enumerate(frames)}, records, windows
    )
    detected = [detection.frame_index for detection in track.detections]
    assert detected == list(frames_in(windows))
    assert len(detected) == len(set(detected))
    assert len(track.frames) == len(frames_in(windows))


def test_track_windows_at_a_phase_frame_matches_track_sequence_on_the_same_clip() -> None:
    # The acceptance guard: the six phase frames must keep their level and
    # source, and their angle within a degree, when only windows are used.
    angles = {index: -70.0 + 3.0 * index for index in range(40)}
    frames, records = _clip(angles)
    config = ClubConfig()
    tracker = ClassicalClubTracker(config)
    full = tracker.track_sequence(frames, records)
    windows = merge_windows([FrameWindow(9, 11), FrameWindow(27, 30)])
    windowed = tracker.track_windows(
        {index: frame for index, frame in enumerate(frames)}, records, windows
    )
    compared = 0
    for frame_index in (10, 28):
        expected = full.at(frame_index)
        found = windowed.at(frame_index)
        assert expected is not None and found is not None
        assert found.level == expected.level
        assert found.status == expected.status
        compared += 1
        if reportable(expected):
            assert abs(angle_difference(found.angle_deg, expected.angle_deg)) < 1.0
    assert compared == 2


def test_torso_scale_comes_from_the_whole_clip_not_just_the_windows() -> None:
    # A windowed run must keep the global torso scale: the reference
    # measurements and every torso-length threshold depend on it.
    records = [_record(index) for index in range(40)]
    for index in range(20, 40):
        points = records[index].points.pixels
        points["hip_mid"] = np.array([150.0, 380.0])
        points["shoulder_mid"] = np.array([150.0, 230.0])
    expected = float(np.median([TORSO] * 20 + [150.0] * 20))
    assert torso_length_px(records) == pytest.approx(expected)

    frames, _ = _clip({index: -70.0 + 3.0 * index for index in range(40)})
    windows = merge_windows([FrameWindow(25, 30)])
    track = ClassicalClubTracker(ClubConfig()).track_windows(
        {index: frame for index, frame in enumerate(frames)}, records, windows
    )
    assert all(detection.hands.torso_px == pytest.approx(expected) for detection in track.detections)
