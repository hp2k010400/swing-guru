"""Deterministic tests for the experimental club / shaft tracker (Engine v0.3).

Synthetic frames contain a known thin shaft leaving known hands, plus the
distractors that break naive line detection: a strong static background line,
a silhouette step edge, forearm and thigh lines and a line nowhere near the
hands. The real-swing validation lives in the developer report, not here.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from swingguru.club import ClassicalClubTracker, ClubConfig
from swingguru.club.geometry import (
    allowed_rotation_deg,
    angle_difference,
    direction_angle_deg,
    overlaps_forearm,
    point_line_distance,
    unit_from_angle,
    wrap_deg,
)
from swingguru.club.metrics import (
    club_top_estimate,
    path_turn_deg,
    reportable,
    unwrap_angles,
)
from swingguru.club.tracker import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_UNAVAILABLE,
    NOISE_FLOOR_SCORE,
    STATUS_INTERPOLATED,
    ClubTrack,
    FrameDetection,
    FrameShaft,
    HandsContext,
    ReferenceFrame,
    ShaftCandidate,
    along_body_segment,
    candidate_margin,
    confidence_level,
    frame_confidence,
    hessian_maps,
    longest_supported_run,
    select_track,
)

W, H = 400, 600
HANDS = np.array([200.0, 300.0])
TORSO = 100.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record(index: int = 0, *, hands=HANDS, extra: dict | None = None):
    pixels = {
        "right_wrist": np.asarray(hands, float),
        "hip_mid": np.array([150.0, 330.0]),
        "shoulder_mid": np.array([150.0, 230.0]),
    }
    pixels.update(extra or {})
    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=W,
        height=H,
        points=SimpleNamespace(pixels=pixels, landmarks={}),
    )


def _canvas(value: int = 150) -> np.ndarray:
    rng = np.random.default_rng(3)
    image = np.full((H, W, 3), value, np.uint8)
    noise = rng.integers(-4, 5, size=(H, W, 1))
    return np.clip(image.astype(int) + noise, 0, 255).astype(np.uint8)


def _shaft(image: np.ndarray, angle_deg: float, *, start=HANDS, length=220.0, colour=40, width=2) -> np.ndarray:
    tip = np.asarray(start, float) + unit_from_angle(angle_deg) * length
    cv2.line(image, tuple(int(v) for v in start), tuple(int(v) for v in tip), (colour,) * 3, width, cv2.LINE_AA)
    return image


def _detect(image, record=None, **kwargs):
    tracker = ClassicalClubTracker(kwargs.pop("config", None))
    return tracker.detect_frame(image, record or _record(), torso_px=TORSO, **kwargs)


def _shaft_frame(index, angle, level=LEVEL_HIGH, confidence=0.9, interpolated=False, available=True):
    if not available:
        return FrameShaft(index, index * 33.3, "unavailable", LEVEL_UNAVAILABLE, 0.0)
    grip = HANDS.copy()
    return FrameShaft(
        index,
        index * 33.3,
        "interpolated" if interpolated else "detected",
        level,
        confidence,
        angle_deg=angle,
        grip=grip,
        tip=grip + unit_from_angle(angle) * 200,
        interpolated=interpolated,
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_screen_angle_convention_is_y_up() -> None:
    assert direction_angle_deg((1, 0)) == pytest.approx(0.0)
    assert direction_angle_deg((0, -1)) == pytest.approx(90.0)  # up the screen
    assert direction_angle_deg((0, 1)) == pytest.approx(-90.0)
    assert direction_angle_deg((-1, 0)) == pytest.approx(180.0)
    for angle in (-170.0, -45.0, 0.0, 33.0, 120.0):
        assert direction_angle_deg(unit_from_angle(angle)) == pytest.approx(angle)
    with pytest.raises(ValueError):
        direction_angle_deg((0, 0))


def test_angle_wrapping_and_signed_difference() -> None:
    assert wrap_deg(190.0) == pytest.approx(-170.0)
    assert wrap_deg(-180.0) == pytest.approx(180.0)
    assert angle_difference(-170.0, 170.0) == pytest.approx(20.0)
    assert angle_difference(170.0, -170.0) == pytest.approx(-20.0)
    assert unwrap_angles([170.0, -175.0, -160.0]) == pytest.approx([170.0, 185.0, 200.0])


def test_point_line_distance_and_forearm_overlap() -> None:
    assert point_line_distance((0, 5), (0, 0), (10, 0)) == pytest.approx(5.0)
    wrist, elbow = np.array([200.0, 300.0]), np.array([200.0, 240.0])  # elbow straight above
    assert overlaps_forearm(90.0, wrist, elbow, tolerance_deg=20.0)
    assert overlaps_forearm(80.0, wrist, elbow, tolerance_deg=20.0)
    assert not overlaps_forearm(-90.0, wrist, elbow, tolerance_deg=20.0)  # away from the elbow
    assert not overlaps_forearm(90.0, wrist, None, tolerance_deg=20.0)


def test_rotation_allowance_grows_with_hand_speed_and_is_capped() -> None:
    slow = allowed_rotation_deg(0.0, base_deg=30.0, per_torso_deg=150.0, cap_deg=100.0)
    fast = allowed_rotation_deg(0.3, base_deg=30.0, per_torso_deg=150.0, cap_deg=100.0)
    huge = allowed_rotation_deg(5.0, base_deg=30.0, per_torso_deg=150.0, cap_deg=100.0)
    missing = allowed_rotation_deg(None, base_deg=30.0, per_torso_deg=150.0, cap_deg=100.0)
    assert slow == pytest.approx(30.0) and missing == pytest.approx(30.0)
    assert fast == pytest.approx(75.0)
    assert huge == pytest.approx(100.0)


def test_longest_supported_run_tolerates_short_gaps_only() -> None:
    hits = np.array([0, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1, 1], bool)
    assert longest_supported_run(hits, gap_samples=1, max_start=5) == (1, 5, 4)
    assert longest_supported_run(hits, gap_samples=0, max_start=5) == (1, 2, 2)
    # A run that starts too far from the hands does not count.
    late = np.array([0, 0, 0, 0, 0, 1, 1, 1], bool)
    assert longest_supported_run(late, gap_samples=1, max_start=3) is None
    assert longest_supported_run(np.zeros(5, bool), gap_samples=1, max_start=3) is None


# ---------------------------------------------------------------------------
# Line selection on synthetic frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("angle", [-60.0, -160.0, 135.0, 100.0])
def test_a_thin_shaft_leaving_the_hands_is_found(angle: float) -> None:
    detection = _detect(_shaft(_canvas(), angle))
    assert detection.candidates, detection.reason
    best = detection.candidates[0]
    assert abs(angle_difference(best.angle_deg, angle)) <= 3.0
    assert np.linalg.norm(best.grip - HANDS) <= 0.2 * TORSO


def test_a_strong_line_that_misses_the_hands_is_not_a_candidate() -> None:
    image = _canvas()
    # A long, dark, perfectly straight fence line well away from the hands.
    cv2.line(image, (0, 120), (W - 1, 120), (20, 20, 20), 3, cv2.LINE_AA)
    detection = _detect(image)
    # Nothing may lie along the fence: every candidate must start at the hands
    # and must not run along y = 120.
    for candidate in detection.candidates:
        assert np.linalg.norm(candidate.grip - HANDS) <= 0.7 * TORSO
        samples = [candidate.grip + (candidate.tip - candidate.grip) * t for t in np.linspace(0.3, 1, 8)]
        on_fence = sum(abs(point[1] - 120) < 6 for point in samples)
        assert on_fence < 4, candidate


def test_the_shaft_wins_over_a_background_line_through_the_same_area() -> None:
    image = _shaft(_canvas(), -60.0)
    cv2.line(image, (0, 310), (W - 1, 290), (30, 30, 30), 3, cv2.LINE_AA)
    references = []
    for index, hands in enumerate(([60.0, 100.0], [340.0, 100.0], [60.0, 520.0], [340.0, 520.0], [60.0, 300.0])):
        clean = _canvas()
        cv2.line(clean, (0, 310), (W - 1, 290), (30, 30, 30), 3, cv2.LINE_AA)
        gray = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY)
        references.append(ReferenceFrame(100 + index, np.array(hands), hessian_maps(gray, (1.0, 2.0))))
    detection = _detect(image, references=references)
    assert detection.candidates
    assert abs(angle_difference(detection.candidates[0].angle_deg, -60.0)) <= 3.0
    static = [c for c in detection.rejected if "static background" in c.rejected]
    assert static, "the horizon-like line through the hands must be recognised as static"


def test_a_silhouette_edge_is_not_mistaken_for_a_shaft() -> None:
    image = _canvas(200)
    # Dark "body" filling everything left of a line that runs down from the hands.
    polygon = np.array([[0, 0], [200, 300], [230, H - 1], [0, H - 1]], np.int32)
    cv2.fillPoly(image, [polygon], (40, 40, 40), cv2.LINE_AA)
    detection = _detect(image)
    for candidate in detection.candidates:
        assert abs(angle_difference(candidate.angle_deg, direction_angle_deg((30, 299)))) > 8.0, candidate


def test_lines_along_the_forearm_and_thigh_are_rejected() -> None:
    elbow = HANDS + np.array([0.0, -70.0])
    image = _canvas()
    cv2.line(image, tuple(int(v) for v in HANDS), tuple(int(v) for v in elbow), (40, 40, 40), 2, cv2.LINE_AA)
    record = _record(extra={"right_elbow": elbow})
    detection = _detect(image, record)
    assert not any(abs(angle_difference(c.angle_deg, 90.0)) < 10 for c in detection.candidates)
    assert any("forearm" in c.rejected for c in detection.rejected)

    hip, knee = np.array([190.0, 290.0]), np.array([205.0, 440.0])
    segments = (("thigh", hip, knee),)
    assert (
        along_body_segment(HANDS, knee + [0, 10], 10.0, segments, torso_px=TORSO,
                           tolerance_deg=20, distance=0.12, overlap=0.5)
        == "thigh"
    )
    assert (
        along_body_segment(HANDS, HANDS + [-180, 60], 10.0, segments, torso_px=TORSO,
                           tolerance_deg=20, distance=0.12, overlap=0.5)
        == ""
    )


def test_missing_hands_means_unavailable_with_a_reason() -> None:
    record = _record()
    record.points.pixels.pop("right_wrist")
    detection = _detect(_shaft(_canvas(), -60.0), record)
    assert detection.candidates == ()
    assert "hands" in detection.reason


def test_a_blank_frame_finds_nothing() -> None:
    detection = _detect(_canvas())
    assert not detection.candidates


# ---------------------------------------------------------------------------
# Temporal selection, interpolation and confidence
# ---------------------------------------------------------------------------


def _candidate(angle: float, score: float) -> ShaftCandidate:
    return ShaftCandidate(
        grip=HANDS.copy(),
        tip=HANDS + unit_from_angle(angle) * 200,
        angle_deg=angle,
        visible_start_px=15.0,
        length_px=200.0,
        support=0.9,
        strength=2.0,
        score=score,
        corroborated=True,
    )


def _detection(index: int, candidates, hands=HANDS) -> FrameDetection:
    context = HandsContext(np.asarray(hands, float), TORSO, source="landmarks")
    return FrameDetection(index, index * 1000.0 / 30.0, context, tuple(candidates))


def test_tracking_does_not_teleport_to_a_one_frame_distractor() -> None:
    detections = []
    for index in range(10):
        candidates = [_candidate(-60.0 - index, 1.2)]
        if index == 5:
            candidates.append(_candidate(90.0, 1.8))  # stronger, but 150 degrees away
        detections.append(_detection(index, candidates))
    track = select_track(detections, config=ClubConfig())
    assert all(abs(angle_difference(frame.angle_deg, -60.0 - i)) < 1 for i, frame in enumerate(track.frames))


def test_a_missing_frame_stays_unavailable_and_short_gaps_are_marked_interpolated() -> None:
    detections = [_detection(i, [_candidate(-60.0 - 2 * i, 1.3)]) for i in range(8)]
    detections[4] = _detection(4, [])
    track = select_track(detections, config=ClubConfig())
    frame = track.frames[4]
    assert frame.status == STATUS_INTERPOLATED and frame.interpolated
    assert frame.level == LEVEL_LOW
    assert abs(angle_difference(frame.angle_deg, -68.0)) < 1.5
    assert not reportable(frame)


def test_long_gaps_are_not_interpolated() -> None:
    detections = [_detection(i, [_candidate(-60.0, 1.3)]) for i in range(12)]
    for index in (4, 5, 6, 7):
        detections[index] = _detection(index, [])
    track = select_track(detections, config=ClubConfig())
    assert all(not track.frames[index].available for index in (4, 5, 6, 7))


def test_confidence_rewards_evidence_and_penalises_a_lone_faint_line() -> None:
    assert candidate_margin(1.5, []) == pytest.approx(1 - NOISE_FLOOR_SCORE / 1.5)
    assert candidate_margin(0.35, []) < 0.2
    assert candidate_margin(1.0, [0.9]) == pytest.approx(0.1)
    strong, _ = frame_confidence(support=0.95, length_torso=1.5, margin=0.8, temporal=1.0, corroborated=True, score=1.5)
    faint, _ = frame_confidence(support=0.5, length_torso=0.5, margin=0.1, temporal=None, corroborated=False, score=0.35)
    assert strong > 0.85 and faint < 0.5
    config = ClubConfig()
    assert confidence_level(strong, config) == LEVEL_HIGH
    assert confidence_level(0.55, config) == LEVEL_MEDIUM
    assert confidence_level(0.4, config) == LEVEL_LOW
    assert confidence_level(0.1, config) == LEVEL_UNAVAILABLE


# ---------------------------------------------------------------------------
# Raw metrics
# ---------------------------------------------------------------------------


def _track(frames) -> ClubTrack:
    return ClubTrack(tuple(frames), (), "test", ClubConfig())


def test_path_turn_follows_rotation_beyond_180_degrees() -> None:
    angles = [-60, -100, -140, -180, 140, 100]  # clockwise on screen, 200 degrees in total
    track = _track([_shaft_frame(i, float(a)) for i, a in enumerate(angles)])
    turn, _ = path_turn_deg(track, 0, 5)
    assert turn == pytest.approx(-200.0)


def test_path_turn_refuses_to_cross_a_long_gap_or_a_weak_end() -> None:
    frames = [_shaft_frame(i, -60.0 - i) for i in range(10)]
    for index in (3, 4, 5, 6, 7):
        frames[index] = _shaft_frame(index, 0.0, available=False)
    turn, note = path_turn_deg(_track(frames), 0, 9)
    assert turn is None and "cannot be followed" in note
    frames = [_shaft_frame(i, -60.0) for i in range(4)]
    frames[3] = _shaft_frame(3, -60.0, level=LEVEL_LOW, confidence=0.4)
    assert path_turn_deg(_track(frames), 0, 3)[0] is None


def test_club_top_is_the_last_frame_before_the_rotation_reverses() -> None:
    angles = [150, 140, 128, 118, 110, 106, 110, 118, 126]
    track = _track([_shaft_frame(100 + i, float(a)) for i, a in enumerate(angles)])
    result = club_top_estimate(track, start_frame=100, end_frame=108)
    assert result["frame"] == 105
    thin = _track([_shaft_frame(100 + i, float(a)) for i, a in enumerate(angles[:3])])
    assert club_top_estimate(thin, start_frame=100, end_frame=108)["frame"] is None
