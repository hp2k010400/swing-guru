"""Time the club-tracking stage and compare window sizes on one real clip.

    .venv\\Scripts\\python.exe scripts\\benchmark_analysis.py VIDEO \\
        --records var\\analysis\\real\\pose_records.pkl \\
        --out var\\bench\\club-tune --radii 3,4,5,6,8

Runs the classical tracker over the whole clip (the original behaviour) and
then over merged event windows of each radius, timing every run and comparing
the six analysis moments frame by frame: shaft angle, level, status, method
and both endpoints. The pose stage is read from a records cache so the club
stage can be tuned cheaply; the end-to-end cold path is measured separately by
``scripts/build_analysis.py``.

Nothing here writes to the production bundle. Results go to ``--out``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

from build_analysis import club_windows, video_digest  # noqa: E402
from swingguru.analysis_bundle import ANALYSIS_PHASES  # noqa: E402
from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.club import ClassicalClubTracker  # noqa: E402
from swingguru.club.cache import (  # noqa: E402
    CandidateCache,
    CandidateIdentity,
    config_fingerprint,
    pose_fingerprint,
    reference_fingerprint,
)
from swingguru.club.tracker import reference_hands, torso_length_px  # noqa: E402
from swingguru.club.windows import frames_in  # noqa: E402
from swingguru.frames import decode_selected_frames, wanted_frames  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import Phase  # noqa: E402
from swingguru.pipeline import apply_reference  # noqa: E402
from swingguru.pose_cache import (  # noqa: E402
    PoseCacheIdentity,
    load_records_any,
    save_pose_cache,
)


def load_records(video: Path, cache: Path | None, pose: str, det_frequency: int = 5) -> list:
    """Baseline pose records for the club benchmark, from either cache format.

    This tool only *reads* the baseline; the strict identity check belongs to
    the build, so an existing file is accepted as-is here.
    """

    if cache is not None and cache.exists():
        return load_records_any(cache)
    identity = PoseCacheIdentity(
        video=video_digest(video),
        provider=pose,
        model="wholebody" if pose == "rtmw" else "",
        mode="balanced" if pose == "rtmw" else "",
        det_frequency=det_frequency if pose == "rtmw" else 0,
        tracker="rtmlib-pose-tracker" if pose == "rtmw" else "mediapipe-pose",
    )
    from swingguru.pipeline import PipelineConfig, process_video
    from swingguru.phases import ManualPhaseProvider

    provider = None
    if pose == "rtmw":
        from swingguru.pose_rtm import RTMPoseAdapter

        provider = RTMPoseAdapter("wholebody", "balanced", det_frequency=det_frequency)
    result = process_video(
        video,
        ROOT / "var" / "bench" / "pose",
        config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
        pose_provider=provider,
        phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
    )
    records = list(result.records)
    if cache is not None:
        save_pose_cache(cache, records, identity)
    return records


def phase_snapshot(track, moments) -> dict[str, dict]:
    snapshot: dict[str, dict] = {}
    for phase in ANALYSIS_PHASES:
        frame_index = moments.frames().get(phase)
        shaft = None if frame_index is None else track.at(int(frame_index))
        snapshot[phase.value] = {
            "frame": None if frame_index is None else int(frame_index),
            "available": bool(shaft is not None and shaft.available),
            "status": None if shaft is None else shaft.status,
            "level": None if shaft is None else shaft.level,
            "method": None if shaft is None else shaft.method,
            "interpolated": None if shaft is None else bool(shaft.interpolated),
            "confidence": None if shaft is None else round(float(shaft.confidence), 4),
            "angle_deg": None if shaft is None or shaft.angle_deg is None else round(float(shaft.angle_deg), 4),
            "grip": None if shaft is None or shaft.grip is None else [round(float(v), 3) for v in shaft.grip],
            "tip": None if shaft is None or shaft.tip is None else [round(float(v), 3) for v in shaft.tip],
        }
    return snapshot


BOOLEAN_FIELDS = ("present", "frame", "available", "status", "level", "method",
                  "interpolated", "angle", "endpoints", "confidence")


def compare(baseline: dict[str, dict], other: dict[str, dict]) -> dict:
    """Whole-clip vs windowed per-moment comparison of the raw tracker output.

    Everything the raw track exposes must match exactly: frame, availability,
    status, level, method, interpolation, confidence and both endpoints. A
    tolerance here would hide exactly the drift this harness exists to catch.
    """

    report: dict = {
        "phases": {},
        "extra_phases": sorted(set(other) - set(baseline)),
        "max_angle_delta_deg": 0.0,
        "max_endpoint_delta_px": 0.0,
        "max_confidence_delta": 0.0,
    }
    for phase, expected in baseline.items():
        found = other.get(phase)
        entry: dict = {"present": isinstance(found, dict)}
        if not isinstance(found, dict):
            report["phases"][phase] = entry
            continue
        for key in ("frame", "available", "status", "level", "method", "interpolated"):
            entry[key] = found.get(key) == expected.get(key)

        angle_before, angle_after = expected.get("angle_deg"), found.get("angle_deg")
        if (angle_before is None) != (angle_after is None):
            entry["angle"] = False
            entry["angle_delta_deg"] = None
        elif angle_before is None:
            entry["angle"] = True
            entry["angle_delta_deg"] = None
        else:
            delta = abs(float(angle_after) - float(angle_before))
            entry["angle"] = delta == 0.0
            entry["angle_delta_deg"] = round(delta, 6)
            report["max_angle_delta_deg"] = max(report["max_angle_delta_deg"], delta)

        grip_before, grip_after = expected.get("grip"), found.get("grip")
        tip_before, tip_after = expected.get("tip"), found.get("tip")
        if None in (grip_before, grip_after, tip_before, tip_after):
            entry["endpoints"] = grip_before == grip_after and tip_before == tip_after
            entry["endpoint_delta_px"] = None
        else:
            distance = max(
                float(np.linalg.norm(np.asarray(grip_after, float) - np.asarray(grip_before, float))),
                float(np.linalg.norm(np.asarray(tip_after, float) - np.asarray(tip_before, float))),
            )
            entry["endpoints"] = distance == 0.0
            entry["endpoint_delta_px"] = round(distance, 6)
            report["max_endpoint_delta_px"] = max(report["max_endpoint_delta_px"], distance)

        confidence_before, confidence_after = expected.get("confidence"), found.get("confidence")
        if (confidence_before is None) != (confidence_after is None):
            entry["confidence"] = False
            entry["confidence_delta"] = None
        elif confidence_before is None:
            entry["confidence"] = True
            entry["confidence_delta"] = None
        else:
            delta = abs(float(confidence_after) - float(confidence_before))
            entry["confidence"] = delta == 0.0
            entry["confidence_delta"] = round(delta, 6)
            report["max_confidence_delta"] = max(report["max_confidence_delta"], delta)

        report["phases"][phase] = entry

    report["max_angle_delta_deg"] = round(report["max_angle_delta_deg"], 6)
    report["max_endpoint_delta_px"] = round(report["max_endpoint_delta_px"], 6)
    report["max_confidence_delta"] = round(report["max_confidence_delta"], 6)
    report["identical"] = not report["extra_phases"] and all(
        all(entry.get(field) is True for field in BOOLEAN_FIELDS)
        for entry in report["phases"].values()
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--records", default=str(ROOT / "var" / "analysis" / "real" / "pose_records.pkl"))
    parser.add_argument("--out", default=str(ROOT / "var" / "bench" / "club-tune"))
    parser.add_argument("--radii", default="3,4,5,6,8")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--pose", choices=("rtmw", "mediapipe"), default="rtmw")
    parser.add_argument("--candidate-cache", action="store_true", help="use the on-disk candidate cache")
    args = parser.parse_args(argv)

    video = Path(args.video)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    radii = [int(item) for item in args.radii.split(",") if item.strip()]
    records = load_records(video, Path(args.records), args.pose)
    moments = estimate_moments(records)
    apply_reference(records, moments.frames().get(Phase.ADDRESS), config=MeasurementConfig())

    tracker = ClassicalClubTracker()
    torso = torso_length_px(records)
    first, last = int(records[0].index), int(records[-1].index)
    references = reference_hands(records, torso_px=torso, config=tracker.config)
    reference_indices = tuple(index for index, _ in references)

    modes: list[tuple[str, int | None]] = [("full", None)] + [(f"window-{r}", r) for r in radii]
    results: dict[str, dict] = {}
    snapshots: dict[str, dict] = {}
    baseline_window_frames: set[int] | None = None

    for name, radius in modes:
        windows = () if radius is None else club_windows(moments, records, radius)
        detection_frames = tuple(range(first, last + 1)) if radius is None else frames_in(windows)
        roles = wanted_frames(detection_frames, reference_indices)
        decode_started = time.monotonic()
        decoded = decode_selected_frames(video, roles, max_index=last)
        decode_seconds = time.monotonic() - decode_started

        cache = None
        if args.candidate_cache:
            identity = CandidateIdentity(
                tracker=tracker.name,
                video=video_digest(video),
                pose=pose_fingerprint(records),
                config=config_fingerprint(tracker.config),
                references=reference_fingerprint(references),
            )
            cache = CandidateCache.load(identity, out / f"candidates-{name}.json")

        timings: list[float] = []
        track = None
        for _ in range(max(1, args.repeat)):
            started = time.monotonic()
            if radius is None:
                track = tracker.track_sequence(decoded.frames, records)
            else:
                track = tracker.track_windows(decoded.frames, records, windows, cache=cache)
            timings.append(time.monotonic() - started)
        if cache is not None:
            cache.save()
        snapshots[name] = phase_snapshot(track, moments)
        results[name] = {
            "mode": "full-sequence" if radius is None else "event-windows",
            "radius_frames": radius,
            "windows": [
                {"start": item.start, "end": item.end, "frames": item.length} for item in windows
            ],
            "frames_requested": len(detection_frames),
            "frames_analysed": len(track.detections),
            "seconds": [round(value, 3) for value in timings],
            "seconds_min": round(min(timings), 3),
            "seconds_median": round(statistics.median(timings), 3),
            "decode_seconds": round(decode_seconds, 3),
            "decoded_frames": decoded.kept,
            "candidate_cache": None if cache is None else cache.stats,
        }
        if radius is not None:
            covered = set(frames_in(windows))
            baseline_window_frames = covered if baseline_window_frames is None else baseline_window_frames & covered

    baseline = snapshots["full"]
    comparisons = {
        name: compare(baseline, snapshot) for name, snapshot in snapshots.items() if name != "full"
    }
    payload = {
        "video": str(video),
        "video_digest": video_digest(video),
        "records": str(args.records),
        "pose_frames": len(records),
        "first_frame": first,
        "last_frame": last,
        "moments": {phase.value: frame for phase, frame in moments.frames().items()},
        "results": results,
        "phase_snapshots": snapshots,
        "comparison_vs_full": comparisons,
        "window_overlap_frames": sorted(baseline_window_frames or []),
    }
    (out / "club-tune.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({
        "timings": {
            name: {
                "mode": item["mode"],
                "frames": item["frames_analysed"],
                "seconds": item["seconds"],
                "decode_seconds": item["decode_seconds"],
            }
            for name, item in results.items()
        },
        "comparison_vs_full": {
            name: {
                "identical": item["identical"],
                "max_angle_delta_deg": item["max_angle_delta_deg"],
                "max_endpoint_delta_px": item["max_endpoint_delta_px"],
                "max_confidence_delta": item["max_confidence_delta"],
                "extra_phases": item["extra_phases"],
                "mismatches": [
                    {"phase": phase, "fields": [field for field in BOOLEAN_FIELDS if entry.get(field) is not True]}
                    for phase, entry in item["phases"].items()
                    if any(entry.get(field) is not True for field in BOOLEAN_FIELDS)
                ],
            }
            for name, item in comparisons.items()
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
