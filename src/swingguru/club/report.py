"""Developer diagnostics for club tracking: panels, tables and an HTML view.

Every frame gets four panels side by side:

1. the original frame (cropped around the golfer);
2. body landmarks used as constraints: hands anchor, forearms, body segments
   and the search radius;
3. every candidate line: accepted (yellow, best first) and rejected (red,
   with the reason);
4. the selected shaft coloured by confidence level, or the reason there is
   none.

Nothing here is used by the golfer-facing app.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from typing import Mapping, Sequence

import cv2
import numpy as np

from .tracker import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    ClubTrack,
    FrameDetection,
    FrameShaft,
)

LEVEL_COLOURS = {
    LEVEL_HIGH: (80, 200, 90),
    LEVEL_MEDIUM: (40, 200, 230),
    LEVEL_LOW: (40, 130, 240),
}
ACCEPTED = (0, 230, 255)
REJECTED = (60, 60, 230)
LANDMARK = (255, 200, 60)


def _pt(point) -> tuple[int, int]:
    return int(round(float(point[0]))), int(round(float(point[1])))


def crop_box(detection: FrameDetection, shape: tuple[int, int], *, radius_torso: float = 3.0) -> tuple[int, int, int, int]:
    """Square-ish crop around the hands (or the whole frame without hands)."""

    height, width = shape[:2]
    origin = detection.hands.origin
    torso = detection.hands.torso_px
    if origin is None or torso <= 1:
        return 0, 0, width, height
    radius = radius_torso * torso
    x0 = int(max(0, origin[0] - radius))
    y0 = int(max(0, origin[1] - radius))
    x1 = int(min(width, origin[0] + radius))
    y1 = int(min(height, origin[1] + radius))
    return x0, y0, x1, y1


def _label(image: np.ndarray, text: str, row: int = 0, colour=(255, 255, 255)) -> None:
    y = 18 + row * 18
    cv2.putText(image, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(image, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)


def frame_panels(
    frame_bgr: np.ndarray,
    detection: FrameDetection,
    shaft: FrameShaft,
    *,
    moment: str = "",
    panel_height: int = 360,
) -> np.ndarray:
    """Four diagnostic panels for one frame, as one BGR image."""

    x0, y0, x1, y1 = crop_box(detection, frame_bgr.shape)
    offset = np.array([x0, y0], float)

    def local(point) -> tuple[int, int]:
        return _pt(np.asarray(point, float) - offset)

    base = frame_bgr[y0:y1, x0:x1].copy()
    original = base.copy()
    landmarks = (base * 0.55).astype(np.uint8)
    candidates = (base * 0.55).astype(np.uint8)
    selected = base.copy()

    hands = detection.hands
    if hands.origin is not None:
        cv2.circle(landmarks, local(hands.origin), 5, LANDMARK, -1, cv2.LINE_AA)
        cv2.circle(
            landmarks,
            local(hands.origin),
            int(2.8 * hands.torso_px),
            LANDMARK,
            1,
            cv2.LINE_AA,
        )
        for wrist, elbow in hands.forearms:
            cv2.line(landmarks, local(wrist), local(elbow), (255, 120, 255), 2, cv2.LINE_AA)
        for _, a, b in hands.body_segments:
            cv2.line(landmarks, local(a), local(b), (200, 200, 200), 1, cv2.LINE_AA)
    _label(landmarks, f"hands: {hands.source}")

    for candidate in detection.rejected:
        cv2.line(candidates, local(candidate.grip), local(candidate.tip), REJECTED, 1, cv2.LINE_AA)
    for rank, candidate in enumerate(detection.candidates):
        thickness = 2 if rank == 0 else 1
        cv2.line(candidates, local(candidate.grip), local(candidate.tip), ACCEPTED, thickness, cv2.LINE_AA)
        cv2.putText(
            candidates,
            f"{candidate.angle_deg:.0f}",
            local(candidate.tip),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            ACCEPTED,
            1,
            cv2.LINE_AA,
        )
    _label(candidates, f"{len(detection.candidates)} kept / {len(detection.rejected)} rejected")
    for row, candidate in enumerate(detection.rejected[:3], start=1):
        _label(candidates, f"x {candidate.angle_deg:.0f}: {candidate.rejected[:34]}", row, (160, 160, 255))

    if shaft.available and shaft.grip is not None and shaft.tip is not None:
        colour = LEVEL_COLOURS.get(shaft.level, (200, 200, 200))
        cv2.line(selected, local(shaft.grip), local(shaft.tip), (0, 0, 0), 5, cv2.LINE_AA)
        cv2.line(selected, local(shaft.grip), local(shaft.tip), colour, 3, cv2.LINE_AA)
        _label(selected, f"{shaft.level} {shaft.confidence:.2f}  {shaft.angle_deg:.0f} deg", 0, colour)
        _label(selected, shaft.method + (" (interpolated)" if shaft.interpolated else ""), 1)
    else:
        _label(selected, "UNAVAILABLE", 0, (120, 120, 255))
        _label(selected, (shaft.reason or detection.reason)[:46], 1)
    _label(original, f"frame {shaft.frame_index} {moment}".strip())

    panels = []
    for image in (original, landmarks, candidates, selected):
        scale = panel_height / max(1, image.shape[0])
        panels.append(cv2.resize(image, (max(1, int(image.shape[1] * scale)), panel_height)))
    return np.hstack(panels)


def continuity_label(shaft: FrameShaft) -> str:
    if shaft.interpolated:
        return "interpolated"
    if shaft.temporal_consistency is None:
        return "isolated" if shaft.available else "-"
    if shaft.temporal_consistency >= 0.7:
        return "smooth"
    if shaft.temporal_consistency >= 0.4:
        return "jumpy"
    return "break"


def write_report(
    track: ClubTrack,
    frames: Sequence[np.ndarray],
    *,
    out_dir: Path,
    moments: Mapping[int, str],
    notes: Mapping[int, str] | None = None,
    metrics: Mapping[str, object] | None = None,
    title: str = "Club tracking diagnostics",
) -> dict[str, Path]:
    """Write CSV/JSON data, key-moment images, all-frame panels and an HTML view."""

    out_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    notes = dict(notes or {})

    rows = []
    for index, (shaft, detection) in enumerate(zip(track.frames, track.detections)):
        moment = moments.get(shaft.frame_index, "")
        rows.append(
            {
                "frame": shaft.frame_index,
                "moment": moment,
                "detected": shaft.available,
                "status": shaft.status,
                "angle_deg": "" if shaft.angle_deg is None else round(shaft.angle_deg, 1),
                "confidence": round(shaft.confidence, 3),
                "level": shaft.level,
                "continuity": continuity_label(shaft),
                "method": shaft.method,
                "grip": "" if shaft.grip is None else f"{shaft.grip[0]:.1f},{shaft.grip[1]:.1f}",
                "tip": "" if shaft.tip is None else f"{shaft.tip[0]:.1f},{shaft.tip[1]:.1f}",
                "candidates": len(detection.candidates),
                "rejected": len(detection.rejected),
                "reason": shaft.reason or detection.reason,
                "notes": notes.get(shaft.frame_index, ""),
            }
        )
        if index < len(frames) and frames[index] is not None:
            panel = frame_panels(frames[index], detection, shaft, moment=moment, panel_height=260)
            cv2.imwrite(str(frames_dir / f"frame_{shaft.frame_index:05d}.jpg"), panel, [cv2.IMWRITE_JPEG_QUALITY, 82])
            if moment:
                key = frame_panels(frames[index], detection, shaft, moment=moment, panel_height=520)
                cv2.imwrite(str(out_dir / f"moment_{moment.lower().replace(' ', '_')}_{shaft.frame_index}.png"), key)

    csv_path = out_dir / "club_track.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else ["frame"])
        writer.writeheader()
        writer.writerows(rows)
    json_path = out_dir / "club_track.json"
    json_path.write_text(
        json.dumps(
            {
                "tracker": track.tracker,
                "frames": [item.to_dict() for item in track.frames],
                "metrics": metrics or {},
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    body = [
        f"<h1>{html.escape(title)}</h1>",
        f"<p>Tracker <code>{html.escape(track.tracker)}</code>. MEDIUM+ coverage "
        f"{track.coverage():.0%}, HIGH coverage {track.coverage(LEVEL_HIGH):.0%}.</p>",
        "<label><input type=checkbox id=keysonly> key moments only</label>",
        "<table><thead><tr><th>Frame</th><th>Moment</th><th>Detected</th><th>Angle</th>"
        "<th>Confidence</th><th>Continuity</th><th>Reason / notes</th></tr></thead><tbody>",
    ]
    for row in rows:
        css = "key" if row["moment"] else ""
        body.append(
            f"<tr class='{css} lvl-{row['level']}'><td>{row['frame']}</td><td>{html.escape(row['moment'])}</td>"
            f"<td>{'yes' if row['detected'] else 'no'}</td><td>{row['angle_deg']}</td>"
            f"<td>{row['level']} {row['confidence']}</td><td>{row['continuity']}</td>"
            f"<td>{html.escape(row['reason'])} {html.escape(row['notes'])}</td></tr>"
            f"<tr class='{css} img'><td colspan=7><img loading=lazy src='frames/frame_{row['frame']:05d}.jpg'></td></tr>"
        )
    body.append("</tbody></table>")
    page = (
        "<!doctype html><meta charset=utf-8><title>Club tracking diagnostics</title>"
        "<style>body{font:14px system-ui;margin:20px;background:#f7f7f2;color:#222}"
        "table{border-collapse:collapse}td,th{padding:4px 8px;border-bottom:1px solid #ddd;text-align:left}"
        "tr.key td{background:#fff7d6}img{max-width:100%}.lvl-HIGH td:nth-child(5){color:#237a3b}"
        ".lvl-UNAVAILABLE td:nth-child(5){color:#a33}body.k tr:not(.key){display:none}</style>"
        + "".join(body)
        + "<script>document.getElementById('keysonly').onchange=e=>document.body.classList.toggle('k',e.target.checked)</script>"
    )
    html_path = out_dir / "index.html"
    html_path.write_text(page, encoding="utf-8")
    return {"csv": csv_path, "json": json_path, "html": html_path}


__all__ = ["continuity_label", "crop_box", "frame_panels", "write_report"]
