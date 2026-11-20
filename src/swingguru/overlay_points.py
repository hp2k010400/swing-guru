"""Body points for the replay's guide lines (presentation only).

The guided viewer draws each measurement with its own geometry. To show *which part of the body* a
measurement is about (the thigh and shin under a hip hinge, the arm chain beside a club plane), it
also needs the tracked body points at each moment. This module exports them, additively, as
``analysis["overlay"]``:

    {"version": 1, "min_score": 0.5,
     "phases": {"address": {"frame": 33, "points": {"left_hip": [0.31, 0.56], ...}}, ...}}

Coordinates are normalised to the video frame (x / width, y / height), like every other geometry
point. Only landmarks the pose model scored at or above ``MIN_SCORE`` (visibility and presence) are
exported, so a guide is never drawn through a joint the tracker did not really see. Nothing here
changes a measurement, a moment, a verdict or the Swing Score.
"""

from __future__ import annotations

from typing import Mapping, Sequence

OVERLAY_VERSION = 1
MIN_SCORE = 0.5
# Paused-frame stabilisation (engine profile ``next`` only, overlay version 2): a guide joint that is
# nearly still around the moment (moves less than this share of the frame height between the frames
# either side) is drawn at the median of frame-1, frame, frame+1, which removes pose jitter. A
# moving joint keeps its exact-frame position, so guides never lag the measured lines. Presentation
# only: no measurement, verdict or score reads these points.
STILL_SHARE = 0.02

# The joints the guide lines use (MediaPipe vocabulary, as produced by every pose adapter).
GUIDE_LANDMARKS: tuple[str, ...] = (
    "nose", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
    "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle",
    "left_heel", "right_heel", "left_foot_index", "right_foot_index",
)


def _score(landmark: object) -> float:
    return min(float(getattr(landmark, "visibility", 0.0) or 0.0), float(getattr(landmark, "presence", 0.0) or 0.0))


def _reliable(record, name):
    if record is None:
        return None
    points = getattr(record, "points", None)
    if points is None:
        return None
    xy = (getattr(points, "pixels", {}) or {}).get(name)
    landmark = (getattr(points, "landmarks", {}) or {}).get(name)
    if xy is None or landmark is None or _score(landmark) < MIN_SCORE:
        return None
    return float(xy[0]), float(xy[1])


def overlay_points(records: Sequence[object], phases: Sequence[Mapping], width: float, height: float,
                   *, stabilise: bool = False) -> dict:
    """The reliably tracked body points at each moment's frame, normalised to the frame."""

    by_index = {int(getattr(record, "index")): record for record in records}
    out: dict = {"version": 2 if stabilise else OVERLAY_VERSION, "min_score": MIN_SCORE, "phases": {}}
    if stabilise:
        out["stabilised"] = {"window": "frame-1..frame+1", "still_share": STILL_SHARE}
    if not width or not height:
        return out
    for phase in phases:
        frame = phase.get("frame_index")
        record = by_index.get(int(frame)) if frame is not None else None
        points = getattr(record, "points", None) if record is not None else None
        if points is None:
            continue
        pixels = getattr(points, "pixels", {}) or {}
        landmarks = getattr(points, "landmarks", {}) or {}
        kept = {}
        for name in GUIDE_LANDMARKS:
            xy = pixels.get(name)
            landmark = landmarks.get(name)
            if xy is None or landmark is None or _score(landmark) < MIN_SCORE:
                continue
            if stabilise:
                before = _reliable(by_index.get(int(frame) - 1), name)
                after = _reliable(by_index.get(int(frame) + 1), name)
                if before and after and abs(after[0] - before[0]) + abs(after[1] - before[1]) < STILL_SHARE * float(height):
                    xy = (sorted((before[0], float(xy[0]), after[0]))[1], sorted((before[1], float(xy[1]), after[1]))[1])
            x, y = float(xy[0]) / float(width), float(xy[1]) / float(height)
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                kept[name] = [round(x, 5), round(y, 5)]
        if kept:
            out["phases"][str(phase.get("phase"))] = {"frame": int(frame), "points": kept}
    return out


__all__ = ["GUIDE_LANDMARKS", "MIN_SCORE", "OVERLAY_VERSION", "overlay_points"]
