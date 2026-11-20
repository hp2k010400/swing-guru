"""Find the real swing inside a longer clip (preprocessing, not the engine).

Phone clips often contain walking in, teeing a ball, waggles, practice swings
and stepping away. The analysis engine assumes a clip that is mostly one swing,
so this stage looks for the swing first with a cheap signal and returns an
*analysis window*; the source video is never modified.

Signal: MediaPipe pose at a reduced rate (every ``stride``-th frame). Per sample
we keep the hand elevation above the hips in torso lengths

    e = (hip_y - hand_y) / torso

and the hand speed in torso lengths per second.

A swing is recognised by its structure, not by one threshold:

1. a *still set-up*: hands low (``e <= low_max``) and slow for ``min_still_s``;
2. a *rise* to a peak with hands near or above the shoulders
   (``e >= peak_min``) within ``max_backswing_s`` of the set-up (Top);
3. a *fast return* to hands-low within ``max_downswing_s`` of the peak
   (Impact region);
4. optionally a second rise (follow-through) within ``follow_window_s``.

Walking, teeing a ball, picking something up and waggles never raise the hands
to shoulder height; lifting the club and slowly lowering it fails (3). A
practice swing is a real swing shape, so several candidates are reported as
``MULTIPLE_SWINGS`` and the strongest complete one is chosen, never silently.

These constants only locate activity in time. They are not golf
measurements, and they do not affect any number shown to a golfer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

SINGLE_SWING = "SINGLE_SWING"
MULTIPLE_SWINGS = "MULTIPLE_SWINGS"
NO_SWING_FOUND = "NO_SWING_FOUND"


@dataclass(frozen=True)
class WindowConfig:
    stride: int = 2
    min_visibility: float = 0.3
    low_max: float = 0.35  # hands at most this far above the hips = "low"
    peak_min: float = 0.8  # hands near shoulder height or above
    follow_min: float = 0.5
    still_speed: float = 0.6  # torso lengths per second
    min_still_s: float = 0.25
    # From the end of the still set-up to Top. Swings in the first batch took
    # 0.46-0.67 s; 1.5 s leaves room for slow tempos but rejects lifting the
    # club and holding it up.
    max_backswing_s: float = 1.5
    return_margin: float = 0.5  # impact hands within this of the set-up height
    cut_correlation: float = 0.5  # colour-histogram similarity below this = scene cut
    max_downswing_s: float = 0.8
    follow_window_s: float = 1.5
    pre_top_s: float = 3.0  # kept before Top: Address, camera check, tush line
    min_pre_still_s: float = 0.6  # at least this much of the set-up is kept
    post_impact_s: float = 2.0  # kept after Impact: follow-through and checks
    min_trim_s: float = 1.0  # trimming less than this keeps the whole clip


@dataclass(frozen=True)
class SwingCandidate:
    still_start_s: float
    still_end_s: float
    top_s: float
    impact_s: float
    peak_elevation: float
    backswing_s: float
    downswing_s: float
    follow_through: bool
    complete: bool  # still set-up + peak + fast return

    @property
    def strength(self) -> tuple:
        """Complete first, then follow-through, then return speed, then height."""

        speed = (self.peak_elevation) / max(self.downswing_s, 1e-3)
        return (self.complete, self.follow_through, round(speed, 3), round(self.peak_elevation, 3))


@dataclass
class WindowDecision:
    status: str
    duration_s: float
    start_s: float
    end_s: float
    trimmed: bool
    confidence: str
    reason: str
    chosen: SwingCandidate | None = None
    candidates: list[SwingCandidate] = field(default_factory=list)
    scene_cuts: list[float] = field(default_factory=list)
    # Engine profile candidate (slowmo): >1 when the swing was only found on a rescaled clock
    # (a slow-motion clip); every time above is real clip time. Default 1.0 = as before.
    time_scale: float = 1.0

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start_s"], data["end_s"] = round(self.start_s, 3), round(self.end_s, 3)
        return data


def _smooth(values: np.ndarray, width: int = 3) -> np.ndarray:
    out = values.copy()
    half = width // 2
    for i in range(len(values)):
        window = values[max(0, i - half): i + half + 1]
        window = window[np.isfinite(window)]
        out[i] = np.median(window) if len(window) else np.nan
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def find_candidates(times: Sequence[float], elevation: Sequence[float], speed: Sequence[float],
                    config: WindowConfig | None = None) -> list[SwingCandidate]:
    """Swing-shaped events in a hand-elevation series (pure; unit tested)."""

    config = config or WindowConfig()
    t = np.asarray(times, float)
    # Elevation stays raw: the impact dip lasts one or two samples and a median
    # filter erases it. Only the speed (used for stillness) is smoothed.
    e = np.asarray(elevation, float)
    v = _smooth(np.asarray(speed, float))
    n = len(t)
    if n < 3:
        return []
    low = np.where(np.isfinite(e), e <= config.low_max, False)
    still = low & np.where(np.isfinite(v), v <= config.still_speed, False)
    candidates: dict[int, SwingCandidate] = {}

    def swing_after(i0: int, still_span: tuple[float, float] | None,
                    level: float | None = None) -> SwingCandidate | None:
        """The first peak after sample ``i0`` and its return, if swing-shaped.

        ``level`` is the hand elevation during the still set-up. At impact the
        hands come back close to it, so the return is judged against the
        golfer's own set-up, and a drop far below it (bending down, walking up
        to the camera) is not a swing.
        """

        cross = next((i for i in range(i0, n) if t[i] - t[i0] <= config.max_backswing_s
                      and np.isfinite(e[i]) and e[i] >= config.peak_min), None)
        if cross is None:
            return None
        end = cross
        while end + 1 < n and np.isfinite(e[end + 1]) and e[end + 1] >= config.peak_min:
            end += 1
        top = cross + int(np.nanargmax(e[cross:end + 1]))
        threshold = config.low_max if level is None else min(config.low_max, level + config.return_margin)
        back = next((i for i in range(top + 1, n) if np.isfinite(e[i]) and e[i] <= threshold), None)
        if back is None or t[back] - t[top] > config.max_downswing_s:
            return None
        if level is not None:
            near = e[(t >= t[back]) & (t <= t[back] + 0.3)]
            if np.isfinite(near).any() and np.nanmin(near) < level - config.return_margin:
                return None
        follow = any(np.isfinite(e[i]) and e[i] >= config.follow_min
                     for i in range(back, n) if t[i] - t[back] <= config.follow_window_s)
        still_start, still_end = still_span if still_span else (t[i0], t[i0])
        return SwingCandidate(
            still_start_s=float(still_start), still_end_s=float(still_end), top_s=float(t[top]),
            impact_s=float(t[back]), peak_elevation=float(e[top]),
            backswing_s=float(t[top] - still_end), downswing_s=float(t[back] - t[top]),
            follow_through=bool(follow), complete=still_span is not None,
        )

    for a, b in _runs(still):
        if t[b] - t[a] < config.min_still_s:
            continue
        found = swing_after(b, (t[a], t[b]), float(np.nanmedian(e[a:b + 1])))
        if found is None:
            continue
        key = int(round(found.top_s * 100))
        previous = candidates.get(key)
        # Several still spells (waggles) before one Top: keep the latest set-up.
        if previous is None or found.still_end_s > previous.still_end_s:
            candidates[key] = found

    # Swing-shaped events with no still set-up (e.g. a continuous practice
    # swing) are still candidates, only incomplete ones.
    tops = {c.top_s for c in candidates.values()}
    for a, b in _runs(np.where(np.isfinite(e), e >= config.peak_min, False)):
        top = a + int(np.nanargmax(e[a:b + 1]))
        if any(abs(t[top] - known) < 0.05 for known in tops):
            continue
        start = next((i for i in range(top, -1, -1) if low[i]), None)
        if start is None or t[top] - t[start] > config.max_backswing_s:
            continue
        found = swing_after(start, None)
        if found is None or abs(found.top_s - t[top]) > 0.05:
            continue
        # A finish (hands high straight after a fast low) is part of a swing
        # already found, not a new one.
        if any(0 <= found.top_s - c.impact_s <= config.follow_window_s for c in candidates.values()):
            continue
        candidates[int(round(found.top_s * 100))] = found
    return sorted(candidates.values(), key=lambda c: c.top_s)


def decide_window(candidates: Sequence[SwingCandidate], duration_s: float,
                  config: WindowConfig | None = None, *, scene_cuts: Sequence[float] = ()) -> WindowDecision:
    """Pick the strongest complete swing and a padded analysis window.

    Scene cuts (an edited clip or a screen recording of one) are reported and
    any cut inside the chosen window makes the window LOW confidence.
    """

    config = config or WindowConfig()
    candidates = list(candidates)
    cuts = [float(c) for c in scene_cuts]
    if not candidates:
        note = f"; {len(cuts)} scene cut(s) - looks edited" if cuts else ""
        return WindowDecision(NO_SWING_FOUND, duration_s, 0.0, duration_s, False, "NONE",
                              "no swing-shaped hand movement found; the whole clip is analysed" + note,
                              None, [], cuts)
    chosen = max(candidates, key=lambda c: c.strength)
    status = SINGLE_SWING if len(candidates) == 1 else MULTIPLE_SWINGS
    if not chosen.complete:
        # Nothing with a still set-up: do not trim to a doubtful event.
        note = f"; {len(cuts)} scene cut(s) - looks edited" if cuts else ""
        return WindowDecision(status, duration_s, 0.0, duration_s, False, "LOW",
                              f"{len(candidates)} swing-shaped event(s) but none with a still set-up; "
                              "the whole clip is analysed" + note, chosen, candidates, cuts)
    before = [c for c in candidates if c.impact_s < chosen.still_start_s]
    after = [c for c in candidates if c.still_start_s > chosen.impact_s]
    start = max(0.0, min(chosen.top_s - config.pre_top_s, chosen.still_end_s - config.min_pre_still_s))
    if before:  # never let the engine see the previous swing
        start = max(start, max(c.impact_s for c in before) + 0.5)
    end = min(duration_s, chosen.impact_s + config.post_impact_s)
    if after:
        end = min(end, min(c.still_start_s for c in after))
    trimmed = (start + (duration_s - end)) >= config.min_trim_s
    if not trimmed:
        start, end = 0.0, duration_s
    if status == MULTIPLE_SWINGS:
        confidence, reason = "MEDIUM", f"{len(candidates)} swing-shaped events; the strongest complete one was chosen"
    elif not chosen.follow_through:
        confidence, reason = "MEDIUM", "no follow-through rise seen after the return"
    else:
        confidence, reason = "HIGH", "one complete swing: still set-up, rise, fast return, follow-through"
    inside = [c for c in cuts if start < c < end]
    if inside:
        confidence = "LOW"
        reason += f"; {len(inside)} scene cut(s) inside the window - the clip looks edited"
    return WindowDecision(status, duration_s, start, end, trimmed, confidence, reason, chosen, candidates, cuts)


# ---------------------------------------------------------------------------
# Cheap signal from the video
# ---------------------------------------------------------------------------


def sample_signal(video: Path | str, config: WindowConfig | None = None) -> dict:
    """Hand elevation and speed from MediaPipe pose on every ``stride``-th frame."""

    import cv2
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    from .pose import default_model_path

    config = config or WindowConfig()
    capture = cv2.VideoCapture(str(video))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0
    options = vision.PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(default_model_path())),
        running_mode=vision.RunningMode.VIDEO,
    )
    landmarker = vision.PoseLandmarker.create_from_options(options)
    times, hands, hips, torsos, cuts = [], [], [], [], []
    previous_hist = None
    index = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        if index % config.stride == 0:
            t = index / fps
            hist = cv2.calcHist([cv2.cvtColor(cv2.resize(frame, (64, 112)), cv2.COLOR_BGR2HSV)],
                                [0, 1], None, [16, 8], [0, 180, 0, 256])
            cv2.normalize(hist, hist)
            if previous_hist is not None and cv2.compareHist(previous_hist, hist, cv2.HISTCMP_CORREL) < config.cut_correlation:
                cuts.append(round(t, 3))
            previous_hist = hist
            result = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)),
                int(round(t * 1000)))
            hand = hip = torso = None
            if result.pose_landmarks:
                lm = result.pose_landmarks[0]
                h, w = frame.shape[:2]

                def xy(i):
                    return np.array([lm[i].x * w, lm[i].y * h]) if (lm[i].visibility or 0) >= config.min_visibility else None

                wrists = [p for p in (xy(15), xy(16)) if p is not None]
                shoulders = [p for p in (xy(11), xy(12)) if p is not None]
                hip_pts = [p for p in (xy(23), xy(24)) if p is not None]
                if wrists:
                    hand = np.mean(wrists, axis=0)
                if hip_pts:
                    hip = np.mean(hip_pts, axis=0)
                if shoulders and hip_pts:
                    torso = float(np.linalg.norm(np.mean(shoulders, axis=0) - hip))
            times.append(t)
            hands.append(hand)
            hips.append(hip)
            torsos.append(torso)
        index += 1
    capture.release()
    landmarker.close()
    valid_torsos = [x for x in torsos if x]
    torso = float(np.median(valid_torsos)) if valid_torsos else float("nan")
    elevation = [float("nan") if hand is None or hip is None else float((hip[1] - hand[1]) / torso)
                 for hand, hip in zip(hands, hips)]
    speed = [float("nan")]
    for i in range(1, len(times)):
        a, b = hands[i - 1], hands[i]
        speed.append(float("nan") if a is None or b is None else
                     float(np.linalg.norm(b - a) / torso / (times[i] - times[i - 1])))
    return {"fps": fps, "frames": index, "duration_s": index / fps, "times": times,
            "elevation": elevation, "speed": speed, "torso_px": torso,
            "detected": sum(h is not None for h in hands), "scene_cuts": cuts}


def locate_swing(video: Path | str, config: WindowConfig | None = None, *,
                 slowmo: bool = False) -> tuple[WindowDecision, dict]:
    config = config or WindowConfig()
    signal = sample_signal(video, config)
    candidates = find_candidates(signal["times"], signal["elevation"], signal["speed"], config)
    decision = decide_window(candidates, signal["duration_s"], config, scene_cuts=signal["scene_cuts"])
    if slowmo and not candidates:
        decision = slowmo_window(signal, config) or decision
    return decision, signal


def slowmo_window(signal: dict, config: WindowConfig) -> WindowDecision | None:
    """The swing on a rescaled clock (slow-motion clip), with every time converted back to clip time."""

    from dataclasses import replace

    from .slowmo import FACTORS

    # Padding in swing time is multiplied by the factor in clip time (3 s before Top is 24 s of video
    # at 8x), which made the replay preview too large. Slow-motion windows keep less padding: still
    # enough set-up stillness for Address (min_pre_still_s) and a short follow-through.
    slow_config = replace(config, pre_top_s=1.4, post_impact_s=0.8, min_pre_still_s=0.5)
    for factor in FACTORS:
        times = [t / factor for t in signal["times"]]
        speed = [s * factor for s in signal["speed"]]
        found = find_candidates(times, signal["elevation"], speed, config)
        if not found:
            continue
        cuts = [c / factor for c in signal["scene_cuts"]]
        d = decide_window(found, signal["duration_s"] / factor, slow_config, scene_cuts=cuts)
        if not d.chosen or not d.chosen.complete:
            continue
        return replace(d, duration_s=signal["duration_s"], start_s=d.start_s * factor, end_s=d.end_s * factor,
                       scene_cuts=list(signal["scene_cuts"]), time_scale=factor,
                       reason=f"{d.reason}; slow-motion clip (about {factor:g}x), found on a rescaled clock")
    return None


def cut_window(video: Path | str, destination: Path | str, start_frame: int, end_frame: int, *,
               step: int = 1, fps: float | None = None) -> None:
    """Frame-exact copy of ``[start_frame, end_frame)`` to a new file (source untouched).

    ``step`` > 1 (slow-motion clips only, see ``slowmo_step``) keeps every ``step``-th frame and plays
    them at the original ``fps``, so the copy is the same swing ``step`` times less slowed down.
    """

    import subprocess

    from .video import ffmpeg_path

    binary = ffmpeg_path()
    if binary is None:
        raise RuntimeError("ffmpeg is needed to cut an analysis window")
    chain = f"trim=start_frame={int(start_frame)}:end_frame={int(end_frame)},setpts=PTS-STARTPTS"
    rate: list[str] = []
    if step > 1:
        if not fps:
            raise ValueError("a stepped cut needs the clip's fps")
        chain += f",select=not(mod(n\\,{int(step)})),setpts=N/({float(fps):.6f}*TB)"
        rate = ["-r", f"{float(fps):.6f}"]
    command = [binary, "-y", "-loglevel", "error", "-i", str(video), "-vf", chain, *rate,
               "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-pix_fmt", "yuv420p", str(destination)]
    subprocess.run(command, check=True, capture_output=True)


def slowmo_step(time_scale: float) -> int:
    """Frames to step over when cutting a slow-motion window: 2 at 4-5x, 3 at 6x, 4 at 8x, else 1.

    The kept frames still sample the real swing at least twice as often as a normal-speed clip, and
    the pose stage (most of the processing time) has that many times fewer frames to fit.
    """

    return max(1, int(float(time_scale) // 2)) if time_scale >= 4.0 else 1


__all__ = [
    "MULTIPLE_SWINGS", "NO_SWING_FOUND", "SINGLE_SWING", "SwingCandidate", "WindowConfig",
    "WindowDecision", "cut_window", "decide_window", "find_candidates", "locate_swing", "sample_signal", "slowmo_step",
]
