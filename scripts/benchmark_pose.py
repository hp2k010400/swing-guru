"""Measure RTMW pose variants against the accepted baseline.

    .venv\\Scripts\\python.exe scripts\\benchmark_pose.py VIDEO ^
        --baseline-records var\\analysis\\real\\pose_records.pkl ^
        --out var\\bench\\pose --variants cadence-5,cadence-10,cadence-15,sample-2

For each variant the harness reports the pose-stage wall time, how many frames
received a person-detection call and a full pose-model call, the six estimated
phase frames against the baseline, wrist/elbow coverage, and the largest
landmark and measurement deltas. Verdicts use the tolerances approved in
``docs/agent-work/pose-performance/PLAN.md``.

The baseline landmark set is used for *evaluation only*; no variant is allowed
to read it while producing candidates.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

import numpy as np  # noqa: E402

from swingguru.analysis_bundle import ANALYSIS_PHASES  # noqa: E402
from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.landmarks import PoseObservation  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import ManualPhaseProvider, Phase  # noqa: E402
from swingguru.pipeline import (  # noqa: E402
    FrameRecord,
    PipelineConfig,
    apply_reference,
    build_record,
    process_video,
)
from swingguru.pose_cache import load_records_any  # noqa: E402
from swingguru.pose_cache import save_pose_cache  # noqa: E402
from swingguru.video import open_video  # noqa: E402

# Approval gates from the plan: maximum review tolerances, not targets.
TOLERANCE_BODY_DEG = 0.5
TOLERANCE_MOVEMENT_PCT = 0.25
TOLERANCE_SHAFT_DEG = 1.0
TOLERANCE_GEOMETRY_PX = 2.0

MEASUREMENT_KEYS = (
    "torso_inclination_deg",
    "lead_knee_flex_deg",
    "trail_knee_flex_deg",
    "torso_angle_change_deg",
    "head_displacement_pct_height",
    "pelvis_displacement_pct_height",
)

TRACKED_JOINTS = (
    "left_wrist",
    "right_wrist",
    "left_elbow",
    "right_elbow",
    "hip_mid",
    "shoulder_mid",
    "nose",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)


class Timed:
    """Proxy that forwards attribute access and records call durations."""

    def __init__(self, target, log: list[float]) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_target"), name)

    def __call__(self, *args, **kwargs):
        began = time.perf_counter()
        out = object.__getattribute__(self, "_target")(*args, **kwargs)
        object.__getattribute__(self, "_log").append(time.perf_counter() - began)
        return out


@dataclass
class RunResult:
    name: str
    configuration: dict
    records: list = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    detector_seconds: float = 0.0
    pose_seconds: float = 0.0
    detector_calls: int = 0
    pose_calls: int = 0
    frames: int = 0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Running a variant
# ---------------------------------------------------------------------------


def provider_for(cadence: int):
    from swingguru.pose_rtm import RTMPoseAdapter

    return RTMPoseAdapter("wholebody", "balanced", det_frequency=cadence)


def run_pipeline_variant(
    video: Path,
    name: str,
    *,
    cadence: int = 5,
    start_frame: int = 0,
    max_frames: int | None = None,
    repeat: int = 1,
) -> RunResult:
    adapter = provider_for(cadence)
    tracker = adapter._ensure()
    detector_log: list[float] = []
    pose_log: list[float] = []
    real_detector, real_pose = tracker.det_model, tracker.pose_model
    tracker.det_model = Timed(real_detector, detector_log)
    tracker.pose_model = Timed(real_pose, pose_log)

    result = RunResult(
        name=name,
        configuration={
            "det_frequency": cadence,
            "start_frame": start_frame,
            "max_frames": max_frames,
            "tracking": tracker.tracking,
        },
    )
    try:
        for _ in range(max(1, repeat)):
            pose_log.clear()
            detector_log.clear()
            # The tracker counts frames internally and picks its own detection
            # frames from that counter, so a repeat must start from zero or it
            # would silently benchmark a different cadence phase.
            tracker.reset()
            began = time.perf_counter()
            outcome = process_video(
                video,
                ROOT / "var" / "bench" / "pose" / name,
                config=PipelineConfig(
                    measurement=MeasurementConfig(),
                    write_annotated=False,
                    start_frame=start_frame,
                    max_frames=max_frames,
                ),
                pose_provider=adapter,
                phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
            )
            result.seconds.append(time.perf_counter() - began)
            result.records = list(outcome.records)
            result.detector_calls = len(detector_log)
            result.pose_calls = len(pose_log)
            result.detector_seconds = sum(detector_log)
            result.pose_seconds = sum(pose_log)
            result.frames = len(result.records)
    finally:
        tracker.det_model = real_detector
        tracker.pose_model = real_pose
    return result


def run_sampling_variant(
    video: Path,
    name: str,
    *,
    step: int,
    cadence: int = 5,
) -> RunResult:
    """Infer every ``step``-th frame and linearly interpolate between them.

    Research path only: interpolated records are tagged in ``notes`` so nothing
    downstream can mistake them for inferred positions.
    """

    adapter = provider_for(cadence)
    tracker = adapter._ensure()
    detector_log: list[float] = []
    pose_log: list[float] = []
    real_detector, real_pose = tracker.det_model, tracker.pose_model
    tracker.det_model = Timed(real_detector, detector_log)
    tracker.pose_model = Timed(real_pose, pose_log)
    config = MeasurementConfig()
    result = RunResult(
        name=name,
        configuration={"sample_step": step, "det_frequency": cadence, "interpolated": True},
    )
    try:
        began = time.perf_counter()
        tracker.reset()
        with open_video(video) as stream:
            info = stream.info
            limit = info.frame_count if info.frame_count is not None else 1 << 30
            observations: dict[int, PoseObservation] = {}
            records: list[FrameRecord] = []
            inferred: list[int] = []
            for decoded in stream.frames():
                if decoded.index >= limit:
                    break
                record = build_record(
                    decoded.index,
                    decoded.timestamp_ms,
                    decoded.image,
                    PoseObservation.missing("interpolated frame; not inferred"),
                    config=config,
                    notes=("pose was not inferred on this frame",),
                )
                records.append(record)
                if decoded.index % step == 0:
                    observation = adapter.detect(decoded.image, decoded.timestamp_ms)
                    observations[decoded.index] = observation
                    inferred.append(decoded.index)
                    records[-1] = build_record(
                        decoded.index,
                        decoded.timestamp_ms,
                        decoded.image,
                        observation,
                        config=config,
                    )
        records = apply_interpolation(records, observations, config=config)
        result.records = records
        result.seconds.append(time.perf_counter() - began)
        result.detector_calls = len(detector_log)
        result.pose_calls = len(pose_log)
        result.detector_seconds = sum(detector_log)
        result.pose_seconds = sum(pose_log)
        result.frames = len(records)
        result.configuration["inferred_frames"] = len(inferred)
    finally:
        tracker.det_model = real_detector
        tracker.pose_model = real_pose
    return result


def apply_interpolation(
    records: Sequence[FrameRecord],
    observations: Mapping[int, PoseObservation],
    *,
    config: MeasurementConfig,
) -> list[FrameRecord]:
    """Fill the gaps between inferred frames by interpolation, tagged in notes.

    ``observations`` holds the inferred frames only. A frame with no inferred
    neighbour on both sides is left untouched (nothing is invented at the ends
    of the clip). Interpolated records keep the neighbouring frames' *lower*
    confidence and say so in ``notes``, so nothing downstream can read them as
    inferred.
    """

    inferred = sorted(observations)
    replaced: list[FrameRecord] = []
    for record in records:
        index = int(record.index)
        if index in observations:
            replaced.append(record)
            continue
        before = max((item for item in inferred if item < index), default=None)
        after = min((item for item in inferred if item > index), default=None)
        if before is None or after is None:
            replaced.append(record)
            continue
        blend = (index - before) / (after - before)
        observation = interpolate_observation(observations[before], observations[after], blend)
        replaced.append(
            build_record(
                index,
                record.timestamp_ms,
                np.zeros((record.height, record.width, 3), np.uint8),
                observation,
                config=config,
                notes=("pose interpolated between inferred frames",),
            )
        )
    return replaced


def interpolate_observation(first: PoseObservation, second: PoseObservation, blend: float) -> PoseObservation:
    """Linear landmark interpolation with the *lower* neighbour confidence."""

    by_name = {landmark.name: landmark for landmark in second.landmarks}
    landmarks = []
    for landmark in first.landmarks:
        other = by_name.get(landmark.name)
        if other is None:
            continue
        visibility = _lower(landmark.visibility, other.visibility)
        presence = _lower(landmark.presence, other.presence)
        landmarks.append(
            type(landmark)(
                index=landmark.index,
                name=landmark.name,
                x=landmark.x + (other.x - landmark.x) * blend,
                y=landmark.y + (other.y - landmark.y) * blend,
                z=landmark.z + (other.z - landmark.z) * blend,
                visibility=visibility,
                presence=presence,
            )
        )
    return PoseObservation(detected=True, landmarks=tuple(landmarks))


def _lower(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return first if second is None else second
    return min(float(first), float(second))


class _ScheduledProvider:
    """Adapts ``ScheduledPoseTracker`` to the pipeline's ``PoseProvider``.

    The pipeline streams every frame of the clip in order, so a call counter is
    the frame index here.
    """

    def __init__(self, tracker) -> None:
        self._tracker = tracker
        self._index = 0
        self.name = "scheduled-pose-tracker"

    def detect(self, frame_bgr, timestamp_ms):
        index = self._index
        self._index += 1
        return self._tracker.detect(frame_bgr, index)

    def close(self) -> None:
        self._tracker.close()


def run_scheduled_variant(
    video: Path,
    name: str,
    *,
    cadence: int = 5,
    interval: int | None = None,
    repeat: int = 1,
) -> RunResult:
    """Persistent-ROI variant: detector only on the schedule or a trigger."""

    from pose_experiments import ScheduledPoseTracker

    tracker = ScheduledPoseTracker(cadence=cadence, interval=interval)
    provider = _ScheduledProvider(tracker)
    result = RunResult(
        name=name,
        configuration={
            "mode": "persistent-roi",
            "detector_cadence": cadence,
            "reanchor_interval": interval,
            "triggers": ["missing-box", "lost-person", "interval", "forced"],
        },
    )
    try:
        for _ in range(max(1, repeat)):
            tracker.reset()
            provider._index = 0
            began = time.perf_counter()
            outcome = process_video(
                video,
                ROOT / "var" / "bench" / "pose" / name,
                config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
                pose_provider=provider,
                phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
            )
            result.seconds.append(time.perf_counter() - began)
            result.records = list(outcome.records)
            result.frames = len(result.records)
        summary = tracker.stats.summary()
        result.detector_calls = summary["detector_calls"]
        result.pose_calls = summary["pose_calls"]
        result.configuration["tracker_stats"] = summary
        result.configuration["crop_stability"] = tracker.crop_stability()
    finally:
        provider.close()
    return result


def run_adaptive_variant(video: Path, name: str, *, sparse_step: int = 5, cadence: int = 5) -> RunResult:
    """Adaptive dense refinement: sparse proposal pass, dense span, marked rest."""

    from pose_experiments import run_adaptive_dense

    result = RunResult(
        name=name,
        configuration={"mode": "adaptive-dense", "sparse_step": sparse_step, "detector_cadence": cadence},
    )
    began = time.perf_counter()
    outcome = run_adaptive_dense(video, sparse_step=sparse_step, cadence=cadence)
    result.seconds.append(time.perf_counter() - began)
    result.records = outcome.records
    result.frames = len(result.records)
    result.configuration.update(outcome.stats)
    result.detector_calls = outcome.stats["sparse"]["detector_calls"] + outcome.stats["dense"]["detector_calls"]
    result.pose_calls = outcome.stats["sparse"]["pose_calls"] + outcome.stats["dense"]["pose_calls"]
    return result


# ---------------------------------------------------------------------------
# Comparing a variant with the baseline records
# ---------------------------------------------------------------------------


def record_index(records: Sequence[object]) -> dict[int, object]:
    return {int(getattr(record, "index")): record for record in records}


def pixel_delta_map(records: Sequence[object], baseline: Mapping[int, object]) -> dict[str, float]:
    worst: dict[str, float] = {}
    for record in records:
        other = baseline.get(int(getattr(record, "index")))
        if other is None:
            continue
        points = getattr(getattr(record, "points", None), "pixels", {}) or {}
        other_points = getattr(getattr(other, "points", None), "pixels", {}) or {}
        for name, value in points.items():
            counterpart = other_points.get(name)
            if counterpart is None:
                continue
            delta = float(np.linalg.norm(np.asarray(value, float) - np.asarray(counterpart, float)))
            worst[name] = max(worst.get(name, 0.0), delta)
    return {name: round(value, 3) for name, value in sorted(worst.items())}


def coverage(records: Sequence[object], names: Iterable[str], *, first: int, last: int) -> dict[str, int]:
    counts = {name: 0 for name in names}
    for record in records:
        index = int(getattr(record, "index"))
        if not first <= index <= last:
            continue
        points = getattr(getattr(record, "points", None), "pixels", {}) or {}
        for name in counts:
            if name in points:
                counts[name] += 1
    return counts


def coverage_losses(baseline: Mapping[str, int], variant: Mapping[str, int]) -> dict[str, int]:
    """Only the *decreases* matter: a gain cannot hide an existing check."""

    return {
        name: variant.get(name, 0) - baseline.get(name, 0)
        for name in sorted(set(baseline) | set(variant))
        if variant.get(name, 0) - baseline.get(name, 0) < 0
    }


def joint_displacement(
    records: Sequence[object],
    baseline: Mapping[int, object],
    *,
    only_frames: set[int] | None = None,
    skip_frames: set[int] | None = None,
) -> dict[str, float]:
    """Largest pixel move for the joints the checks read, in source pixels."""

    worst: dict[str, float] = {}
    for record in records:
        index = int(getattr(record, "index"))
        if only_frames is not None and index not in only_frames:
            continue
        if skip_frames is not None and index in skip_frames:
            continue
        other = baseline.get(index)
        if other is None:
            continue
        here = getattr(getattr(record, "points", None), "pixels", {}) or {}
        there = getattr(getattr(other, "points", None), "pixels", {}) or {}
        for name in TRACKED_JOINTS:
            if name not in here or name not in there:
                continue
            delta = float(np.linalg.norm(np.asarray(here[name], float) - np.asarray(there[name], float)))
            worst[name] = max(worst.get(name, 0.0), delta)
    return {name: round(value, 3) for name, value in sorted(worst.items())}


def landmark_displacement(records: Sequence[object], baseline: Mapping[int, object]) -> dict[str, float]:
    """Largest joint move over the whole clip (dead time included)."""

    return joint_displacement(records, baseline)


def moment_summary(records: Sequence[object]) -> dict:
    moments = estimate_moments(records)
    frames = moments.frames()
    return {
        "frames": {phase.value: frames.get(phase) for phase in frames},
        "parts": {phase.value: frames.get(phase) for phase in ANALYSIS_PHASES},
        "finish": frames.get(Phase.FINISH),
        "coverage": moments.sequence.get("coverage"),
        "torso_scale_px": moments.sequence.get("torso_scale_px"),
        "first_frame": moments.sequence.get("first_frame"),
        "last_frame": moments.sequence.get("last_frame"),
        "gates": moments.sequence.get("gates"),
    }


def measurement_summary(records: Sequence[object], first: int, last: int) -> dict:
    frames = moment_summary(records)["parts"]
    address = frames.get("address")
    apply_reference(records, address, config=MeasurementConfig())
    by_index = record_index(records)
    out: dict[str, dict] = {}
    for phase, frame in frames.items():
        record = None if frame is None else by_index.get(int(frame))
        if record is None:
            out[phase] = {"frame": frame, "values": {}}
            continue
        values = {}
        for key in MEASUREMENT_KEYS:
            result = (getattr(record, "measurements", {}) or {}).get(key)
            values[key] = None if result is None or not result.valid else round(float(result.value), 4)
        out[phase] = {"frame": frame, "values": values}
    return out


def compare_measurements(baseline: Mapping, other: Mapping) -> dict:
    """Per-phase measurement deltas, availability changes and bad values."""

    worst: dict[str, float] = {}
    deltas: dict[str, dict] = {}
    availability: dict[str, list[str]] = {}
    bad_values: dict[str, list[str]] = {}
    for phase, entry in baseline.items():
        found = other.get(phase, {})
        per_phase: dict[str, float | None] = {}
        missing: list[str] = []
        bad: list[str] = []
        for key, value in (entry.get("values") or {}).items():
            other_value = (found.get("values") or {}).get(key)
            if value is None and other_value is None:
                per_phase[key] = None
                continue
            if value is None or other_value is None:
                # A measurement that appeared or vanished changes the payload.
                per_phase[key] = None
                missing.append(key)
                continue
            if not np.isfinite(other_value):
                per_phase[key] = None
                bad.append(key)
                continue
            delta = abs(float(other_value) - float(value))
            per_phase[key] = round(delta, 4)
            worst[key] = max(worst.get(key, 0.0), delta)
        deltas[phase] = per_phase
        if missing:
            availability[phase] = missing
        if bad:
            bad_values[phase] = bad
    return {
        "per_phase": deltas,
        "max_by_measurement": {k: round(v, 4) for k, v in worst.items()},
        "availability_changes": availability,
        "non_finite_values": bad_values,
    }


def verdict(run: Mapping, comparison: Mapping, baseline_frame_count: int) -> dict:
    """Apply the plan's gates to one variant."""

    failures: list[str] = []
    phase_deltas = comparison["phase_frames"]
    if any(value != 0 for value in phase_deltas.values()):
        failures.append(f"phase frames moved: {phase_deltas}")
    losses = comparison["coverage_losses"]
    if losses:
        failures.append(f"wrist/elbow coverage dropped: {losses}")
    # The gate is on the joints the *phase overlays* draw, at the phase frames,
    # in source pixels. Dead-time displacement is reported, not gated.
    worst_phase = max(comparison["phase_geometry_px"].values(), default=0.0)
    if worst_phase > TOLERANCE_GEOMETRY_PX:
        failures.append(f"phase-frame joint moved {worst_phase:.2f}px")
    measurements = comparison["measurements"]
    measurement = comparison["measurements"]["max_by_measurement"]
    for key in ("torso_inclination_deg", "torso_angle_change_deg", "lead_knee_flex_deg", "trail_knee_flex_deg"):
        if measurement.get(key, 0.0) > TOLERANCE_BODY_DEG:
            failures.append(f"{key} moved {measurement[key]}deg")
    for key in ("head_displacement_pct_height", "pelvis_displacement_pct_height"):
        if measurement.get(key, 0.0) > TOLERANCE_MOVEMENT_PCT:
            failures.append(f"{key} moved {measurement[key]}pp")
    if measurements["availability_changes"]:
        failures.append(f"measurement availability changed: {measurements['availability_changes']}")
    if measurements["non_finite_values"]:
        failures.append(f"non-finite measurements: {measurements['non_finite_values']}")
    return {"pass": not failures, "failures": failures}


def speedup(baseline_seconds: float, seconds: float) -> float:
    return round(baseline_seconds / seconds, 3) if seconds > 0 else 0.0


def downstream_check(
    video: Path,
    records: Sequence[object],
    *,
    out_dir: Path,
    baseline_bundle: Path,
    options: Mapping[str, object],
    det_frequency: int = 5,
) -> dict:
    """Build the real bundle from a variant's records and compare the payload.

    OWLv2 is never re-run: the accepted bundle's ``club_fallback.json`` is
    copied in first, which is the "same OWL cache" condition the plan requires.
    """

    import shutil

    import build_analysis as builder
    import compare_analysis

    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "pose_records.pkl"
    identity = builder.pose_identity(video, "rtmw", det_frequency, options=options)
    save_pose_cache(cache, records, identity)
    owl_cache = baseline_bundle / "club_fallback.json"
    if owl_cache.is_file():
        shutil.copyfile(owl_cache, out_dir / "club_fallback.json")

    analysis = builder.build(
        video,
        out_dir,
        pose="rtmw",
        cache=cache,
        club_cache=True,
        det_frequency=det_frequency,
        pose_options=options,
    )
    baseline = json.loads((baseline_bundle / "analysis.json").read_text(encoding="utf-8"))
    comparison = compare_analysis.compare(baseline, analysis)
    return {
        "status": "run",
        "pose_cache_status": analysis["engine"]["timings"].get("pose_cache"),
        "timings": analysis["engine"]["timings"],
        "owlv2_frames_run": analysis["engine"]["counters"].get("owlv2_frames_run"),
        "equivalent": comparison["equivalent"],
        "exact_payload_except_engine": comparison["exact_payload_except_engine"],
        "max_angle_delta_deg": comparison["max_angle_delta_deg"],
        "max_geometry_delta_px": comparison["max_geometry_delta_px"],
        "differences": comparison["differences"],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_variant(video: Path, name: str, *, repeat: int = 1, **kwargs) -> RunResult:
    if name.startswith("sample-"):
        step = int(name.split("-", 1)[1])
        return run_sampling_variant(video, name, step=step, cadence=kwargs.get("cadence", 5))
    if name.startswith("adaptive"):
        step = 5
        if name.startswith("adaptive-"):
            step = int(name.split("-", 1)[1])
        return run_adaptive_variant(video, name, sparse_step=step, cadence=kwargs.get("cadence", 5))
    if name.startswith("roi"):
        parts = name.split("-")
        if "control" in parts:
            return run_scheduled_variant(video, name, cadence=5, interval=None, repeat=repeat)
        if "interval" in parts:
            return run_scheduled_variant(
                video, name, cadence=1 << 30, interval=int(parts[parts.index("interval") + 1]), repeat=repeat
            )
        return run_scheduled_variant(video, name, cadence=1 << 30, interval=None, repeat=repeat)
    if name == "window":
        window = kwargs.get("window")
        if window is None or not window.get("available"):
            raise SystemExit("the active-window variant needs an available window proposal")
        start = int(window["start"])
        end = int(window["end"])
        return run_pipeline_variant(
            video,
            name,
            cadence=kwargs.get("cadence", 5),
            start_frame=start,
            max_frames=end - start + 1,
            repeat=repeat,
        )
    cadence = kwargs.get("cadence", 5)
    if name.startswith("cadence-"):
        cadence = int(name.split("-", 1)[1])
    return run_pipeline_variant(
        video,
        name,
        cadence=cadence,
        start_frame=kwargs.get("start_frame", 0),
        max_frames=kwargs.get("max_frames"),
        repeat=repeat,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--baseline-records", default=str(ROOT / "var" / "analysis" / "real" / "pose_records.pkl"))
    parser.add_argument("--out", default=str(ROOT / "var" / "bench" / "pose"))
    parser.add_argument("--variants", default="cadence-5,cadence-10,cadence-15")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--probe", action="store_true", help="also print the ONNX model shapes")
    parser.add_argument("--roi-probe", action="store_true", help="also measure detector cost on a padded ROI")
    parser.add_argument("--window-mad", type=float, default=6.0, help="MAD multiplier for the active-window proposal")
    parser.add_argument("--window-margin-before", type=float, default=1.5)
    parser.add_argument("--window-margin-after", type=float, default=2.0)
    parser.add_argument(
        "--downstream",
        action="store_true",
        help="build the real bundle for variants that pass the pose gates",
    )
    parser.add_argument(
        "--baseline-bundle",
        default=str(ROOT / "docs" / "agent-work" / "pose-performance" / "baseline" / "real"),
    )
    args = parser.parse_args(argv)

    video = Path(args.video)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    baseline_records = load_records_any(Path(args.baseline_records))
    baseline_by_index = record_index(baseline_records)
    baseline_moments = moment_summary(baseline_records)
    baseline_measurements = measurement_summary(
        baseline_records, baseline_moments["first_frame"], baseline_moments["last_frame"]
    )

    report: dict = {
        "video": str(video),
        "baseline_records": str(args.baseline_records),
        "baseline_frames": len(baseline_records),
        "baseline_moments": baseline_moments,
        "variants": {},
    }
    if args.probe:
        report["probe"] = probe_models()
    if args.roi_probe:
        report["roi_probe"] = roi_detector_probe(video)
        print(json.dumps({"roi_probe": report["roi_probe"]}))

    for name in [item.strip() for item in args.variants.split(",") if item.strip()]:
        if name == "window":
            window = propose_active_window(
                video,
                margin_before_s=args.window_margin_before,
                margin_after_s=args.window_margin_after,
                threshold_mad=args.window_mad,
            )
            report["active_window"] = window
            run = run_variant(video, name, repeat=args.repeat, window=window)
        else:
            run = run_variant(video, name, repeat=args.repeat)
        moments = moment_summary(run.records)
        measurements = measurement_summary(run.records, moments["first_frame"], moments["last_frame"])
        phase_deltas = {
            phase: None
            if moments["parts"].get(phase) is None or baseline_moments["parts"].get(phase) is None
            else int(moments["parts"][phase]) - int(baseline_moments["parts"][phase])
            for phase in baseline_moments["parts"]
        }
        baseline_coverage = coverage(
            baseline_records,
            TRACKED_JOINTS,
            first=min(v for v in baseline_moments["parts"].values() if v is not None),
            last=max(v for v in baseline_moments["parts"].values() if v is not None),
        )
        variant_coverage = coverage(
            run.records,
            TRACKED_JOINTS,
            first=min(v for v in baseline_moments["parts"].values() if v is not None),
            last=max(v for v in baseline_moments["parts"].values() if v is not None),
        )
        coverage_delta = {
            name_: variant_coverage[name_] - baseline_coverage[name_]
            for name_ in TRACKED_JOINTS
            if variant_coverage[name_] != baseline_coverage[name_]
        }
        phase_frames = {value for value in baseline_moments["parts"].values() if value is not None}
        phase_geometry = joint_displacement(run.records, baseline_by_index, only_frames=phase_frames)
        dead_time = joint_displacement(run.records, baseline_by_index, skip_frames=phase_frames)
        comparison = {
            "phase_frames": phase_deltas,
            "coverage": {"baseline": baseline_coverage, "variant": variant_coverage},
            "coverage_delta": coverage_delta,
            "coverage_losses": coverage_losses(baseline_coverage, variant_coverage),
            "phase_geometry_px": phase_geometry,
            "dead_time_displacement_px": dead_time,
            "landmark_displacement": landmark_displacement(run.records, baseline_by_index),
            "pixel_delta_map": pixel_delta_map(run.records, baseline_by_index),
            "measurements": compare_measurements(baseline_measurements, measurements),
        }
        entry = {
            "configuration": run.configuration,
            "seconds": [round(value, 3) for value in run.seconds],
            "seconds_median": round(statistics.median(run.seconds), 3) if run.seconds else None,
            "detector_calls": run.detector_calls,
            "pose_calls": run.pose_calls,
            "frames": run.frames,
            "detector_seconds": round(run.detector_seconds, 3),
            "pose_seconds": round(run.pose_seconds, 3),
            "moments": moments,
            "measurements": measurements,
            "comparison": comparison,
        }
        entry["verdict"] = verdict(entry, comparison, len(baseline_records))
        # Downstream (club + consumer payload) is only meaningful once the pose
        # stage passes its own gates; otherwise it is reported as not run.
        if entry["verdict"]["pass"] and args.downstream:
            entry["downstream"] = downstream_check(
                video,
                run.records,
                out_dir=out / f"downstream-{name}",
                baseline_bundle=Path(args.baseline_bundle),
                options={"variant": name, **run.configuration},
            )
        else:
            entry["downstream"] = {
                "status": "not-run",
                "reason": (
                    "rejected at the pose gate"
                    if not entry["verdict"]["pass"]
                    else "passes the pose gates; rerun with --downstream to build the bundle"
                ),
            }
        report["variants"][name] = entry
        print(
            json.dumps(
                {
                    name: {
                        "seconds": entry["seconds_median"],
                        "detector_calls": entry["detector_calls"],
                        "pose_calls": entry["pose_calls"],
                        "phase_deltas": phase_deltas,
                        "coverage_losses": comparison["coverage_losses"],
                        "max_phase_geometry_px": max(phase_geometry.values(), default=0.0),
                        "max_dead_time_px": max(dead_time.values(), default=0.0),
                        "max_measurement_delta": comparison["measurements"]["max_by_measurement"],
                        "availability_changes": comparison["measurements"]["availability_changes"],
                        "roi": run.configuration.get("tracker_stats"),
                        "crop_stability": run.configuration.get("crop_stability"),
                        "adaptive": {
                            key: run.configuration.get(key)
                            for key in ("span", "inferred_frames", "dense_frames", "interpolated_frames", "missing_frames")
                            if key in run.configuration
                        }
                        or None,
                        "verdict": entry["verdict"],
                    }
                }
            )
        )

    baseline_cadence = report["variants"].get("cadence-5", {}).get("seconds_median")
    if baseline_cadence:
        for entry in report["variants"].values():
            entry["speedup_vs_cadence5"] = speedup(baseline_cadence, entry["seconds_median"] or 0.0)
    (out / "pose-variants.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"wrote {out / 'pose-variants.json'}")
    return 0


def probe_models() -> dict:
    from swingguru.pose_rtm import RTMPoseAdapter

    tracker = RTMPoseAdapter("wholebody", "balanced")._ensure()
    out = {}
    for label, tool in (("detector", tracker.det_model), ("pose", tracker.pose_model)):
        session = tool.session
        out[label] = {
            "input_shapes": [list(item.shape) for item in session.get_inputs()],
            "providers": session.get_providers(),
            "model_input_size": list(tool.model_input_size),
        }
    return out


def roi_detector_probe(
    video: Path,
    *,
    samples: int = 6,
    x_range: tuple[float, float] = (0.20, 0.70),
    y_range: tuple[float, float] = (0.15, 0.98),
) -> dict:
    """Detector cost on a full frame versus a padded golfer ROI.

    A diagnostic, not a candidate: the ROI is the central region of the frame,
    which is where the golfer is in a down-the-line clip. The point is the
    structural fact that YOLOX letterboxes whatever it is given into its fixed
    640x640 input, so cropping cannot reduce the forward-pass cost.
    """

    import cv2

    adapter = provider_for(5)
    detector = adapter._ensure().det_model
    capture = cv2.VideoCapture(str(video))
    full_times: list[float] = []
    crop_times: list[float] = []
    crop_shape = None
    index = 0
    try:
        while len(full_times) < samples:
            ok, frame = capture.read()
            if not ok:
                break
            if index % 12:
                index += 1
                continue
            index += 1
            height, width = frame.shape[:2]
            x0, x1 = int(width * x_range[0]), int(width * x_range[1])
            y0, y1 = int(height * y_range[0]), int(height * y_range[1])
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(width, x1), min(height, y1)
            crop = frame[y0:y1, x0:x1]
            crop_shape = list(crop.shape[:2])

            began = time.perf_counter()
            detector(frame)
            full_times.append(time.perf_counter() - began)
            began = time.perf_counter()
            detector(crop)
            crop_times.append(time.perf_counter() - began)
    finally:
        capture.release()
    return {
        "onnx_input_shapes": [list(item.shape) for item in detector.session.get_inputs()],
        "model_input_size": list(detector.model_input_size),
        "samples": len(full_times),
        "full_frame_ms": round(1000.0 * statistics.median(full_times), 1) if full_times else None,
        "roi_crop_ms": round(1000.0 * statistics.median(crop_times), 1) if crop_times else None,
        "roi_shape": crop_shape,
        "note": "both paths are letterboxed to the model's fixed input size",
    }


# ---------------------------------------------------------------------------
# Cheap active-window proposal (no pose model involved)
# ---------------------------------------------------------------------------


def motion_series(video: Path, *, scale: float = 0.25) -> list[tuple[int, float]]:
    """Frame-to-frame motion from downscaled grayscale thumbnails.

    Deliberately pose-free and model-free: it only decides *where to look*, and
    every candidate it selects is then processed by the real pipeline.
    """

    import cv2

    capture = cv2.VideoCapture(str(video))
    series: list[tuple[int, float]] = []
    previous = None
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if previous is not None:
                series.append((index, float(np.mean(np.abs(gray - previous)))))
            previous = gray
            index += 1
    finally:
        capture.release()
    return series


def propose_active_window(
    video: Path,
    *,
    fps: float = 30.0,
    margin_before_s: float = 1.5,
    margin_after_s: float = 2.0,
    threshold_mad: float = 6.0,
    floor: float = 0.5,
) -> dict:
    """Robust motion-outlier window with a frame-count safety margin.

    Threshold is ``median + threshold_mad * MAD`` of the motion series, raised
    to ``floor``, so it adapts to the clip rather than to this swing. No frame,
    coordinate or phase index is hardcoded.
    """

    series = motion_series(video)
    if not series:
        return {"available": False, "reason": "no decodable frames"}
    values = np.asarray([value for _, value in series], dtype=np.float64)
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) or 1e-6
    threshold = max(floor, median + threshold_mad * mad)
    active = [index for index, value in series if value > threshold]
    if not active:
        return {
            "available": False,
            "reason": "no frame exceeded the motion threshold",
            "median": round(median, 4),
            "mad": round(mad, 4),
            "threshold": round(threshold, 4),
        }
    first, last = min(active), max(active)
    before = int(round(margin_before_s * fps))
    after = int(round(margin_after_s * fps))
    return {
        "available": True,
        "median": round(median, 4),
        "mad": round(mad, 4),
        "threshold": round(threshold, 4),
        "motion_first": first,
        "motion_last": last,
        "start": max(0, first - before),
        "end": last + after,
        "margin_before_frames": before,
        "margin_after_frames": after,
    }


__all__ = [
    "RunResult",
    "apply_interpolation",
    "compare_measurements",
    "coverage",
    "coverage_losses",
    "downstream_check",
    "interpolate_observation",
    "joint_displacement",
    "landmark_displacement",
    "main",
    "measurement_summary",
    "moment_summary",
    "motion_series",
    "propose_active_window",
    "probe_models",
    "roi_detector_probe",
    "run_adaptive_variant",
    "run_pipeline_variant",
    "run_sampling_variant",
    "run_scheduled_variant",
    "run_variant",
    "verdict",
]


if __name__ == "__main__":
    raise SystemExit(main())
