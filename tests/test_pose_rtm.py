"""The RTMPose/RTMW adapter's mapping onto the engine's landmark vocabulary.

These tests need no model download: they feed synthetic keypoint arrays in the
COCO-WholeBody (133) and COCO body (17) layouts through the adapter's pure
functions and check the engine sees the right joints with the right quality.
"""

from __future__ import annotations

import numpy as np
import pytest

from swingguru.landmarks import NAME_TO_INDEX
from swingguru.measurements import MeasurementConfig
from swingguru.pipeline import build_record
from swingguru.pose_rtm import (
    BODY17_NAMES,
    WHOLEBODY_TO_MEDIAPIPE,
    RTMPoseAdapter,
    select_golfer,
    to_observation,
)

W, H = 400, 800


def _person(count: int, *, offset=(0.0, 0.0), score=0.9) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(count)
    keypoints = np.column_stack(
        [rng.uniform(100, 300, count) + offset[0], rng.uniform(100, 700, count) + offset[1]]
    )
    return keypoints, np.full(count, score)


def test_wholebody_maps_every_engine_landmark_to_the_right_source() -> None:
    keypoints, scores = _person(133)
    observation = to_observation(keypoints, scores, width=W, height=H)
    assert observation.detected
    names = {landmark.name for landmark in observation.landmarks}
    assert names == set(NAME_TO_INDEX)
    for landmark in observation.landmarks:
        source = WHOLEBODY_TO_MEDIAPIPE[landmark.name]
        assert landmark.x == pytest.approx(keypoints[source][0] / W)
        assert landmark.y == pytest.approx(keypoints[source][1] / H)
        assert landmark.index == NAME_TO_INDEX[landmark.name]
    # The hand knuckles come from the hand model, not the wrist.
    assert WHOLEBODY_TO_MEDIAPIPE["left_index"] != WHOLEBODY_TO_MEDIAPIPE["left_wrist"]
    assert 91 <= WHOLEBODY_TO_MEDIAPIPE["left_index"] <= 111
    assert 112 <= WHOLEBODY_TO_MEDIAPIPE["right_pinky"] <= 132


def test_body17_output_leaves_unmapped_joints_absent() -> None:
    keypoints, scores = _person(17)
    observation = to_observation(keypoints, scores, width=W, height=H)
    names = {landmark.name for landmark in observation.landmarks}
    assert names == set(BODY17_NAMES)
    assert "left_index" not in names and "left_heel" not in names


def test_scores_become_visibility_and_presence_and_are_clipped() -> None:
    keypoints, scores = _person(133)
    scores = scores.copy()
    scores[WHOLEBODY_TO_MEDIAPIPE["left_wrist"]] = 0.2
    scores[WHOLEBODY_TO_MEDIAPIPE["right_wrist"]] = 1.7
    observation = to_observation(keypoints, scores, width=W, height=H)
    left = observation.by_name("left_wrist")
    right = observation.by_name("right_wrist")
    assert left.visibility == pytest.approx(0.2) and left.presence == pytest.approx(0.2)
    assert right.visibility == pytest.approx(1.0)


def test_low_scored_joints_fail_the_engines_quality_check() -> None:
    keypoints, scores = _person(133)
    scores = scores.copy()
    scores[WHOLEBODY_TO_MEDIAPIPE["left_wrist"]] = 0.2
    record = build_record(
        0, 0.0, np.zeros((H, W, 3), np.uint8),
        to_observation(keypoints, scores, width=W, height=H), config=MeasurementConfig(),
    )
    assert "left_wrist" not in record.points.pixels
    assert "right_wrist" in record.points.pixels


def test_the_golfer_is_the_largest_confident_person() -> None:
    small, small_scores = _person(133, offset=(0, 0))
    small = small * 0.3  # a distant person
    big, big_scores = _person(133)
    faint, faint_scores = _person(133, score=0.1)
    keypoints = np.stack([small, big, faint])
    scores = np.stack([small_scores, big_scores, faint_scores])
    assert select_golfer(keypoints, scores) == 1
    assert select_golfer(np.zeros((0, 133, 2)), np.zeros((0, 133))) is None


def test_adapter_rejects_unknown_models_and_names_itself() -> None:
    assert RTMPoseAdapter("wholebody", "balanced").name == "rtm-wholebody-balanced"
    with pytest.raises(ValueError):
        RTMPoseAdapter("hands")
    empty = RTMPoseAdapter().detect(np.zeros((0, 0, 3), np.uint8), 0.0)
    assert not empty.detected
