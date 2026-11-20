"""RTMW pose speed and determinism for one clip under different ONNX thread settings.

    python scripts/bench_pose_threads.py VIDEO [--frames N] [--threads unset,2,4,8]

Each setting runs in a fresh subprocess (ORT reads the setting when the
session is created) and reports seconds per frame plus a hash of every
keypoint, so a thread count that changes results is visible, not assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def child(video: str, frames: int) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    import cv2
    import numpy as np

    from swingguru.pose_rtm import RTMPoseAdapter

    capture = cv2.VideoCapture(video)
    images = []
    while len(images) < frames:
        ok, image = capture.read()
        if not ok:
            break
        images.append(image)
    adapter = RTMPoseAdapter()
    began = time.monotonic()
    adapter._ensure()
    load = time.monotonic() - began
    digest = hashlib.sha256()
    began = time.monotonic()
    for index, image in enumerate(images):
        keypoints, scores = adapter._ensure()(image)
        digest.update(np.ascontiguousarray(keypoints, dtype=np.float32).tobytes())
        digest.update(np.ascontiguousarray(scores, dtype=np.float32).tobytes())
    run = time.monotonic() - began
    print(json.dumps({"frames": len(images), "load_s": round(load, 2), "run_s": round(run, 2),
                      "s_per_frame": round(run / max(1, len(images)), 3), "hash": digest.hexdigest()[:16]}))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--threads", default="unset,2,4,8")
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        child(args.video, args.frames)
        return 0
    for setting in args.threads.split(","):
        env = {k: v for k, v in os.environ.items() if k != "SG_ORT_THREADS"}
        if setting != "unset":
            env["SG_ORT_THREADS"] = setting
        out = subprocess.run([sys.executable, __file__, args.video, "--frames", str(args.frames), "--child"],
                             capture_output=True, text=True, env=env)
        line = (out.stdout.strip().splitlines() or [out.stderr.strip()[-300:]])[-1]
        print(f"threads={setting:>5}  {line}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
