"""Landmark vocabulary, side mapping and observation helpers."""

from __future__ import annotations

import pytest

from swingguru import landmarks
from swingguru.landmarks import (
    LANDMARK_COUNT,
    LANDMARK_NAMES,
    POSE_CONNECTIONS,
    Landmark,
    PoseObservation,
    build_landmarks,
    lead_side,
    normalize_handedness,
    side_landmark,
    trail_side,
)


def test_topology_has_33_named_landmarks() -> None:
    assert len(LANDMARK_NAMES) == 33
    assert LANDMARK_COUNT == 33
    assert len(set(LANDMARK_NAMES)) == 33


def test_expected_landmark_indices() -> None:
    assert landmarks.index_of("nose") == 0
    assert landmarks.index_of("left_shoulder") == 11
    assert landmarks.index_of("right_hip") == 24
    assert landmarks.index_of("left_knee") == 25
    assert landmarks.index_of("right_ankle") == 28
    assert landmarks.index_of("right_foot_index") == 32


def test_connection_pairs_are_valid_indices() -> None:
    assert POSE_CONNECTIONS
    for start, end in POSE_CONNECTIONS:
        assert 0 <= start < LANDMARK_COUNT
        assert 0 <= end < LANDMARK_COUNT
        assert start != end


def test_handedness_and_side_mapping() -> None:
    assert normalize_handedness(None) == "right"
    assert normalize_handedness("RIGHT") == "right"
    assert normalize_handedness("lh") == "left"
    assert lead_side("right") == "left"
    assert trail_side("right") == "right"
    assert lead_side("left") == "right"
    assert trail_side("left") == "left"
    with pytest.raises(ValueError):
        normalize_handedness("sideways")


def test_side_landmark_builds_names() -> None:
    assert side_landmark("left", "knee") == "left_knee"
    with pytest.raises(ValueError):
        side_landmark("middle", "knee")
    with pytest.raises(ValueError):
        side_landmark("left", "elbow_gap")


def test_landmark_frame_and_finiteness() -> None:
    inside = Landmark(index=0, name="nose", x=0.5, y=0.5, z=0.0)
    assert inside.finite and inside.in_frame()
    outside = Landmark(index=0, name="nose", x=1.4, y=0.5, z=0.0)
    assert outside.finite and not outside.in_frame()
    assert not outside.in_frame(margin=0.5) or outside.in_frame(margin=0.5)
    broken = Landmark(
        index=0, name="nose", x=float("nan"), y=0.5, z=0.0
    )
    assert not broken.finite
    assert not broken.in_frame()


def test_build_landmarks_from_rows() -> None:
    built = build_landmarks([(0.1, 0.2, 0.3, 0.9, 0.8), (0.4, 0.5, 0.6)])
    assert [item.name for item in built] == ["nose", "left_eye_inner"]
    assert built[0].visibility == pytest.approx(0.9)
    assert built[1].visibility is None


def test_observation_helpers() -> None:
    observation = PoseObservation(
        detected=True,
        landmarks=build_landmarks([(0.1, 0.2, 0.0, 0.9, 0.9)]),
    )
    assert observation.by_name("nose") is not None
    assert observation.by_index(0) is not None
    assert observation.by_index(5) is None
    assert observation.missing_names(["nose", "left_knee"]) == ("left_knee",)
    assert set(observation.as_name_map()) == {"nose"}
    with pytest.raises(KeyError):
        observation.by_name("not_a_landmark")


def test_missing_observation_factory() -> None:
    observation = PoseObservation.missing("nothing here")
    assert not observation.detected
    assert observation.note == "nothing here"
    assert observation.landmarks == ()
