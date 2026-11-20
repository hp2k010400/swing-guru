"""Compare MediaPipe (current engine) with RTMPose / RTMW on one swing.

    .venv\\Scripts\\python.exe scripts\\research\\pose_compare.py VIDEO --out var\\research\\pose

For every frame, records per-model keypoints and scores for the joints Swing
Guru relies on (shoulders, elbows, wrists, hips, knees, ankles, nose), then
writes a key-moment contact sheet with each model's joints drawn on the frame
and a JSON summary of how often each joint is tracked above a score floor.

RTMPose/RTMW (Apache-2.0) run through ``rtmlib`` on ONNX Runtime, CPU only.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

JOINTS = [
    "nose",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_hip", "right_hip",
    "left_knee", "right_knee",
    "left_ankle", "right_ankle",
]
# COCO-17 ordering (shared by RTMPose body and the first 17 of RTMW's 133).
COCO = {
    "nose": 0, "left_shoulder": 5, "right_shoulder": 6, "left_elbow": 7, "right_elbow": 8,
    "left_wrist": 9, "right_wrist": 10, "left_hip": 11, "right_hip": 12,
    "left_knee": 13, "right_knee": 14, "left_ankle": 15, "right_ankle": 16,
}
BONES = [
    ("left_shoulder", "right_shoulder"), ("left_shoulder", "left_elbow"), ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"), ("right_elbow", "right_wrist"), ("left_hip", "right_hip"),
    ("left_shoulder", "left_hip"), ("right_shoulder", "right_hip"), ("left_hip", "left_knee"),
    ("left_knee", "left_ankle"), ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
]
SCORE_FLOOR = 0.5
MOMENTS = {108: "Address", 116: "Takeaway", 120: "Mid-Back", 127: "Top", 132: "Mid-Down", 134: "Impact"}


def mediapipe_frames(records) -> list[dict]:
    out = []
    for record in records:
        points = {}
        landmarks = getattr(getattr(record, "points", None), "landmarks", {}) or {}
        for name in JOINTS:
            landmark = landmarks.get(name)
            if landmark is None or not landmark.finite:
                continue
            score = min(float(landmark.visibility or 0), float(landmark.presence or 0))
            points[name] = (landmark.x * record.width, landmark.y * record.height, score)
        out.append(points)
    return out


def rtm_frames(frames, model) -> tuple[list[dict], float]:
    out = []
    started = time.monotonic()
    for frame in frames:
        keypoints, scores = model(frame)
        points = {}
        if keypoints is not None and len(keypoints):
            # One golfer: take the person with the highest mean body score.
            best = int(np.argmax([float(np.mean(s[:17])) for s in scores]))
            for name, index in COCO.items():
                x, y = keypoints[best][index]
                points[name] = (float(x), float(y), float(scores[best][index]))
        out.append(points)
    return out, (time.monotonic() - started) / max(1, len(frames))


def draw(frame, points, colour):
    image = frame.copy()
    for a, b in BONES:
        if a in points and b in points and points[a][2] >= SCORE_FLOOR and points[b][2] >= SCORE_FLOOR:
            cv2.line(image, (int(points[a][0]), int(points[a][1])), (int(points[b][0]), int(points[b][1])), colour, 2, cv2.LINE_AA)
    for name, (x, y, score) in points.items():
        ok = score >= SCORE_FLOOR
        radius = 5 if "wrist" in name else 3
        cv2.circle(image, (int(x), int(y)), radius, colour if ok else (0, 0, 255), -1 if ok else 1, cv2.LINE_AA)
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--records", default=str(ROOT / "var" / "real_probe" / "records.pkl"))
    parser.add_argument("--out", default=str(ROOT / "var" / "research" / "pose"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(args.video)
    frames = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    records = pickle.loads(Path(args.records).read_bytes())
    frames = frames[: len(records)]

    from rtmlib import Body, Wholebody  # imported late: optional research dependency

    results = {"mediapipe": mediapipe_frames(records)}
    timings = {}
    for label, factory in (
        ("rtmpose-m (body)", lambda: Body(mode="balanced", backend="onnxruntime", device="cpu")),
        ("rtmw (whole-body)", lambda: Wholebody(mode="balanced", backend="onnxruntime", device="cpu")),
    ):
        model = factory()
        results[label], timings[label] = rtm_frames(frames, model)
        print(f"{label}: {timings[label] * 1000:.0f} ms/frame")

    swing = range(108, 136)
    summary = {}
    for label, per_frame in results.items():
        rates = {}
        for name in JOINTS:
            tracked = [1.0 if name in per_frame[i] and per_frame[i][name][2] >= SCORE_FLOOR else 0.0 for i in swing]
            rates[name] = round(float(np.mean(tracked)), 3)
        summary[label] = {
            "tracked_rate_address_to_impact": rates,
            "ms_per_frame": None if label == "mediapipe" else round(timings[label] * 1000, 1),
            "moments": {
                MOMENTS[i]: {
                    name: None if name not in per_frame[i] else [round(v, 1) if k < 2 else round(v, 3) for k, v in enumerate(per_frame[i][name])]
                    for name in ("left_wrist", "right_wrist", "left_elbow", "right_elbow")
                }
                for i in MOMENTS
            },
        }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    with open(out / "keypoints.pkl", "wb") as handle:
        pickle.dump(results, handle)

    colours = {"mediapipe": (60, 200, 255), "rtmpose-m (body)": (80, 220, 90), "rtmw (whole-body)": (255, 160, 60)}
    rows = []
    for index, moment in MOMENTS.items():
        tiles = []
        for label, per_frame in results.items():
            tile = draw(frames[index], per_frame[index], colours[label])[300:832, 0:360]
            cv2.putText(tile, f"{moment} f{index} {label}", (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
            tiles.append(tile)
        rows.append(np.hstack(tiles))
    cv2.imwrite(str(out / "moments.png"), np.vstack(rows))
    print(json.dumps({k: v["tracked_rate_address_to_impact"] for k, v in summary.items()}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
