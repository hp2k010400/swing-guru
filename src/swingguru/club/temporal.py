"""Temporal club recovery through short gaps (backswing blur, club over the forearm).

The classical tracker judges every frame on its own evidence first. Between
Takeaway and the Top two things defeat that on real 30 fps phone video:

* the shaft lies over the trail forearm in a down-the-line picture, so the
  per-frame "runs along a forearm" rule throws the real club away;
* the club moves 30+ degrees per frame, blurs, and loses contrast.

This layer only revisits frames *between two confident detections* (anchors)
and never touches a frame the tracker already reported at MEDIUM or HIGH:

1. each gap frame is detected again with the forearm exemption on (a line
   that continues well past the elbow is not the forearm's edge) - the
   body-segment and whole-clip static-background tests still apply;
2. a candidate that is also present, at the same place, in the anchor frames
   (where the club is known to be elsewhere) is a background line - this is
   the local static test that still works when the camera drifts slowly;
3. a small Viterbi pass over ``anchor -> gap frames -> anchor`` picks the
   candidates that make a physically coherent rotation (same transition model
   as the tracker), with an explicit "no club" option per frame.

Every recovered frame needs its own image evidence (a thin line leaving the
hands in *that* frame). Nothing is interpolated. Recovered frames are marked
``status = "recovered"`` and their confidence is capped below HIGH.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .geometry import allowed_rotation_deg, angle_difference
from .tracker import (
    LEVEL_HIGH,
    LEVEL_MEDIUM,
    LEVEL_UNAVAILABLE,
    STATUS_DETECTED,
    ClassicalClubTracker,
    ClubTrack,
    FrameShaft,
    ShaftCandidate,
    candidate_margin,
    confidence_level,
    frame_confidence,
    hessian_maps,
    ridge_along,
)

STATUS_RECOVERED = "recovered"


@dataclass(frozen=True)
class TemporalConfig:
    max_gap_frames: int = 8
    beyond_elbow_torso: float = 0.3      # a shaft over the forearm continues this far past the elbow
    confidence_cap: float = 0.69         # below HIGH (0.70): recovered frames are never HIGH
    static_offsets_px: tuple[float, ...] = (-3.0, -1.5, 0.0, 1.5, 3.0)
    static_share: float = 0.6            # share of the line still there in an anchor frame
    static_response: float = 0.5         # of the line's own median response
    anchor_separation_deg: float = 20.0  # anchor club far enough away to test the background


def _anchor(frame: FrameShaft) -> bool:
    return frame.status == STATUS_DETECTED and not frame.interpolated and frame.level in (LEVEL_HIGH, LEVEL_MEDIUM)


def _gaps(frames: Sequence[FrameShaft], max_gap: int) -> list[tuple[int, int]]:
    """``(before, after)`` positions of anchors enclosing a short run of non-anchor frames."""

    out = []
    anchors = [i for i, f in enumerate(frames) if _anchor(f)]
    for a, b in zip(anchors, anchors[1:]):
        consecutive = all(frames[k + 1].frame_index == frames[k].frame_index + 1 for k in range(a, b))
        if 1 < b - a <= max_gap + 1 and consecutive:
            out.append((a, b))
    return out


def _line_samples(candidate: ShaftCandidate, step: float = 3.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    direction = np.asarray(candidate.tip, float) - np.asarray(candidate.grip, float)
    length = float(np.linalg.norm(direction))
    unit = direction / max(length, 1e-6)
    radii = np.arange(candidate.visible_start_px, max(candidate.visible_start_px + step, length), step)
    return (candidate.grip[0] + unit[0] * radii, candidate.grip[1] + unit[1] * radii, unit)


def background_share(candidate: ShaftCandidate, own_maps, other_maps, config: TemporalConfig,
                     along_ratio: float) -> float:
    """Share of the candidate's line still present (same place) in another frame."""

    xs, ys, unit = _line_samples(candidate)
    own = ridge_along(own_maps, xs[None, :], ys[None, :], unit, along_ratio=along_ratio)[0]
    level = float(np.median(own)) * config.static_response
    if level <= 0:
        return 0.0
    normal = np.array([-unit[1], unit[0]])
    best = np.zeros_like(own)
    for offset in config.static_offsets_px:
        other = ridge_along(other_maps, (xs + normal[0] * offset)[None, :], (ys + normal[1] * offset)[None, :],
                            unit, along_ratio=along_ratio)[0]
        best = np.maximum(best, other)
    return float(np.mean(best >= level))


def recover_track(
    tracker: ClassicalClubTracker,
    track: ClubTrack,
    frames: Mapping[int, np.ndarray],
    records: Sequence[object],
    *,
    torso_px: float,
    references: Sequence[object] = (),
    config: TemporalConfig | None = None,
) -> tuple[ClubTrack, list[dict]]:
    """A copy of ``track`` with short gaps between anchors re-examined. Returns (track, log)."""

    config = config or TemporalConfig()
    club = tracker.config
    by_index = {int(getattr(r, "index")): r for r in records}
    result = list(track.frames)
    log: list[dict] = []
    maps_cache: dict[int, list] = {}

    def maps_of(index: int):
        if index not in maps_cache:
            import cv2

            image = frames.get(index)
            maps_cache[index] = None if image is None else hessian_maps(
                cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), club.hessian_sigmas)
        return maps_cache[index]

    for a, b in _gaps(track.frames, config.max_gap_frames):
        before, after = track.frames[a], track.frames[b]
        gap = list(range(a + 1, b))
        options: list[list[ShaftCandidate | None]] = []
        hands: list[np.ndarray | None] = []
        entry = {"from": before.frame_index, "to": after.frame_index, "frames": {}}
        for position in gap:
            index = track.frames[position].frame_index
            image, record = frames.get(index), by_index.get(index)
            if image is None or record is None:
                options.append([None])
                hands.append(None)
                entry["frames"][index] = {"reason": "no image or pose"}
                continue
            detection = tracker.detect_frame(image, record, torso_px=torso_px, references=references,
                                             shaft_over_forearm_torso=config.beyond_elbow_torso)
            hands.append(detection.hands.origin)
            kept, dropped = [], []
            own = maps_of(index)
            for candidate in detection.candidates:
                static = []
                for anchor in (before, after):
                    other = maps_of(anchor.frame_index)
                    if other is None or anchor.angle_deg is None:
                        continue
                    if abs(angle_difference(candidate.angle_deg, anchor.angle_deg)) < config.anchor_separation_deg:
                        continue
                    static.append(background_share(candidate, own, other, config, club.along_ratio))
                if len(static) == 2 and min(static) >= config.static_share:
                    dropped.append(round(candidate.angle_deg, 1))
                else:
                    kept.append(candidate)
            options.append(kept + [None])
            entry["frames"][index] = {"candidates": [round(c.angle_deg, 1) for c in kept],
                                      "background": dropped, "reason": detection.reason}
        chosen = _viterbi(before, after, options, hands, [track.frames[p] for p in gap], club, torso_px)
        for k, position in enumerate(gap):
            candidate = chosen[k]
            original = result[position]
            index = original.frame_index
            if candidate is None:
                entry["frames"][index]["chosen"] = None
                continue
            neighbours = [c for c in (chosen[k - 1] if k else None, chosen[k + 1] if k + 1 < len(chosen) else None)
                          if c is not None]
            anchors = ([before] if k == 0 else []) + ([after] if k == len(gap) - 1 else [])
            deltas = [abs(angle_difference(candidate.angle_deg, c.angle_deg)) for c in neighbours] + \
                     [abs(angle_difference(candidate.angle_deg, f.angle_deg)) for f in anchors]
            temporal = float(np.mean([max(0.0, 1.0 - d / 90.0) for d in deltas])) if deltas else None
            alternatives = [o.score for o in options[k] if o is not None and o is not candidate
                            and abs(angle_difference(o.angle_deg, candidate.angle_deg)) > 15.0]
            confidence, parts = frame_confidence(
                support=candidate.support,
                length_torso=(candidate.length_px - candidate.visible_start_px) / max(torso_px, 1e-6),
                margin=candidate_margin(candidate.score, alternatives), temporal=temporal,
                corroborated=candidate.corroborated, score=candidate.score)
            confidence = min(confidence, config.confidence_cap)
            level = confidence_level(confidence, club)
            entry["frames"][index].update({"chosen": round(candidate.angle_deg, 1), "confidence": round(confidence, 3),
                                           "level": level})
            if level == LEVEL_UNAVAILABLE:
                continue
            # Never overwrite a better original frame (a LOW detection with a
            # higher confidence than the recovery stays as it was).
            if original.available and not original.interpolated and original.confidence >= confidence:
                continue
            result[position] = FrameShaft(
                index, original.timestamp_ms, STATUS_RECOVERED, level, confidence,
                angle_deg=candidate.angle_deg, grip=candidate.grip, tip=candidate.tip,
                method=candidate.method + "+temporal", temporal_consistency=temporal,
                reason=f"recovered between frames {before.frame_index} and {after.frame_index}",
                components={**parts, "capped": 1.0})
        log.append(entry)
    return ClubTrack(tuple(result), track.detections, track.tracker + "+temporal-v1", track.config), log


def _viterbi(before: FrameShaft, after: FrameShaft, options, hands, gap_frames, club, torso_px):
    """Best coherent choice per gap frame, pinned to the two anchors."""

    times = [before.timestamp_ms] + [f.timestamp_ms for f in gap_frames] + [after.timestamp_ms]
    pins = [ShaftCandidate(before.grip, before.tip, float(before.angle_deg), 0.0, 1.0, 1.0, 1.0, 1.0)]
    pins_after = ShaftCandidate(after.grip, after.tip, float(after.angle_deg), 0.0, 1.0, 1.0, 1.0, 1.0)
    grid = [pins] + list(options) + [[pins_after]]
    hand_points = [before.grip] + list(hands) + [after.grip]

    def limit(i: int) -> float:
        dt = max(1e-3, (times[i] - times[i - 1]) / 1000.0)
        a, b = hand_points[i - 1], hand_points[i]
        step = None if a is None or b is None else float(np.linalg.norm(np.asarray(b) - np.asarray(a))) / max(torso_px, 1)
        return allowed_rotation_deg(step, base_deg=club.rotation_base_deg_per_s * dt,
                                    per_torso_deg=club.rotation_per_torso_deg,
                                    cap_deg=club.rotation_cap_deg_per_s * dt)

    def emission(c):
        return club.none_cost if c is None else -min(2.0, c.score)

    # Carry the last real candidate through "none" states so the rotation
    # between the frames either side of a skipped frame is still checked.
    states = [[(0.0, None, 0)]]  # per option: (cost, back pointer, last-real (layer, option))
    last_real = [[(0, 0)]]
    for i in range(1, len(grid)):
        row, row_last = [], []
        for oi, option in enumerate(grid[i]):
            best = (math.inf, None)
            best_last = None
            for j, previous in enumerate(grid[i - 1]):
                cost = states[i - 1][j][0]
                if not math.isfinite(cost):
                    continue
                layer, ref = last_real[i - 1][j]
                anchor = grid[layer][ref]
                if option is None:
                    step = club.gap_cost if previous is not None else 0.0
                    candidate_last = (layer, ref)
                else:
                    span = sum(limit(k) for k in range(layer + 1, i + 1))
                    delta = abs(angle_difference(option.angle_deg, anchor.angle_deg))
                    if delta > 2.0 * span:
                        continue
                    step = club.transition_weight * (delta / span) ** 2 + (club.gap_cost if previous is None else 0.0)
                    candidate_last = None
                total = cost + step
                if total < best[0]:
                    best = (total, j)
                    best_last = candidate_last
            if option is not None and math.isfinite(best[0]):
                best_last = (i, oi)
            row.append((best[0] + emission(option) if math.isfinite(best[0]) else math.inf, best[1], 0))
            row_last.append(best_last if best_last is not None else (0, 0))
        states.append(row)
        last_real.append(row_last)
    end = states[-1][0]
    if not math.isfinite(end[0]):
        return [None] * len(options)
    picks = []
    j = end[1]
    for i in range(len(grid) - 2, 0, -1):
        picks.append(grid[i][j])
        j = states[i][j][1]
    picks.reverse()
    return picks


__all__ = ["STATUS_RECOVERED", "TemporalConfig", "background_share", "recover_track"]
