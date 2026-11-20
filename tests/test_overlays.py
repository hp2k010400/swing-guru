"""Overlay rendering must be safe for every frame state."""

from __future__ import annotations

import numpy as np
import pytest

from swingguru.landmarks import PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.overlays import OverlayConfig, render_overlay
from swingguru.pipeline import build_record
from swingguru.synthetic import SyntheticClip, joints_to_observation, synthetic_joints

CLIP = SyntheticClip(width=320, height=180, fps=24.0, frame_count=4)


def _record(observation: PoseObservation, *, width: int = 320, height: int = 180):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    return build_record(
        0,
        0.0,
        frame,
        observation,
        config=MeasurementConfig(camera_view="face-on"),
    )


def test_overlay_draws_on_top_of_the_frame() -> None:
    frame = np.zeros((180, 320, 3), dtype=np.uint8)
    record = _record(joints_to_observation(synthetic_joints(0, clip=CLIP)))
    annotated = render_overlay(frame, record, OverlayConfig(fps=24.0))
    assert annotated.shape == frame.shape
    assert annotated.dtype == np.uint8
    assert np.any(annotated != frame)
    assert np.any(frame != annotated)  # input is not mutated in place
    assert not np.any(frame)


def test_overlay_handles_a_missing_pose() -> None:
    frame = np.full((180, 320, 3), 40, dtype=np.uint8)
    record = _record(PoseObservation.missing())
    annotated = render_overlay(frame, record, OverlayConfig(fps=24.0))
    assert annotated.shape == frame.shape


@pytest.mark.parametrize(("width", "height"), [(64, 64), (160, 90), (90, 160)])
def test_overlay_scales_to_small_and_portrait_frames(width: int, height: int) -> None:
    clip = SyntheticClip(width=width, height=height, fps=24.0, frame_count=4)
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    record = _record(
        joints_to_observation(synthetic_joints(1, clip=clip)),
        width=width,
        height=height,
    )
    annotated = render_overlay(frame, record)
    assert annotated.shape == (height, width, 3)


def test_overlay_config_can_disable_every_layer() -> None:
    frame = np.full((180, 320, 3), 20, dtype=np.uint8)
    record = _record(joints_to_observation(synthetic_joints(0, clip=CLIP)))
    annotated = render_overlay(
        frame,
        record,
        OverlayConfig(
            draw_skeleton=False,
            draw_measurements=False,
            draw_hud=False,
            draw_quality=False,
        ),
    )
    assert np.array_equal(annotated, frame)


def test_overlay_rejects_an_empty_frame() -> None:
    record = _record(PoseObservation.missing())
    with pytest.raises(ValueError):
        render_overlay(np.zeros((0, 0, 3), dtype=np.uint8), record)


def test_quality_colour_uses_the_shared_landmark_predicate() -> None:
    """Overlay colouring must not disagree with measurements or the CSV."""

    from swingguru.landmarks import Landmark
    from swingguru.overlays import (
        COLOUR_FAIR,
        COLOUR_GOOD,
        COLOUR_POOR,
        OverlayConfig,
        _quality_colour,
    )

    config = OverlayConfig(quality=MeasurementConfig())
    good = Landmark(0, "nose", 0.5, 0.5, 0.0, 1.0, 1.0)
    assert _quality_colour(good, config) == COLOUR_GOOD

    # NaN visibility used to compare False against the threshold and read as
    # good; it must now be treated as unusable.
    nan_visibility = Landmark(0, "nose", 0.5, 0.5, 0.0, float("nan"), 1.0)
    assert _quality_colour(nan_visibility, config) == COLOUR_POOR

    missing_quality = Landmark(0, "nose", 0.5, 0.5, 0.0, None, None)
    assert _quality_colour(missing_quality, config) == COLOUR_POOR

    # Confidence below the threshold stays distinguishable from unusable.
    low_presence = Landmark(0, "nose", 0.5, 0.5, 0.0, 0.9, 0.1)
    assert _quality_colour(low_presence, config) == COLOUR_FAIR
