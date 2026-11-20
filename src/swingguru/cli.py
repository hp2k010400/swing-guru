"""Command line entry point for Swing Guru Engine v0.1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .measurements import MeasurementConfig
from .phases import ManualPhaseProvider, parse_phase_assignments
from .pipeline import PipelineConfig, PipelineError, process_video
from .pose import (
    DEFAULT_MODEL_URL,
    ModelNotFoundError,
    PoseAdapterError,
    default_model_path,
    describe_runtime,
    download_model,
    resolve_model_path,
)
from .synthetic import SyntheticClip, make_synthetic_clip
from .video import (
    DEFAULT_MAX_FRAMES,
    VideoError,
    ffmpeg_path,
    probe_video,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VIDEO = 3
EXIT_MODEL = 4
EXIT_OUTPUT = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swingguru",
        description=(
            "Swing Guru Engine v0.1 - local pose and geometry engine for golf "
            "swing video. No cloud services, no scoring, no coaching output."
        ),
    )
    parser.add_argument("--version", action="version", version=f"swingguru {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="report runtime versions, model and ffmpeg availability"
    )
    doctor.set_defaults(handler=_cmd_doctor)

    info = subparsers.add_parser("info", help="print geometry and timing of a video")
    info.add_argument("--input", required=True, help="path to a video file")
    info.set_defaults(handler=_cmd_info)

    process = subparsers.add_parser(
        "process", help="run pose detection, measurements, overlays and exports"
    )
    process.add_argument("--input", required=True, help="path to a video file")
    process.add_argument("--output-dir", required=True, help="directory for artifacts")
    process.add_argument(
        "--model",
        default=None,
        help=f"pose landmarker .task file (default: {default_model_path()})",
    )
    process.add_argument(
        "--handedness",
        default="right",
        choices=("right", "left"),
        help="golfer handedness; right-handed means the left limb leads",
    )
    process.add_argument(
        "--camera-view",
        default="unknown",
        choices=("face-on", "down-the-line", "unknown"),
        help="recorded camera convention for this clip",
    )
    process.add_argument(
        "--mirrored-input",
        action="store_true",
        help="record that the clip was mirrored before analysis",
    )
    process.add_argument(
        "--min-visibility", type=float, default=0.5, help="landmark visibility floor"
    )
    process.add_argument(
        "--min-presence", type=float, default=0.5, help="landmark presence floor"
    )
    process.add_argument(
        "--max-frames",
        type=int,
        default=DEFAULT_MAX_FRAMES,
        help=f"stop after this many frames (default {DEFAULT_MAX_FRAMES})",
    )
    process.add_argument(
        "--start-frame", type=int, default=0, help="first frame index to process"
    )
    process.add_argument(
        "--reference-frame",
        type=int,
        default=None,
        help="frame index used as the origin for relative measurements",
    )
    process.add_argument(
        "--no-derive-reference",
        action="store_true",
        help="never reuse the manually assigned address frame as the reference",
    )
    process.add_argument(
        "--phase",
        action="append",
        default=[],
        metavar="PHASE=FRAME",
        help="assign a phase frame, repeatable (address, takeaway, top, downswing, "
        "impact, finish)",
    )
    process.add_argument(
        "--no-transcode",
        action="store_true",
        help="keep the raw MPEG-4 annotated video instead of re-encoding to H.264",
    )
    process.add_argument(
        "--no-annotated-video",
        action="store_true",
        help="skip annotated video rendering and only write data exports",
    )
    process.add_argument(
        "--json",
        action="store_true",
        help="print the run summary as JSON instead of text",
    )
    process.add_argument(
        "--pose",
        choices=("mediapipe", "rtmw"),
        default="mediapipe",
        help="pose model: MediaPipe BlazePose (default) or RTMW whole-body via rtmlib/ONNX",
    )
    process.set_defaults(handler=_cmd_process)

    fixture = subparsers.add_parser(
        "fixture", help="write a deterministic synthetic clip for plumbing tests"
    )
    fixture.add_argument("--output", required=True, help="destination .mp4 path")
    fixture.add_argument("--width", type=int, default=640)
    fixture.add_argument("--height", type=int, default=360)
    fixture.add_argument("--fps", type=float, default=30.0)
    fixture.add_argument("--frames", type=int, default=48)
    fixture.add_argument("--handedness", default="right", choices=("right", "left"))
    fixture.add_argument("--mirrored", action="store_true")
    fixture.set_defaults(handler=_cmd_fixture)

    download = subparsers.add_parser(
        "download-model", help="download the official MediaPipe pose landmarker"
    )
    download.add_argument("--dest", default=None, help="destination .task path")
    download.add_argument("--url", default=DEFAULT_MODEL_URL, help="override model URL")
    download.set_defaults(handler=_cmd_download_model)

    return parser


def _cmd_doctor(args: argparse.Namespace) -> int:
    runtime = describe_runtime()
    model_path = default_model_path()
    payload = {
        "engine_version": __version__,
        "runtime": runtime,
        "model_path": str(model_path),
        "model_present": model_path.exists() and model_path.stat().st_size > 0,
        "model_url": DEFAULT_MODEL_URL,
        "ffmpeg": ffmpeg_path() or "not found on PATH",
    }
    print(json.dumps(payload, indent=2))
    return EXIT_OK


def _cmd_info(args: argparse.Namespace) -> int:
    try:
        info = probe_video(args.input)
    except VideoError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_VIDEO
    print(json.dumps(info.to_dict(), indent=2))
    return EXIT_OK


def _cmd_fixture(args: argparse.Namespace) -> int:
    clip = SyntheticClip(
        width=args.width,
        height=args.height,
        fps=args.fps,
        frame_count=args.frames,
        handedness=args.handedness,
        mirrored=args.mirrored,
    )
    try:
        path = make_synthetic_clip(args.output, clip=clip)
    except (VideoError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_OUTPUT
    print(json.dumps({"fixture": str(path), "clip": clip.__dict__}, indent=2))
    return EXIT_OK


def _cmd_download_model(args: argparse.Namespace) -> int:
    try:
        path = download_model(args.dest, url=args.url)
    except PoseAdapterError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_MODEL
    print(json.dumps({"model": str(path), "bytes": path.stat().st_size}, indent=2))
    return EXIT_OK


def _cmd_process(args: argparse.Namespace) -> int:
    try:
        model_path = resolve_model_path(args.model)
    except ModelNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_MODEL

    try:
        assignments = parse_phase_assignments(args.phase)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE

    measurement_config = MeasurementConfig(
        min_visibility=args.min_visibility,
        min_presence=args.min_presence,
        handedness=args.handedness,
        camera_view=args.camera_view,
        mirrored_input=args.mirrored_input,
    )
    pipeline_config = PipelineConfig(
        model_path=model_path,
        measurement=measurement_config,
        max_frames=None if args.max_frames <= 0 else args.max_frames,
        start_frame=args.start_frame,
        write_annotated=not args.no_annotated_video,
        transcode_h264=not args.no_transcode,
    )
    phase_provider = ManualPhaseProvider(
        frames=assignments,
        reference_frame=args.reference_frame,
        derive_reference_from_address=not args.no_derive_reference,
    )

    last_message = ""

    def progress(fraction: float, message: str) -> None:
        nonlocal last_message
        if message != last_message:
            last_message = message
            print(f"[{fraction * 100:5.1f}%] {message}", file=sys.stderr, flush=True)

    pose_provider = None
    if getattr(args, "pose", "mediapipe") == "rtmw":
        from .pose_rtm import RTMPoseAdapter, rtm_available

        if not rtm_available():
            print(
                "error: --pose rtmw needs onnxruntime and rtmlib "
                "(pip install onnxruntime && pip install --no-deps rtmlib)",
                file=sys.stderr,
            )
            return EXIT_ERROR
        pose_provider = RTMPoseAdapter("wholebody", "balanced")

    try:
        result = process_video(
            args.input,
            args.output_dir,
            config=pipeline_config,
            pose_provider=pose_provider,
            phase_provider=phase_provider,
            progress=progress,
        )
    except ModelNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_MODEL
    except VideoError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_VIDEO
    except (PipelineError, PoseAdapterError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_OUTPUT

    summary = summarize_run(result)
    (result.output_dir / "run.log").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(text_report(result, summary))
    return EXIT_OK


def summarize_run(result: object) -> dict[str, object]:
    stats = getattr(result, "stats", {})
    phases = getattr(result, "phases", None)
    return {
        "input": str(getattr(result, "input_path")),
        "output_dir": str(getattr(result, "output_dir")),
        "video": getattr(result, "info").to_dict(),  # type: ignore[union-attr]
        "frames_processed": stats.get("frames") if isinstance(stats, dict) else None,
        "frames_with_pose": (
            stats.get("frames_with_pose") if isinstance(stats, dict) else None
        ),
        "reference_frame": None if phases is None else phases.reference_frame,
        "phases": None if phases is None else phases.to_dict(),
        "pose_provider": getattr(result, "pose_provider_name", None),
        "pose_errors": getattr(result, "pose_errors", None),
        "duration_s": getattr(result, "duration_s", None),
        "warnings": list(getattr(result, "warnings", [])),
        "measurements": (
            stats.get("measurements") if isinstance(stats, dict) else None
        ),
        "artifacts": {
            key: str(value)
            for key, value in getattr(result, "artifacts", {}).items()
        },
    }


def text_report(result: object, summary: dict[str, object]) -> str:
    info = getattr(result, "info")
    lines = [
        f"input            {getattr(result, 'input_path')}",
        f"resolution       {info.width}x{info.height} ({info.orientation})"
        f" @ {info.fps:.3f} fps",
        f"frames processed {summary['frames_processed']}",
        f"frames with pose {summary['frames_with_pose']}",
        f"reference frame  {summary['reference_frame']}",
        f"pose provider    {summary['pose_provider']}",
        f"elapsed          {summary['duration_s']:.2f} s",
    ]
    measurements = summary.get("measurements") or {}
    if measurements:
        lines.append("measurement validity")
        for key, payload in measurements.items():
            lines.append(
                f"  {key:<32} {payload['valid_frames']:>5} valid"
                f" / {payload['invalid_frames']:>5} rejected"
            )
    warnings = summary.get("warnings") or []
    if warnings:
        lines.append("warnings")
        lines.extend(f"  {warning}" for warning in warnings)
    lines.append("artifacts")
    for key, path in (summary.get("artifacts") or {}).items():
        lines.append(f"  {key:<24} {path}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse enforces a subcommand
        parser.print_help()
        return EXIT_USAGE
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
