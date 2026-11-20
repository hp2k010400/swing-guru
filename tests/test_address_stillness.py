"""Address detection that survives wrist jitter and wrist dropouts - and nothing else.

The hand-speed stillness test uses the worst single frame, so one jittering
wrist breaks a visibly still set-up; 1-3 frame wrist dropouts also split one
swing into pieces (robustness batch swings 13, 14). The fallback runs only when
the original test finds no swing, bridges short wrist gaps and accepts
body-wide position stillness. It must not turn walking, teeing a ball or an
edited montage into an Address.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.auto_moments import estimate_moments
from swingguru.phases import Phase

TORSO = 100.0  # px
PELVIS = np.array([300.0, 500.0])

# Hand offsets from the pelvis in torso lengths (y down): set-up, takeaway,
# top, downswing, impact, finish.
SETUP, TAKEAWAY, TOP, DOWN, IMPACT, FINISH = (
    (-0.10, 0.55), (-0.40, 0.45), (0.60, -0.60), (0.15, 0.05), (-0.05, 0.55), (0.25, -0.45))


def _lerp(a, b, w):
    w = min(1.0, max(0.0, w))
    w = w * w * (3 - 2 * w)
    return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w)


def _swing_hands(t: float, start: float) -> tuple[float, float]:
    """A swing starting at ``start`` s: 0.3 s takeaway, top at +0.8 s, impact +1.1 s."""

    keys = [(start, SETUP), (start + 0.3, TAKEAWAY), (start + 0.8, TOP), (start + 1.0, DOWN),
            (start + 1.1, IMPACT), (start + 1.6, FINISH)]
    if t <= keys[0][0]:
        return SETUP
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t <= t1:
            return _lerp(v0, v1, (t - t0) / (t1 - t0))
    return FINISH


SETUP_LEAN = np.radians(40.0)  # a down-the-line set-up bends forward from the hips


def _record(index, fps, hands, *, body_shift=(0.0, 0.0), bend=0.0, jitter=None, lean=SETUP_LEAN):
    """One pose record in pixels. ``bend`` lowers the shoulders (torso lengths)."""

    pelvis = PELVIS + np.array(body_shift) * TORSO
    shoulder = pelvis + np.array([np.sin(lean) * TORSO, -np.cos(lean) * TORSO + bend * TORSO])
    pixels = {"hip_mid": pelvis, "shoulder_mid": shoulder,
              "left_ankle": pelvis + [-8.0, 300.0], "right_ankle": pelvis + [8.0, 300.0]}
    if hands is not None:
        hand = pelvis + np.array(hands) * TORSO
        lead = hand.copy()
        if jitter is not None:
            lead = lead + jitter  # one wrist jitters (hidden behind the other at set-up)
        pixels["left_wrist"], pixels["right_wrist"] = lead, hand
    return SimpleNamespace(index=index, timestamp_ms=index * 1000.0 / fps,
                           points=SimpleNamespace(pixels=pixels, landmarks={}))


def _jitter(index: int, amplitude: float) -> np.ndarray:
    rng = np.random.default_rng(index)
    return rng.normal(0.0, amplitude * TORSO, 2)


def _clip(duration=4.0, fps=30.0, swing_at=2.0, *, jitter=0.0, dropouts=(), setup=None):
    records = []
    for i in range(int(duration * fps)):
        t = i / fps
        hands = setup(t) if setup is not None and t < swing_at else _swing_hands(t, swing_at)
        if i in dropouts:
            hands = None
        records.append(_record(i, fps, hands, jitter=_jitter(i, jitter) if jitter else None))
    return records


def _frames(records):
    moments = estimate_moments(records)
    return {e.phase: e.frame_index for e in moments.estimates}, moments.sequence


# --- positive: still golfer, noisy wrist -------------------------------------------


def test_clean_swing_uses_the_original_hand_speed_test() -> None:
    frames, sequence = _frames(_clip())
    assert sequence["setup_stillness"] == "hand-speed" and "bridged_hand_frames" not in sequence
    assert frames[Phase.ADDRESS] is not None and 45 <= frames[Phase.ADDRESS] < frames[Phase.TAKEAWAY]


def _hand_speed_only_address(records):
    import swingguru.auto_moments as am

    original = am._find_episodes
    am._find_episodes = lambda *a, body_wide=False: original(*a, body_wide=False)
    try:
        return {e.phase: e.frame_index for e in am.estimate_moments(records).estimates}[Phase.ADDRESS]
    finally:
        am._find_episodes = original


def test_noisy_wrist_with_a_still_body_finds_the_address_before_the_swing() -> None:
    records = _clip(jitter=0.08)  # one wrist jitters ~8 % of a torso length per frame
    # The hand-speed test alone finds no swing, so the fallback is what places it.
    assert _hand_speed_only_address(records) is None
    frames, sequence = _frames(records)
    assert sequence["setup_stillness"].startswith("fallback")
    address, takeaway = frames[Phase.ADDRESS], frames[Phase.TAKEAWAY]
    assert address is not None and takeaway is not None
    # The set-up just before the swing (hands start moving at frame 60, gently).
    assert 45 <= address < takeaway <= 75


def test_wrist_dropouts_during_the_swing_are_bridged() -> None:
    records = _clip(jitter=0.03, dropouts=(66, 67, 93, 94, 95))  # takeaway and impact blinks
    frames, sequence = _frames(records)
    assert sequence["bridged_hand_frames"] == 5
    assert frames[Phase.ADDRESS] is not None and frames[Phase.TOP] is not None
    assert frames[Phase.ADDRESS] < frames[Phase.TAKEAWAY] < frames[Phase.TOP] < frames[Phase.IMPACT]


def test_long_dropouts_are_not_bridged() -> None:
    # Half a second without wrists is not a blink; nothing is invented.
    records = _clip(jitter=0.03, dropouts=tuple(range(66, 81)))
    _, sequence = _frames(records)
    assert not sequence.get("bridged_hand_frames")


@pytest.mark.parametrize("fps", [24.0, 30.0, 60.0])
def test_address_is_stable_across_frame_rates(fps) -> None:
    frames, _ = _frames(_clip(fps=fps, jitter=0.08))
    address_t = frames[Phase.ADDRESS] / fps
    assert 1.4 <= address_t <= 2.2  # the set-up just before the 2.0 s swing start, never seconds early
    assert frames[Phase.ADDRESS] < frames[Phase.TAKEAWAY]


# --- negative: walking, teeing a ball, a montage -------------------------------------


def test_walking_is_never_an_address() -> None:
    """Walking (body moving 1.5 torso/s) with arm motion shaped like a swing."""

    fps, records = 30.0, []
    for i in range(120):
        t = i / fps
        hands = _swing_hands(t, 1.5)
        records.append(_record(i, fps, hands, body_shift=(1.5 * t, 0.02 * np.sin(8 * t)),
                               jitter=_jitter(i, 0.03)))
    frames, sequence = _frames(records)
    assert frames[Phase.ADDRESS] is None
    assert sequence.get("setup_stillness") is None


def test_standing_upright_and_lifting_the_club_is_not_an_address() -> None:
    """Upright at the bag, pulling a club out overhead (the 69 s clip at ~7 s)."""

    fps, records = 30.0, []
    for i in range(120):
        t = i / fps
        hands = _swing_hands(t, 1.5)
        records.append(_record(i, fps, hands, jitter=_jitter(i, 0.08), lean=np.radians(3.0)))
    frames, _ = _frames(records)
    assert frames[Phase.ADDRESS] is None


def test_teeing_a_ball_does_not_become_the_address() -> None:
    """Bend down, pause with the hands at the ground, stand up, set up, swing."""

    fps, records = 30.0, []
    ground = (0.25, 1.6)
    for i in range(int(6.0 * fps)):
        t = i / fps
        bend = 0.0
        if t < 1.0:  # bending down
            bend, hands = 0.6 * t, _lerp(SETUP, ground, t)
        elif t < 1.6:  # still at the bottom, placing the ball
            bend, hands = 0.6, ground
        elif t < 2.6:  # standing back up into the set-up
            bend, hands = 0.6 * (2.6 - t), _lerp(ground, SETUP, t - 1.6)
        else:
            hands = _swing_hands(t, 4.0)
        records.append(_record(i, fps, hands, bend=bend, jitter=_jitter(i, 0.03)))
    frames, _ = _frames(records)
    address = frames[Phase.ADDRESS]
    assert address is not None
    assert address / fps > 2.6  # the set-up after standing up, not the bent-over pause


def test_teeing_without_a_swing_has_no_address() -> None:
    fps, records = 30.0, []
    ground = (0.25, 1.6)
    for i in range(int(4.0 * fps)):
        t = i / fps
        if t < 1.0:
            bend, hands = 0.6 * t, _lerp(SETUP, ground, t)
        elif t < 1.6:
            bend, hands = 0.6, ground
        else:
            bend, hands = max(0.0, 0.6 * (2.6 - t)), _lerp(ground, SETUP, t - 1.6)
        records.append(_record(i, fps, hands, bend=bend, jitter=_jitter(i, 0.03)))
    frames, _ = _frames(records)
    assert frames[Phase.ADDRESS] is None


def test_hard_cut_montage_gets_no_false_address() -> None:
    """Edited clips that each start mid-swing, with the golfer jumping between cuts."""

    fps, records = 30.0, []
    offsets = [(0.0, 0.0), (1.2, -0.3), (-0.8, 0.4)]
    for i in range(int(3 * 1.2 * fps)):
        t = i / fps
        clip, local = divmod(t, 1.2)
        hands = _swing_hands(local + 0.6, 0.0)  # starts at the top of the backswing
        records.append(_record(i, fps, hands, body_shift=offsets[int(clip)], jitter=_jitter(i, 0.03)))
    frames, _ = _frames(records)
    assert frames[Phase.ADDRESS] is None


def test_two_complete_swings_stay_ambiguous_in_the_fallback() -> None:
    fps, records = 30.0, []
    for i in range(int(7.0 * fps)):
        t = i / fps
        hands = _swing_hands(t, 1.5) if t < 4.0 else _swing_hands(t, 5.0)
        records.append(_record(i, fps, hands, jitter=_jitter(i, 0.03)))
    frames, sequence = _frames(records)
    assert frames[Phase.ADDRESS] is None and sequence["ambiguous"] is True
