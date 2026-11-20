"""CSV and JSON exports for a processed run.

Exports are written so a run can be inspected without the engine: frame-level
quality and measurement values in ``measurements.csv``, every detected
landmark in long form in ``landmarks.csv``, and JSON metadata that records the
exact configuration, runtime versions and measurement definitions used.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .landmarks import LANDMARK_NAMES
from .measurements import (
    MEASUREMENT_KEYS,
    MEASUREMENT_SPECS,
    MeasurementConfig,
    MeasurementResult,
    landmark_rejection_reason,
    summarize_series,
)
from .phases import PHASE_ORDER, PhaseSelection

ROUND_DIGITS = 6


@runtime_checkable
class RecordLike(Protocol):
    index: int
    timestamp_ms: float
    width: int
    height: int
    measurements: Mapping[str, MeasurementResult]
    notes: tuple[str, ...]

    @property
    def pose_detected(self) -> bool:
        ...


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), ROUND_DIGITS)


def measurements_header() -> list[str]:
    header = [
        "frame_index",
        "timestamp_ms",
        "frame_width",
        "frame_height",
        "pose_detected",
        "reference_frame",
    ]
    for key in MEASUREMENT_KEYS:
        header.append(f"{key}")
        header.append(f"{key}_valid")
    header.append("warnings")
    return header


def write_measurements_csv(records: Sequence[RecordLike], path: Path | str) -> Path:
    """One row per decoded frame, including frames where tracking was lost."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(measurements_header())
        for record in records:
            row: list[object] = [
                record.index,
                _round(record.timestamp_ms),
                record.width,
                record.height,
                int(bool(record.pose_detected)),
                None if not getattr(record, "reference", None) else record.reference.frame_index,  # type: ignore[attr-defined]
            ]
            warnings: list[str] = list(record.notes)
            for key in MEASUREMENT_KEYS:
                result = record.measurements.get(key)
                if result is None:
                    row.extend([None, 0])
                    warnings.append(f"{key}: measurement not computed")
                    continue
                row.append(_round(result.value))
                row.append(int(result.valid))
                for warning in result.warnings:
                    warnings.append(f"{key}: {warning}")
            row.append(" | ".join(warnings))
            writer.writerow(row)
    return target


def landmarks_header() -> list[str]:
    return [
        "frame_index",
        "timestamp_ms",
        "landmark_index",
        "landmark_name",
        "x",
        "y",
        "z",
        "visibility",
        "presence",
        "world_x",
        "world_y",
        "world_z",
        "in_frame",
        "usable",
        "quality",
    ]


def write_landmarks_csv(
    records: Sequence[RecordLike],
    path: Path | str,
    *,
    config: MeasurementConfig,
) -> Path:
    """Long-format landmarks. Only frames with a detected pose contribute rows.

    ``usable`` applies the same visibility, presence and in-frame checks the
    measurements use, so a rejected measurement can be traced to the exact
    landmark that failed.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(landmarks_header())
        for record in records:
            observation = getattr(record, "observation", None)
            if observation is None or not observation.detected:
                continue
            world_by_index = {
                landmark.index: landmark for landmark in observation.world_landmarks
            }
            for landmark in observation.landmarks:
                world = world_by_index.get(landmark.index)
                in_frame = landmark.in_frame(config.frame_margin)
                quality = landmark_rejection_reason(
                    landmark, landmark.name, config
                )
                writer.writerow(
                    [
                        record.index,
                        _round(record.timestamp_ms),
                        landmark.index,
                        landmark.name,
                        _round(landmark.x),
                        _round(landmark.y),
                        _round(landmark.z),
                        _round(landmark.visibility),
                        _round(landmark.presence),
                        _round(None if world is None else world.x),
                        _round(None if world is None else world.y),
                        _round(None if world is None else world.z),
                        int(in_frame),
                        int(quality is None),
                        quality or "",
                    ]
                )
    return target


def write_json(payload: object, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False, allow_nan=False)
        handle.write("\n")
    return target


def build_metadata(
    *,
    input_path: Path,
    info: Mapping[str, object],
    config: MeasurementConfig,
    pipeline_config: Mapping[str, object],
    phases: PhaseSelection,
    artifacts: Mapping[str, str],
    runtime: Mapping[str, str],
    stats: Mapping[str, object],
    warnings: Sequence[str],
) -> dict[str, object]:
    """Run metadata: everything needed to interpret the exported numbers."""

    return {
        "engine": "swing-guru-engine",
        "engine_version": "0.1.0",
        "input_path": str(input_path),
        "video": dict(info),
        "measurement_config": config.to_dict(),
        "pipeline_config": dict(pipeline_config),
        "reference_frame": phases.reference_frame,
        "phase_source": phases.source,
        "phase_warnings": list(phases.warnings),
        "measurements": [spec.to_dict(config) for spec in MEASUREMENT_SPECS],
        "landmark_names": list(LANDMARK_NAMES),
        "runtime": dict(runtime),
        "stats": dict(stats),
        "warnings": list(warnings),
        "artifacts": dict(artifacts),
        "notes": [
            "Pixel and percentage values are camera-relative; no centimetre "
            "conversion is attempted because the camera is uncalibrated.",
            "World landmarks are single-frame model estimates, not calibrated "
            "measurements, and are exported for inspection only.",
            "Visibility and presence are landmark confidence signals. They do "
            "not measure biomechanical accuracy.",
        ],
    }


def summarize_records(records: Sequence[RecordLike]) -> dict[str, object]:
    """Per-measurement validity statistics and descriptive spread."""

    total = len(records)
    pose_frames = sum(1 for record in records if record.pose_detected)
    summary: dict[str, object] = {
        "frames": total,
        "frames_with_pose": pose_frames,
        "frames_without_pose": total - pose_frames,
        "pose_detection_rate": (pose_frames / total) if total else None,
        "measurements": {},
    }

    per_measurement: dict[str, object] = {}
    for key in MEASUREMENT_KEYS:
        values: list[float | None] = []
        valid = 0
        reasons: Counter[str] = Counter()
        for record in records:
            result = record.measurements.get(key)
            if result is None:
                values.append(None)
                reasons["not computed"] += 1
                continue
            values.append(result.value if result.valid else None)
            if result.valid:
                valid += 1
            else:
                for warning in result.warnings:
                    reasons[warning.split(":")[0].split(" (")[0]] += 1
        stats = summarize_series(values)
        per_measurement[key] = {
            "valid_frames": valid,
            "invalid_frames": total - valid,
            "valid_fraction": (valid / total) if total else None,
            "stats": stats,
            "rejection_reasons": dict(reasons.most_common()),
        }
    summary["measurements"] = per_measurement

    stability = {
        "head_displacement_px_range": per_measurement["head_displacement_px"]["stats"]["range"],  # type: ignore[index]
        "head_displacement_px_std": per_measurement["head_displacement_px"]["stats"]["std"],  # type: ignore[index]
        "pelvis_displacement_px_range": per_measurement["pelvis_displacement_px"]["stats"]["range"],  # type: ignore[index]
        "pelvis_displacement_px_std": per_measurement["pelvis_displacement_px"]["stats"]["std"],  # type: ignore[index]
    }
    summary["stability_note"] = (
        "Head and pelvis stability are reported as the spread of displacement "
        "from the selected reference frame, not against an ideal range."
    )
    summary["stability"] = stability
    return summary


def phase_rows(selection: PhaseSelection) -> Iterable[dict[str, object]]:
    assigned = selection.assigned()
    for phase in PHASE_ORDER:
        yield {
            "phase": phase.value,
            "frame_index": assigned.get(phase),
            "assigned": phase in assigned,
            "is_reference": assigned.get(phase) == selection.reference_frame,
        }
