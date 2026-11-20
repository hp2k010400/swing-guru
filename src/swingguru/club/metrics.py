"""Experimental raw shaft measurements per swing moment.

Raw numbers only: the shaft's screen angle at each moment and how much it
turned between moments. No plane, steep/shallow, laid-off or across-the-line
classification - those need validated golf reference rules and a known camera
position.

Angles use the screen convention in :mod:`swingguru.club.geometry`: grip to
clubhead, counter-clockwise from the image +x axis, y up.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from ..phases import PHASE_LABELS, Phase
from .geometry import angle_difference
from .tracker import LEVEL_HIGH, LEVEL_MEDIUM, ClubTrack, FrameShaft

REPORTABLE = (LEVEL_HIGH, LEVEL_MEDIUM)

SHAFT_PHASES: tuple[Phase, ...] = (
    Phase.ADDRESS,
    Phase.TAKEAWAY,
    Phase.MID_BACK,
    Phase.TOP,
    Phase.DOWNSWING,
    Phase.IMPACT,
)

RELATIVE_PAIRS: tuple[tuple[Phase, Phase], ...] = (
    (Phase.ADDRESS, Phase.TAKEAWAY),
    (Phase.ADDRESS, Phase.MID_BACK),
    (Phase.ADDRESS, Phase.TOP),
    (Phase.ADDRESS, Phase.DOWNSWING),
    (Phase.TAKEAWAY, Phase.MID_BACK),
    (Phase.MID_BACK, Phase.TOP),
    (Phase.TOP, Phase.DOWNSWING),
)


def reportable(shaft: FrameShaft | None) -> bool:
    """Only directly detected MEDIUM/HIGH frames produce a number."""

    return bool(
        shaft is not None
        and shaft.available
        and not shaft.interpolated
        and shaft.level in REPORTABLE
    )


def shaft_at_moment(track: ClubTrack, frame_index: int | None) -> dict[str, object]:
    if frame_index is None:
        return {"frame": None, "status": "no moment", "angle_deg": None, "level": None}
    shaft = track.at(int(frame_index))
    if shaft is None:
        return {"frame": frame_index, "status": "frame not analysed", "angle_deg": None, "level": None}
    return {
        "frame": frame_index,
        "status": "reported" if reportable(shaft) else "not reported",
        "angle_deg": round(shaft.angle_deg, 1) if reportable(shaft) else None,
        "level": shaft.level,
        "confidence": round(shaft.confidence, 3),
        "raw_angle_deg": None if shaft.angle_deg is None else round(shaft.angle_deg, 1),
        "reason": shaft.reason,
    }


def unwrap_angles(angles: Sequence[float]) -> list[float]:
    """Continuous angle sequence (no jumps of 360)."""

    if not angles:
        return []
    result = [float(angles[0])]
    for angle in angles[1:]:
        result.append(result[-1] + angle_difference(angle, result[-1]))
    return result


def path_turn_deg(
    track: ClubTrack,
    start_frame: int,
    end_frame: int,
    *,
    max_gap_frames: int = 3,
) -> tuple[float | None, str]:
    """Signed turn of the shaft along the tracked path between two frames.

    Unlike the shortest angular difference this follows the club frame by
    frame, so turns beyond 180 degrees are reported correctly. Both ends must
    be reportable; any frame in between with a shaft (any level) is used to
    follow the rotation, and a gap longer than ``max_gap_frames`` makes the
    turn unknown rather than guessed.
    """

    first = track.at(start_frame)
    last = track.at(end_frame)
    if not reportable(first) or not reportable(last):
        return None, "an end frame has no confident shaft"
    path = [
        shaft
        for shaft in track.frames
        if start_frame <= shaft.frame_index <= end_frame and shaft.available
    ]
    for before, after in zip(path, path[1:]):
        if after.frame_index - before.frame_index > max_gap_frames + 1:
            return None, (
                f"no shaft between frames {before.frame_index} and {after.frame_index}, "
                "so the rotation cannot be followed"
            )
    angles = unwrap_angles([float(shaft.angle_deg) for shaft in path])  # type: ignore[arg-type]
    return round(angles[-1] - angles[0], 1), f"followed through {len(path)} frames"


def club_top_estimate(
    track: ClubTrack,
    *,
    start_frame: int,
    end_frame: int,
    min_turn_deg: float = 2.0,
) -> dict[str, object]:
    """Frame where the shaft's rotation reverses between two moments.

    Uses only directly detected MEDIUM/HIGH frames in ``[start, end]``. The
    backswing turning direction is taken from the first half of the window;
    the top is the last frame before the rotation reverses and stays reversed
    for two consecutive steps. Returns ``frame: None`` with a reason when the
    evidence is too thin.
    """

    usable = [
        shaft
        for shaft in track.frames
        if start_frame <= shaft.frame_index <= end_frame and reportable(shaft)
    ]
    if len(usable) < 5:
        return {"frame": None, "reason": f"only {len(usable)} confident shaft frames between the moments"}
    angles = unwrap_angles([float(shaft.angle_deg) for shaft in usable])  # type: ignore[arg-type]
    steps = np.diff(angles)
    frames = [shaft.frame_index for shaft in usable]
    half = max(1, len(steps) // 2)
    moving = [step for step in steps[:half] if abs(step) >= min_turn_deg]
    if not moving:
        return {"frame": None, "reason": "the shaft barely turned before the top"}
    direction = float(np.sign(np.median(moving)))
    for index in range(len(steps) - 1):
        if (
            np.sign(steps[index]) == -direction
            and abs(steps[index]) >= min_turn_deg
            and np.sign(steps[index + 1]) == -direction
        ):
            return {
                "frame": frames[index],
                "reason": "last frame before the shaft's rotation reversed for two steps",
                "direction": "counter-clockwise" if direction > 0 else "clockwise",
                "frames_used": len(usable),
            }
    return {"frame": None, "reason": "no sustained reversal of the shaft's rotation was found"}


def phase_shaft_metrics(track: ClubTrack, moments: object, *, fps: float) -> dict[str, object]:
    """Raw shaft angles per moment, relative turns, and the Top comparison."""

    frames: Mapping[Phase, int | None] = moments.frames()  # type: ignore[attr-defined]
    per_phase = {
        PHASE_LABELS[phase]: shaft_at_moment(track, frames.get(phase)) for phase in SHAFT_PHASES
    }
    relative = {}
    for first, second in RELATIVE_PAIRS:
        a = per_phase[PHASE_LABELS[first]]
        b = per_phase[PHASE_LABELS[second]]
        key = f"{PHASE_LABELS[first]} -> {PHASE_LABELS[second]}"
        if a["frame"] is None or b["frame"] is None:
            relative[key] = {"turn_deg": None, "note": "a moment is missing"}
            continue
        turn, note = path_turn_deg(track, int(a["frame"]), int(b["frame"]))
        relative[key] = {"turn_deg": turn, "note": note}

    hand_top = frames.get(Phase.TOP)
    start = frames.get(Phase.MID_BACK) or frames.get(Phase.TAKEAWAY)
    end = frames.get(Phase.DOWNSWING) or frames.get(Phase.IMPACT)
    if start is None or end is None:
        club_top: dict[str, object] = {"frame": None, "reason": "needs Mid-Back and Mid-Down moments"}
    else:
        club_top = club_top_estimate(track, start_frame=int(start), end_frame=int(end))
    difference = None
    if hand_top is not None and club_top.get("frame") is not None:
        difference = int(club_top["frame"]) - int(hand_top)  # type: ignore[arg-type]

    return {
        "angle_convention": "grip to clubhead, degrees counter-clockwise from screen +x, y up",
        "per_phase": per_phase,
        "relative_turn_deg": relative,
        "top_comparison": {
            "hand_based_frame": hand_top,
            "club_based": club_top,
            "difference_frames": difference,
            "difference_ms": None if difference is None else round(difference * 1000.0 / fps, 1),
        },
        "coverage": {
            "medium_or_better": round(track.coverage(LEVEL_MEDIUM), 3),
            "high": round(track.coverage(LEVEL_HIGH), 3),
        },
    }


__all__ = [
    "RELATIVE_PAIRS",
    "SHAFT_PHASES",
    "club_top_estimate",
    "path_turn_deg",
    "phase_shaft_metrics",
    "reportable",
    "shaft_at_moment",
    "unwrap_angles",
]
