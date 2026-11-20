"""Club tracking through the backswing: direct vs temporal recovery, P2 and Top.

    .venv\\Scripts\\python.exe scripts\\qa_club_temporal.py [--only 1,8] [--out var\\qa\\club_temporal]

For every analysed robustness swing (``var/qa/test_swings/*/bundle``) this
rebuilds the classical track from the bundle's candidate cache (no new
detection for already-detected frames), checks it matches the bundle, runs
:func:`swingguru.club.temporal.recover_track`, and reports:

* per-frame source (direct / recovered / unavailable) from Takeaway-3 to Top+3;
* P2 on the direct track and on the refined track;
* the Top frame's shaft on both;
* the same P2 detection on every other frame only (a 15 fps proxy), to see
  how much the sampling rate matters;
* human-review sheets: raw crop + overlay for P2-1..P2+1 and Top-1..Top+1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_analysis as ba  # noqa: E402
from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.club import ClassicalClubTracker  # noqa: E402
from swingguru.club.cache import CandidateIdentity, config_fingerprint, pose_fingerprint, reference_fingerprint  # noqa: E402
from swingguru.club.temporal import recover_track  # noqa: E402
from swingguru.club.tracker import build_references, hands_context, reference_hands, torso_length_px  # noqa: E402
from swingguru.club.windows import frames_in  # noqa: E402
from swingguru.frames import decode_selected_frames, wanted_frames  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import Phase  # noqa: E402
from swingguru.pipeline import apply_reference  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402
from swingguru.takeaway_p2 import detect, elevation_deg  # noqa: E402

SWINGS = ROOT / "var" / "qa" / "test_swings"
COLOURS = {"detected": (80, 220, 80), "recovered": (0, 165, 255), "interpolated": (200, 200, 200)}


def swing_video(folder: Path) -> Path:
    window = json.loads((folder / "swing_window.json").read_text(encoding="utf-8"))["window"]
    return folder / "window.mp4" if window["trimmed"] else ROOT / "var" / "test_swings" / window["analysed_file"]


def path_of(track, lo: int, hi: int) -> list[dict]:
    out = []
    for index in range(lo, hi + 1):
        shaft = track.at(index)
        if shaft is None:
            continue
        out.append({"frame": index, "available": bool(shaft.available), "interpolated": bool(shaft.interpolated),
                    "status": shaft.status, "level": shaft.level, "confidence": round(float(shaft.confidence), 3),
                    "angle_deg": None if shaft.angle_deg is None else round(float(shaft.angle_deg), 2),
                    "grip": None if shaft.grip is None else [round(float(v), 2) for v in shaft.grip],
                    "tip": None if shaft.tip is None else [round(float(v), 2) for v in shaft.tip]})
    return out


def shaft_summary(shaft) -> dict:
    if shaft is None or not shaft.available:
        return {"available": False, "status": None if shaft is None else shaft.status,
                "reason": None if shaft is None else shaft.reason}
    return {"available": True, "status": shaft.status, "level": shaft.level,
            "confidence": round(float(shaft.confidence), 3), "angle_deg": round(float(shaft.angle_deg), 1),
            "interpolated": shaft.interpolated, "elevation": round(elevation_deg(shaft.grip, shaft.tip), 1)}


def tile(image, shaft, hands, label: str, *, overlay: bool, size: int = 240):
    h, w = image.shape[:2]
    s = int(min(h * 0.24, w / 2 - 1))
    centre = hands if hands is not None else np.array([w / 2, h / 2])
    x0 = int(np.clip(centre[0] - s, 0, w - 2 * s))
    y0 = int(np.clip(centre[1] - s, 0, h - 2 * s))
    crop = image[y0:y0 + 2 * s, x0:x0 + 2 * s].copy()
    scale = size / (2 * s)
    crop = cv2.resize(crop, (size, size))
    if overlay:
        if shaft is not None and shaft.available:
            kind = "interpolated" if shaft.interpolated else shaft.status
            g = ((np.asarray(shaft.grip) - [x0, y0]) * scale).astype(int)
            t = ((np.asarray(shaft.tip) - [x0, y0]) * scale).astype(int)
            cv2.line(crop, tuple(g), tuple(t), COLOURS.get(kind, (255, 255, 255)), 2, cv2.LINE_AA)
        if hands is not None:
            c = ((hands - [x0, y0]) * scale).astype(int)
            cv2.circle(crop, tuple(c), 4, (255, 255, 0), -1)
    cv2.rectangle(crop, (0, 0), (size, 18), (0, 0, 0), -1)
    cv2.putText(crop, label, (3, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)
    return crop


def describe(shaft) -> str:
    if shaft is None or not shaft.available:
        return "none"
    kind = "interp" if shaft.interpolated else ("REC" if shaft.status == "recovered" else "dir")
    return f"{kind} {shaft.level[0]} {shaft.confidence:.2f} {elevation_deg(shaft.grip, shaft.tip):+.0f}"


def analyse(folder: Path, out: Path) -> dict:
    bundle = folder / "bundle"
    analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    records = load_records_any(bundle / "pose_records.pkl")
    moments = estimate_moments(records)
    apply_reference(records, moments.frames().get(Phase.ADDRESS), config=MeasurementConfig())
    frames_of = {p["phase"]: p["frame_index"] for p in analysis["phases"]}
    assert frames_of["top"] == moments.frames()[Phase.TOP], "moments differ from the bundle"

    tracker = ClassicalClubTracker()
    torso = torso_length_px(records)
    windows = ba.club_windows(moments, records, ba.DEFAULT_CLUB_WINDOW)
    references = reference_hands(records, torso_px=torso, config=tracker.config)
    roles = wanted_frames(frames_in(windows), tuple(i for i, _ in references))
    video = swing_video(folder)
    decoded = decode_selected_frames(video, roles, max_index=int(records[-1].index))
    frames = decoded.frames
    identity = CandidateIdentity(tracker=tracker.name, video=ba.video_digest(video), pose=pose_fingerprint(records),
                                 config=config_fingerprint(tracker.config), references=reference_fingerprint(references))
    cache = ba.open_candidate_cache(bundle, identity, club_cache=True, full_sequence=False)
    track = tracker.track_windows(frames, records, windows, cache=cache)
    stored = {p["frame"]: p for p in analysis["engine"]["club_path"]}
    # The bundle stores the refined path since temporal recovery; compare the
    # frames recovery did not touch.
    mismatch = [f for f, p in stored.items()
                if p.get("status") != "recovered" and track.at(f) is not None
                and (p["angle_deg"] != (None if track.at(f).angle_deg is None
                                                                  else round(float(track.at(f).angle_deg), 2)))]
    refs = build_references(frames, records, torso_px=torso, config=tracker.config)
    refined, log = recover_track(tracker, track, frames, records, torso_px=torso, references=refs)

    takeaway, mid_back, top = frames_of["takeaway"], frames_of["mid_back"], frames_of["top"]
    lo, hi = takeaway - 3, top + 3
    address = track.at(frames_of["address"])
    length = None
    if address is not None and address.available:
        length = float(np.hypot(*(np.asarray(address.tip) - np.asarray(address.grip))))
    ctx = analysis["geometry_context"]
    first = int(records[0].index)
    fps = float(analysis["fps"])

    def p2_of(t, keep=lambda f: True):
        path = [p for p in path_of(t, lo, mid_back + 3) if keep(p["frame"])]
        return detect(path, takeaway_frame=takeaway, mid_back_frame=mid_back, toward_ball=ctx["toward_ball"],
                      address_shaft_px=length, fps=fps, first_frame=first)

    result = {
        "swing": folder.name[:2],
        "phases": {"takeaway": takeaway, "mid_back": mid_back, "top": top},
        "track_matches_bundle": not mismatch,
        "mismatched_frames": mismatch,
        "direct_path": path_of(track, lo, hi),
        "refined_path": path_of(refined, lo, hi),
        "recovery_log": [e for e in log if lo - 10 <= e["from"] <= hi],
        "p2_direct": p2_of(track),
        "p2_refined": p2_of(refined),
        "p2_refined_even": p2_of(refined, lambda f: f % 2 == 0),
        "p2_refined_odd": p2_of(refined, lambda f: f % 2 == 1),
        "top_direct": shaft_summary(track.at(top)),
        "top_refined": shaft_summary(refined.at(top)),
        "top_fallback": next((e for e in analysis["engine"]["club_fallback"] if e.get("phase") == "top"), None),
        "address_shaft_px": None if length is None else round(length, 1),
    }
    # Recovery must never change a frame the tracker reported at MEDIUM/HIGH.
    changed = [f.frame_index for f, g in zip(track.frames, refined.frames)
               if f.level in ("HIGH", "MEDIUM") and not f.interpolated and
               (g.angle_deg != f.angle_deg or g.status != f.status)]
    result["confident_frames_changed"] = changed

    by_index = {int(r.index): r for r in records}

    def hands_at(index):
        record = by_index.get(index)
        return None if record is None else hands_context(record, torso_px=torso, config=tracker.config).origin

    rows = []
    p2 = result["p2_refined"]
    centre = p2["frame"] if p2.get("detected") else (p2.get("bracket") or [takeaway + 2])[0]
    for label, middle in (("P2" if p2.get("detected") else "P2?", centre), ("Top", top)):
        raw, over = [], []
        for index in (middle - 1, middle, middle + 1):
            image = frames.get(index)
            if image is None:
                continue
            hands = hands_at(index)
            name = f"{result['swing']} {label} f{index}"
            raw.append(tile(image, None, hands, name + " raw", overlay=False))
            over.append(tile(image, refined.at(index), hands, f"{name} {describe(refined.at(index))}", overlay=True))
        if over:
            rows.append(np.hstack(raw + over))
    if rows:
        width = max(r.shape[1] for r in rows)
        rows = [cv2.copyMakeBorder(r, 0, 0, 0, width - r.shape[1], cv2.BORDER_CONSTANT) for r in rows]
        cv2.imwrite(str(out / f"{result['swing']}_p2_top.png"), np.vstack(rows))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "club_temporal"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {f"{int(x):02d}" for x in args.only.split(",")} if args.only else None
    results = []
    for folder in sorted(SWINGS.iterdir()):
        if not (folder / "bundle" / "analysis.json").exists() or folder.name[:2] in ("03", "05"):
            continue
        if only and folder.name[:2] not in only:
            continue
        result = analyse(folder, out)
        results.append(result)
        p2d, p2r = result["p2_direct"], result["p2_refined"]
        fmt = lambda p: (f"f{p['frame']} {p['elevation_deg']:+.0f} {p['source']} {p['level']}"
                         if p.get("detected") else "none")
        print(f"{result['swing']}: P2 direct {fmt(p2d)} | refined {fmt(p2r)} | even {fmt(result['p2_refined_even'])}"
              f" | odd {fmt(result['p2_refined_odd'])} | top {result['top_direct'].get('level')}->"
              f"{result['top_refined'].get('level')} {result['top_refined'].get('status')} | match {result['track_matches_bundle']}"
              f" changed {result['confident_frames_changed']}", flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
