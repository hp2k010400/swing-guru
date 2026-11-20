"""Locating the real swing inside a longer clip (preprocessing only)."""

from __future__ import annotations

import numpy as np
import pytest

from swingguru.swing_window import (
    MULTIPLE_SWINGS,
    NO_SWING_FOUND,
    SINGLE_SWING,
    WindowConfig,
    decide_window,
    find_candidates,
)

RATE = 15.0  # samples per second (stride 2 at 30 fps)


def _series(*segments: tuple[float, float, float]) -> tuple[list[float], list[float], list[float]]:
    """Piecewise-linear hand elevation: (duration_s, start_e, end_e) per segment."""

    values: list[float] = []
    for duration, start, end in segments:
        count = max(1, int(round(duration * RATE)))
        values += list(np.linspace(start, end, count, endpoint=False))
    times = [i / RATE for i in range(len(values))]
    speed = [0.0] + [abs(b - a) * RATE for a, b in zip(values, values[1:])]
    return times, values, speed


SWING = [(0.6, -0.3, 1.2), (0.3, 1.2, -0.1), (0.3, -0.1, 1.5), (1.5, 1.5, 1.4)]


def test_one_swing_after_a_still_set_up_is_found() -> None:
    times, e, v = _series((6.0, -0.3, -0.3), *SWING, (3.0, 1.4, -0.3))
    candidates = find_candidates(times, e, v)
    assert len(candidates) == 1
    swing = candidates[0]
    assert swing.complete and swing.follow_through
    assert swing.top_s == pytest.approx(6.6, abs=0.1)
    decision = decide_window(candidates, times[-1])
    assert decision.status == SINGLE_SWING and decision.confidence == "HIGH"
    assert decision.trimmed
    # Enough before Top for Address / camera / tush line, enough after Impact.
    assert decision.start_s <= swing.still_end_s - WindowConfig().min_pre_still_s + 1e-9
    assert decision.end_s >= swing.impact_s + 1.9


def test_walking_and_teeing_a_ball_are_not_swings() -> None:
    # Hands drop far below the hips (bending to tee) and come back; walking jitter.
    times, e, v = _series((2.0, -0.2, -0.1), (1.0, -0.1, -1.6), (1.0, -1.6, -0.2), (2.0, -0.2, 0.1), (1.0, 0.1, -0.3))
    assert find_candidates(times, e, v) == []
    decision = decide_window([], times[-1])
    assert decision.status == NO_SWING_FOUND and not decision.trimmed and decision.confidence == "NONE"


def test_lifting_the_club_and_lowering_it_slowly_is_not_a_swing() -> None:
    times, e, v = _series((2.0, -0.3, -0.3), (0.8, -0.3, 1.2), (2.5, 1.2, -0.3), (1.0, -0.3, -0.3))
    assert find_candidates(times, e, v) == []


def test_a_one_sample_impact_dip_is_not_smoothed_away() -> None:
    times, e, v = _series((3.0, -0.2, -0.2), (0.6, -0.2, 1.3), (0.2, 1.3, 1.3), (1.0, 1.4, 1.4), (1.0, 1.3, 1.2))
    top = int(3.8 * RATE)
    e[top + 3] = 0.05  # the only low sample between Top and the finish
    candidates = find_candidates(times, e, v)
    assert len(candidates) == 1 and candidates[0].impact_s == pytest.approx(times[top + 3])


def test_dropping_far_below_the_set_up_is_not_an_impact() -> None:
    # Hands go up, then down well below the set-up height (walking up to the camera).
    times, e, v = _series((3.0, 0.2, 0.2), (0.6, 0.2, 1.3), (0.3, 1.3, -0.9), (2.0, -0.9, -0.9))
    candidates = find_candidates(times, e, v)
    # It may stay listed as an incomplete event, never as a complete swing, so
    # the clip is not trimmed to it.
    assert not any(c.complete for c in candidates)
    decision = decide_window(candidates, times[-1])
    assert not decision.trimmed and decision.confidence in ("LOW", "NONE")


def test_two_swings_are_reported_and_the_window_excludes_the_other() -> None:
    times, e, v = _series((3.0, -0.3, -0.3), *SWING, (2.0, 1.4, -0.3), (4.0, -0.3, -0.3), *SWING, (2.0, 1.4, -0.3))
    candidates = find_candidates(times, e, v)
    assert len(candidates) == 2
    decision = decide_window(candidates, times[-1])
    assert decision.status == MULTIPLE_SWINGS and decision.confidence == "MEDIUM"
    other = [c for c in candidates if c is not decision.chosen][0]
    assert not (decision.start_s <= other.top_s <= decision.end_s)


def test_no_complete_swing_means_the_whole_clip_at_low_confidence() -> None:
    # Swing-shaped but no still set-up before it (hands moving the whole time).
    times, e, v = _series((0.5, 0.3, -0.2), (0.4, -0.2, 1.3), (0.25, 1.3, -0.1), (0.3, -0.1, 1.4), (3.0, 1.4, 1.2))
    candidates = find_candidates(times, e, v)
    assert candidates and not any(c.complete for c in candidates)
    decision = decide_window(candidates, times[-1])
    assert decision.confidence == "LOW" and not decision.trimmed
    assert (decision.start_s, decision.end_s) == (0.0, times[-1])


def test_a_scene_cut_inside_the_window_lowers_confidence() -> None:
    times, e, v = _series((6.0, -0.3, -0.3), *SWING, (3.0, 1.4, -0.3))
    candidates = find_candidates(times, e, v)
    clean = decide_window(candidates, times[-1], scene_cuts=[0.5])
    assert clean.confidence == "HIGH"
    cut = decide_window(candidates, times[-1], scene_cuts=[6.0])
    assert cut.confidence == "LOW" and "edited" in cut.reason


def test_a_tightly_trimmed_clip_is_left_whole() -> None:
    times, e, v = _series((2.0, -0.3, -0.3), *SWING)
    decision = decide_window(find_candidates(times, e, v), times[-1])
    assert decision.status == SINGLE_SWING and not decision.trimmed
    assert (decision.start_s, decision.end_s) == (0.0, times[-1])


def test_candidate_detection_is_deterministic() -> None:
    times, e, v = _series((3.0, -0.3, -0.3), *SWING, (2.0, 1.4, -0.3), (4.0, -0.3, -0.3), *SWING)
    assert find_candidates(times, e, v) == find_candidates(list(times), list(e), list(v))
