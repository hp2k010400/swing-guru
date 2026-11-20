"""OWLv2 shaft fallback: pure geometry and gating, tested without the model."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from swingguru import analysis_bundle as bundle
from swingguru.club.fallback import (
    BoxCandidate,
    FallbackConfig,
    box_gap,
    fallback_shaft,
    far_corner,
    temporal_expectation,
)
from swingguru.club.geometry import angle_difference, unit_from_angle
from swingguru.club.tracker import ClubConfig, ClubTrack, FrameShaft, HandsContext
from swingguru.phases import Phase

HANDS = np.array([200.0, 300.0])
TORSO = 100.0


def _frame_with_shaft(angle: float, length: float = 150.0) -> np.ndarray:
    image = np.full((600, 400, 3), 150, np.uint8)
    tip = HANDS + unit_from_angle(angle) * length
    cv2.line(image, tuple(int(v) for v in HANDS), tuple(int(v) for v in tip), (40, 40, 40), 2, cv2.LINE_AA)
    return image


def _box_for(angle: float, length: float = 150.0, score: float = 0.5) -> BoxCandidate:
    tip = HANDS + unit_from_angle(angle) * length
    x0, x1 = sorted([HANDS[0], tip[0]])
    y0, y1 = sorted([HANDS[1], tip[1]])
    return BoxCandidate((x0 - 3, y0 - 3, x1 + 3, y1 + 3), score)


def _hands(forearms=()) -> HandsContext:
    return HandsContext(HANDS.copy(), TORSO, forearms=tuple(forearms), source="landmarks")


def _track(entries: dict[int, float] | None = None) -> ClubTrack:
    frames = []
    for index in range(20):
        angle = (entries or {}).get(index)
        if angle is None:
            frames.append(FrameShaft(index, index * 33.3, "unavailable", "UNAVAILABLE", 0.0))
        else:
            frames.append(FrameShaft(index, index * 33.3, "detected", "HIGH", 0.9, angle_deg=angle,
                                     grip=HANDS, tip=HANDS + unit_from_angle(angle) * 100))
    return ClubTrack(tuple(frames), (), "test", ClubConfig())


def test_box_geometry_helpers() -> None:
    assert box_gap((0, 0, 10, 10), np.array([5.0, 5.0])) == 0.0
    assert box_gap((0, 0, 10, 10), np.array([13.0, 14.0])) == pytest.approx(5.0)
    assert far_corner((0, 0, 10, 20), np.array([1.0, 1.0])).tolist() == [10.0, 20.0]


def test_no_boxes_or_boxes_away_from_the_hands_are_unavailable() -> None:
    frame = _frame_with_shaft(135.0)
    shaft, info = fallback_shaft(frame, 10, 333.0, _hands(), _track(), [])
    assert not shaft.available and "no shaft box" in info["reason"]
    far = BoxCandidate((0.0, 0.0, 40.0, 40.0), 0.9)
    shaft, info = fallback_shaft(frame, 10, 333.0, _hands(), _track(), [far])
    assert not shaft.available


@pytest.mark.parametrize("angle", [135.0, 105.0, 45.0])
def test_a_box_from_the_hands_gives_the_shaft_but_never_high(angle: float) -> None:
    frame = _frame_with_shaft(angle)
    shaft, info = fallback_shaft(frame, 10, 333.0, _hands(), _track(), [_box_for(angle)])
    assert shaft.available, info
    assert abs(angle_difference(shaft.angle_deg, angle)) <= 4.0
    assert shaft.level in ("MEDIUM", "LOW") and shaft.level != "HIGH"
    assert shaft.method == "owlv2-fallback"
    assert np.allclose(shaft.grip, HANDS)


def test_a_short_box_along_the_forearm_is_rejected_but_a_long_box_is_not() -> None:
    elbow = HANDS + unit_from_angle(135.0) * 60.0  # forearm 0.6 torso long, pointing up-left
    hands = _hands([(HANDS.copy(), elbow)])
    short = _box_for(135.0, length=60.0)
    shaft, info = fallback_shaft(_frame_with_shaft(135.0, 60.0), 10, 333.0, hands, _track(), [short])
    assert not shaft.available and "forearm" in info["reason"]
    stub = HANDS + unit_from_angle(125.0) * 15.0  # foreshortened forearm, 0.15 torso
    shaft, _ = fallback_shaft(_frame_with_shaft(135.0), 10, 333.0, _hands([(HANDS.copy(), stub)]),
                              _track(), [_box_for(135.0)])
    assert shaft.available


def test_the_next_box_is_tried_when_the_best_one_fails() -> None:
    elbow = HANDS + unit_from_angle(135.0) * 60.0
    hands = _hands([(HANDS.copy(), elbow)])
    shaft, _ = fallback_shaft(_frame_with_shaft(60.0), 10, 333.0, hands, _track(),
                              [_box_for(135.0, 60.0, 0.9), _box_for(60.0, 150.0, 0.5)])
    assert shaft.available and abs(angle_difference(shaft.angle_deg, 60.0)) <= 4.0


def test_weak_evidence_stays_unavailable() -> None:
    blank = np.full((600, 400, 3), 150, np.uint8)
    weak = _box_for(135.0, length=55.0, score=0.11)
    shaft, info = fallback_shaft(blank, 10, 333.0, _hands(), _track({8: -40.0, 12: -30.0}), [weak])
    assert not shaft.available and "confidence" in info["reason"]


def test_temporal_expectation_interpolates_confident_neighbours() -> None:
    track = _track({5: 150.0, 15: -170.0})  # crosses the +/-180 seam
    expected = temporal_expectation(track, 10, 8)
    assert expected == pytest.approx(170.0)
    assert temporal_expectation(_track({5: 150.0}), 10, 8) is None


def test_guided_selection_keeps_the_walkthrough_short_and_ordered() -> None:
    def check(kind):
        return {"kind": kind}
    impact = [check("head"), check("shaft"), check("pelvis"), check("posture")]
    bundle.mark_guided(Phase.IMPACT, impact)
    assert [c["kind"] for c in impact] == ["posture", "pelvis", "shaft", "head"]
    assert [c["guided"] for c in impact] == [True, True, True, False]
    takeaway = [check("head"), check("shaft")]
    bundle.mark_guided(Phase.TAKEAWAY, takeaway)
    assert [(c["kind"], c["guided"]) for c in takeaway] == [("shaft", True), ("head", False)]
    assert sum(bundle.GUIDED_CAP.values()) <= 12
