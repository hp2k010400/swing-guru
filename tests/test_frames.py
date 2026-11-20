"""Selective single-pass decoding (performance bundle).

The analysis build needs the moment windows plus the background-reference
frames. These tests pin that only those frames are retained, that the decoder
still lands on the right images, and that the counters separate scanned,
kept and skipped frames.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from swingguru.frames import decode_selected_frames, wanted_frames
from swingguru.video import probe_video


def _reference_frames(video, indices) -> dict[int, np.ndarray]:
    capture = cv2.VideoCapture(str(video))
    found: dict[int, np.ndarray] = {}
    index = 0
    wanted = set(indices)
    while wanted:
        ok, frame = capture.read()
        if not ok:
            break
        if index in wanted:
            found[index] = frame
            wanted.discard(index)
        index += 1
    capture.release()
    return found


def test_wanted_frames_merges_roles_and_deduplicates() -> None:
    roles = wanted_frames([3, 1, 3], [3, 9])
    assert roles == {1: ("detection",), 3: ("detection", "reference"), 9: ("reference",)}


def test_wanted_frames_of_nothing_is_empty() -> None:
    assert wanted_frames([], []) == {}


def test_decoding_nothing_touches_the_decoder_at_all() -> None:
    result = decode_selected_frames("does-not-exist.mp4", [])
    assert result.counters() == {
        "frames_scanned": 0,
        "frames_kept": 0,
        "frames_skipped": 0,
        "frames_missing": 0,
    }
    assert result.frames == {}


def test_only_the_requested_frames_are_retained(fixture_video) -> None:
    wanted = [2, 5, 17]
    result = decode_selected_frames(fixture_video, wanted)
    assert sorted(result.frames) == wanted
    assert result.counters() == {
        "frames_scanned": 18,
        "frames_kept": 3,
        "frames_skipped": 15,
        "frames_missing": 0,
    }
    assert result.missing == ()


def test_retained_frames_match_a_sequential_decode(fixture_video) -> None:
    wanted = [0, 5, 23]
    result = decode_selected_frames(fixture_video, wanted)
    expected = _reference_frames(fixture_video, wanted)
    assert sorted(expected) == wanted
    for index, image in expected.items():
        assert result.get(index) is not None
        assert np.array_equal(result.get(index), image)


def test_frames_the_container_cannot_deliver_are_reported(fixture_video) -> None:
    info = probe_video(fixture_video)
    assert info.frame_count is not None
    beyond = info.frame_count + 4
    result = decode_selected_frames(fixture_video, [1, beyond])
    assert sorted(result.frames) == [1]
    assert result.missing == (beyond,)
    assert result.counters()["frames_missing"] == 1


def test_requests_beyond_max_index_are_dropped_not_missing(fixture_video) -> None:
    # max_index bounds the analysed range, so a request past it is "not
    # needed" rather than "the container could not deliver it".
    result = decode_selected_frames(fixture_video, [1, 50], max_index=8)
    assert sorted(result.frames) == [1]
    assert result.scanned == 2
    assert result.missing == ()


def test_max_index_above_the_last_request_changes_nothing(fixture_video) -> None:
    bounded = decode_selected_frames(fixture_video, [3, 6], max_index=100)
    assert sorted(bounded.frames) == [3, 6]
    assert bounded.scanned == 7


def test_negative_indices_are_ignored(fixture_video) -> None:
    result = decode_selected_frames(fixture_video, [-3, 4])
    assert sorted(result.frames) == [4]
    assert result.missing == ()


def test_has_and_get_work_by_index(fixture_video) -> None:
    result = decode_selected_frames(fixture_video, [7])
    assert result.has(7) and not result.has(8)
    assert result.get(7) is not None
    assert result.get(8) is None
    assert len(result) == 1


def test_a_contiguous_range_decodes_like_the_old_reader(fixture_video) -> None:
    info = probe_video(fixture_video)
    assert info.frame_count is not None
    wanted = list(range(info.frame_count))
    result = decode_selected_frames(fixture_video, wanted)
    assert sorted(result.frames) == wanted
    assert result.counters()["frames_skipped"] == 0
    expected = _reference_frames(fixture_video, wanted)
    assert np.array_equal(result.get(0), expected[0])
    assert np.array_equal(result.get(wanted[-1]), expected[wanted[-1]])


def test_requesting_a_frame_from_a_missing_file_raises_a_clear_error() -> None:
    from swingguru.video import VideoOpenError

    with pytest.raises(VideoOpenError):
        decode_selected_frames("definitely-not-here.mp4", [0])
