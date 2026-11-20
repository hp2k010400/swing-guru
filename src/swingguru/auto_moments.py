"""Experimental automatic swing-moment estimates from measured body motion.

This module estimates when each checkpoint of a swing probably happened, using
only the landmark points the engine already validated. It is deliberately
conservative: it fails closed and returns ``unavailable`` (with a reason)
instead of inventing a frame whenever the evidence is weak, the tracking is
patchy, the clip starts or ends mid-swing, or the sequence looks like more than
one swing.

What it is:

* a deterministic, sequence-based heuristic over a *single* clip's measured
  wrists and hip/shoulder centres, normalised by the golfer's torso length and
  measured in real seconds;
* an *estimate* for every moment it does return - always labelled
  ``Estimated moment`` in the interface and always open to manual correction.

What it is not:

* a trained model, a club or ball tracker, a plane-of-swing analysis or a
  score;
* evidence about a real golfer's swing. High landmark confidence says the body
  was tracked, not that a phase boundary is correct.

The thresholds below are engineering thresholds in body-lengths and seconds.
They are not golf ideals, coaching ranges or "correct" positions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

import numpy as np

from .landmarks import lead_side, normalize_handedness, trail_side
from .phases import PHASE_LABELS, PHASE_ORDER, Phase, coerce_phase

SOURCE_ESTIMATED = "estimated"
SOURCE_MANUAL = "manual"
SOURCE_UNAVAILABLE = "unavailable"

@dataclass(frozen=True)
class MomentConfig:
    """Engineering thresholds for the moment estimate.

    Distances are in torso lengths (hip midpoint to shoulder midpoint) and
    times are in seconds, so the same numbers apply to a phone clip and a
    broadcast-resolution clip, to portrait and landscape, and to any frame
    rate. None of these values is a golf ideal.
    """

    min_frames: int = 8
    min_duration_s: float = 0.5
    min_point_coverage: float = 0.6
    max_gap_s: float = 0.5
    smooth_window_s: float = 0.10
    stability_window_s: float = 0.30
    stability_speed: float = 0.35
    departure_threshold: float = 0.30
    departure_sustain_s: float = 0.10
    top_rise_threshold: float = 0.50
    top_fall_threshold: float = 0.15
    backswing_fraction: float = 0.5
    return_fraction: float = 0.5
    impact_max_distance: float = 0.45
    impact_exit_distance: float = 0.30
    finish_stability_s: float = 0.30
    refine_window_s: float = 0.07
    top_plateau: float = 0.05
    # Body-wide set-up stillness, only in the fallback when the hand-speed test
    # finds no swing at all. The hand-speed test allows ``stability_speed`` over
    # ``stability_window_s`` (0.35 x 0.3 s ~= 0.10 torso lengths of motion) but
    # applies it to the worst single frame, so one jittering wrist breaks a
    # visibly still set-up (robustness batch swings 13, 14). The body test spends
    # the same ~0.10 budget on net position drift of the hands *and* of the
    # pelvis, shoulders and ankles, so jitter averages out while any real
    # movement (walking, bending, a waggle) still exceeds it.
    still_radius: float = 0.10
    # The body test also needs a set-up posture: the hip-to-shoulder line at
    # least this far from vertical. Standing upright at the bag, walking or
    # pulling a club out passed the body test in the 69 s robustness clip and
    # produced false episodes; every real set-up in the batch leans far more
    # (address torso angles in the 40s). A detection gate, not a golf ideal.
    # The hand-speed test is unchanged and has no posture condition.
    setup_min_lean_deg: float = 15.0
    # Only when no swing is found otherwise: hand gaps up to this long (both wrists under
    # the measurement confidence gate for 1-3 frames, seen at takeaway and
    # impact on swings 13 and 14) are bridged by linear interpolation, so a
    # blink in wrist tracking does not split one swing into pieces. Far below
    # ``max_gap_s``, the gap the clip as a whole already tolerates.
    bridge_gap_s: float = 0.15
    handedness: str = "right"
    # Engine profile ``candidate`` fix ``address_recheck`` (see _recheck_address). Off by default, so
    # every other profile and caller estimates exactly as before.
    address_recheck: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "handedness", normalize_handedness(self.handedness))
        if self.min_frames < 3:
            raise ValueError("min_frames must be at least 3")
        if self.stability_window_s <= 0 or self.finish_stability_s <= 0:
            raise ValueError("stability windows must be positive")
        if not 0.0 < self.min_point_coverage <= 1.0:
            raise ValueError("min_point_coverage must be in (0, 1]")
        if not 0.0 < self.return_fraction < 1.0:
            raise ValueError("return_fraction must be in (0, 1)")
        if not 0.0 < self.backswing_fraction < 1.0:
            raise ValueError("backswing_fraction must be in (0, 1)")


@dataclass(frozen=True)
class MomentEstimate:
    """One checkpoint slot: an estimate, a manual choice, or unavailable."""

    phase: Phase
    frame_index: int | None
    timestamp_ms: float | None
    source: str = SOURCE_UNAVAILABLE
    review_required: bool = True
    confidence: float | None = None
    evidence: tuple[str, ...] = ()
    manual_override: bool = False

    @property
    def available(self) -> bool:
        return self.frame_index is not None

    @property
    def label(self) -> str:
        return PHASE_LABELS[self.phase]

    @property
    def provenance_label(self) -> str:
        if self.source == SOURCE_MANUAL:
            return "Selected manually"
        if self.source == SOURCE_ESTIMATED:
            return "Estimated moment"
        return "Not available"

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "label": self.label,
            "frame_index": self.frame_index,
            "timestamp_ms": self.timestamp_ms,
            "source": self.source,
            "provenance": self.provenance_label,
            "review_required": self.review_required,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "manual_override": self.manual_override,
        }


@dataclass(frozen=True)
class MomentSet:
    """All seven checkpoint slots plus the honesty of the sequence behind them."""

    estimates: tuple[MomentEstimate, ...]
    config: MomentConfig
    sequence: Mapping[str, object] = field(default_factory=dict)

    def get(self, phase: Phase | str) -> MomentEstimate:
        target = coerce_phase(phase)
        for estimate in self.estimates:
            if estimate.phase == target:
                return estimate
        raise KeyError(f"no moment slot for {target!r}")

    def frames(self) -> dict[Phase, int | None]:
        return {estimate.phase: estimate.frame_index for estimate in self.estimates}

    def available_frames(self) -> dict[Phase, int]:
        return {
            estimate.phase: int(estimate.frame_index)
            for estimate in self.estimates
            if estimate.frame_index is not None
        }

    @property
    def has_estimates(self) -> bool:
        return any(
            estimate.source == SOURCE_ESTIMATED and estimate.available
            for estimate in self.estimates
        )

    @property
    def ambiguous(self) -> bool:
        return bool(self.sequence.get("ambiguous"))

    def to_dict(self) -> dict[str, object]:
        return {
            "moments": [estimate.to_dict() for estimate in self.estimates],
            "sequence": dict(self.sequence),
            "config": {
                "min_point_coverage": self.config.min_point_coverage,
                "stability_window_s": self.config.stability_window_s,
                "departure_threshold": self.config.departure_threshold,
                "top_rise_threshold": self.config.top_rise_threshold,
                "top_fall_threshold": self.config.top_fall_threshold,
                "backswing_fraction": self.config.backswing_fraction,
                "return_fraction": self.config.return_fraction,
                "impact_max_distance": self.config.impact_max_distance,
                "impact_exit_distance": self.config.impact_exit_distance,
                "handedness": self.config.handedness,
            },
        }


@dataclass(frozen=True)
class MotionSeries:
    """Body-normalised motion features for one clip."""

    frames: tuple[int, ...]
    times_s: tuple[float, ...]
    hands: tuple[np.ndarray | None, ...]
    raw_hands: tuple[np.ndarray | None, ...]
    torso_scale_px: float
    coverage: float
    usable: tuple[bool, ...]
    reasons: tuple[str, ...]
    # Pelvis, shoulder centre and ankle centre per frame in torso lengths
    # (image position / torso scale); ``None`` when any is missing.
    body: tuple[np.ndarray | None, ...] = ()

    @property
    def duration_s(self) -> float:
        return 0.0 if not self.times_s else self.times_s[-1] - self.times_s[0]


def _point(record: object, name: str) -> np.ndarray | None:
    points = getattr(record, "points", None)
    if points is None:
        return None
    return points.pixels.get(name)


def _hand_point(record: object, config: MomentConfig) -> np.ndarray | None:
    """Mean of the usable wrists for the selected playing hand."""

    lead = lead_side(config.handedness)
    trail = trail_side(config.handedness)
    available = []
    for side in (lead, trail):
        point = _point(record, f"{side}_wrist")
        if point is not None:
            available.append(np.asarray(point, dtype=float))
    if not available:
        return None
    return np.mean(np.stack(available, axis=0), axis=0)


def build_motion_series(
    records: Sequence[object], *, config: MomentConfig | None = None
) -> MotionSeries:
    """Body-normalised hand path plus the coverage the estimate depends on."""

    settings = config or MomentConfig()
    frames: list[int] = []
    times: list[float] = []
    raw_hands: list[np.ndarray | None] = []
    raw_pelvis: list[np.ndarray | None] = []
    scales: list[float] = []

    for record in records:
        frame_index = int(getattr(record, "index"))
        timestamp_ms = float(getattr(record, "timestamp_ms"))
        frames.append(frame_index)
        times.append(timestamp_ms / 1000.0)
        hands = _hand_point(record, settings)
        pelvis = _point(record, "hip_mid")
        shoulder = _point(record, "shoulder_mid")
        finite = (
            hands is not None
            and pelvis is not None
            and shoulder is not None
            and np.all(np.isfinite(hands))
            and np.all(np.isfinite(pelvis))
            and np.all(np.isfinite(shoulder))
        )
        if finite:
            raw_hands.append(hands)
            raw_pelvis.append(np.asarray(pelvis, dtype=float))
            scales.append(float(np.linalg.norm(np.asarray(shoulder, float) - np.asarray(pelvis, float))))
        else:
            raw_hands.append(None)
            raw_pelvis.append(None)

    if not scales:
        return MotionSeries(
            frames=tuple(frames),
            times_s=tuple(times),
            hands=tuple([None] * len(frames)),
            raw_hands=tuple(raw_hands),
            torso_scale_px=0.0,
            coverage=0.0,
            usable=tuple([False] * len(frames)),
            reasons=("no frame had usable wrists, hips and shoulders together",),
        )

    scale = float(np.median(np.asarray(scales, dtype=float)))
    reasons: list[str] = []
    if not math.isfinite(scale) or scale <= 1.0:
        scale = 0.0
        reasons.append("torso length was too small to normalise body motion")

    hands_norm: list[np.ndarray | None] = []
    usable: list[bool] = []
    for hands, pelvis in zip(raw_hands, raw_pelvis):
        if hands is None or pelvis is None or scale <= 0.0:
            hands_norm.append(None)
            usable.append(False)
            continue
        hands_norm.append((hands - pelvis) / scale)
        usable.append(True)

    body: list[np.ndarray | None] = []
    for record in records:
        anchors = [_point(record, "hip_mid"), _point(record, "shoulder_mid")]
        ankles = [_point(record, "left_ankle"), _point(record, "right_ankle")]
        if scale <= 0.0 or any(p is None for p in anchors + ankles):
            body.append(None)
            continue
        stacked = np.stack([np.asarray(anchors[0], float), np.asarray(anchors[1], float),
                            np.mean(np.stack([np.asarray(a, float) for a in ankles]), axis=0)])
        body.append(stacked / scale if np.all(np.isfinite(stacked)) else None)

    coverage = sum(1 for flag in usable if flag) / len(frames) if frames else 0.0
    return MotionSeries(
        frames=tuple(frames),
        times_s=tuple(times),
        hands=tuple(hands_norm),
        raw_hands=tuple(raw_hands),
        torso_scale_px=scale,
        coverage=coverage,
        usable=tuple(usable),
        reasons=tuple(reasons),
        body=tuple(body),
    )


def _bridge_hand_gaps(series: MotionSeries, max_gap_s: float) -> tuple[MotionSeries, int]:
    """Series with short internal hand gaps linearly interpolated.

    Only gaps whose surrounding usable frames are at most ``max_gap_s`` apart
    and whose frame indices are contiguous are filled. Returns the new series
    and the number of frames filled.
    """

    hands = list(series.hands)
    usable = list(series.usable)
    filled = 0
    runs = _runs(series.usable)
    for (_, end), (next_start, _) in zip(runs, runs[1:]):
        before, after = end - 1, next_start
        if series.times_s[after] - series.times_s[before] > max_gap_s:
            continue
        if series.frames[after] - series.frames[before] != after - before:
            continue  # frames missing from the record list itself
        a, b = series.hands[before], series.hands[after]
        if a is None or b is None:
            continue
        span = series.times_s[after] - series.times_s[before]
        for index in range(before + 1, after):
            weight = (series.times_s[index] - series.times_s[before]) / span
            hands[index] = a + (b - a) * weight
            usable[index] = True
            filled += 1
    if not filled:
        return series, 0
    coverage = sum(1 for flag in usable if flag) / len(usable) if usable else 0.0
    return replace(series, hands=tuple(hands), usable=tuple(usable), coverage=coverage), filled


def _runs(usable: Sequence[bool]) -> list[tuple[int, int]]:
    """Contiguous usable index ranges (inclusive start, exclusive end)."""

    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(usable):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(usable)))
    return runs


def _smoothed(series: MotionSeries, config: MomentConfig) -> list[np.ndarray | None]:
    """Moving average inside contiguous usable runs; gaps stay gaps."""

    smoothed: list[np.ndarray | None] = [None] * len(series.frames)
    half = max(config.smooth_window_s / 2.0, 1e-6)
    for start, end in _runs(series.usable):
        for index in range(start, end):
            moment = series.times_s[index]
            window = [
                other
                for other in range(start, end)
                if abs(series.times_s[other] - moment) <= half
            ]
            stacked = [
                series.hands[other]
                for other in window
                if series.hands[other] is not None
            ]
            if not stacked:
                continue
            smoothed[index] = np.mean(np.stack(stacked, axis=0), axis=0)
    return smoothed


def _speeds(
    series: MotionSeries, smoothed: Sequence[np.ndarray | None]
) -> list[float | None]:
    speeds: list[float | None] = [None] * len(series.frames)
    for index in range(1, len(series.frames)):
        previous = smoothed[index - 1]
        current = smoothed[index]
        dt = series.times_s[index] - series.times_s[index - 1]
        if previous is None or current is None or dt <= 0:
            continue
        consecutive = series.frames[index] - series.frames[index - 1] == 1
        if not consecutive:
            continue
        speeds[index] = float(np.linalg.norm(current - previous) / dt)
    return speeds


def _window_end(
    series: MotionSeries, start: int, window_s: float, limit: int
) -> int:
    end = start
    while end + 1 < limit and series.times_s[end + 1] - series.times_s[start] <= window_s:
        end += 1
    return end


def _sustained(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    baseline: np.ndarray,
    *,
    start: int,
    limit: int,
    threshold: float,
    sustain_s: float,
) -> int | None:
    """First index whose displacement from the baseline is sustained."""

    run_start: int | None = None
    for index in range(start, limit):
        current = smoothed[index]
        if current is None:
            run_start = None
            continue
        distance = float(np.linalg.norm(current - baseline))
        if distance >= threshold:
            if run_start is None:
                run_start = index
            if series.times_s[index] - series.times_s[run_start] >= sustain_s:
                return run_start
        else:
            run_start = None
    return None


def _stable_window_end(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    speeds: Sequence[float | None],
    index: int,
    end: int,
    window_s: float,
    speed_limit: float,
) -> int | None:
    """End of a still window starting at ``index``, or ``None`` if it moves."""

    window_end = _window_end(series, index, window_s, end)
    window_speeds = [
        speed for speed in speeds[index + 1 : window_end + 1] if speed is not None
    ]
    stable = (
        smoothed[index] is not None
        and window_end > index
        and len(window_speeds) == window_end - index
        and max(window_speeds, default=0.0) <= speed_limit
    )
    return window_end if stable else None


def _drifts(hands: Sequence[np.ndarray], radius: float) -> bool:
    """Net hand drift across a window: last third vs first third over half the budget.

    Jitter is zero-mean; the slow start of a takeaway is a steady drift, which
    both stillness tests otherwise accept (under 0.35 torso lengths/s per
    frame, or within the radius), so a window could end a few frames into the
    takeaway with the club visibly moving (robustness batch 01, 02, 07, 09, 12).
    """

    stacked = np.stack(hands)
    third = max(1, len(hands) // 3)
    return float(np.linalg.norm(stacked[-third:].mean(axis=0) - stacked[:third].mean(axis=0))) > 0.5 * radius


def _body_still_window_end(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    speeds: Sequence[float | None],
    index: int,
    end: int,
    window_s: float,
    radius: float,
    min_lean_deg: float = 0.0,
) -> int | None:
    """End of a body-wide still window starting at ``index``, or ``None``.

    Every frame in the window must have smoothed hands and all body anchors,
    frames must be consecutive (as for the hand-speed test), the hands, the
    pelvis, the shoulder centre and the ankle centre must each stay within
    ``radius`` torso lengths of their own median position in the window, and
    the median hip-to-shoulder line must lean at least ``min_lean_deg`` from
    vertical (a set-up posture, not standing upright).
    """

    window_end = _window_end(series, index, window_s, end)
    if window_end <= index or not series.body:
        return None
    if any(speeds[i] is None for i in range(index + 1, window_end + 1)):
        return None
    hands = [smoothed[i] for i in range(index, window_end + 1)]
    body = [series.body[i] for i in range(index, window_end + 1)]
    if any(h is None for h in hands) or any(b is None for b in body):
        return None
    hands_arr = np.stack(hands)
    if float(np.max(np.linalg.norm(hands_arr - np.median(hands_arr, axis=0), axis=1))) > radius:
        return None
    if _drifts(hands, radius):
        return None
    body_arr = np.stack(body)  # (frames, 3 anchors, 2)
    median = np.median(body_arr, axis=0)
    if float(np.max(np.linalg.norm(body_arr - median, axis=2))) > radius:
        return None
    torso = median[1] - median[0]  # hip centre -> shoulder centre, image y down
    lean = math.degrees(math.atan2(abs(float(torso[0])), max(-float(torso[1]), 1e-9)))
    if lean < min_lean_deg:
        return None
    return window_end


def _elevation(point: np.ndarray | None) -> float | None:
    """Hand height above the pelvis in torso lengths (image y points down)."""

    return None if point is None else float(-point[1])


def _first_reversal(
    smoothed: Sequence[np.ndarray | None],
    config: MomentConfig,
    *,
    start: int,
    limit: int,
    base_elevation: float | None = None,
) -> tuple[int, float, float] | None:
    """First prominent hand-height peak after ``start``.

    A peak counts once the hands have risen at least ``top_rise_threshold``
    above the base and then dropped at least ``top_fall_threshold`` below the
    highest point so far. The *first* such reversal is the top of the swing;
    the finish is often higher than the top, so the highest point in the clip
    is not used. Without a base (no set-up found) the lowest point seen before
    the peak is used instead.
    """

    peak_index: int | None = None
    peak: float | None = None
    running_low: float | None = None
    low_before_peak: float | None = None
    for index in range(start, limit):
        value = _elevation(smoothed[index])
        if value is None:
            continue
        running_low = value if running_low is None else min(running_low, value)
        if peak is None or value > peak:
            peak_index, peak = index, value
            low_before_peak = running_low
            continue
        base = base_elevation if base_elevation is not None else low_before_peak
        if base is None or peak_index is None:
            continue
        rise = peak - base
        fall = peak - value
        if rise >= config.top_rise_threshold and fall >= config.top_fall_threshold:
            return peak_index, rise, fall
    return None


def _distance_to(
    smoothed: Sequence[np.ndarray | None], index: int, target: np.ndarray
) -> float | None:
    current = smoothed[index]
    if current is None:
        return None
    return float(np.linalg.norm(current - target))


def _return_index(
    smoothed: Sequence[np.ndarray | None],
    baseline: np.ndarray,
    *,
    start: int,
    limit: int,
    max_distance: float,
) -> int | None:
    """First index after ``start`` whose hands are back near the set-up."""

    for index in range(start, limit):
        distance = _distance_to(smoothed, index, baseline)
        if distance is not None and distance <= max_distance:
            return index
    return None


@dataclass(frozen=True)
class SwingEpisode:
    """One complete swing-like movement: set-up, departure, top, return."""

    address: int
    takeaway: int
    top: int
    returned: int
    rise: float
    fall: float
    setup_start: int | None = None  # first frame of the still window ending at ``address``


def _find_episodes(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    speeds: Sequence[float | None],
    config: MomentConfig,
    *,
    body_wide: bool = False,
) -> list[SwingEpisode]:
    """Every still set-up that leads to a departure, a top and a return.

    ``body_wide`` also accepts a window that passes the body-wide position
    test (``_body_still_window_end``) when the hand-speed test fails, so the
    latest still window before the departure is still the set-up; everything
    after the set-up (departure, top, return) is unchanged.

    Holding a finish and then lowering the club is *not* an episode, because
    the hands fall rather than rise after leaving that still window. A
    practice swing followed by a real one gives two episodes, which the caller
    treats as ambiguous.
    """

    by_top: dict[int, SwingEpisode] = {}
    departure_cache: dict[int, tuple[int, float, float, int] | None] = {}
    for start, end in _runs(series.usable):
        for index in range(start, end):
            window_end = None
            if body_wide:
                window_end = _body_still_window_end(
                    series, smoothed, speeds, index, end, config.stability_window_s, config.still_radius,
                    config.setup_min_lean_deg,
                )
            if window_end is None:
                window_end = _stable_window_end(
                    series,
                    smoothed,
                    speeds,
                    index,
                    end,
                    config.stability_window_s,
                    config.stability_speed,
                )
                if (window_end is not None and body_wide
                        and _drifts([smoothed[i] for i in range(index, window_end + 1)], config.still_radius)):
                    window_end = None
            if window_end is None:
                continue
            baseline = smoothed[window_end]
            assert baseline is not None
            departure = _sustained(
                series,
                smoothed,
                baseline,
                start=window_end + 1,
                limit=end,
                threshold=config.departure_threshold,
                sustain_s=config.departure_sustain_s,
            )
            if departure is None:
                continue
            key = departure
            if key not in departure_cache:
                reversal = _first_reversal(
                    smoothed,
                    config,
                    start=departure,
                    limit=end,
                    base_elevation=_elevation(baseline),
                )
                if reversal is None:
                    departure_cache[key] = None
                else:
                    top, rise, fall = reversal
                    returned = _return_index(
                        smoothed,
                        baseline,
                        start=top + 1,
                        limit=end,
                        max_distance=config.impact_max_distance,
                    )
                    departure_cache[key] = (
                        None if returned is None else (top, rise, fall, returned)
                    )
            found = departure_cache[key]
            if found is None:
                continue
            top, rise, fall, returned = found
            existing = by_top.get(top)
            # The latest still window before the departure is the set-up.
            if existing is None or window_end > existing.address:
                by_top[top] = SwingEpisode(
                    address=window_end,
                    takeaway=departure,
                    top=top,
                    returned=returned,
                    rise=rise,
                    fall=fall,
                    setup_start=index,
                )
    return sorted(by_top.values(), key=lambda episode: episode.top)


def _refine(
    series: MotionSeries,
    index: int,
    score,
    *,
    window_s: float,
    lower: int,
    upper: int,
) -> int:
    """Best raw (unsmoothed) frame near a smoothed estimate.

    Smoothing flattens a fast peak, so the smoothed extremum can sit a frame
    early or late. Only frames within ``window_s`` of the estimate, inside
    ``[lower, upper)`` and consecutive with it are considered; the estimate is
    kept when no raw frame is usable.
    """

    best, best_score = index, None
    centre_time = series.times_s[index]
    for candidate in range(max(lower, 0), min(upper, len(series.frames))):
        if abs(series.times_s[candidate] - centre_time) > window_s + 1e-9:
            continue
        if abs(series.frames[candidate] - series.frames[index]) != abs(candidate - index):
            continue  # a dropped frame sits between them
        point = series.hands[candidate]
        if point is None:
            continue
        value = score(point)
        if best_score is None or value > best_score:
            best, best_score = candidate, value
    return best


def _plateau_centre(
    series: MotionSeries,
    index: int,
    *,
    tolerance: float,
    window_s: float,
    lower: int,
    upper: int,
) -> int:
    """Centre of the raw hand-height plateau around a smoothed top.

    At the top the hands hover for a few frames while the club finishes
    setting, so the single highest raw frame tends to be early. The plateau is
    the contiguous run of consecutive raw frames within ``tolerance`` torso
    lengths of the highest one near the estimate; its centre is a steadier
    estimate than either end. Without club detection this is still a
    hand-based estimate.
    """

    centre_time = series.times_s[index]
    candidates = [
        i
        for i in range(max(lower, 0), min(upper, len(series.frames)))
        if abs(series.times_s[i] - centre_time) <= window_s + 1e-9
        and series.hands[i] is not None
    ]
    if not candidates:
        return index
    peak = max(candidates, key=lambda i: float(-series.hands[i][1]))  # type: ignore[index]
    peak_height = float(-series.hands[peak][1])  # type: ignore[index]

    def on_plateau(i: int) -> bool:
        return (
            i in candidates
            and float(-series.hands[i][1]) >= peak_height - tolerance  # type: ignore[index]
        )

    start = peak
    while on_plateau(start - 1) and series.frames[start] - series.frames[start - 1] == 1:
        start -= 1
    end = peak
    while on_plateau(end + 1) and series.frames[end + 1] - series.frames[end] == 1:
        end += 1
    return (start + end + 1) // 2


def _partial_setup(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    speeds: Sequence[float | None],
    config: MomentConfig,
) -> tuple[int, int] | None:
    """Set-up and takeaway for a clip that stops before the swing completes.

    The departure must be followed by the hands rising at least
    ``top_rise_threshold`` above the set-up, so a held finish followed by the
    club being lowered is never mistaken for a set-up.
    """

    for start, end in _runs(series.usable):
        best: tuple[int, int] | None = None
        for index in range(start, end):
            window_end = _stable_window_end(
                series, smoothed, speeds, index, end,
                config.stability_window_s, config.stability_speed,
            )
            if window_end is None:
                continue
            baseline = smoothed[window_end]
            assert baseline is not None
            departure = _sustained(
                series, smoothed, baseline,
                start=window_end + 1, limit=end,
                threshold=config.departure_threshold,
                sustain_s=config.departure_sustain_s,
            )
            if departure is None:
                continue
            base = _elevation(baseline)
            highest = max(
                (
                    value
                    for value in (_elevation(smoothed[i]) for i in range(departure, end))
                    if value is not None
                ),
                default=None,
            )
            if base is None or highest is None or highest - base < config.top_rise_threshold:
                continue
            if best is None or departure == best[1]:
                best = (window_end, departure)
            else:
                break
        if best is not None:
            return best
    return None


def _estimate_mid_back(
    smoothed: Sequence[np.ndarray | None],
    config: MomentConfig,
    *,
    baseline: np.ndarray | None,
    start: int,
    top_index: int,
) -> tuple[int | None, tuple[str, ...]]:
    if baseline is None:
        return None, ("no set-up hand position to measure the backswing from",)
    distance_at_top = _distance_to(smoothed, top_index, baseline)
    if distance_at_top is None:
        return None, ("the top frame has no usable hand position",)
    target = distance_at_top * config.backswing_fraction
    for index in range(start, top_index):
        distance = _distance_to(smoothed, index, baseline)
        if distance is not None and distance >= target:
            return index, (
                f"hands travelled {config.backswing_fraction:.0%} of the way from "
                "the set-up to the top",
            )
    return None, ("no usable hand positions between takeaway and the top",)


def _estimate_downswing(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    config: MomentConfig,
    *,
    baseline: np.ndarray | None,
    top_index: int,
    limit: int,
) -> tuple[int | None, tuple[str, ...]]:
    if baseline is None:
        return None, ("no set-up hand position to measure a return against",)
    distance_at_top = _distance_to(smoothed, top_index, baseline)
    if distance_at_top is None:
        return None, ("the top frame has no usable hand position",)
    target = distance_at_top * config.return_fraction
    for index in range(top_index + 1, limit):
        distance = _distance_to(smoothed, index, baseline)
        if distance is None:
            continue
        if distance <= target and distance < distance_at_top:
            return index, (
                f"hands came {1 - config.return_fraction:.0%} of the way back "
                "toward the set-up hand region after the top",
            )
    return None, ("hands never returned toward the set-up region after the top",)


def _estimate_impact(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    config: MomentConfig,
    *,
    baseline: np.ndarray | None,
    search_start: int,
    limit: int,
) -> tuple[int | None, tuple[str, ...]]:
    """Closest approach to the set-up hands in the first return after the top.

    The search stops once the hands have clearly left the set-up region again
    (the follow-through), so lowering the club later in the clip cannot pull
    the estimate late.
    """

    if baseline is None:
        return None, ("no set-up hand position to compare against",)
    best_index: int | None = None
    best_distance: float | None = None
    for index in range(search_start, limit):
        distance = _distance_to(smoothed, index, baseline)
        if distance is None:
            continue
        if best_distance is None or distance < best_distance:
            best_index, best_distance = index, distance
        elif (
            best_distance <= config.impact_max_distance
            and distance - best_distance >= config.impact_exit_distance
        ):
            break
    if best_index is None or best_distance is None:
        return None, ("no usable hands after the top",)
    if best_distance > config.impact_max_distance:
        return None, (
            f"hands never came back near the set-up region "
            f"(closest {best_distance:.2f} body-lengths)",
        )
    return best_index, (
        "hands nearest the set-up hand region on the way through; "
        "this is not a confirmed club-ball contact",
    )


def _estimate_finish(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    speeds: Sequence[float | None],
    config: MomentConfig,
    *,
    search_start: int,
    limit: int,
) -> tuple[int | None, tuple[str, ...], bool]:
    index = max(search_start, 0)
    while index < limit:
        window_end = _window_end(series, index, config.finish_stability_s, limit)
        window_speeds = [
            speed for speed in speeds[index + 1 : window_end + 1] if speed is not None
        ]
        settled = (
            smoothed[index] is not None
            and window_end > index
            and len(window_speeds) == window_end - index
            and max(window_speeds, default=0.0) <= config.stability_speed
        )
        if settled:
            middle = (index + window_end) // 2
            reaches_end = window_end >= limit - 1
            evidence = (
                "hand movement settled after the swing",
                "the settled window reaches the end of the clip"
                if reaches_end
                else "the settled window sits inside the clip",
            )
            return middle, evidence, reaches_end
        index += 1
    return None, ("hand movement never settled into a finish window",), False


def _count_reversals(
    series: MotionSeries,
    smoothed: Sequence[np.ndarray | None],
    config: MomentConfig,
    *,
    start: int,
    limit: int,
) -> int:
    """Count prominent hand-elevation reversals across a range.

    A "reversal" is a local elevation maximum with a real rise before it and a
    real fall after it, so small tracking jitter does not count. Two of them
    mean the clip contains more than one swing-like episode.
    """

    elevations = [
        (index, float(-smoothed[index][1]))
        for index in range(start, limit)
        if smoothed[index] is not None
    ]
    if len(elevations) < 3:
        return 0
    values = [item[1] for item in elevations]

    peaks: list[int] = []
    position = 1
    while position < len(values) - 1:
        plateau_end = position
        while plateau_end + 1 < len(values) and values[plateau_end + 1] == values[position]:
            plateau_end += 1
        left = values[position - 1]
        right = values[plateau_end + 1] if plateau_end + 1 < len(values) else None
        if right is not None and values[position] > left and values[position] > right:
            peaks.append(position)
        position = plateau_end + 1

    count = 0
    for order, peak in enumerate(peaks):
        previous_peak = peaks[order - 1] if order > 0 else 0
        next_peak = peaks[order + 1] if order + 1 < len(peaks) else len(values) - 1
        left_min = min(values[previous_peak:peak])
        right_min = min(values[peak : next_peak + 1])
        rise = values[peak] - left_min
        fall = values[peak] - right_min
        if rise >= config.top_rise_threshold and fall >= config.top_fall_threshold:
            count += 1
    return count


# Address re-check (engine profile ``candidate``, fix ``address_recheck``). The hand-speed test judges
# set-up stillness by its single worst frame, so one jittery wrist frame can disqualify the real final
# set-up and leave an earlier pause as the Address (validation prod/02: 2.2 s before the takeaway;
# median on 67 swings: 0.25 s). Only an Address this far before the takeaway is re-checked, with the
# existing body-wide stillness test (net drift of hands, pelvis, shoulders, ankles; posture and
# no-drift guards). docs/agent-work/analysis-v2/ADDRESS_RULE.md: changes 4 of 67 validation swings.
ADDRESS_RECHECK_GAP_S = 0.6


def _recheck_address(series: MotionSeries, smoothed: Sequence[np.ndarray | None], speeds: Sequence[float | None],
                     config: MomentConfig, episode: SwingEpisode) -> int | None:
    """A later Address for ``episode`` when its hand-speed set-up sits suspiciously early, else None."""

    if not config.address_recheck:
        return None
    times = series.times_s
    if times[episode.takeaway] - times[episode.address] <= ADDRESS_RECHECK_GAP_S:
        return None
    later = [e.address for e in _find_episodes(series, smoothed, speeds, config, body_wide=True)
             if abs(e.top - episode.top) <= 2 and episode.address < e.address < episode.takeaway]
    return max(later) if later else None


def _unavailable(phase: Phase, *reasons: str) -> MomentEstimate:
    return MomentEstimate(
        phase=phase,
        frame_index=None,
        timestamp_ms=None,
        source=SOURCE_UNAVAILABLE,
        review_required=True,
        confidence=None,
        evidence=tuple(reasons),
    )


def _estimate(
    phase: Phase,
    index: int | None,
    series: MotionSeries,
    evidence: Sequence[str],
    *,
    confidence: float,
    review_required: bool = True,
) -> MomentEstimate:
    if index is None:
        return _unavailable(phase, *evidence)
    if not 0 <= index < len(series.frames):
        return _unavailable(phase, "the estimated frame is outside the analysed range")
    return MomentEstimate(
        phase=phase,
        frame_index=series.frames[index],
        timestamp_ms=series.times_s[index] * 1000.0,
        source=SOURCE_ESTIMATED,
        review_required=review_required,
        confidence=confidence,
        evidence=tuple(evidence),
    )


def estimate_moments(
    records: Sequence[object],
    *,
    config: MomentConfig | None = None,
    previous: MomentSet | None = None,
) -> MomentSet:
    """Estimate the seven checkpoint moments for one analysed clip.

    Manual choices in ``previous`` always win: re-estimating never silently
    overwrites a frame the golfer picked by hand.
    """

    settings = config or MomentConfig()
    series = build_motion_series(records, config=settings)
    sequence: dict[str, object] = {
        "frames": len(series.frames),
        "duration_s": round(series.duration_s, 4),
        "coverage": round(series.coverage, 4),
        "torso_scale_px": round(series.torso_scale_px, 3),
        "first_frame": series.frames[0] if series.frames else None,
        "last_frame": series.frames[-1] if series.frames else None,
        "ambiguous": False,
        "gates": list(series.reasons),
    }

    def blocked(reason: str) -> MomentSet:
        estimates = tuple(
            _unavailable(phase, reason) for phase in PHASE_ORDER
        )
        return MomentSet(estimates=estimates, config=settings, sequence=sequence)

    if len(series.frames) < settings.min_frames:
        sequence["gates"] = [*sequence["gates"], "too few frames to estimate moments"]
        return blocked("too few frames were analysed to estimate this moment")
    if series.duration_s < settings.min_duration_s:
        sequence["gates"] = [
            *sequence["gates"],
            f"clip shorter than {settings.min_duration_s:g}s",
        ]
        return blocked("the analysed clip is too short to estimate this moment")
    if series.coverage < settings.min_point_coverage:
        sequence["gates"] = [
            *sequence["gates"],
            f"usable tracking coverage {series.coverage:.0%}",
        ]
        return blocked(
            "not enough reliable body tracking in this clip to estimate moments"
        )

    gaps: list[float] = []
    for (_, end), (next_start, _) in zip(_runs(series.usable), _runs(series.usable)[1:]):
        if next_start > end:
            gaps.append(series.times_s[next_start - 1] - series.times_s[end])
    longest_gap = max(gaps, default=0.0)
    sequence["longest_gap_s"] = round(longest_gap, 4)
    if longest_gap >= settings.max_gap_s:
        sequence["gates"] = [
            *sequence["gates"],
            f"a tracking gap of {longest_gap:.2f}s breaks the swing sequence",
        ]
        return blocked(
            "a tracking gap of "
            f"{longest_gap:.2f}s breaks the swing sequence, so no moments were "
            "estimated"
        )

    smoothed = _smoothed(series, settings)
    speeds = _speeds(series, smoothed)
    limit = len(series.frames)

    reversals = _count_reversals(series, smoothed, settings, start=0, limit=limit)
    sequence["hand_reversals"] = reversals

    # The original hand-speed test first: it is the one that is sensitive to
    # the slow start of the takeaway, so every clip it handles keeps exactly
    # the frames it had. (Using the body-wide test here as well moved Address
    # into the first frames of the takeaway on four batch clips - shaft 80 deg
    # instead of 55 deg at "Address" - so it is only a fallback.)
    episodes = _find_episodes(series, smoothed, speeds, settings)
    setup_basis = "hand-speed"
    real_usable: tuple[bool, ...] | None = None  # set when wrist dropouts are bridged
    if not episodes:
        # Only when no swing is found (robustness batch 13, 14): bridge 1-3
        # frame wrist dropouts that split one swing into pieces, and accept a
        # body-wide still window (hand jitter at set-up), with its posture and
        # no-drift guards. Departure, top and return are tested unchanged, so a
        # clip without a real swing still has no episode, and several episodes
        # stay ambiguous.
        bridged, filled = _bridge_hand_gaps(series, settings.bridge_gap_s)
        b_smoothed = _smoothed(bridged, settings) if filled else smoothed
        b_speeds = _speeds(bridged, b_smoothed) if filled else speeds
        found = _find_episodes(bridged, b_smoothed, b_speeds, settings, body_wide=True)
        if found:
            episodes = found
            if filled:
                real_usable = series.usable
                sequence["bridged_hand_frames"] = filled
            series, smoothed, speeds = bridged, b_smoothed, b_speeds
            setup_basis = "fallback: hand-speed or body-wide" + (", wrist dropouts bridged" if filled else "")
    sequence["setup_stillness"] = setup_basis if episodes else None
    sequence["swing_episodes"] = len(episodes)
    if len(episodes) >= 2:
        reason = (
            f"{len(episodes)} separate set-up, backswing and return sequences were "
            "found, so this looks like more than one swing; moment estimates are "
            "withheld rather than guessed"
        )
        sequence["ambiguous"] = True
        sequence["gates"] = [*sequence["gates"], reason]
        return blocked(reason)

    address_index: int | None = None
    takeaway_index: int | None = None
    top_index: int | None = None
    baseline: np.ndarray | None = None
    address_evidence: tuple[str, ...]
    takeaway_evidence: tuple[str, ...]
    top_evidence: tuple[str, ...]

    if episodes:
        episode = episodes[0]
        address_index = episode.address
        takeaway_index = episode.takeaway
        top_index = episode.top
        baseline = smoothed[address_index]
        address_evidence = (
            "stable hand window immediately before a sustained departure",
            f"motion under {settings.stability_speed:g} body-lengths per second "
            f"for {settings.stability_window_s:g}s",
        ) if setup_basis == "hand-speed" else (
            "still window immediately before a sustained departure, found by the "
            "fallback because the hand-speed test found no swing",
            f"hand motion under {settings.stability_speed:g} body-lengths per second, or "
            f"hands, pelvis, shoulders and ankles each within {settings.still_radius:g} "
            f"body-lengths with no net hand drift, for {settings.stability_window_s:g}s",
        ) + ((f"{sequence['bridged_hand_frames']} wrist-dropout frame(s) bridged",)
             if sequence.get("bridged_hand_frames") else ())
        takeaway_evidence = ("hands left the set-up region and kept moving",)
        top_evidence = (
            f"first hand-height reversal after the takeaway: rose "
            f"{episode.rise:.2f} and dropped {episode.fall:.2f} body-lengths",
        )
        rechecked = _recheck_address(series, smoothed, speeds, settings, episode) if setup_basis == "hand-speed" else None
        if rechecked is not None:
            sequence["address_recheck"] = {
                "hand_speed_address_s": round(series.times_s[address_index], 4),
                "address_s": round(series.times_s[rechecked], 4),
                "takeaway_s": round(series.times_s[takeaway_index], 4),
            }
            address_index = rechecked
            baseline = smoothed[address_index]
            address_evidence = address_evidence + (
                f"re-checked: the hand-speed set-up ended more than {ADDRESS_RECHECK_GAP_S:g}s before the "
                "takeaway, so the later body-wide still window before the same swing was used",
            )
    else:
        # No complete swing. Keep whatever the evidence still supports: a
        # set-up and takeaway if the clip ends early, or a top if it starts
        # mid-swing, and leave the rest unavailable.
        address_evidence = (
            "no stable set-up window before a full swing was found "
            "(the clip may start mid-swing)",
        )
        takeaway_evidence = (
            "no set-up baseline to measure the start of the swing against",
        )
        partial = _partial_setup(series, smoothed, speeds, settings)
        if partial is not None:
            address_index, takeaway_index = partial
            baseline = smoothed[address_index]
            address_evidence = (
                "stable hand window immediately before a sustained rising "
                "departure; the rest of the swing was not complete",
            )
            takeaway_evidence = ("hands left the set-up region and kept moving",)
        reversal = _first_reversal(
            smoothed,
            settings,
            start=takeaway_index if takeaway_index is not None else 0,
            limit=limit,
            base_elevation=None if baseline is None else _elevation(baseline),
        )
        if reversal is None:
            top_evidence = (
                "no clear rise-then-reversal in the hand path; a top would be a "
                "guess",
            )
        else:
            top_index = reversal[0]
            top_evidence = (
                f"first hand-height reversal: rose {reversal[1]:.2f} and dropped "
                f"{reversal[2]:.2f} body-lengths",
            )
        if baseline is None:
            for start, _ in _runs(series.usable):
                if smoothed[start] is not None:
                    baseline = smoothed[start]
                    break

    if top_index is not None:
        top_index = _plateau_centre(
            series,
            top_index,
            tolerance=settings.top_plateau,
            window_s=2 * settings.refine_window_s,
            lower=(takeaway_index + 1) if takeaway_index is not None else 0,
            upper=limit,
        )

    mid_back_index: int | None = None
    mid_back_evidence: tuple[str, ...]
    if top_index is None or takeaway_index is None or address_index is None:
        mid_back_evidence = ("needs both a set-up and a top to place it between",)
    else:
        mid_back_index, mid_back_evidence = _estimate_mid_back(
            smoothed,
            settings,
            baseline=baseline,
            start=takeaway_index,
            top_index=top_index,
        )

    downswing_index: int | None = None
    downswing_evidence: tuple[str, ...] = ()
    if top_index is None:
        downswing_evidence = ("no top to measure a return from",)
    else:
        downswing_index, downswing_evidence = _estimate_downswing(
            series,
            smoothed,
            settings,
            baseline=baseline,
            top_index=top_index,
            limit=limit,
        )

    impact_search = downswing_index if downswing_index is not None else top_index
    impact_index: int | None = None
    impact_evidence: tuple[str, ...] = ()
    if impact_search is None:
        impact_evidence = ("no top or downswing to measure a return from",)
    else:
        impact_index, impact_evidence = _estimate_impact(
            series,
            smoothed,
            settings,
            baseline=baseline,
            search_start=impact_search,
            limit=limit,
        )
        if impact_index is not None and baseline is not None:
            target = baseline
            impact_index = _refine(
                series,
                impact_index,
                lambda point: -float(np.linalg.norm(point - target)),
                window_s=settings.refine_window_s,
                lower=impact_search + 1,
                upper=limit,
            )

    finish_search =impact_index if impact_index is not None else impact_search
    finish_index: int | None = None
    finish_evidence: tuple[str, ...] = ()
    finish_reaches_end = False
    if finish_search is None:
        finish_evidence = ("no impact or downswing to settle after",)
    else:
        finish_index, finish_evidence, finish_reaches_end = _estimate_finish(
            series,
            smoothed,
            speeds,
            settings,
            search_start=finish_search,
            limit=limit,
        )

    if real_usable is not None:
        # Bridged frames only join the hand path; a moment is always placed on
        # a frame whose pose was actually measured.
        def real(index: int | None) -> int | None:
            if index is None or real_usable[index]:
                return index
            for step in range(1, len(real_usable)):
                for candidate in (index + step, index - step):
                    if 0 <= candidate < len(real_usable) and real_usable[candidate]:
                        return candidate
            return None

        address_index, takeaway_index, mid_back_index, top_index = (
            real(address_index), real(takeaway_index), real(mid_back_index), real(top_index))
        downswing_index, impact_index, finish_index = real(downswing_index), real(impact_index), real(finish_index)

    def pick(index: int | None, confidence: float) -> float:
        return confidence if index is not None else 0.0

    estimates = (
        _estimate(Phase.ADDRESS, address_index, series, address_evidence,
                  confidence=pick(address_index, 0.6)),
        _estimate(Phase.TAKEAWAY, takeaway_index, series, takeaway_evidence,
                  confidence=pick(takeaway_index, 0.55)),
        _estimate(Phase.MID_BACK, mid_back_index, series, mid_back_evidence,
                  confidence=pick(mid_back_index, 0.45)),
        _estimate(Phase.TOP, top_index, series, top_evidence,
                  confidence=pick(top_index, 0.6)),
        _estimate(Phase.DOWNSWING, downswing_index, series, downswing_evidence,
                  confidence=pick(downswing_index, 0.45)),
        _estimate(Phase.IMPACT, impact_index, series, impact_evidence,
                  confidence=pick(impact_index, 0.4)),
        _estimate(Phase.FINISH, finish_index, series, finish_evidence,
                  confidence=pick(finish_index, 0.55)),
    )

    if previous is not None:
        estimates = tuple(_carry_manual(estimate, previous) for estimate in estimates)

    sequence["available_count"] = sum(
        1 for estimate in estimates if estimate.available
    )
    sequence["estimated_count"] = sum(
        1
        for estimate in estimates
        if estimate.available and estimate.source == SOURCE_ESTIMATED
    )
    sequence["manual_count"] = sum(
        1 for estimate in estimates if estimate.source == SOURCE_MANUAL
    )
    if finish_index is not None and finish_reaches_end:
        sequence["notes"] = ["the finish window reaches the end of the clip"]
    return MomentSet(estimates=estimates, config=settings, sequence=sequence)


def _carry_manual(estimate: MomentEstimate, previous: MomentSet) -> MomentEstimate:
    if estimate.phase not in {item.phase for item in previous.estimates}:
        return estimate
    previous_estimate = previous.get(estimate.phase)
    if not previous_estimate.manual_override:
        return estimate
    return previous_estimate


def apply_manual_overrides(
    moment_set: MomentSet,
    overrides: Mapping[Phase | str, int | None],
    *,
    timestamps_ms: Mapping[int, float] | None = None,
) -> MomentSet:
    """Apply hand-picked frames; ``None`` clears a slot back to unavailable."""

    updates: dict[Phase, MomentEstimate] = {}
    for raw_phase, frame_index in overrides.items():
        phase = coerce_phase(raw_phase)
        if frame_index is None:
            updates[phase] = MomentEstimate(
                phase=phase,
                frame_index=None,
                timestamp_ms=None,
                source=SOURCE_UNAVAILABLE,
                review_required=True,
                confidence=None,
                evidence=("cleared by hand",),
                manual_override=True,
            )
            continue
        resolved = int(frame_index)
        timestamp = None
        if timestamps_ms is not None:
            timestamp = timestamps_ms.get(resolved)
        updates[phase] = MomentEstimate(
            phase=phase,
            frame_index=resolved,
            timestamp_ms=timestamp,
            source=SOURCE_MANUAL,
            review_required=False,
            confidence=None,
            evidence=("selected by hand",),
            manual_override=True,
        )

    estimates = tuple(
        updates.get(estimate.phase, estimate) for estimate in moment_set.estimates
    )
    sequence = dict(moment_set.sequence)
    sequence["manual_count"] = sum(
        1 for estimate in estimates if estimate.source == SOURCE_MANUAL
    )
    sequence["available_count"] = sum(
        1 for estimate in estimates if estimate.available
    )
    return MomentSet(estimates=estimates, config=moment_set.config, sequence=sequence)


def to_phase_selection(moment_set: MomentSet) -> "PhaseSelection":
    """Adapter for the existing export/pipeline contracts."""

    from .phases import ManualPhaseProvider

    return ManualPhaseProvider(
        frames=moment_set.available_frames(),
        reference_frame=moment_set.get(Phase.ADDRESS).frame_index,
        derive_reference_from_address=False,
    ).select()


def moment_summary(moment_set: MomentSet) -> str:
    """One short, honest sentence for the interface."""

    available = [item for item in moment_set.estimates if item.available]
    if moment_set.ambiguous:
        return (
            "Two swing-like movement episodes were found, so no moments were "
            "estimated. Pick moments by hand if this is one swing."
        )
    if not available:
        return (
            "No moments could be estimated from this clip. Pick moments by hand "
            "or analyse a clearer video."
        )
    estimated = sum(1 for item in available if item.source == SOURCE_ESTIMATED)
    manual = sum(1 for item in available if item.source == SOURCE_MANUAL)
    parts = []
    if estimated:
        parts.append(f"{estimated} estimated")
    if manual:
        parts.append(f"{manual} chosen by hand")
    return (
        f"{len(available)} of {len(PHASE_ORDER)} moments available ({', '.join(parts)}). "
        "Estimates are a heuristic from body motion; check them and adjust any "
        "that look wrong."
    )


__all__ = [
    "MomentConfig",
    "MomentEstimate",
    "MomentSet",
    "MotionSeries",
    "SOURCE_ESTIMATED",
    "SOURCE_MANUAL",
    "SOURCE_UNAVAILABLE",
    "apply_manual_overrides",
    "build_motion_series",
    "estimate_moments",
    "moment_summary",
    "to_phase_selection",
]
