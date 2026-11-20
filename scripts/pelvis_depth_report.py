"""Visual QA and stability report for the rear-of-pelvis (tush line) measurement.

    .venv\\Scripts\\python.exe scripts\\pelvis_depth_report.py VIDEO var\\analysis\\real \
        --out var\\qa\\pelvis

Writes:
* ``moments.png`` - Address / Top / Mid-Down / Impact with the silhouette
  outline, the pelvis band, the Address tush line (dashed), the current rear
  point and its displacement, and the old hip-midpoint for comparison;
* ``stability.json`` - rear-point jitter over the still frames before Address
  (the golfer is not moving, so any spread is measurement noise), and the
  old-vs-new comparison per phase;
* ``REPORT.md`` - the same numbers as tables.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from swingguru.pelvis_depth import PersonSegmenter, rear_pelvis  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402

PHASES = [("address", "Address"), ("top", "Top"), ("downswing", "Mid-Down"), ("impact", "Impact")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("bundle")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "pelvis"))
    parser.add_argument("--still", type=int, default=15, help="still frames before Address to test jitter")
    args = parser.parse_args(argv)
    bundle, out = Path(args.bundle), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    records = {int(r.index): r for r in load_records_any(bundle / "pose_records.pkl")}
    context = analysis["geometry_context"]
    toward, body = context["toward_ball"], context["body_height_px"]
    depth = analysis["pelvis_depth"]
    frames_by_phase = {p["phase"]: p["frame_index"] for p in analysis["phases"]}
    torso = float(np.median([np.linalg.norm(r.points.pixels["shoulder_mid"] - r.points.pixels["hip_mid"])
                             for r in records.values() if "shoulder_mid" in r.points.pixels and "hip_mid" in r.points.pixels]))

    capture = cv2.VideoCapture(args.video)

    def frame(index):
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
        ok, image = capture.read()
        return image if ok else None

    segmenter = PersonSegmenter()
    # --- stability over still frames before Address -------------------------
    address = frames_by_phase["address"]
    xs = []
    for index in range(address - args.still, address + 1):
        record = records.get(index)
        image = frame(index)
        if record is None or image is None:
            continue
        hip = record.points.pixels.get("hip_mid")
        result = rear_pelvis(segmenter.mask(image), None if hip is None else np.asarray(hip),
                             toward_ball=toward, torso_px=torso, frame_index=index)
        if result.available:
            xs.append(result.point[0])
    jitter_px = float(np.std(xs)) if len(xs) > 1 else None
    stability = {
        "still_frames": len(xs),
        "rear_x_std_px": None if jitter_px is None else round(jitter_px, 3),
        "rear_x_range_px": None if not xs else round(max(xs) - min(xs), 3),
        "rear_x_std_pct_height": None if jitter_px is None else round(100 * jitter_px / body, 3),
        "body_height_px": body,
    }

    # --- old vs new per phase -------------------------------------------------
    facts = {(f["key"], f["phase"]): f for f in analysis["interpretations"]["facts"]}
    comparison = []
    for phase, label in PHASES[1:]:
        old = facts.get(("pelvis_toward_ball_pct", phase))
        new = depth["phases"].get(phase, {})
        old_v = None if old is None else old["value"]
        new_v = new.get("displacement_pct_height")
        comparison.append({
            "phase": label,
            "hip_midpoint_toward_ball_pct": None if old_v is None else round(old_v, 2),
            "rear_pelvis_toward_ball_pct": None if new_v is None else round(new_v, 2),
            "same_direction": None if old_v is None or new_v is None else (old_v >= 0) == (new_v >= 0),
            "difference_pct": None if old_v is None or new_v is None else round(new_v - old_v, 2),
            "rear_level": new.get("level"),
        })

    # --- overlays --------------------------------------------------------------
    tush_x = depth["tush_line_x"]
    tiles = []
    for phase, label in PHASES:
        index = frames_by_phase[phase]
        image, record = frame(index), records[index]
        mask = segmenter.mask(image)
        vis = image.copy()
        contours, _ = cv2.findContours((mask > 0.5).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cv2.drawContours(vis, contours, -1, (255, 255, 255), 1, cv2.LINE_AA)
        info = depth["address"] if phase == "address" else depth["phases"][phase].get("rear")
        hip = record.points.pixels["hip_mid"]
        cv2.circle(vis, (int(hip[0]), int(hip[1])), 4, (0, 140, 255), -1, cv2.LINE_AA)  # old: hip midpoint
        if info and info.get("band"):
            top, bottom = info["band"]
            cv2.line(vis, (0, int(top)), (int(hip[0]) + 10, int(top)), (180, 180, 180), 1)
            cv2.line(vis, (0, int(bottom)), (int(hip[0]) + 10, int(bottom)), (180, 180, 180), 1)
        for y in range(int(info["band"][0] - 60), int(info["band"][1] + 170), 10):  # dashed tush line
            cv2.line(vis, (int(round(tush_x)), y), (int(round(tush_x)), y + 5), (255, 255, 255), 2, cv2.LINE_AA)
        if info and info.get("point"):
            rx, ry = info["point"]
            cv2.arrowedLine(vis, (int(round(tush_x)), int(ry)), (int(round(rx)), int(ry)), (80, 200, 90), 2,
                            cv2.LINE_AA, tipLength=0.4)
            cv2.circle(vis, (int(round(rx)), int(ry)), 5, (80, 200, 90), -1, cv2.LINE_AA)
        crop = vis[380:760, 0:240]
        crop = cv2.resize(crop, (360, 570), interpolation=cv2.INTER_CUBIC)
        pct = "" if phase == "address" else f" {depth['phases'][phase].get('displacement_pct_height', 0):+.1f}%"
        cv2.putText(crop, f"{label}{pct}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(crop, f"{label}{pct}", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        tiles.append(crop)
    segmenter.close()
    cv2.imwrite(str(out / "moments.png"), np.hstack(tiles))

    report = {"stability": stability, "comparison": comparison, "camera_check": analysis["camera_check"]}
    (out / "stability.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Rear-of-pelvis measurement QA", "",
             f"Still-frame jitter before Address ({stability['still_frames']} frames): "
             f"std {stability['rear_x_std_px']} px = {stability['rear_x_std_pct_height']} % of body height "
             f"(range {stability['rear_x_range_px']} px).", "",
             "| Phase | Hip midpoint toward ball | Rear pelvis toward ball | Same direction | Difference | Rear level |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in comparison:
        lines.append(f"| {row['phase']} | {row['hip_midpoint_toward_ball_pct']} % | {row['rear_pelvis_toward_ball_pct']} % | "
                     f"{row['same_direction']} | {row['difference_pct']} | {row['rear_level']} |")
    lines += ["", f"Camera check: **{analysis['camera_check']['status']}**", ""]
    for name, signal in analysis["camera_check"]["signals"].items():
        lines.append(f"* {name}: {signal['status']} ({signal['value']}) - {signal['note']}")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
