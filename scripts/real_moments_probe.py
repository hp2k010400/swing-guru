"""Run MediaPipe + the automatic moment estimator over a real swing clip.

    .venv\\Scripts\\python.exe scripts\\real_moments_probe.py VIDEO [--handedness right]

Prints the estimated moments, the gates and a coarse hand-path trace so a
human can compare the estimates against the video. This is a diagnostic, not
an accuracy measurement.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from swingguru.auto_moments import (  # noqa: E402
    MomentConfig,
    _smoothed,
    build_motion_series,
    estimate_moments,
)
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import ManualPhaseProvider  # noqa: E402
from swingguru.pipeline import PipelineConfig, process_video  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--handedness", default="right")
    parser.add_argument("--output-dir", default=str(ROOT / "var" / "real_probe"))
    parser.add_argument("--reuse", action="store_true", help="reuse cached records")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--brief", action="store_true", help="one line per moment")
    args = parser.parse_args(argv)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "records.pkl"
    if args.reuse and cache.exists():
        records = pickle.loads(cache.read_bytes())
    else:
        result = process_video(
            Path(args.video),
            out,
            config=PipelineConfig(
                measurement=MeasurementConfig(handedness=args.handedness),
                write_annotated=False,
            ),
            phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
        )
        records = list(result.records)
        cache.write_bytes(pickle.dumps(records))

    config = MomentConfig(handedness=args.handedness)
    moments = estimate_moments(records, config=config)
    if args.brief:
        for estimate in moments.estimates:
            when = (
                "-" if estimate.timestamp_ms is None
                else f"{estimate.timestamp_ms / 1000:.3f}s"
            )
            print(
                f"{estimate.label:10} frame={str(estimate.frame_index):>5} {when:>8}  "
                f"{estimate.evidence[0] if estimate.evidence else ''}"
            )
        print(json.dumps(moments.sequence, default=str))
    else:
        print(json.dumps(moments.to_dict(), indent=2, default=str))

    if args.trace:
        series = build_motion_series(records, config=config)
        smoothed = _smoothed(series, config)
        for index, (frame, t) in enumerate(zip(series.frames, series.times_s)):
            point = smoothed[index]
            text = "-" if point is None else f"x={point[0]:+.2f} y={point[1]:+.2f}"
            print(f"{frame:4d} {t:6.3f}s {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
