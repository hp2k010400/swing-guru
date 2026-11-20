"""Run the experimental club tracker on a real swing and write diagnostics.

    .venv\\Scripts\\python.exe scripts\\club_report.py VIDEO --out var\\club\\real

Reuses cached pose records from ``scripts/real_moments_probe.py``
(``var/real_probe/records.pkl``) when ``--records`` points at them, otherwise
runs the body engine first. Writes ``index.html`` (all frames, four panels
each), ``club_track.csv/json``, key-moment PNGs and ``metrics.json``.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.club import ClassicalClubTracker  # noqa: E402
from swingguru.club.metrics import phase_shaft_metrics  # noqa: E402
from swingguru.club.report import write_report  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import PHASE_LABELS, ManualPhaseProvider  # noqa: E402
from swingguru.pipeline import PipelineConfig, process_video  # noqa: E402


def load_records(video: Path, cache: Path | None, out: Path):
    if cache is not None and cache.exists():
        return pickle.loads(cache.read_bytes())
    result = process_video(
        video,
        out / "pose",
        config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
        phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
    )
    return list(result.records)


def read_frames(video: Path, count: int) -> list:
    capture = cv2.VideoCapture(str(video))
    frames = []
    while len(frames) < count:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    capture.release()
    return frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--out", default=str(ROOT / "var" / "club" / "real"))
    parser.add_argument("--records", default=str(ROOT / "var" / "real_probe" / "records.pkl"))
    parser.add_argument("--notes", default=None, help="JSON {frame: note} from manual inspection")
    args = parser.parse_args(argv)

    video = Path(args.video)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records = load_records(video, Path(args.records) if args.records else None, out)
    frames = read_frames(video, len(records))

    started = time.monotonic()
    track = ClassicalClubTracker().track_sequence(frames, records)
    elapsed = time.monotonic() - started
    moments = estimate_moments(records)
    moment_frames = {
        estimate.frame_index: PHASE_LABELS[estimate.phase]
        for estimate in moments.estimates
        if estimate.frame_index is not None
    }
    metrics = phase_shaft_metrics(track, moments, fps=1000.0 / max(1e-6, records[1].timestamp_ms - records[0].timestamp_ms))
    metrics["seconds"] = round(elapsed, 2)
    notes = {}
    if args.notes:
        notes = {int(key): value for key, value in json.loads(Path(args.notes).read_text()).items()}
    paths = write_report(
        track,
        frames,
        out_dir=out,
        moments=moment_frames,
        notes=notes,
        metrics=metrics,
        title=f"Club tracking diagnostics - {video.name}",
    )
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
    print(json.dumps({"seconds": round(elapsed, 1), **{k: str(v) for k, v in paths.items()}}, indent=2))
    print(json.dumps(metrics, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
