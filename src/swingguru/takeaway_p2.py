"""Shaft-parallel-to-ground in the backswing (P2), from the tracked club.

Our Takeaway moment is the hands first leaving set-up, which is too early for
the classic takeaway checkpoint. P2 is the frame where the club is parallel to
the ground on the way back. In a down-the-line view the image vertical is the
world vertical, so "parallel to the ground" is the clubhead reaching the height
of the hands: the shaft's *elevation* (clubhead above hands, degrees) crosses
zero while rising. Its left/right direction is deliberately not used for
detection - a club parallel to the target line points at the camera, and its 2D
direction is then unreliable.

Detection (fails closed):
* frames from shortly before Takeaway to a few frames after Mid-Back (so the
  frame *after* the crossing can be seen), with a shaft that was detected, or
  recovered by :mod:`swingguru.club.temporal`, at HIGH or MEDIUM confidence -
  never interpolated;
* the elevation must rise through zero between two such frames at most
  ``max_bracket_frames`` apart (clubhead below the hands, then level or
  above): backswing direction confirmed, and P2 is pinned to within a frame;
* the P2 frame is the frame of that pair nearest to level; it must be at or
  before Mid-Back. At 30 fps the club turns ~30-35 degrees per frame here, so
  the nearest frame can be up to ~17 degrees from level; ``max_level_deg`` is
  only a sanity bound;
* the rise must be witnessed: within ``approach_frames`` before the crossing
  the club was seen at least ``approach_rise_deg`` lower;
* the line must not be longer in the image than the club at Address - near
  P2 the shaft points roughly at a down-the-line camera and is foreshortened,
  so a longer line is a background edge (the horizon, a mat or a fence).

Also reported at P2 (information for a future verdict, not a verdict):
``lateral_offset`` - clubhead minus hands, horizontally, toward the ball,
in Address shaft lengths; ``projected_length`` - shaft length in the image
relative to Address (foreshortening: ~0 means pointing at the camera).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

USABLE_LEVELS = ("HIGH", "MEDIUM")
SOURCE_DIRECT = "direct"
SOURCE_RECOVERED = "temporal recovery"


@dataclass(frozen=True)
class P2Config:
    frames_before_takeaway: int = 3
    max_bracket_frames: int = 2
    max_level_deg: float = 25.0
    high_level_deg: float = 8.0
    max_projected_length: float = 1.1
    approach_frames: int = 3
    approach_rise_deg: float = 10.0


def elevation_deg(grip, tip) -> float | None:
    """Clubhead height above the hands as an angle (+ = clubhead above)."""

    g, t = np.asarray(grip, float), np.asarray(tip, float)
    v = t - g
    length = float(np.hypot(*v))
    if length < 1e-6:
        return None
    return math.degrees(math.asin(max(-1.0, min(1.0, -float(v[1]) / length))))


def _source(point: Mapping) -> str:
    return SOURCE_RECOVERED if point.get("status") == "recovered" else SOURCE_DIRECT


def detect(path: Sequence[Mapping], *, takeaway_frame: int | None, mid_back_frame: int | None,
           toward_ball: float | None, address_shaft_px: float | None, config: P2Config | None = None,
           fps: float | None = None, first_frame: int = 0) -> dict:
    config = config or P2Config()
    if takeaway_frame is None or mid_back_frame is None:
        return {"detected": False, "reason": "needs both Takeaway and Mid-Back"}
    lo = takeaway_frame - config.frames_before_takeaway
    usable = [p for p in path if int(p["frame"]) >= lo and p.get("available") and not p.get("interpolated")
              and p.get("level") in USABLE_LEVELS and p.get("grip") is not None and p.get("tip") is not None]
    usable.sort(key=lambda p: int(p["frame"]))
    series = [(p, elevation_deg(p["grip"], p["tip"])) for p in usable]
    series = [(p, e) for p, e in series if e is not None]
    checked = [int(p["frame"]) for p, _ in series]
    crossing, loose = None, None
    for (a, ea), (b, eb) in zip(series, series[1:]):
        if int(a["frame"]) > mid_back_frame:
            break
        if ea < 0 <= eb:
            if int(b["frame"]) - int(a["frame"]) <= config.max_bracket_frames:
                crossing = (a, ea, b, eb)
                break
            loose = loose or (a, b)
    if crossing is None:
        reason = ("the club was not seen rising through level between Takeaway and Mid-Back"
                  if loose is None else
                  f"the club rose through level between frames {int(loose[0]['frame'])} and "
                  f"{int(loose[1]['frame'])} but was not seen in between")
        return {"detected": False, "reason": reason, "frames_checked": checked}
    a, ea, b, eb = crossing
    bracket = [int(a["frame"]), int(b["frame"])]
    chosen, elevation = (a, ea) if abs(ea) <= abs(eb) else (b, eb)
    if int(chosen["frame"]) > mid_back_frame:
        chosen, elevation = a, ea
    if abs(elevation) > config.max_level_deg:
        return {"detected": False, "reason": f"nearest frame is {abs(elevation):.0f} deg from level",
                "bracket": bracket, "frames_checked": checked}
    # Direction must be witnessed: shortly before the crossing the club was
    # seen clearly lower (still coming up from set-up). A lone near-level
    # line with nothing before it is as likely the horizon as the club.
    approach = [e for p, e in series if int(a["frame"]) - config.approach_frames <= int(p["frame"]) < int(a["frame"])]
    if not any(e <= ea - config.approach_rise_deg for e in approach):
        return {"detected": False, "bracket": bracket, "frames_checked": checked,
                "reason": "no rising approach seen before the level frame (could be a background line)"}
    grip, tip = np.asarray(chosen["grip"], float), np.asarray(chosen["tip"], float)
    length = float(np.hypot(*(tip - grip)))
    projected = None if not address_shaft_px else length / address_shaft_px
    if projected is not None and projected > config.max_projected_length:
        return {"detected": False, "bracket": bracket, "frames_checked": checked,
                "reason": f"the level line is {projected:.2f}x the Address club length - "
                          "a background line, not the foreshortened club"}
    lateral = None if not toward_ball or not address_shaft_px else float(tip[0] - grip[0]) * toward_ball / address_shaft_px
    sources = {_source(a), _source(b)}
    both_high = a.get("level") == "HIGH" and b.get("level") == "HIGH"
    level = "HIGH" if both_high and abs(elevation) <= config.high_level_deg and sources == {SOURCE_DIRECT} else "MEDIUM"
    frame = int(chosen["frame"])
    return {
        "detected": True,
        "frame": frame,
        "time_s": None if not fps else round((frame - first_frame) / fps, 4),
        "elevation_deg": round(elevation, 2),
        "angle_deg": chosen.get("angle_deg"),
        "bracket": bracket,
        "bracket_elevations": [round(ea, 1), round(eb, 1)],
        "source": _source(chosen),
        "bracket_sources": [_source(a), _source(b)],
        "level": level,
        "grip": [round(float(v), 2) for v in grip],
        "tip": [round(float(v), 2) for v in tip],
        "lateral_offset": None if lateral is None else round(lateral, 3),
        "projected_length": None if projected is None else round(projected, 3),
        "method": "elevation crosses level (rising) between tracked frames at most "
                  f"{config.max_bracket_frames} apart",
    }


__all__ = ["P2Config", "SOURCE_DIRECT", "SOURCE_RECOVERED", "detect", "elevation_deg"]
