"""End-to-end processing: video in, annotated video and data exports out.

The run is split into a detection pass and an optional render pass.

* Detection pass: decode each frame once, run pose, keep a light record per
  frame (landmarks plus absolute measurements). No decoded image is retained.
* Resolve phases and the reference frame, then recompute measurement values
  that depend on the reference. This is pure geometry over the retained
  records.
* Render pass: decode the video a second time, draw the overlay for each frame
  and write the annotated video.

Decoding twice costs decode time only; it keeps memory flat for long clips and
means a reference selected late in the clip still produces a correct annotated
video from the first frame.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from . import export
from .landmarks import PoseObservation
from .measurements import (
    MEASUREMENT_KEYS,
    FramePoints,
    MeasurementConfig,
    MeasurementResult,
    ReferenceState,
    build_frame_points,
    build_reference,
    compute_frame_measurements,
)
from .overlays import OverlayConfig, render_overlay
from .phases import ManualPhaseProvider, PhaseProvider, PhaseSelection
from .pose import (
    MediaPipePoseAdapter,
    PoseAdapterError,
    PoseProvider,
    describe_runtime,
    resolve_model_path,
)
from .video import (
    DEFAULT_MAX_FRAMES,
    AnnotatedVideoWriter,
    VideoInfo,
    VideoWriteError,
    open_video,
    probe_video,
    transcode_to_h264,
)

ProgressCallback = Callable[[float, str], None]

MAX_CONSECUTIVE_POSE_ERRORS = 10


class PipelineError(RuntimeError):
    """Raised when a run cannot continue."""


@dataclass
class FrameRecord:
    """Everything retained for one decoded frame.

    A record exists for every decoded frame, including frames where the pose
    model found nobody. ``measurements`` for such a frame are constructed by
    the normal code path and rejected with an explicit warning, so a valid
    value is never carried across tracking loss.
    """

    index: int
    timestamp_ms: float
    width: int
    height: int
    observation: PoseObservation
    points: FramePoints | None
    measurements: dict[str, MeasurementResult]
    reference: ReferenceState | None = None
    notes: tuple[str, ...] = ()

    @property
    def pose_detected(self) -> bool:
        return bool(self.observation.detected)

    def measurement(self, key: str) -> MeasurementResult | None:
        return self.measurements.get(key)

    def valid_keys(self) -> tuple[str, ...]:
        return tuple(key for key, result in self.measurements.items() if result.valid)

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_index": self.index,
            "timestamp_ms": self.timestamp_ms,
            "width": self.width,
            "height": self.height,
            "pose_detected": self.pose_detected,
            "reference_frame": None if self.reference is None else self.reference.frame_index,
            "notes": list(self.notes),
            "measurements": {
                key: result.to_dict() for key, result in self.measurements.items()
            },
            "landmarks": [landmark.to_dict() for landmark in self.observation.landmarks],
            "world_landmarks": [
                landmark.to_dict() for landmark in self.observation.world_landmarks
            ],
        }


@dataclass
class PipelineConfig:
    """Options that shape a single processing run."""

    model_path: Path | str | None = None
    measurement: MeasurementConfig = field(default_factory=MeasurementConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    start_frame: int = 0
    max_frames: int | None = DEFAULT_MAX_FRAMES
    write_annotated: bool = True
    transcode_h264: bool = True
    keep_raw_annotated: bool = True
    annotated_name: str = "annotated.mp4"
    raw_annotated_name: str = "annotated_raw.mp4"
    pose_detection_confidence: float = 0.5
    pose_presence_confidence: float = 0.5
    pose_tracking_confidence: float = 0.5
    pose_delegate: str = "cpu"

    def to_dict(self) -> dict[str, object]:
        return {
            "model_path": None if self.model_path is None else str(self.model_path),
            "start_frame": self.start_frame,
            "max_frames": self.max_frames,
            "write_annotated": self.write_annotated,
            "transcode_h264": self.transcode_h264,
            "keep_raw_annotated": self.keep_raw_annotated,
            "annotated_name": self.annotated_name,
            "raw_annotated_name": self.raw_annotated_name,
            "pose_detection_confidence": self.pose_detection_confidence,
            "pose_presence_confidence": self.pose_presence_confidence,
            "pose_tracking_confidence": self.pose_tracking_confidence,
            "pose_delegate": self.pose_delegate,
            "overlay": {
                "draw_skeleton": self.overlay.draw_skeleton,
                "draw_measurements": self.overlay.draw_measurements,
                "draw_quality": self.overlay.draw_quality,
                "show_all_landmarks": self.overlay.show_all_landmarks,
            },
        }


@dataclass
class ProcessingResult:
    """Everything a caller needs to inspect or continue a run."""

    input_path: Path
    output_dir: Path
    info: VideoInfo
    config: PipelineConfig
    records: list[FrameRecord]
    phases: PhaseSelection
    reference: ReferenceState | None
    artifacts: dict[str, Path]
    warnings: list[str]
    stats: dict[str, object]
    runtime: dict[str, str]
    pose_provider_name: str
    pose_errors: int
    duration_s: float

    @property
    def annotated_path(self) -> Path | None:
        return self.artifacts.get("annotated_video")

    def artifact(self, key: str) -> Path | None:
        return self.artifacts.get(key)

    def record_at(self, index: int) -> FrameRecord | None:
        for record in self.records:
            if record.index == index:
                return record
        return None

    def frame_index_bounds(self) -> tuple[int, int] | None:
        if not self.records:
            return None
        return self.records[0].index, self.records[-1].index


def _emit(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    if progress is not None:
        progress(max(0.0, min(1.0, fraction)), message)


def build_record(
    decoded_index: int,
    timestamp_ms: float,
    frame: np.ndarray,
    observation: PoseObservation,
    *,
    config: MeasurementConfig,
    notes: Sequence[str] = (),
) -> FrameRecord:
    """Turn one decoded frame plus its pose into a frame record."""

    height, width = frame.shape[:2]
    points = build_frame_points(
        observation, width=width, height=height, config=config
    )
    measurements = compute_frame_measurements(points, None, config)
    return FrameRecord(
        index=decoded_index,
        timestamp_ms=timestamp_ms,
        width=width,
        height=height,
        observation=observation,
        points=points,
        measurements=measurements,
        reference=None,
        notes=tuple(notes),
    )


def apply_reference(
    records: Sequence[FrameRecord],
    reference_frame: int | None,
    *,
    config: MeasurementConfig,
) -> ReferenceState | None:
    """Recompute every record's measurements against the selected reference.

    Returns the reference state used. ``None`` means no reference frame was
    selected, in which case relative measurements stay unavailable on every
    frame.
    """

    if reference_frame is None:
        for record in records:
            record.reference = None
            record.measurements = compute_frame_measurements(
                record.points or FramePoints(pixels={}, landmarks={}), None, config
            )
        return None

    anchor = next((r for r in records if r.index == reference_frame), None)
    width = anchor.width if anchor is not None else (records[0].width if records else 0)
    height = (
        anchor.height if anchor is not None else (records[0].height if records else 0)
    )
    reference = build_reference(
        reference_frame,
        None if anchor is None else anchor.points,
        width=width,
        height=height,
        config=config,
    )

    for record in records:
        record.reference = reference
        record.measurements = compute_frame_measurements(
            record.points or FramePoints(pixels={}, landmarks={}), reference, config
        )
    return reference


def _detection_pass(
    input_path: Path,
    config: PipelineConfig,
    provider: PoseProvider,
    *,
    progress: ProgressCallback | None,
    progress_span: tuple[float, float],
) -> tuple[list[FrameRecord], VideoInfo, int, list[str]]:
    warnings: list[str] = []
    records: list[FrameRecord] = []
    pose_errors = 0
    consecutive_errors = 0

    with open_video(input_path) as stream:
        info = stream.info
        if info.pixels > 3840 * 2160:
            warnings.append(
                f"high-resolution input ({info.width}x{info.height}); processing "
                "will be slow"
            )
        if info.frame_count is None:
            warnings.append(
                "container did not report a frame count; frame numbering comes "
                "from the decode loop"
            )

        limit = config.max_frames
        total_hint = (
            min(info.frame_count - config.start_frame, limit)
            if info.frame_count is not None and limit is not None
            else (info.frame_count if info.frame_count is not None else None)
        )

        for decoded in stream.frames(start=config.start_frame, max_frames=limit):
            notes: list[str] = []
            try:
                observation = provider.detect(decoded.image, decoded.timestamp_ms)
                consecutive_errors = 0
            except Exception as error:  # noqa: BLE001 - surfaced in run warnings
                pose_errors += 1
                consecutive_errors += 1
                observation = PoseObservation.missing(
                    f"pose detection failed on frame {decoded.index}"
                )
                notes.append(f"pose error: {type(error).__name__}: {error}")
                if consecutive_errors >= MAX_CONSECUTIVE_POSE_ERRORS:
                    raise PipelineError(
                        "pose detection failed on "
                        f"{consecutive_errors} consecutive frames; last error: {error}"
                    ) from error

            records.append(
                build_record(
                    decoded.index,
                    decoded.timestamp_ms,
                    decoded.image,
                    observation,
                    config=config.measurement,
                    notes=notes,
                )
            )

            processed = len(records)
            if total_hint:
                fraction = progress_span[0] + (progress_span[1] - progress_span[0]) * min(
                    1.0, processed / max(1, total_hint)
                )
            else:
                fraction = progress_span[0]
            _emit(progress, fraction, f"pose frame {decoded.index}")

    if not records:
        raise PipelineError(
            f"no frames were decoded from {input_path} "
            f"(start_frame={config.start_frame}, max_frames={config.max_frames})"
        )
    if pose_errors:
        warnings.append(
            f"{pose_errors} frame(s) raised a pose error and were recorded as "
            "tracking loss"
        )
    return records, info, pose_errors, warnings


def _render_pass(
    input_path: Path,
    records: Sequence[FrameRecord],
    config: PipelineConfig,
    output_dir: Path,
    *,
    progress: ProgressCallback | None,
    progress_span: tuple[float, float],
) -> tuple[dict[str, Path], list[str]]:
    artifacts: dict[str, Path] = {}
    warnings: list[str] = []
    by_index = {record.index: record for record in records}
    first_index = records[0].index

    raw_path = output_dir / config.raw_annotated_name
    final_path = output_dir / config.annotated_name

    with open_video(input_path) as stream:
        info = stream.info
        overlay_config = replace(
            config.overlay, fps=info.fps, quality=config.measurement
        )
        writer = AnnotatedVideoWriter(
            raw_path,
            fps=info.fps,
            size=(info.width, info.height),
        )
        try:
            written = 0
            for decoded in stream.frames(start=first_index):
                record = by_index.get(decoded.index)
                if record is None:
                    break
                annotated = render_overlay(decoded.image, record, overlay_config)
                if annotated.shape[:2] != (info.height, info.width):
                    annotated = np.ascontiguousarray(annotated)
                writer.write(annotated)
                written += 1
                span = max(1, len(records))
                _emit(
                    progress,
                    progress_span[0]
                    + (progress_span[1] - progress_span[0]) * min(1.0, written / span),
                    f"render frame {decoded.index}",
                )
                if written >= len(records):
                    break
        finally:
            writer.close()

    if writer.frames_written != len(records):
        warnings.append(
            f"annotated video contains {writer.frames_written} of "
            f"{len(records)} processed frames"
        )

    if config.transcode_h264:
        result = transcode_to_h264(raw_path, final_path)
        if result.ok:
            artifacts["annotated_video"] = final_path
            artifacts["annotated_video_codec"] = Path(str(result.codec or "h264"))
            if not config.keep_raw_annotated:
                raw_path.unlink(missing_ok=True)
            else:
                artifacts["annotated_video_raw"] = raw_path
            warnings.append(f"browser playback: {result.message}")
        else:
            artifacts["annotated_video"] = raw_path
            warnings.append(
                "browser playback: " + result.message + "; using the raw MPEG-4 file"
            )
    else:
        artifacts["annotated_video"] = raw_path
        warnings.append(
            "H.264 transcode disabled; the raw MPEG-4 output may not play in a "
            "browser"
        )
    return artifacts, warnings


def process_video(
    input_path: Path | str,
    output_dir: Path | str,
    *,
    config: PipelineConfig | None = None,
    pose_provider: PoseProvider | None = None,
    phase_provider: PhaseProvider | None = None,
    progress: ProgressCallback | None = None,
) -> ProcessingResult:
    """Process one video end to end.

    ``pose_provider`` and ``phase_provider`` exist so tests and the debug UI can
    substitute deterministic providers without touching the MediaPipe runtime.
    """

    started = time.perf_counter()
    settings = config or PipelineConfig()
    source = Path(input_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    _emit(progress, 0.0, "probing video")
    info = probe_video(source)

    owns_provider = pose_provider is None
    provider = pose_provider
    if provider is None:
        model_path = resolve_model_path(settings.model_path)
        try:
            provider = MediaPipePoseAdapter(
                model_path,
                num_poses=1,
                min_detection_confidence=settings.pose_detection_confidence,
                min_presence_confidence=settings.pose_presence_confidence,
                min_tracking_confidence=settings.pose_tracking_confidence,
                delegate=settings.pose_delegate,
            )
        except PoseAdapterError:
            raise

    try:
        records, info, pose_errors, warnings = _detection_pass(
            source,
            settings,
            provider,
            progress=progress,
            progress_span=(0.0, 0.8 if settings.write_annotated else 0.95),
        )
    finally:
        if owns_provider:
            try:
                provider.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass

    return finalize_run(
        source,
        destination,
        records,
        info=info,
        config=settings,
        phase_provider=phase_provider,
        progress=progress,
        progress_span=(0.8, 0.97) if settings.write_annotated else (0.85, 0.97),
        pose_provider_name=str(getattr(provider, "name", type(provider).__name__)),
        pose_errors=pose_errors,
        warnings=warnings,
        started=started,
    )


def finalize_run(
    input_path: Path | str,
    output_dir: Path | str,
    records: list[FrameRecord],
    *,
    info: VideoInfo,
    config: PipelineConfig,
    phase_provider: PhaseProvider | None = None,
    phases: PhaseSelection | None = None,
    progress: ProgressCallback | None = None,
    progress_span: tuple[float, float] = (0.8, 0.97),
    pose_provider_name: str = "reused-records",
    pose_errors: int = 0,
    warnings: Sequence[str] = (),
    runtime: Mapping[str, str] | None = None,
    started: float | None = None,
) -> ProcessingResult:
    """Resolve phases, apply the reference, render and export.

    Split out from :func:`process_video` so the debug viewer can re-render and
    re-export from cached detection records when the user changes the reference
    frame, without paying for pose inference again.
    """

    begin = started if started is not None else time.perf_counter()
    source = Path(input_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    collected_warnings = list(warnings)

    if phases is None:
        selector: PhaseProvider = phase_provider or ManualPhaseProvider()
        phases = selector.select(
            records if selector.requires_records else None
        )
    processed_frames = [record.index for record in records]
    validation = phases.validate(processed_frames)
    if validation:
        phases = replace(phases, warnings=phases.warnings + validation)
        collected_warnings.extend(validation)

    if phases.reference_frame is None:
        collected_warnings.append(
            "relative measurements are unavailable: no reference frame selected"
        )

    if (
        config.max_frames is not None
        and len(processed_frames) >= config.max_frames
        and info.frame_count is not None
        and processed_frames[-1] < info.frame_count - 1
    ):
        collected_warnings.append(
            f"processing stopped at the max_frames limit ({config.max_frames}); "
            f"the clip is truncated at frame {processed_frames[-1]} of "
            f"{info.frame_count - 1}"
        )
    if info.fps_estimated:
        collected_warnings.append(
            "the container did not report a usable frame rate; timestamps assume "
            f"{info.fps:g} fps and may not match the real capture rate"
        )
    if config.measurement.mirrored_input:
        collected_warnings.append(
            "mirrored input recorded: lead and trail anatomical labels assume an "
            "unmirrored video and were NOT corrected for mirroring"
        )

    reference = apply_reference(
        records, phases.reference_frame, config=config.measurement
    )

    artifacts: dict[str, Path] = {}
    if config.write_annotated:
        rendered, render_warnings = _render_pass(
            source,
            records,
            config,
            destination,
            progress=progress,
            progress_span=progress_span,
        )
        artifacts.update(rendered)
        collected_warnings.extend(render_warnings)

    _emit(progress, 0.98, "writing exports")
    artifacts["measurements_csv"] = export.write_measurements_csv(
        records, destination / "measurements.csv"
    )
    artifacts["landmarks_csv"] = export.write_landmarks_csv(
        records, destination / "landmarks.csv", config=config.measurement
    )
    artifacts["phases_json"] = export.write_json(
        phases.to_dict(), destination / "phases.json"
    )
    stats = export.summarize_records(records)
    artifacts["summary_json"] = export.write_json(
        stats, destination / "summary.json"
    )

    runtime_info = dict(runtime) if runtime is not None else describe_runtime()
    metadata = export.build_metadata(
        input_path=source,
        info=info.to_dict(),
        config=config.measurement,
        pipeline_config=config.to_dict(),
        phases=phases,
        artifacts={key: str(value) for key, value in artifacts.items()},
        runtime=runtime_info,
        stats=stats,
        warnings=collected_warnings,
    )
    artifacts["metadata_json"] = export.write_json(
        metadata, destination / "metadata.json"
    )

    _emit(progress, 1.0, "done")
    return ProcessingResult(
        input_path=source,
        output_dir=destination,
        info=info,
        config=config,
        records=records,
        phases=phases,
        reference=reference,
        artifacts=artifacts,
        warnings=collected_warnings,
        stats=stats,
        runtime=runtime_info,
        pose_provider_name=pose_provider_name,
        pose_errors=pose_errors,
        duration_s=time.perf_counter() - begin,
    )


def recompute_with_reference(
    records: Sequence[FrameRecord],
    reference_frame: int | None,
    *,
    config: MeasurementConfig,
) -> ReferenceState | None:
    """Re-derive relative measurements for cached records.

    Used by the debug viewer so a reference change updates every value and the
    next export without paying for pose inference again.
    """

    return apply_reference(records, reference_frame, config=config)


__all__ = [
    "FrameRecord",
    "finalize_run",
    "PipelineConfig",
    "PipelineError",
    "ProcessingResult",
    "apply_reference",
    "build_record",
    "process_video",
    "recompute_with_reference",
    "VideoWriteError",
    "MEASUREMENT_KEYS",
]
