"""Does better pose fix club tracking? Run the ClubTracker with each pose source.

    .venv\\Scripts\\python.exe scripts\\research\\club_with_pose.py VIDEO

Takes the MediaPipe records and swaps in the arm joints (wrists, elbows,
shoulders) from RTMPose / RTMW (``var/research/pose/keypoints.pkl`` written
by ``pose_compare.py``) wherever that model's score is >= 0.5. The club
tracker itself is unchanged.
"""

from __future__ import annotations

import copy
import json
import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from swingguru.club import ClassicalClubTracker  # noqa: E402

ARM = ("left_wrist", "right_wrist", "left_elbow", "right_elbow", "left_shoulder", "right_shoulder")
MOMENTS = {108: "Address", 116: "Takeaway", 120: "Mid-Back", 127: "Top", 132: "Mid-Down", 134: "Impact"}
# Read by eye from full-resolution frames (see docs/agent-work/club-v03/REPORT.md).
TRUTH = {108: -56.0, 116: -162.0, 120: 140.0, 127: 105.0, 132: 124.0, 134: -70.0}


def with_pose(records, keypoints, floor=0.5):
    swapped = []
    for record, points in zip(records, keypoints):
        clone = copy.copy(record)
        pixels = dict(record.points.pixels)
        for name in ARM:
            pixels.pop(name, None)
            if name in points and points[name][2] >= floor:
                pixels[name] = np.array(points[name][:2], float)
        clone.points = copy.copy(record.points)
        object.__setattr__(clone.points, "pixels", pixels) if hasattr(clone.points, "__dataclass_fields__") else setattr(clone.points, "pixels", pixels)
        swapped.append(clone)
    return swapped


def main() -> int:
    video = sys.argv[1]
    records = pickle.loads((ROOT / "var" / "real_probe" / "records.pkl").read_bytes())
    keypoints = pickle.loads((ROOT / "var" / "research" / "pose" / "keypoints.pkl").read_bytes())
    capture = cv2.VideoCapture(video)
    frames = []
    while len(frames) < len(records):
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)

    table = {}
    for label in ("mediapipe", "rtmpose-m (body)", "rtmw (whole-body)"):
        source = records if label == "mediapipe" else with_pose(records, keypoints[label])
        track = ClassicalClubTracker().track_sequence(frames, source)
        rows = {}
        for index, moment in MOMENTS.items():
            shaft = track.frames[index]
            error = None
            if shaft.angle_deg is not None:
                error = abs(((shaft.angle_deg - TRUTH[index]) + 180) % 360 - 180)
            rows[moment] = {
                "level": shaft.level,
                "angle": None if shaft.angle_deg is None else round(shaft.angle_deg, 1),
                "abs_error_deg": None if error is None else round(error, 1),
                "interpolated": shaft.interpolated,
            }
        window = [track.frames[i] for i in range(108, 135)]
        table[label] = {
            "moments": rows,
            "address_to_impact_medium_plus": sum(1 for s in window if s.level in ("HIGH", "MEDIUM") and not s.interpolated),
            "window_frames": len(window),
        }
        print(label)
        for moment, row in rows.items():
            print(f"   {moment:9} {row['level']:11} angle={row['angle']} err={row['abs_error_deg']}")
        print(f"   MEDIUM+ {table[label]['address_to_impact_medium_plus']}/{len(window)}")
    out = ROOT / "var" / "research" / "club_with_pose.json"
    out.write_text(json.dumps(table, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
