"""Write the deterministic synthetic clip used for plumbing checks.

    .venv\\Scripts\\python.exe scripts\\make_fixture_video.py --output var\\fixtures\\plumbing.mp4

The clip shows a drawn golfer figure, not a real person. It exercises decode,
measurement, overlay and export code; it is not evidence that the pose model
tracks a real golf swing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from swingguru.synthetic import SyntheticClip, make_synthetic_clip  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "var" / "fixtures" / "plumbing.mp4"))
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--frames", type=int, default=48)
    parser.add_argument("--handedness", default="right", choices=("right", "left"))
    parser.add_argument("--mirrored", action="store_true")
    args = parser.parse_args(argv)

    clip = SyntheticClip(
        width=args.width,
        height=args.height,
        fps=args.fps,
        frame_count=args.frames,
        handedness=args.handedness,
        mirrored=args.mirrored,
    )
    path = make_synthetic_clip(args.output, clip=clip)
    print(f"wrote {path} ({clip.frame_count} frames, {clip.width}x{clip.height} @ {clip.fps} fps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
