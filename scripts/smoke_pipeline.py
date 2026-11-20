"""Run the real engine over the synthetic plumbing clip.

    .venv\\Scripts\\python.exe scripts\\smoke_pipeline.py --output-dir var\\smoke

Requires ``models/pose_landmarker_full.task``. Reports whether the pose model
found the drawn figure, which is expected to be a partial or zero hit rate: the
clip is a synthetic figure, so this is a plumbing and inference smoke test, not
a golf tracking result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import ManualPhaseProvider, Phase  # noqa: E402
from swingguru.pipeline import PipelineConfig, process_video  # noqa: E402
from swingguru.synthetic import SyntheticClip, make_synthetic_clip  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(ROOT / "var" / "smoke"))
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clip = SyntheticClip(
        width=args.width,
        height=args.height,
        fps=args.fps,
        frame_count=args.frames,
    )
    video = make_synthetic_clip(output_dir / "smoke_input.mp4", clip=clip)

    result = process_video(
        video,
        output_dir,
        config=PipelineConfig(
            measurement=MeasurementConfig(camera_view="down-the-line"),
            transcode_h264=True,
        ),
        phase_provider=ManualPhaseProvider(
            frames={Phase.ADDRESS: 0, Phase.TOP: clip.frame_count // 2},
            reference_frame=0,
        ),
    )
    print(
        json.dumps(
            {
                "frames": result.stats["frames"],
                "frames_with_pose": result.stats["frames_with_pose"],
                "annotated_video": str(result.annotated_path),
                "measurements_csv": str(result.artifact("measurements_csv")),
                "warnings": result.warnings,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
