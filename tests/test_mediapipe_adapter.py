"""Integration checks against the real MediaPipe pose model.

These are skipped unless the official ``pose_landmarker_full.task`` asset has
been downloaded into ``models/``. They prove the adapter, video mode and frame
pacing work; they say nothing about tracking a real golf swing.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from swingguru.landmarks import LANDMARK_NAMES, PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.phases import ManualPhaseProvider, Phase
from swingguru.pipeline import PipelineConfig, process_video
from swingguru.pose import (
    DEFAULT_MODEL_URL,
    MediaPipePoseAdapter,
    PoseAdapterError,
    default_model_path,
    describe_runtime,
)
from swingguru.synthetic import SyntheticClip, draw_synthetic_frame, make_synthetic_clip

MODEL_PATH = default_model_path()
MODEL_AVAILABLE = MODEL_PATH.exists() and MODEL_PATH.stat().st_size > 0

requires_model = pytest.mark.skipif(
    not MODEL_AVAILABLE,
    reason=f"pose model not present at {MODEL_PATH} (see {DEFAULT_MODEL_URL})",
)

GENERATED_PHOTO = (
    Path(__file__).resolve().parent / "fixtures" / "generated" / "golfer_address.jpg"
)


@pytest.mark.integration
@requires_model
def test_adapter_creates_and_runs_on_a_real_frame() -> None:
    clip = SyntheticClip(width=320, height=180, fps=24.0, frame_count=4)
    frame = draw_synthetic_frame(0, clip=clip)
    adapter = MediaPipePoseAdapter(MODEL_PATH)
    try:
        observation = adapter.detect(frame, 0.0)
        assert isinstance(observation, PoseObservation)
        if observation.detected:
            assert len(observation.landmarks) == len(LANDMARK_NAMES)
        # Repeating a timestamp must not raise: the adapter advances it.
        second = adapter.detect(frame, 0.0)
        assert isinstance(second, PoseObservation)
        assert adapter.frames_seen == 2
    finally:
        adapter.close()


@pytest.mark.integration
@requires_model
def test_adapter_rejects_a_bad_frame() -> None:
    adapter = MediaPipePoseAdapter(MODEL_PATH)
    try:
        with pytest.raises(PoseAdapterError):
            adapter.detect(np.zeros((0, 0, 3), dtype=np.uint8), 0.0)
        with pytest.raises(PoseAdapterError):
            adapter.detect(np.zeros((10, 10), dtype=np.uint8), 0.0)
    finally:
        adapter.close()


@pytest.mark.integration
@requires_model
def test_real_model_runs_end_to_end_over_a_synthetic_clip(tmp_path: Path) -> None:
    clip = SyntheticClip(width=320, height=180, fps=24.0, frame_count=12)
    source = make_synthetic_clip(tmp_path / "smoke.mp4", clip=clip)
    result = process_video(
        source,
        tmp_path / "out",
        config=PipelineConfig(
            model_path=MODEL_PATH,
            measurement=MeasurementConfig(camera_view="down-the-line"),
            write_annotated=True,
            transcode_h264=False,
        ),
        phase_provider=ManualPhaseProvider(
            frames={Phase.ADDRESS: 0}, reference_frame=0
        ),
    )
    assert result.stats["frames"] == clip.frame_count
    assert result.pose_errors == 0
    assert result.pose_provider_name == "mediapipe-pose-landmarker"
    assert result.annotated_path is not None and result.annotated_path.exists()
    metadata = json.loads(
        result.artifact("metadata_json").read_text(encoding="utf-8")
    )
    assert metadata["runtime"]["mediapipe"]
    # The drawn figure is not a photograph, so this run is a plumbing check:
    # whether the model actually found a person is recorded, not asserted.
    assert metadata["stats"]["frames_with_pose"] >= 0


@pytest.mark.integration
@requires_model
def test_real_model_detects_a_photographic_person_if_supplied() -> None:
    if not GENERATED_PHOTO.exists():
        pytest.skip(
            "no photographic fixture present; golf tracking accuracy is "
            "unverified without real footage"
        )
    import cv2

    frame = cv2.imread(str(GENERATED_PHOTO))
    assert frame is not None
    adapter = MediaPipePoseAdapter(MODEL_PATH)
    try:
        observation = adapter.detect(frame, 0.0)
    finally:
        adapter.close()
    assert observation.detected, "the pose model found no person in the fixture"
    assert len(observation.landmarks) == len(LANDMARK_NAMES)


def test_mediapipe_connections_match_the_local_topology() -> None:
    pytest.importorskip("mediapipe")
    from swingguru.runtime_env import ensure_runtime_env
    from swingguru.landmarks import POSE_CONNECTIONS

    ensure_runtime_env()
    from mediapipe.tasks.python.vision import PoseLandmarksConnections

    # MediaPipe 1.0 exposes the connection list as ``POSE_LANDMARKS``; older
    # releases used ``POSE_CONNECTIONS``. Accept either shape.
    raw = getattr(PoseLandmarksConnections, "POSE_CONNECTIONS", None) or getattr(
        PoseLandmarksConnections, "POSE_LANDMARKS"
    )
    upstream = {
        (int(connection.start), int(connection.end))
        for connection in raw
        if hasattr(connection, "start") and hasattr(connection, "end")
    }
    assert upstream, "no connection list found in the installed MediaPipe"
    assert set(POSE_CONNECTIONS) == upstream


def test_runtime_description_reports_versions() -> None:
    runtime = describe_runtime()
    assert runtime["python"]
    assert runtime["opencv"]
    assert runtime["numpy"]
