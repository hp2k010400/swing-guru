"""End-to-end pipeline behaviour with a deterministic pose provider."""

from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path

import pytest

from swingguru.landmarks import LANDMARK_NAMES
from swingguru.measurements import MEASUREMENT_KEYS, MeasurementConfig
from swingguru.phases import ManualPhaseProvider, Phase
from swingguru.pipeline import (
    PipelineConfig,
    PipelineError,
    finalize_run,
    process_video,
    recompute_with_reference,
)
from swingguru.synthetic import (
    ScriptedPoseProvider,
    SyntheticClip,
    make_synthetic_clip,
)
from swingguru.video import VideoOpenError, is_readable_video

LANDSCAPE = SyntheticClip(width=320, height=180, fps=24.0, frame_count=24)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def run(
    source: Path,
    output: Path,
    provider: ScriptedPoseProvider,
    *,
    measurement: MeasurementConfig | None = None,
    transcode: bool = False,
    phases: dict[Phase, int] | None = None,
    reference: int | None = 0,
    write_annotated: bool = True,
    max_frames: int | None = None,
    start_frame: int = 0,
):
    config = PipelineConfig(
        measurement=measurement or MeasurementConfig(),
        write_annotated=write_annotated,
        transcode_h264=transcode,
        max_frames=max_frames,
        start_frame=start_frame,
    )
    return process_video(
        source,
        output,
        config=config,
        pose_provider=provider,
        phase_provider=ManualPhaseProvider(
            frames=phases or {Phase.ADDRESS: 0, Phase.TOP: 12, Phase.IMPACT: 20},
            reference_frame=reference,
            # Tests pass the reference explicitly, including "none".
            derive_reference_from_address=False,
        ),
    )


@pytest.fixture()
def landscape_video(tmp_path: Path) -> Path:
    return make_synthetic_clip(tmp_path / "landscape.mp4", clip=LANDSCAPE)


def test_full_run_writes_every_artifact(tmp_path: Path, landscape_video: Path) -> None:
    output = tmp_path / "out"
    provider = ScriptedPoseProvider(clip=LANDSCAPE)
    result = run(landscape_video, output, provider)

    assert result.stats["frames"] == LANDSCAPE.frame_count
    assert result.stats["frames_with_pose"] == LANDSCAPE.frame_count
    assert result.pose_errors == 0
    assert result.phases.reference_frame == 0

    for key in ("measurements_csv", "landmarks_csv", "phases_json", "summary_json",
                "metadata_json", "annotated_video"):
        path = result.artifact(key)
        assert path is not None, key
        assert Path(path).exists(), key

    measurements = read_csv(result.artifact("measurements_csv"))
    assert len(measurements) == LANDSCAPE.frame_count
    assert measurements[0]["frame_index"] == "0"
    assert measurements[0]["pose_detected"] == "1"
    assert measurements[0]["reference_frame"] == "0"
    for key in MEASUREMENT_KEYS:
        assert key in measurements[0]
        assert f"{key}_valid" in measurements[0]

    landmarks = read_csv(result.artifact("landmarks_csv"))
    assert len(landmarks) == LANDSCAPE.frame_count * len(LANDMARK_NAMES)
    assert {row["landmark_name"] for row in landmarks} == set(LANDMARK_NAMES)
    assert all(row["usable"] == "1" for row in landmarks)

    metadata = json.loads(result.artifact("metadata_json").read_text("utf-8"))
    assert metadata["reference_frame"] == 0
    assert metadata["video"]["orientation"] == "landscape"
    assert metadata["runtime"]["mediapipe"]
    assert len(metadata["measurements"]) == len(MEASUREMENT_KEYS)
    assert metadata["phase_source"] == "manual"

    phases = json.loads(result.artifact("phases_json").read_text("utf-8"))
    assert phases["frames"]["address"] == 0
    assert phases["reference_frame"] == 0

    annotated = result.annotated_path
    assert annotated is not None and is_readable_video(annotated)


def test_reference_frame_drives_relative_measurements(
    tmp_path: Path, landscape_video: Path
) -> None:
    output = tmp_path / "out"
    provider = ScriptedPoseProvider(clip=LANDSCAPE)
    result = run(landscape_video, output, provider, reference=0)
    rows = read_csv(result.artifact("measurements_csv"))
    assert float(rows[0]["head_displacement_px"]) == pytest.approx(0.0, abs=1e-6)
    assert any(float(row["head_displacement_px"]) > 0 for row in rows[5:])

    without = run(
        landscape_video, tmp_path / "out_none", provider, reference=None
    )
    assert without.phases.reference_frame is None
    rows = read_csv(without.artifact("measurements_csv"))
    assert all(row["head_displacement_px"] == "" for row in rows)
    assert all(row["head_displacement_px_valid"] == "0" for row in rows)
    assert any("no reference frame selected" in warning for warning in without.warnings)


def test_tracking_loss_keeps_frame_records_without_carrying_values(
    tmp_path: Path, landscape_video: Path
) -> None:
    output = tmp_path / "out"
    provider = ScriptedPoseProvider(clip=LANDSCAPE, drop_frames=frozenset({5, 6}))
    result = run(landscape_video, output, provider)

    assert len(result.records) == LANDSCAPE.frame_count
    lost = [record for record in result.records if record.index in (5, 6)]
    assert len(lost) == 2
    assert all(not record.pose_detected for record in lost)
    for record in lost:
        assert all(not item.valid for item in record.measurements.values())
        assert all(item.value is None for item in record.measurements.values())

    rows = read_csv(result.artifact("measurements_csv"))
    assert len(rows) == LANDSCAPE.frame_count
    lost_rows = [row for row in rows if row["frame_index"] in {"5", "6"}]
    assert len(lost_rows) == 2
    for row in lost_rows:
        assert row["pose_detected"] == "0"
        assert row["torso_inclination_deg"] == ""
        assert row["torso_inclination_deg_valid"] == "0"
        assert "no pose detected" in row["warnings"]

    landmarks = read_csv(result.artifact("landmarks_csv"))
    assert len(landmarks) == (LANDSCAPE.frame_count - 2) * len(LANDMARK_NAMES)
    assert result.stats["frames_without_pose"] == 2


def test_handedness_swaps_lead_and_trail(tmp_path: Path, landscape_video: Path) -> None:
    provider = ScriptedPoseProvider(clip=LANDSCAPE)
    right = run(landscape_video, tmp_path / "right", provider)
    left = run(
        landscape_video,
        tmp_path / "left",
        provider,
        measurement=MeasurementConfig(handedness="left"),
    )
    right_row = read_csv(right.artifact("measurements_csv"))[8]
    left_row = read_csv(left.artifact("measurements_csv"))[8]
    assert float(right_row["lead_knee_flex_deg"]) == pytest.approx(
        float(left_row["trail_knee_flex_deg"]), abs=1e-6
    )
    assert float(right_row["trail_knee_flex_deg"]) == pytest.approx(
        float(left_row["lead_knee_flex_deg"]), abs=1e-6
    )


def test_occluded_landmark_rejects_only_the_affected_measurements(
    tmp_path: Path, landscape_video: Path
) -> None:
    output = tmp_path / "out"
    provider = ScriptedPoseProvider(clip=LANDSCAPE, occlude=frozenset({"left_knee"}))
    result = run(landscape_video, output, provider)
    rows = read_csv(result.artifact("measurements_csv"))
    assert all(row["lead_knee_flex_deg_valid"] == "0" for row in rows)
    assert any("visibility below threshold" in row["warnings"] for row in rows)
    assert any(row["trail_knee_flex_deg_valid"] == "1" for row in rows)
    assert all(row["torso_inclination_deg_valid"] == "1" for row in rows)


def test_portrait_video_is_processed(tmp_path: Path) -> None:
    clip = SyntheticClip(width=180, height=360, fps=30.0, frame_count=10)
    source = make_synthetic_clip(tmp_path / "portrait.mp4", clip=clip)
    result = run(
        source, tmp_path / "out", ScriptedPoseProvider(clip=clip)
    )
    assert result.info.orientation == "portrait"
    assert result.stats["frames"] == 10
    rows = read_csv(result.artifact("measurements_csv"))
    assert rows[0]["frame_width"] == "180"
    assert rows[0]["frame_height"] == "360"


def test_start_and_max_frames_limit_the_run(
    tmp_path: Path, landscape_video: Path
) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        start_frame=5,
        max_frames=5,
    )
    assert [record.index for record in result.records] == [5, 6, 7, 8, 9]
    assert result.stats["frames"] == 5
    rows = read_csv(result.artifact("measurements_csv"))
    assert [row["frame_index"] for row in rows] == ["5", "6", "7", "8", "9"]
    assert result.annotated_path is not None
    assert is_readable_video(result.annotated_path)


def test_measurements_only_run_skips_the_video(tmp_path: Path, landscape_video: Path) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        write_annotated=False,
    )
    assert result.annotated_path is None
    assert "annotated_video" not in result.artifacts
    assert result.artifact("measurements_csv") is not None


def test_recompute_with_reference_updates_cached_records(
    tmp_path: Path, landscape_video: Path
) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        reference=None,
        write_annotated=False,
    )
    assert all(
        not record.measurements["head_displacement_px"].valid
        for record in result.records
    )
    recompute_with_reference(
        result.records, 4, config=result.config.measurement
    )
    assert result.records[4].measurements["head_displacement_px"].valid
    assert result.records[4].measurements["head_displacement_px"].value == pytest.approx(
        0.0, abs=1e-6
    )
    assert result.records[4].reference is not None


def test_finalize_run_reexports_from_cached_records(
    tmp_path: Path, landscape_video: Path
) -> None:
    analysis = run(
        landscape_video,
        tmp_path / "analysis",
        ScriptedPoseProvider(clip=LANDSCAPE),
        reference=None,
        write_annotated=False,
    )
    export_config = PipelineConfig(
        measurement=analysis.config.measurement,
        write_annotated=True,
        transcode_h264=False,
    )
    finished = finalize_run(
        landscape_video,
        tmp_path / "final",
        analysis.records,
        info=analysis.info,
        config=export_config,
        phases=ManualPhaseProvider(
            frames={Phase.ADDRESS: 3}, reference_frame=3
        ).select(),
    )
    assert finished.annotated_path is not None
    assert is_readable_video(finished.annotated_path)
    rows = read_csv(finished.artifact("measurements_csv"))
    assert rows[3]["head_displacement_px"] == "0.0"
    assert rows[0]["head_displacement_px_valid"] == "1"


def test_invalid_video_raises_before_processing(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not video")
    with pytest.raises(VideoOpenError):
        run(broken, tmp_path / "out", ScriptedPoseProvider(clip=LANDSCAPE))


def test_out_of_range_reference_is_reported(tmp_path: Path, landscape_video: Path) -> None:
    output = tmp_path / "out"
    result = run(
        landscape_video,
        output,
        ScriptedPoseProvider(clip=LANDSCAPE),
        reference=999,
    )
    assert any("outside the processed frames" in warning for warning in result.warnings)
    rows = read_csv(result.artifact("measurements_csv"))
    assert all(row["head_displacement_px_valid"] == "0" for row in rows)


def test_reference_inside_a_truncated_run_is_valid(
    tmp_path: Path, landscape_video: Path
) -> None:
    """A run that starts at frame 5 must validate against 5..7, not 0..2."""

    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        start_frame=5,
        max_frames=3,
        reference=7,
        phases={Phase.ADDRESS: 7},
    )
    assert result.phases.reference_frame == 7
    assert not any(
        "outside the processed frames" in warning for warning in result.warnings
    )
    rows = read_csv(result.artifact("measurements_csv"))
    assert [row["frame_index"] for row in rows] == ["5", "6", "7"]
    assert all(row["head_displacement_px_valid"] == "1" for row in rows)


def test_reference_outside_the_processed_window_is_unavailable(
    tmp_path: Path, landscape_video: Path
) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        start_frame=5,
        max_frames=3,
        reference=0,
    )
    assert any(
        "reference frame 0 is outside the processed frames (5..7)" in warning
        for warning in result.warnings
    )
    rows = read_csv(result.artifact("measurements_csv"))
    assert all(row["head_displacement_px_valid"] == "0" for row in rows)


def test_truncated_run_reports_the_max_frames_limit(
    tmp_path: Path, landscape_video: Path
) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        max_frames=5,
    )
    assert any("max_frames limit (5)" in warning for warning in result.warnings)


def test_estimated_frame_rate_is_surfaced(
    tmp_path: Path, landscape_video: Path
) -> None:
    analysis = run(
        landscape_video,
        tmp_path / "analysis",
        ScriptedPoseProvider(clip=LANDSCAPE),
        write_annotated=False,
    )
    finished = finalize_run(
        landscape_video,
        tmp_path / "out",
        analysis.records,
        info=replace(analysis.info, fps_estimated=True, fps=25.0),
        config=PipelineConfig(
            measurement=analysis.config.measurement,
            write_annotated=False,
        ),
        phases=ManualPhaseProvider(
            frames={Phase.ADDRESS: 0}, reference_frame=0
        ).select(),
    )
    assert any("frame rate" in warning for warning in finished.warnings)


def test_mirrored_input_is_reported_in_warnings_and_metadata(
    tmp_path: Path, landscape_video: Path
) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        measurement=MeasurementConfig(mirrored_input=True),
    )
    assert any("mirrored input recorded" in warning for warning in result.warnings)
    metadata = json.loads(result.artifact("metadata_json").read_text("utf-8"))
    assert metadata["measurement_config"]["mirrored_input"] is True


def test_phase_out_of_range_is_reported(tmp_path: Path, landscape_video: Path) -> None:
    result = run(
        landscape_video,
        tmp_path / "out",
        ScriptedPoseProvider(clip=LANDSCAPE),
        phases={Phase.ADDRESS: 0, Phase.FINISH: 500},
    )
    assert any("finish frame 500" in warning for warning in result.warnings)


def test_missing_frames_are_rejected_when_no_frames_decode(tmp_path: Path) -> None:
    clip = SyntheticClip(width=160, height=90, fps=24.0, frame_count=4)
    source = make_synthetic_clip(tmp_path / "short.mp4", clip=clip)
    with pytest.raises(PipelineError):
        run(
            source,
            tmp_path / "out",
            ScriptedPoseProvider(clip=clip),
            start_frame=100,
        )
