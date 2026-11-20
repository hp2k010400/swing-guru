"""Robustness batch: run the unchanged engine over a folder of swings and report.

    .venv\\Scripts\\python.exe scripts\\batch_validate.py run var\\test_swings
    .venv\\Scripts\\python.exe scripts\\batch_validate.py report      # re-aggregate only

Each video is analysed by ``scripts/build_analysis.py`` in its own process (the
production command, unchanged; a native crash only loses that one video), into
``var/qa/test_swings/<NN>_<name>/bundle``. The bundle is then read back to write,
per swing:

* ``report.json`` - general, camera, phases, pose, club, pelvis, interpretation;
* ``contact_sheet.jpg`` - the six key frames with body, shaft and tush-line
  overlays (the numbers alone are not enough).

Visual verdicts (is the phase right, is the shaft on the club, is the rear
point on the golfer) cannot come from the engine itself. They are recorded by a
reviewer in ``review.json`` next to each sheet and merged into the status; an
unreviewed swing can never be PASS.

The batch summary is ``docs/agent-work/robustness/BATCH_REPORT.md``.
Nothing here changes a threshold or the engine; it only measures it.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

PHASES = [
    ("address", "Address"), ("takeaway", "Takeaway"), ("mid_back", "Mid-Back"),
    ("top", "Top"), ("downswing", "Mid-Down"), ("impact", "Impact"),
]
PELVIS_PHASES = ("top", "downswing", "impact")
JOINT_GROUPS = {
    "head": ("nose",),
    "shoulders": ("left_shoulder", "right_shoulder"),
    "elbows": ("left_elbow", "right_elbow"),
    "wrists": ("left_wrist", "right_wrist"),
    "hips": ("left_hip", "right_hip"),
    "knees": ("left_knee", "right_knee"),
    "ankles": ("left_ankle", "right_ankle"),
}
SKELETON = [
    ("left_shoulder", "right_shoulder"), ("left_hip", "right_hip"),
    ("left_shoulder", "left_hip"), ("right_shoulder", "right_hip"),
    ("left_shoulder", "left_elbow"), ("left_elbow", "left_wrist"),
    ("right_shoulder", "right_elbow"), ("right_elbow", "right_wrist"),
    ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
    ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
]
# The engine's own RTMW gate for MEDIUM (analysis_bundle.SCORE_GATES); a joint
# below it is never used for a shown check, so it is "not usable" here too.
USABLE_SCORE = 0.45
OUT_ROOT = ROOT / "var" / "qa" / "test_swings"
REPORT_PATH = ROOT / "docs" / "agent-work" / "robustness" / "BATCH_REPORT.md"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi"}


# ---------------------------------------------------------------------------
# Pure helpers (unit tested)
# ---------------------------------------------------------------------------


def joint_score(record, name: str) -> float | None:
    landmark = (getattr(getattr(record, "points", None), "landmarks", {}) or {}).get(name)
    if landmark is None:
        return None
    values = [v for v in (landmark.visibility, landmark.presence) if v is not None and math.isfinite(float(v))]
    return float(min(values)) if values else None


def joint_usable(record, name: str, width: int, height: int) -> bool:
    """Tracked, above the engine's MEDIUM gate, and inside the frame."""

    score = joint_score(record, name)
    point = (getattr(getattr(record, "points", None), "pixels", {}) or {}).get(name)
    if score is None or point is None or score < USABLE_SCORE:
        return False
    x, y = float(point[0]), float(point[1])
    return 0 <= x < width and 0 <= y < height


def pose_table(record, width: int, height: int) -> dict:
    """Per body group: usable only when every joint of the group is."""

    if record is None:
        return {group: None for group in JOINT_GROUPS}
    return {group: all(joint_usable(record, n, width, height) for n in names) for group, names in JOINT_GROUPS.items()}


def classify(row: dict) -> tuple[str, list[str]]:
    """Conservative PASS / PARTIAL / FAIL with the reasons that decided it.

    PASS: build ran, camera VALID, six phases found and confirmed correct by
    review, every body group usable at every phase, shaft shown at >= 5 of 6
    phases with no shaft judged wrong by review, tush line plus all three later
    pelvis phases measured and judged plausible, interpretation ran with no
    verdict and no banned wording.

    FAIL: the build did not complete, the camera is INVALID for a reason other
    than camera motion (motion alone only withholds position measurements and
    is PARTIAL), fewer than four
    phases were found, or review found Address, Top or Impact on the wrong
    frame (every measurement is relative to those, so none can be trusted).

    PARTIAL: everything else.
    """

    reasons: list[str] = []
    if not row.get("build_ok"):
        error = str(row.get("error", "unknown"))
        return "FAIL", [error if error.startswith("report:") else "engine did not complete: " + error]
    review = row.get("review") or {}
    phases_correct = review.get("phases_correct") or {}
    camera = row["camera"]["status"]
    found = row["phases_found"]
    signals = (row["camera"].get("signals") or {})
    # A camera that only moved invalidates position measurements (tush line,
    # hip/head movement), which the engine then withholds; angles, phases and
    # shafts still stand, so that alone is PARTIAL, not FAIL.
    motion_only = camera == "INVALID" and bool(signals) and all(
        s.get("status") != "INVALID" for name, s in signals.items() if name != "camera_motion")
    if camera == "INVALID" and not motion_only:
        reasons.append("camera INVALID")
    if found < 4:
        reasons.append(f"only {found}/6 phases found")
    wrong_anchor = [p for p in ("address", "top", "impact") if phases_correct.get(p) is False]
    if wrong_anchor:
        reasons.append("wrong frame for " + ", ".join(wrong_anchor))
    if reasons:
        return "FAIL", reasons

    window = row.get("window") or {}
    if window.get("confidence") in ("LOW", "NONE"):
        reasons.append(f"swing window {window.get('status')} ({window.get('confidence')})")
    if motion_only:
        reasons.append("camera moved beyond the position noise floor: tush line and hip/head movement withheld")
    elif camera != "VALID":
        reasons.append(f"camera {camera}")
    if found < 6:
        reasons.append(f"{found}/6 phases found")
    if not review:
        reasons.append("not visually reviewed")
    else:
        wrong = [p for p, ok in phases_correct.items() if ok is False]
        if wrong:
            reasons.append("wrong frame for " + ", ".join(wrong))
    if not row["pose_usable"]:
        reasons.append("pose gaps: " + ", ".join(row["pose_gaps"][:4]) + ("..." if len(row["pose_gaps"]) > 4 else ""))
    shaft_errors = review.get("shaft_errors") or []
    if row["shaft_shown"] < 5:
        reasons.append(f"shaft shown at {row['shaft_shown']}/6")
    if shaft_errors:
        reasons.append("shaft wrong at " + ", ".join(shaft_errors))
    if not row["pelvis_valid"]:
        reasons.append("pelvis: " + row["pelvis_reason"])
    elif review and review.get("pelvis_plausible") is False:
        reasons.append("rear-pelvis point judged implausible")
    if not row["interpretation_ok"]:
        reasons.append("interpretation: " + row["interpretation_note"])
    return ("PASS", []) if not reasons else ("PARTIAL", reasons)


# ---------------------------------------------------------------------------
# Running the engine
# ---------------------------------------------------------------------------


def slug_for(index: int, video: Path) -> str:
    stem = video.stem.replace("WhatsApp Video ", "").replace(" at ", "_").replace(".", "-").replace(" ", "_")
    return f"{index:02d}_{stem}"


def videos_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in VIDEO_EXTENSIONS)


def run_engine(video: Path, bundle: Path) -> dict:
    bundle.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "scripts" / "build_analysis.py"), str(video),
               "--out", str(bundle), "--records", str(bundle / "pose_records.pkl")]
    began = time.monotonic()
    process = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    elapsed = time.monotonic() - began
    (bundle / "build.log").write_text(process.stdout + "\n--- stderr ---\n" + process.stderr, encoding="utf-8")
    return {"returncode": process.returncode, "wall_s": round(elapsed, 1),
            "stderr_tail": process.stderr.strip().splitlines()[-3:] if process.returncode else []}


# ---------------------------------------------------------------------------
# Reading one bundle back
# ---------------------------------------------------------------------------


def read_frames(video: Path, indices: set[int]) -> dict[int, np.ndarray]:
    """Sequential decode (as the engine does), keeping only ``indices``."""

    import cv2

    capture = cv2.VideoCapture(str(video))
    frames, index, last = {}, 0, max(indices) if indices else -1
    while index <= last:
        ok, image = capture.read()
        if not ok:
            break
        if index in indices:
            frames[index] = image
        index += 1
    capture.release()
    return frames


def locate(video: Path, folder: Path) -> dict:
    """Stage 1: find the swing and, if needed, cut an analysis window (source untouched)."""

    from swingguru.swing_window import cut_window, locate_swing

    began = time.monotonic()
    decision, signal = locate_swing(video)
    window = decision.to_dict()
    fps = float(signal["fps"])
    window.update({"fps": fps, "locate_s": round(time.monotonic() - began, 1),
                   "start_frame": int(round(decision.start_s * fps)), "end_frame": int(round(decision.end_s * fps)),
                   "analysed_file": video.name})
    if decision.trimmed:
        target = folder / "window.mp4"
        cut_window(video, target, window["start_frame"], window["end_frame"])
        window["analysed_file"] = str(target.relative_to(ROOT))
    else:
        window["start_frame"] = 0
    (folder / "swing_window.json").write_text(json.dumps({"window": window, "signal": signal}, default=float),
                                             encoding="utf-8")
    finder_sheet(video, folder, window, signal)
    return window


def analysed_video(video: Path, window: dict) -> Path:
    return ROOT / window["analysed_file"] if window.get("trimmed") else video


def finder_sheet(video: Path, folder: Path, window: dict, signal: dict) -> None:
    """Hand elevation over the whole clip, candidates, the chosen window, thumbnails."""

    import cv2

    duration = max(float(signal["duration_s"]), 1e-3)
    width, height = 1600, 220
    plot = np.full((height, width, 3), 255, np.uint8)

    def x_of(t):
        return int(t / duration * (width - 1))

    def y_of(e):
        return int(height - 20 - (e + 1.0) / 3.5 * (height - 40))

    for level, colour in ((0.35, (200, 200, 255)), (0.8, (200, 255, 200))):
        cv2.line(plot, (0, y_of(level)), (width, y_of(level)), colour, 1)
    points = [(x_of(t), y_of(e)) for t, e in zip(signal["times"], signal["elevation"]) if e == e]
    for a, b in zip(points, points[1:]):
        cv2.line(plot, a, b, (0, 0, 0), 1, cv2.LINE_AA)
    cv2.rectangle(plot, (x_of(window["start_s"]), 0), (x_of(window["end_s"]), 8), (0, 160, 0), -1)
    for candidate in window["candidates"]:
        colour = (0, 0, 255) if candidate["complete"] else (0, 165, 255)
        cv2.line(plot, (x_of(candidate["top_s"]), 10), (x_of(candidate["top_s"]), height), colour, 1)
    for cut in window.get("scene_cuts", []):
        cv2.line(plot, (x_of(cut), 10), (x_of(cut), height), (200, 0, 200), 1)
    for second in range(int(duration) + 1):
        cv2.putText(plot, str(second), (x_of(second), height - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (90, 90, 90), 1)
    cv2.putText(plot, f"{window['status']} {window['confidence']} window {window['start_s']}-{window['end_s']}s",
                (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (20, 20, 20), 1, cv2.LINE_AA)
    capture = cv2.VideoCapture(str(video))
    fps, thumbs = float(signal["fps"]), []
    for k in range(24):
        t = (k + 0.5) * duration / 24
        capture.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
        ok, frame = capture.read()
        thumb = cv2.resize(frame, (width // 12, int(frame.shape[0] * (width // 12) / frame.shape[1]))) if ok \
            else np.zeros((230, width // 12, 3), np.uint8)
        cv2.putText(thumb, f"{t:.1f}", (3, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        thumbs.append(thumb)
    capture.release()
    tallest = max(t.shape[0] for t in thumbs)
    thumbs = [cv2.copyMakeBorder(t, 0, tallest - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in thumbs]
    grid = np.vstack([np.hstack(thumbs[:12]), np.hstack(thumbs[12:])])
    grid = cv2.resize(grid, (width, int(grid.shape[0] * width / grid.shape[1])))
    cv2.imwrite(str(folder / "swing_window.jpg"), np.vstack([plot, grid]), [cv2.IMWRITE_JPEG_QUALITY, 82])


def swing_row(video: Path, folder: Path, engine: dict, window: dict | None = None) -> dict:
    from swingguru.auto_moments import estimate_moments
    from swingguru.interpretation import terminology_violations
    from swingguru.phases import Phase
    from swingguru.pose_cache import load_records_any
    from swingguru.video import probe_video

    window = window or {"trimmed": False, "start_s": 0.0}
    info = probe_video(video)
    analysed = analysed_video(video, window)
    offset_s = float(window.get("start_s", 0.0)) if window.get("trimmed") else 0.0
    bundle = folder / "bundle"
    row: dict = {
        "video": video.name, "slug": folder.name,
        "general": {
            "duration_s": round((info.frame_count or 0) / info.fps, 2) if info.fps else None,
            "resolution": f"{info.width}x{info.height}", "fps": round(float(info.fps), 2),
            "frames": info.frame_count, "handedness": "unknown",
            "analysis_runtime_s": engine.get("wall_s"),
        },
        "window": {k: window.get(k) for k in ("status", "confidence", "reason", "start_s", "end_s", "trimmed",
                                              "start_frame", "end_frame", "scene_cuts", "locate_s", "analysed_file")},
        "candidates": [{k: round(c[k], 2) if isinstance(c[k], float) else c[k] for k in
                        ("still_end_s", "top_s", "impact_s", "complete", "follow_through")}
                       for c in window.get("candidates", [])],
        "build_ok": engine.get("returncode") == 0 and (bundle / "analysis.json").exists(),
    }
    review_path = folder / "review.json"
    row["review"] = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None
    if not row["build_ok"]:
        row["error"] = f"exit {engine.get('returncode')}: " + " | ".join(engine.get("stderr_tail") or [])
        row.update({"camera": {"status": "n/a"}, "phases_found": 0, "pose_usable": False, "pose_gaps": [],
                    "shaft_shown": 0, "pelvis_valid": False, "pelvis_reason": "not run",
                    "interpretation_ok": False, "interpretation_note": "not run"})
        row["status"], row["status_reasons"] = classify(row)
        return row

    analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    records = {int(r.index): r for r in load_records_any(bundle / "pose_records.pkl")}
    width, height = int(analysis["width"]), int(analysis["height"])
    timings = analysis["engine"]["timings"]
    row["general"]["engine_total_s"] = timings.get("total_s")
    row["general"]["pose_s"] = timings.get("pose_s")
    row["general"]["analysed_size"] = f"{width}x{height}"

    # Handedness: the engine assumes right-handed; the Address hands-vs-hips
    # side is only evidence for it on a standard DTL camera.
    context = analysis["geometry_context"]
    if context.get("toward_ball_known"):
        row["general"]["handedness"] = ("right (inferred: ball on image right)" if context["toward_ball"] > 0
                                        else "left (inferred: ball on image left)")

    # Camera -----------------------------------------------------------------
    camera = analysis["camera_check"]
    signals = camera.get("signals", {})

    def sig(name):
        return (signals.get(name) or {}).get("status")

    row["camera"] = {
        "status": camera.get("status"),
        "reason": "; ".join(f"{n}: {s.get('status')} ({s.get('value', s.get('note', ''))})"
                            for n, s in signals.items() if s.get("status") != "VALID")
                  or ("all signals VALID" if signals else camera.get("note", "")),
        "fully_visible": sig("full_body"),
        "stable": sig("camera_motion"),
        "dtl_geometry": "VALID" if sig("orientation_width") == sig("hands_in_front") == "VALID"
        else f"width {sig('orientation_width')}, hands {sig('hands_in_front')}",
        "signals": {n: {"status": s.get("status"), "value": s.get("value")} for n, s in signals.items()},
    }

    # Phases -----------------------------------------------------------------
    moments = estimate_moments(list(records.values()))  # deterministic; same call as the engine
    by_phase = {p["phase"]: p for p in analysis["phases"]}
    row["phases"] = {}
    for key, label in PHASES:
        phase = by_phase.get(key, {})
        estimate = moments.get(Phase(key))
        agree = estimate.frame_index == phase.get("frame_index")
        row["phases"][key] = {
            "label": label, "frame": phase.get("frame_index"),
            # Time in the source clip (window offset added back).
            "time_s": None if phase.get("frame_index") is None
            else round(records[phase["frame_index"]].timestamp_ms / 1000 + offset_s, 3),
            "source_frame": None if phase.get("frame_index") is None
            else int(phase["frame_index"]) + int(window.get("start_frame") or 0),
            "confidence": estimate.confidence, "found": phase.get("frame_index") is not None,
            "evidence": list(estimate.evidence)[:3], "matches_bundle": agree,
        }
    row["phases_found"] = sum(1 for p in row["phases"].values() if p["found"])

    # Pose -------------------------------------------------------------------
    row["pose"], gaps = {}, []
    for key, label in PHASES:
        frame = row["phases"][key]["frame"]
        record = records.get(frame) if frame is not None else None
        table = pose_table(record, width, height)
        scores = {n: (None if record is None else joint_score(record, n)) for n in ("left_wrist", "left_elbow")}
        row["pose"][key] = {"groups": table, "lead_wrist_score": scores["left_wrist"], "lead_elbow_score": scores["left_elbow"]}
        gaps += [f"{label} {g}" for g, ok in table.items() if ok is False]
    top_groups = row["pose"]["top"]["groups"]
    row["pose_focus"] = {
        "lead_wrist_all_phases": all(row["pose"][k]["groups"]["wrists"] for k, _ in PHASES if row["phases"][k]["found"]),
        "lead_elbow_all_phases": all(row["pose"][k]["groups"]["elbows"] for k, _ in PHASES if row["phases"][k]["found"]),
        "both_hands_at_top": top_groups["wrists"],
        "impact_all_groups": all(v for v in row["pose"]["impact"]["groups"].values()),
    }
    row["pose_gaps"] = gaps
    row["pose_usable"] = not gaps and row["phases_found"] == 6

    # Club / shaft -------------------------------------------------------------
    hidden = {(h["phase"], h["kind"]): h.get("reason", "") for h in analysis["hidden"]}
    fallback = {f["phase"]: f for f in analysis["engine"].get("club_fallback", [])}
    row["club"] = {}
    for key, label in PHASES:
        check = next((c for c in by_phase.get(key, {}).get("checks", []) if c["kind"] == "shaft"), None)
        if check:
            row["club"][key] = {"detected": True, "source": check.get("source", "classical"),
                                "angle_deg": check["raw"]["shaft_from_horizontal_deg"], "level": check["level"],
                                "confidence": check.get("confidence")}
        else:
            reason = hidden.get((key, "shaft"), "")
            if key in fallback:
                reason += f"; OWLv2 tried: {fallback[key].get('reason', fallback[key].get('level'))}"
            row["club"][key] = {"detected": False, "source": "unavailable", "angle_deg": None, "level": None,
                                "confidence": None, "reason": reason}
    row["shaft_shown"] = sum(1 for c in row["club"].values() if c["detected"])
    row["owlv2_frames_run"] = analysis["engine"]["timings"].get("owlv2_frames_run", 0)

    # Pelvis depth -------------------------------------------------------------
    depth = analysis.get("pelvis_depth") or {}
    address = depth.get("address") or {}
    row["pelvis"] = {
        "tush_line": bool(address.get("available")), "tush_line_level": address.get("level"),
        "tush_line_reason": address.get("reason", ""), "tush_line_x": depth.get("tush_line_x"),
        "edge_margin_px": address.get("edge_margin_px"),
    }
    for key in PELVIS_PHASES:
        info = (depth.get("phases") or {}).get(key) or {}
        row["pelvis"][key] = {"pct": info.get("displacement_pct_height"), "level": info.get("level"),
                              "direction": info.get("direction"), "reason": info.get("reason", "")}
    missing = [k for k in PELVIS_PHASES if row["pelvis"][k]["pct"] is None]
    moved = (signals.get("camera_motion") or {}).get("status") == "INVALID"
    row["pelvis"]["camera_moved"] = moved
    row["pelvis_valid"] = row["pelvis"]["tush_line"] and not missing and not moved
    row["pelvis_reason"] = ("no tush line: " + row["pelvis"]["tush_line_reason"]) if not row["pelvis"]["tush_line"] else \
        ("; ".join(f"{k}: {row['pelvis'][k]['reason']}" for k in missing) if missing else
         "measured, but the camera moved more than the position noise floor" if moved else "")

    # Interpretation -------------------------------------------------------------
    interp = analysis.get("interpretations") or {}
    results = interp.get("results", [])
    described = [r for r in results if r["status"] == "described"]
    patterns = [r for r in results if r["status"] in ("observed", "fired") and r["calibration"] != "DESCRIPTIVE_ONLY"]
    verdicts = [r for r in results if r["status"] == "fired" and r["calibration"] == "VALIDATED"]
    words = sorted({w for r in results for t in (r.get("user_text"), r.get("why_it_matters")) for w in terminology_violations(t)})
    row["interpretation"] = {
        "descriptive": [r["user_text"] for r in described],
        "patterns": [{"rule_id": r["rule_id"], "status": r["status"], "calibration": r["calibration"],
                      "text": r["user_text"]} for r in patterns],
        "blocked": {r["rule_id"]: r.get("reason") for r in results if r["status"] == "blocked"},
        "verdicts": [r["rule_id"] for r in verdicts],
        "focus": analysis.get("focus"), "banned_terms": words,
    }
    row["interpretation_ok"] = bool(results) and not verdicts and not words and analysis.get("focus") is None
    row["interpretation_note"] = "" if row["interpretation_ok"] else (
        f"verdicts {row['interpretation']['verdicts']}, banned {words}, focus {analysis.get('focus')}")

    row["status"], row["status_reasons"] = classify(row)
    contact_sheet(analysed, folder, analysis, records, row)
    return row


# ---------------------------------------------------------------------------
# Contact sheet
# ---------------------------------------------------------------------------


def contact_sheet(video: Path, folder: Path, analysis: dict, records: dict, row: dict) -> None:
    import cv2

    width, height = int(analysis["width"]), int(analysis["height"])
    by_phase = {p["phase"]: p for p in analysis["phases"]}
    wanted = {p["frame_index"] for p in analysis["phases"] if p["frame_index"] is not None}
    frames = read_frames(video, wanted)
    depth = analysis.get("pelvis_depth") or {}
    tush_x = depth.get("tush_line_x")
    tile_h = 640
    scale = tile_h / height
    tile_w = int(round(width * scale))
    tiles = []
    for key, label in PHASES:
        frame_index = by_phase.get(key, {}).get("frame_index")
        image = frames.get(frame_index) if frame_index is not None else None
        if image is None:
            tile = np.full((tile_h, tile_w, 3), 40, np.uint8)
            cv2.putText(tile, f"{label}: not found", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            tiles.append(tile)
            continue
        if image.shape[:2] != (height, width):
            image = cv2.resize(image, (width, height))
        tile = cv2.resize(image, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        record = records.get(frame_index)

        def p(point):
            return (int(round(float(point[0]) * scale)), int(round(float(point[1]) * scale)))

        if record is not None:
            pixels = record.points.pixels
            for a, b in SKELETON:
                if a in pixels and b in pixels:
                    ok = joint_usable(record, a, width, height) and joint_usable(record, b, width, height)
                    cv2.line(tile, p(pixels[a]), p(pixels[b]), (90, 220, 90) if ok else (60, 60, 230), 2, cv2.LINE_AA)
            for names in JOINT_GROUPS.values():
                for n in names:
                    if n in pixels:
                        ok = joint_usable(record, n, width, height)
                        colour = (0, 255, 255) if n == "left_wrist" else ((90, 220, 90) if ok else (60, 60, 230))
                        cv2.circle(tile, p(pixels[n]), 4 if n == "left_wrist" else 3, colour, -1, cv2.LINE_AA)
        shaft = next((c for c in by_phase[key]["checks"] if c["kind"] == "shaft"), None)
        if shaft:
            line = next(g for g in shaft["geometry"] if g.get("role") == "shaft")
            (x1, y1), (x2, y2) = line["points"]
            colour = (0, 200, 255) if shaft.get("source") == "classical" else (255, 120, 255)
            cv2.line(tile, (int(x1 * tile_w), int(y1 * tile_h)), (int(x2 * tile_w), int(y2 * tile_h)), colour, 3, cv2.LINE_AA)
        if tush_x is not None and key in ("address",) + PELVIS_PHASES:
            x = int(round(tush_x * scale))
            for y in range(0, tile_h, 12):
                cv2.line(tile, (x, y), (x, y + 6), (255, 255, 255), 1, cv2.LINE_AA)
            rear = depth["address"] if key == "address" else (depth["phases"].get(key) or {}).get("rear")
            if rear and rear.get("point"):
                cv2.circle(tile, p(rear["point"]), 5, (255, 80, 40), -1, cv2.LINE_AA)
        club = row["club"][key]
        lines = [f"{label} f{frame_index} {row['phases'][key]['time_s']}s",
                 f"shaft {club['angle_deg']:.0f} {club['level']} {club['source'][:5]}" if club["detected"] else "shaft: none"]
        if key in PELVIS_PHASES and row["pelvis"][key]["pct"] is not None:
            lines.append(f"rear {row['pelvis'][key]['pct']:+.1f}% {row['pelvis'][key]['level']}")
        cv2.rectangle(tile, (0, 0), (tile_w, 8 + 22 * len(lines)), (25, 25, 25), -1)
        for i, text in enumerate(lines):
            cv2.putText(tile, text, (6, 22 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        tiles.append(tile)
    sheet = np.vstack([np.hstack(tiles[:3]), np.hstack(tiles[3:])])
    header = np.full((34, sheet.shape[1], 3), 245, np.uint8)
    text = (f"{row['video']} | camera {row['camera']['status']} | phases {row['phases_found']}/6 | "
            f"shaft {row['shaft_shown']}/6 | tush line {'yes' if row['pelvis']['tush_line'] else 'no'}")
    cv2.putText(header, text, (8, 23), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 1, cv2.LINE_AA)
    cv2.imwrite(str(folder / "contact_sheet.jpg"), np.vstack([header, sheet]), [cv2.IMWRITE_JPEG_QUALITY, 88])


# ---------------------------------------------------------------------------
# Batch report
# ---------------------------------------------------------------------------


def yes(value) -> str:
    return "yes" if value else "no"


def write_report(rows: list[dict], path: Path) -> None:
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("PASS", "PARTIAL", "FAIL")}
    lines = [
        "# Robustness batch - baseline (engine unchanged)", "",
        f"Videos: {len(rows)} | PASS {counts['PASS']} | PARTIAL {counts['PARTIAL']} | FAIL {counts['FAIL']}", "",
        "Generated by `scripts/batch_validate.py`. Per-swing detail and contact sheets: "
        "`var/qa/test_swings/<swing>/` (`report.json`, `contact_sheet.jpg`, `review.json`, `bundle/`).", "",
        "Status definitions (conservative, see `classify()`):", "",
        "* **PASS** - camera VALID; 6/6 phases found and confirmed on the right frame by visual review; every body "
        "group usable at every phase; shaft shown at >= 5/6 phases with none judged wrong; tush line plus Top/Mid-Down/"
        "Impact measured and plausible; interpretation ran with no verdict and no banned wording.",
        "* **FAIL** - engine did not complete, camera INVALID (other than camera motion alone, which withholds only "
        "the position measurements and is PARTIAL), < 4 phases, or Address/Top/Impact on the wrong frame.",
        "* **PARTIAL** - anything in between.", "",
        "| # | Video | Swing window | Camera | 6/6 phases? | Phases right (review) | Pose usable? | Shaft coverage | "
        "Tush line | Interpretation | Runtime | Overall |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        review = r.get("review") or {}
        correct = review.get("phases_correct") or {}
        right = f"{sum(1 for v in correct.values() if v)}/6" if correct else "unreviewed"
        pelvis = r.get("pelvis") or {}
        tush = "no" if not pelvis.get("tush_line") else ("yes, 3/3" if r["pelvis_valid"] else "line only")
        w = r.get("window") or {}
        window = (f"{w['status'].replace('_', ' ').lower()}, {w.get('confidence')}, "
                  f"{w.get('start_s')}-{w.get('end_s')} s{' cut' if w.get('trimmed') else ''}") if w.get("status") else "-"
        lines.append(
            f"| {r['slug'][:2]} | {r['video'].replace('WhatsApp Video 2026-09-29 at ', '')} | {window} | {r['camera']['status']} | "
            f"{r['phases_found']}/6 | {right} | {yes(r['pose_usable'])} | {r['shaft_shown']}/6 | {tush} | "
            f"{'ok' if r['interpretation_ok'] else 'check'} | {r['general'].get('analysis_runtime_s')} s | **{r['status']}** |")
    findings = path.parent / "FINDINGS.md"
    if findings.exists():  # hand-written reviewer findings, kept across regenerations
        lines += ["", findings.read_text(encoding="utf-8").strip()]
    lines += ["", "## Why each swing is not PASS", ""]
    for r in rows:
        if r["status"] != "PASS":
            lines.append(f"* **{r['slug']}** ({r['status']}): " + "; ".join(r["status_reasons"]))
    lines += ["", "## Per-swing detail", ""]
    for r in rows:
        lines += detail_lines(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def detail_lines(r: dict) -> list[str]:
    g = r["general"]
    out = [f"### {r['slug']} - {r['status']}", "",
           f"{r['video']} | {g.get('duration_s')} s | {g.get('resolution')} (analysed {g.get('analysed_size', '?')}) | "
           f"{g.get('fps')} fps | handedness {g.get('handedness')} | runtime {g.get('analysis_runtime_s')} s", ""]
    w = r.get("window") or {}
    if w:
        tops = ", ".join(f"{c['top_s']} s{'' if c['complete'] else ' (incomplete)'}" for c in r.get("candidates", []))
        out += [f"Swing window: **{w.get('status')}** ({w.get('confidence')}) - {w.get('reason')}. Analysed "
                f"{w.get('start_s')}-{w.get('end_s')} s of {g.get('duration_s')} s, "
                f"{'trimmed automatically' if w.get('trimmed') else 'whole clip'}. Candidate Tops: {tops or 'none'}."
                + (f" Scene cuts at {w.get('scene_cuts')} s." if w.get("scene_cuts") else ""), ""]
    if not r["build_ok"]:
        return out + [f"Engine error: {r.get('error')}", ""]
    c = r["camera"]
    out.append(f"Camera **{c['status']}** - {c['reason']} | full body {c['fully_visible']} | stable {c['stable']} | "
               f"DTL geometry {c['dtl_geometry']}")
    out.append("")
    review = r.get("review") or {}
    correct = review.get("phases_correct") or {}
    out += ["| Phase | Frame | Time | Conf | Right? | Pose gaps | Shaft | Rear pelvis |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for key, label in PHASES:
        ph, club, pose = r["phases"][key], r["club"][key], r["pose"][key]["groups"]
        gaps = ", ".join(k for k, v in pose.items() if v is False) or "-"
        shaft = (f"{club['angle_deg']:.0f} deg {club['level']} {club['source']}" if club["detected"]
                 else "none" + (f" ({club.get('reason', '')[:70]})" if club.get("reason") else ""))
        rear = "-"
        if key == "address":
            rear = f"tush line {r['pelvis']['tush_line_level']}" if r["pelvis"]["tush_line"] else f"none ({r['pelvis']['tush_line_reason']})"
        elif key in PELVIS_PHASES:
            pv = r["pelvis"][key]
            rear = f"{pv['pct']:+.1f}% {pv['level']}" if pv["pct"] is not None else f"none ({pv['reason'][:50]})"
        right = {True: "yes", False: "**no**", None: "?"}[correct.get(key)] if correct else "?"
        out.append(f"| {label} | {ph['source_frame']} | {ph['time_s']} | {ph['confidence']} | {right} | {gaps} | {shaft} | {rear} |")
    i = r["interpretation"]
    out += ["", f"Interpretation: {len(i['descriptive'])} descriptive; patterns: "
            + (", ".join(f"{p['rule_id']} ({p['status']}, {p['calibration']})" for p in i["patterns"]) or "none")
            + f"; verdicts: {i['verdicts'] or 'none'}; focus: {i['focus']}"]
    if review.get("notes"):
        out += ["", "Review: " + review["notes"]]
    out.append("")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "report"))
    parser.add_argument("folder", nargs="?", default=str(ROOT / "var" / "test_swings"))
    parser.add_argument("--out", default=str(OUT_ROOT))
    parser.add_argument("--only", default=None, help="comma-separated swing numbers to (re)run")
    args = parser.parse_args(argv)
    out_root = Path(args.out)
    only = {int(x) for x in args.only.split(",")} if args.only else None
    rows = []
    for number, video in enumerate(videos_in(Path(args.folder)), start=1):
        folder = out_root / slug_for(number, video)
        folder.mkdir(parents=True, exist_ok=True)
        engine_path, window_path = folder / "engine_run.json", folder / "swing_window.json"
        if args.mode == "run" and (only is None or number in only):
            print(f"[{number:02d}] {video.name} ...", flush=True)
            window = locate(video, folder)
            print(f"     window {window['status']} {window['confidence']} {window['start_s']}-{window['end_s']} s"
                  f"{' (trimmed)' if window['trimmed'] else ''}", flush=True)
            engine = run_engine(analysed_video(video, window), folder / "bundle")
            engine_path.write_text(json.dumps(engine, indent=2), encoding="utf-8")
            print(f"     exit {engine['returncode']} in {engine['wall_s']} s", flush=True)
        engine = json.loads(engine_path.read_text(encoding="utf-8")) if engine_path.exists() else {"returncode": None}
        window = json.loads(window_path.read_text(encoding="utf-8"))["window"] if window_path.exists() else None
        try:
            row = swing_row(video, folder, engine, window)
        except Exception as error:  # a report problem must not hide the other swings
            row = {"video": video.name, "slug": folder.name, "build_ok": False, "error": f"report: {error!r}",
                   "general": {}, "camera": {"status": "n/a"}, "phases_found": 0, "pose_usable": False,
                   "pose_gaps": [], "shaft_shown": 0, "pelvis_valid": False, "pelvis_reason": "", "interpretation_ok": False,
                   "interpretation_note": ""}
            row["status"], row["status_reasons"] = classify(row)
        (folder / "report.json").write_text(json.dumps(row, indent=2, default=str), encoding="utf-8")
        rows.append(row)
    write_report(rows, REPORT_PATH)
    print(json.dumps({r["slug"]: [r["status"]] + r["status_reasons"] for r in rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
