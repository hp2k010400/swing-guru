"""Plane checks from the nearest clear frame (engine profile ``candidate``, fix ``plane_neighbour``).

At Mid-Back and Mid-Down the club moves fast. When the moment frame itself has no usable club
detection (motion blur: nothing found, or only a LOW-confidence line), the club is often detected
cleanly one frame earlier or later. This module lets the plane check be measured on that adjacent
frame, under strict rules, instead of being lost:

* only when the moment frame has **no** usable detection (UNAVAILABLE, LOW or interpolated). A
  moment frame that has a HIGH / MEDIUM reading is never second-guessed here, even if that reading is
  later refused as implausible;
* only the frames **immediately** either side (+-1), never further;
* the neighbour must be a direct detection (never interpolated) at HIGH or MEDIUM (the normal
  reporting levels; nothing is lowered), **and** it needs a witness: the frame one further out must
  also be a direct HIGH / MEDIUM detection whose line agrees within ``WITNESS_DEG``. Two agreeing
  consecutive frames show the club was moving slowly there, so the one-frame substitution error is
  small; where the club moves fast (much of Mid-Down: 8-10 deg per frame) nothing is substituted.
  Hold-out (moments that *were* detected, hidden and recovered from neighbours, 68 swings): Mid-Back
  error median 2 deg, 84 % within its 7 deg noise; Mid-Down median 2 deg, 90th pct 3.2 deg. Without
  the witness Mid-Down's 90th percentile was 18.7 deg, larger than the On-plane band;
* geometric validation: the neighbour's grip end must sit at the golfer's hands (within
  ``GRIP_TORSO`` torso lengths of the wrists), and the shaft must not run through the golfer's head
  (no head point - nose, ears - within ``HEAD_CLEAR_TORSO`` torso lengths of the shaft beyond its
  grip end). A real club at Mid-Back / Mid-Down points away from the head; a line through the head
  is an edge of the cap, hair or shoulder. (Found on validation swing 46: a cap-brim edge at MEDIUM,
  persisting two frames so the witness agreed; it would have read "Too shallow".)
* when both neighbours qualify they must agree within ``AGREE_DEG[phase]`` (about twice that
  moment's measured frame noise), otherwise nothing is recovered (ambiguous);
* the resulting check is capped at **MEDIUM** (estimated), records which frame it was measured on,
  and still passes the verdict layer's plausibility gate. No band or score rule changes: v3 already
  treats MEDIUM with 1.5x the measurement-noise allowance.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from .tracker import LEVEL_HIGH, LEVEL_MEDIUM

WITNESS_DEG = 5.0
GRIP_TORSO = 0.5
HEAD_CLEAR_TORSO = 0.3
AGREE_DEG = {"mid_back": 14.0, "downswing": 8.0}   # ~2x the measured one-frame noise (7.0 / 4.0 deg)


def _line(angle_deg: float) -> float:
    return float(angle_deg) % 180.0


def _diff(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _usable(shaft) -> bool:
    return (shaft is not None and shaft.available and not shaft.interpolated
            and shaft.level in (LEVEL_HIGH, LEVEL_MEDIUM))


def _hands(record) -> np.ndarray | None:
    pixels = getattr(getattr(record, "points", None), "pixels", None) or {}
    wrists = [np.asarray(pixels[n], float) for n in ("left_wrist", "right_wrist") if pixels.get(n) is not None]
    return np.mean(wrists, axis=0) if wrists else None


def _through_head(record, grip: np.ndarray, tip: np.ndarray, torso_px: float) -> bool:
    """True when the shaft (beyond the first 15 % from the grip) passes within HEAD_CLEAR_TORSO of the head."""

    pixels = getattr(getattr(record, "points", None), "pixels", None) or {}
    d = tip - grip
    length = float(np.dot(d, d))
    if length <= 0:
        return False
    for name in ("nose", "left_ear", "right_ear"):
        head = pixels.get(name)
        if head is None:
            continue
        t = float(np.dot(np.asarray(head, float) - grip, d)) / length
        if t < 0.15:
            continue
        closest = grip + min(1.0, t) * d
        if float(np.linalg.norm(np.asarray(head, float) - closest)) < HEAD_CLEAR_TORSO * torso_px:
            return True
    return False


def nearest_clear_frame(track, frame: int, phase: str, records: Sequence[object], torso_px: float,
                        overrides: Mapping[int, object] | None = None) -> tuple[object | None, dict]:
    """``(shaft, diagnostics)``: the adjacent frame's detection to measure on, or ``(None, why)``."""

    centre = (overrides or {}).get(int(frame)) or track.at(int(frame))
    diag: dict = {"moment_frame": int(frame), "moment_level": None if centre is None else centre.level, "candidates": []}
    if _usable(centre):
        diag["result"] = "moment frame usable"
        return None, diag
    by_index = {int(getattr(r, "index")): r for r in records}
    qualified = []
    for d in (-1, 1):
        f = int(frame) + d
        shaft, outer = track.at(f), track.at(f + d)
        row = {"frame": f, "level": None if shaft is None else shaft.level,
               "line_deg": None if shaft is None or shaft.angle_deg is None else round(_line(shaft.angle_deg), 2)}
        if not _usable(shaft):
            row["rejected"] = "not a direct HIGH/MEDIUM detection"
        else:
            line = _line(shaft.angle_deg)
            outer_line = _line(outer.angle_deg) if _usable(outer) else None
            hands = _hands(by_index.get(f))
            grip = None if shaft.grip is None else np.asarray(shaft.grip, float)
            if outer_line is None:
                row["rejected"] = "no witness: the frame further out has no direct HIGH/MEDIUM detection"
            elif _diff(line, outer_line) > WITNESS_DEG:
                row["rejected"] = f"witness disagrees by {_diff(line, outer_line):.0f} deg (club moving fast or a false line)"
            elif hands is None or grip is None or torso_px <= 0 or float(np.linalg.norm(grip - hands)) > GRIP_TORSO * torso_px:
                row["rejected"] = "grip not at the golfer's hands"
            elif shaft.tip is not None and _through_head(by_index.get(f), grip, np.asarray(shaft.tip, float), torso_px):
                row["rejected"] = "line runs through the golfer's head (cap, hair or shoulder edge, not the club)"
            else:
                qualified.append((d, shaft, line))
        diag["candidates"].append(row)
    if not qualified:
        diag["result"] = "no qualifying neighbour"
        return None, diag
    if len(qualified) == 2 and _diff(qualified[0][2], qualified[1][2]) > AGREE_DEG.get(phase, 8.0):
        diag["result"] = "neighbours disagree"
        return None, diag
    chosen = max(qualified, key=lambda q: (q[1].level == LEVEL_HIGH, float(q[1].confidence)))
    diag.update({"result": "recovered", "offset": chosen[0], "frame": int(chosen[1].frame_index)})
    return chosen[1], diag


__all__ = ["AGREE_DEG", "GRIP_TORSO", "HEAD_CLEAR_TORSO", "WITNESS_DEG", "nearest_clear_frame"]
