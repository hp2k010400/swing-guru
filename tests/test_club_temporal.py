"""Temporal club recovery: the forearm exemption, gap recovery, background lines, confidence cap."""

from __future__ import annotations

from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from swingguru.club import ClassicalClubTracker, ClubConfig
from swingguru.club.geometry import unit_from_angle
from swingguru.club.temporal import STATUS_RECOVERED, TemporalConfig, recover_track
from swingguru.club.tracker import LEVEL_HIGH, LEVEL_UNAVAILABLE, ClubTrack, FrameShaft, passes_elbow

W, H = 400, 600
HANDS = np.array([200.0, 300.0])
ELBOW = np.array([200.0, 245.0])  # straight above the hands: a shaft pointing up lies over the forearm
TORSO = 100.0


def _record(index: int):
    pixels = {"right_wrist": HANDS.copy(), "right_elbow": ELBOW.copy(),
              "hip_mid": np.array([150.0, 330.0]), "shoulder_mid": np.array([150.0, 230.0])}
    return SimpleNamespace(index=index, timestamp_ms=index * 1000.0 / 30.0, width=W, height=H,
                           points=SimpleNamespace(pixels=pixels, landmarks={}))


def _image(shaft_angle: float | None = None, *, length: float = 220.0, background: bool = False) -> np.ndarray:
    rng = np.random.default_rng(5)
    image = np.clip(150 + rng.integers(-4, 5, size=(H, W, 1)), 0, 255).astype(np.uint8).repeat(3, axis=2)
    if background:  # a thin static line crossing near the hands (fence, horizon)
        cv2.line(image, (0, 330), (W - 1, 330), (40, 40, 40), 2, cv2.LINE_AA)
    if shaft_angle is not None:
        tip = HANDS + unit_from_angle(shaft_angle) * length
        cv2.line(image, tuple(int(v) for v in HANDS), tuple(int(v) for v in tip), (40, 40, 40), 2, cv2.LINE_AA)
    return image


def _anchor(index: int, angle: float) -> FrameShaft:
    return FrameShaft(index, index * 1000.0 / 30.0, "detected", LEVEL_HIGH, 0.9, angle_deg=angle,
                      grip=HANDS.copy(), tip=HANDS + unit_from_angle(angle) * 200)


def _gap(index: int) -> FrameShaft:
    return FrameShaft(index, index * 1000.0 / 30.0, "unavailable", LEVEL_UNAVAILABLE, 0.0,
                      reason="only rejected lines were found (runs along a forearm)")


def test_shaft_over_the_forearm_is_rejected_by_default_and_kept_when_it_passes_the_elbow() -> None:
    tracker = ClassicalClubTracker()
    image = _image(90.0)
    default = tracker.detect_frame(image, _record(0), torso_px=TORSO)
    assert not any(abs(c.angle_deg - 90.0) < 5 for c in default.candidates)
    assert any(abs(c.angle_deg - 90.0) < 5 and "forearm" in c.rejected for c in default.rejected)
    exempt = tracker.detect_frame(image, _record(0), torso_px=TORSO, shaft_over_forearm_torso=0.3)
    assert any(abs(c.angle_deg - 90.0) < 5 for c in exempt.candidates)


def test_a_line_that_stops_at_the_elbow_is_still_the_forearm() -> None:
    tracker = ClassicalClubTracker(ClubConfig(min_visible_length=0.2))
    image = _image(90.0, length=58.0)  # ends at the elbow
    exempt = tracker.detect_frame(image, _record(0), torso_px=TORSO, shaft_over_forearm_torso=0.3)
    assert not any(abs(c.angle_deg - 90.0) < 5 for c in exempt.candidates)


def test_passes_elbow_measures_along_the_line() -> None:
    from swingguru.club.tracker import ShaftCandidate

    candidate = ShaftCandidate(HANDS, HANDS + [0.0, -220.0], 90.0, 10.0, 220.0, 1.0, 1.0, 1.0)
    assert passes_elbow(candidate, ELBOW, 30.0)
    assert not passes_elbow(candidate, ELBOW, 200.0)


def _recover(images: dict[int, np.ndarray], track_frames: list[FrameShaft]):
    tracker = ClassicalClubTracker()
    track = ClubTrack(tuple(track_frames), (), tracker.name, tracker.config)
    records = [_record(i) for i in images]
    return recover_track(tracker, track, images, records, torso_px=TORSO)


def test_gap_frames_are_recovered_from_their_own_image_evidence_and_capped_below_high() -> None:
    angles = {0: 80.0, 1: 85.0, 2: 90.0, 3: 95.0, 4: 100.0}
    images = {i: _image(a) for i, a in angles.items()}
    refined, log = _recover(images, [_anchor(0, 80.0), _gap(1), _gap(2), _gap(3), _anchor(4, 100.0)])
    for index in (1, 2, 3):
        shaft = refined.at(index)
        assert shaft.status == STATUS_RECOVERED
        assert shaft.angle_deg == pytest.approx(angles[index], abs=3.0)
        assert shaft.level != LEVEL_HIGH and shaft.confidence <= TemporalConfig().confidence_cap
    assert refined.at(0) is refined.frames[0] and refined.at(0).status == "detected"  # anchors untouched
    assert log and log[0]["from"] == 0 and log[0]["to"] == 4


def test_nothing_is_invented_where_the_image_has_no_club() -> None:
    images = {0: _image(80.0), 1: _image(None), 2: _image(100.0)}
    refined, _ = _recover(images, [_anchor(0, 80.0), _gap(1), _anchor(2, 100.0)])
    assert not refined.at(1).available  # no interpolation


def test_a_background_line_present_in_the_anchor_frames_is_not_the_club() -> None:
    images = {0: _image(80.0, background=True), 1: _image(None, background=True), 2: _image(100.0, background=True)}
    refined, log = _recover(images, [_anchor(0, 80.0), _gap(1), _anchor(2, 100.0)])
    assert not refined.at(1).available
    frame = log[0]["frames"][1]
    assert not any(abs(abs(a) - 0) < 10 or abs(abs(a) - 180) < 10 for a in frame["candidates"])


def test_long_gaps_and_confident_frames_are_left_alone() -> None:
    frames = [_anchor(0, 80.0)] + [_gap(i) for i in range(1, 11)] + [_anchor(11, 100.0)]
    images = {i: _image(90.0) for i in range(12)}
    refined, log = _recover(images, frames)
    assert log == [] and all(not refined.at(i).available for i in range(1, 11))
