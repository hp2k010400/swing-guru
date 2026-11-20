"""The synthetic fixture must be deterministic and self-consistent."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from swingguru.landmarks import LANDMARK_NAMES
from swingguru.synthetic import (
    ScriptedPoseProvider,
    SyntheticClip,
    draw_synthetic_frame,
    joints_to_observation,
    make_synthetic_clip,
    synthetic_joints,
)
from swingguru.video import probe_video

CLIP = SyntheticClip(width=320, height=180, fps=24.0, frame_count=16)


def test_joints_cover_every_landmark_inside_the_frame() -> None:
    for frame_index in (0, 7, 15):
        joints = synthetic_joints(frame_index, clip=CLIP)
        assert set(joints) == set(LANDMARK_NAMES)
        for name, (x, y, _z, visibility, presence) in joints.items():
            assert 0.0 <= x <= 1.0, name
            assert 0.0 <= y <= 1.0, name
            assert 0.0 <= visibility <= 1.0
            assert 0.0 <= presence <= 1.0


def test_mirrored_clip_swaps_sides_and_flips_x() -> None:
    upright = synthetic_joints(4, clip=CLIP)
    mirrored = synthetic_joints(
        4,
        clip=SyntheticClip(
            width=CLIP.width,
            height=CLIP.height,
            fps=CLIP.fps,
            frame_count=CLIP.frame_count,
            mirrored=True,
        ),
    )
    assert set(upright) == set(mirrored)
    assert mirrored["right_shoulder"][0] == pytest.approx(
        1.0 - upright["left_shoulder"][0], abs=1e-9
    )


def test_drawing_is_deterministic() -> None:
    first = draw_synthetic_frame(3, clip=CLIP)
    second = draw_synthetic_frame(3, clip=CLIP)
    assert first.shape == (CLIP.height, CLIP.width, 3)
    assert np.array_equal(first, second)
    assert not np.array_equal(first, draw_synthetic_frame(4, clip=CLIP))


def test_scripted_provider_is_frame_accurate(tmp_path: Path) -> None:
    provider = ScriptedPoseProvider(clip=CLIP)
    frame = draw_synthetic_frame(6, clip=CLIP)
    observation = provider.detect(frame, 6 * 1000.0 / CLIP.fps)
    assert observation.detected
    assert len(observation.landmarks) == len(LANDMARK_NAMES)
    assert len(observation.world_landmarks) == len(LANDMARK_NAMES)


def test_scripted_provider_quality_knobs() -> None:
    provider = ScriptedPoseProvider(
        clip=CLIP, drop_frames=frozenset({2}), occlude=frozenset({"left_knee"})
    )
    frame = draw_synthetic_frame(2, clip=CLIP)
    assert not provider.detect(frame, 2 * 1000.0 / CLIP.fps).detected

    frame = draw_synthetic_frame(3, clip=CLIP)
    observation = provider.detect(frame, 3 * 1000.0 / CLIP.fps)
    knee = observation.by_name("left_knee")
    assert knee is not None and knee.visibility == pytest.approx(0.05)


def test_observation_builder_indexes_by_topology() -> None:
    observation = joints_to_observation(synthetic_joints(0, clip=CLIP))
    assert [item.index for item in observation.landmarks] == list(
        range(len(LANDMARK_NAMES))
    )
    assert observation.by_index(0).name == "nose"


def test_make_synthetic_clip_writes_a_playable_video(tmp_path: Path) -> None:
    path = make_synthetic_clip(tmp_path / "clip.mp4", clip=CLIP)
    info = probe_video(path)
    assert info.frame_count == CLIP.frame_count
    assert (info.width, info.height) == (CLIP.width, CLIP.height)
    assert info.fps == pytest.approx(CLIP.fps, rel=1e-3)
