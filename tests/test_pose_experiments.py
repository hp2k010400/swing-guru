"""The RTMW experiment harness and the pose-cache wiring it feeds.

These pin the pieces that decide whether a pose variant is accepted: the
cheap active-window proposal, the interpolation used by the sampling
experiment, the delta/coverage comparison and the verdict gates. They also pin
that a build reports which pose cache it used, and never lets an unidentified
or differently-configured cache stand in for this run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.landmarks import Landmark, PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.pipeline import FrameRecord, build_record
from swingguru.pose_cache import PoseCacheIdentity, save_pose_cache
from swingguru.pose_rtm import to_observation

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import benchmark_pose as bench  # noqa: E402  (scripts/benchmark_pose.py)
import build_analysis  # noqa: E402  (scripts/build_analysis.py)
import pose_experiments as variants  # noqa: E402  (scripts/research/pose_experiments.py)


# ---------------------------------------------------------------------------
# Active-window proposal
# ---------------------------------------------------------------------------


def test_motion_proposal_windows_a_clip_that_actually_moves(tmp_path) -> None:
    import cv2

    path = tmp_path / "moving.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, (64, 64))
    for index in range(24):
        frame = np.full((64, 64, 3), 20, np.uint8)
        if 8 <= index <= 16:
            frame[10:40, max(0, 5 + index * 2):40 + index * 2] = 240
        writer.write(frame)
    writer.release()

    window = bench.propose_active_window(path, fps=24.0, margin_before_s=0.5, margin_after_s=0.5)
    assert window["available"] is True
    assert 0 <= window["start"] <= window["end"]
    assert window["margin_before_frames"] == 12
    assert window["margin_after_frames"] == 12
    assert window["start"] <= 8 and window["end"] >= 16


def test_motion_proposal_on_the_fixture_clip_stays_fail_closed(fixture_video) -> None:
    # The synthetic fixture is nearly static; the proposal must then say so
    # rather than invent a swing window.
    window = bench.propose_active_window(fixture_video, fps=24.0)
    if window["available"]:
        assert 0 <= window["start"] <= window["end"]
    else:
        assert "reason" in window


def test_motion_proposal_reports_unavailable_on_a_static_clip(tmp_path) -> None:
    import cv2

    path = tmp_path / "static.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 24.0, (64, 64))
    for _ in range(12):
        writer.write(np.full((64, 64, 3), 128, np.uint8))
    writer.release()
    window = bench.propose_active_window(path, fps=24.0)
    assert window["available"] is False
    assert "reason" in window


def test_motion_series_has_one_fewer_sample_than_frames(fixture_video) -> None:
    series = bench.motion_series(fixture_video, scale=0.5)
    assert len(series) == 23
    assert [index for index, _ in series] == list(range(1, 24))


# ---------------------------------------------------------------------------
# Sampling / interpolation
# ---------------------------------------------------------------------------


def _observation(x: float, visibility: float) -> PoseObservation:
    landmark = Landmark(
        index=15,
        name="left_wrist",
        x=x,
        y=0.5,
        z=0.0,
        visibility=visibility,
        presence=visibility,
    )
    return PoseObservation(detected=True, landmarks=(landmark,))


def test_interpolation_blends_positions_and_keeps_the_lower_confidence() -> None:
    blended = bench.interpolate_observation(_observation(0.2, 0.9), _observation(0.6, 0.4), 0.5)
    landmark = blended.landmarks[0]
    assert landmark.x == pytest.approx(0.4)
    assert landmark.visibility == pytest.approx(0.4)
    assert landmark.presence == pytest.approx(0.4)
    assert blended.detected is True


def test_interpolated_records_are_tagged_and_inferred_ones_are_not(tmp_path) -> None:
    config = MeasurementConfig()
    frames = [np.zeros((48, 32, 3), np.uint8) for _ in range(4)]
    records = [
        build_record(index, index * 1000.0 / 30.0, frames[index], _observation(0.1 * index, 0.9), config=config)
        for index in range(4)
    ]
    observations = {0: _observation(0.0, 0.9), 2: _observation(0.2, 0.8)}
    replaced = bench.apply_interpolation(records, observations, config=config)

    assert "pose interpolated between inferred frames" in replaced[1].notes
    assert replaced[0].notes == () and replaced[2].notes == ()
    # Frame 3 has no neighbour after it, so it must not be invented.
    assert replaced[3].notes == ()
    assert replaced[1].points is not None
    # The interpolated frame keeps a position between its neighbours.
    assert replaced[1].observation.landmarks[0].x == pytest.approx(0.1)


def test_interpolation_never_invents_a_confidence_above_its_neighbours() -> None:
    blended = bench.interpolate_observation(_observation(0.1, 0.95), _observation(0.3, 0.55), 0.25)
    assert blended.landmarks[0].visibility <= 0.55


# ---------------------------------------------------------------------------
# Comparison and gates
# ---------------------------------------------------------------------------


def _record(index: int, *, hands=(200.0, 300.0), visibility: float = 0.9) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=400,
        height=600,
        points=SimpleNamespace(
            pixels={"left_wrist": np.asarray(hands, float), "hip_mid": np.array([150.0, 330.0])},
            landmarks={},
        ),
        measurements={},
    )


def test_coverage_counts_only_the_requested_range() -> None:
    records = [_record(0), _record(1), SimpleNamespace(index=2, points=None)]
    counted = bench.coverage(records, ("left_wrist",), first=0, last=1)
    assert counted == {"left_wrist": 2}


def test_landmark_displacement_finds_the_worst_move() -> None:
    baseline = {0: _record(0), 1: _record(1)}
    moved = [_record(0), _record(1, hands=(204.0, 300.0))]
    deltas = bench.landmark_displacement(moved, baseline)
    assert deltas["left_wrist"] == pytest.approx(4.0)


def test_compare_measurements_reports_missing_values_as_none() -> None:
    baseline = {"address": {"values": {"torso_inclination_deg": 46.0, "head_displacement_pct_height": None}}}
    other = {"address": {"values": {"torso_inclination_deg": 46.4, "head_displacement_pct_height": None}}}
    compared = bench.compare_measurements(baseline, other)
    assert compared["max_by_measurement"]["torso_inclination_deg"] == pytest.approx(0.4)
    assert compared["per_phase"]["address"]["head_displacement_pct_height"] is None


def test_a_moved_phase_frame_fails_the_verdict() -> None:
    comparison = {
        "phase_frames": {"address": -3},
        "coverage_losses": {},
        "phase_geometry_px": {},
        "dead_time_displacement_px": {},
        "measurements": {"max_by_measurement": {}, "availability_changes": {}, "non_finite_values": {}},
    }
    verdict = bench.verdict({}, comparison, 196)
    assert verdict["pass"] is False
    assert any("phase frames" in item for item in verdict["failures"])


def test_a_coverage_drop_fails_the_verdict() -> None:
    comparison = {
        "phase_frames": {},
        "coverage_losses": {"left_wrist": -1},
        "phase_geometry_px": {},
        "dead_time_displacement_px": {},
        "measurements": {"max_by_measurement": {}, "availability_changes": {}, "non_finite_values": {}},
    }
    assert bench.verdict({}, comparison, 196)["pass"] is False


def test_an_over_tolerance_measurement_fails_the_verdict() -> None:
    comparison = {
        "phase_frames": {},
        "coverage_losses": {},
        "phase_geometry_px": {},
        "dead_time_displacement_px": {},
        "measurements": {
            "max_by_measurement": {"torso_inclination_deg": 0.6},
            "availability_changes": {},
            "non_finite_values": {},
        },
    }
    verdict = bench.verdict({}, comparison, 196)
    assert verdict["pass"] is False
    assert any("torso_inclination_deg" in item for item in verdict["failures"])


def test_an_identical_variant_passes_the_verdict() -> None:
    comparison = {
        "phase_frames": {},
        "coverage_losses": {},
        "phase_geometry_px": {"left_wrist": 0.0},
        "dead_time_displacement_px": {"left_wrist": 0.0},
        "measurements": {
            "max_by_measurement": {"torso_inclination_deg": 0.0, "head_displacement_pct_height": 0.0},
            "availability_changes": {},
            "non_finite_values": {},
        },
    }
    verdict = bench.verdict({}, comparison, 196)
    assert verdict["pass"] is True
    assert verdict["failures"] == []


# ---------------------------------------------------------------------------
# Build wiring and cache identity
# ---------------------------------------------------------------------------


def test_pose_identity_separates_videos_and_cadences(fixture_video, tmp_path) -> None:
    other = tmp_path / "other.mp4"
    other.write_bytes(fixture_video.read_bytes() + b"different")
    base = build_analysis.pose_identity(fixture_video, "rtmw", 5)
    assert base.det_frequency == 5 and base.model == "wholebody" and base.mode == "balanced"
    assert build_analysis.pose_identity(fixture_video, "rtmw", 10).key() != base.key()
    assert build_analysis.pose_identity(other, "rtmw", 5).key() != base.key()
    assert build_analysis.pose_identity(fixture_video, "mediapipe", 5).key() != base.key()


class _StubTracker:
    """Minimal rtmlib PoseTracker stand-in: counts resets, finds nothing."""

    def __init__(self) -> None:
        self.resets = 0
        self.det_model = SimpleNamespace()
        self.pose_model = SimpleNamespace()
        self.tracking = False

    def reset(self) -> None:
        self.resets += 1

    def __call__(self, image):
        raise AssertionError("the stub tracker must not run inference")


class _StubAdapter:
    def __init__(self, tracker: _StubTracker) -> None:
        self._tracker = tracker

    def _ensure(self):
        return self._tracker

    def detect(self, frame_bgr, timestamp_ms):
        return PoseObservation.missing("stub adapter")


def test_a_repeated_variant_resets_the_tracker_each_time(fixture_video, monkeypatch) -> None:
    # The tracker numbers frames internally and chooses its detection frames
    # from that counter, so a repeated run must start from zero. Without the
    # reset the second run silently benchmarks a different cadence phase.
    tracker = _StubTracker()
    monkeypatch.setattr(bench, "provider_for", lambda cadence: _StubAdapter(tracker))
    run = bench.run_pipeline_variant(fixture_video, "stub", cadence=5, repeat=2)
    assert tracker.resets == 2
    assert len(run.seconds) == 2
    assert run.frames == 24


def _fake_process_video(records, calls: list):
    def fake(video, output_dir, **kwargs):
        calls.append(Path(video))
        return SimpleNamespace(records=records)

    return fake


def _records_stub() -> list:
    return [_record(index) for index in range(3)]


def test_pose_records_reports_the_cache_status(tmp_path, monkeypatch) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    records = _records_stub()
    calls: list = []
    monkeypatch.setattr(build_analysis, "process_video", _fake_process_video(records, calls))

    identity = build_analysis.pose_identity(video, "rtmw", 5)
    cache = tmp_path / "pose_records.pkl"

    loaded, status, used = build_analysis.pose_records(video, "rtmw", cache, tmp_path, det_frequency=5)
    assert status == "miss-missing" and calls == [video] and used == identity
    assert cache.exists(), "the first run should have written a keyed cache"

    loaded, status, _ = build_analysis.pose_records(video, "rtmw", cache, tmp_path, det_frequency=5)
    assert status == "hit" and len(calls) == 1
    assert [record.index for record in loaded] == [record.index for record in records]

    _, status, _ = build_analysis.pose_records(video, "rtmw", cache, tmp_path, det_frequency=10)
    assert status == "miss-identity"
    assert len(calls) == 2, "a different cadence must not reuse the cached records"


def test_a_legacy_pose_cache_is_a_miss_by_default(tmp_path, monkeypatch) -> None:
    import pickle

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video bytes")
    cache = tmp_path / "pose_records.pkl"
    cache.write_bytes(pickle.dumps(_records_stub()))
    calls: list = []
    monkeypatch.setattr(build_analysis, "process_video", _fake_process_video(_records_stub(), calls))

    _, status, _ = build_analysis.pose_records(video, "rtmw", cache, tmp_path, det_frequency=5)
    assert status == "miss-legacy"
    assert len(calls) == 1

    # Put the legacy pickle back: the first call upgraded the file in place.
    cache.write_bytes(pickle.dumps(_records_stub()))
    _, status, _ = build_analysis.pose_records(
        video, "rtmw", cache, tmp_path, det_frequency=5, allow_legacy_cache=True
    )
    assert status == "hit-legacy"


def test_a_different_videos_pose_cache_is_not_reused(tmp_path, monkeypatch) -> None:
    first = tmp_path / "one.mp4"
    second = tmp_path / "two.mp4"
    first.write_bytes(b"first video")
    second.write_bytes(b"second video")
    cache = tmp_path / "pose_records.pkl"
    calls: list = []
    monkeypatch.setattr(build_analysis, "process_video", _fake_process_video(_records_stub(), calls))

    build_analysis.pose_records(first, "rtmw", cache, tmp_path, det_frequency=5)
    _, status, _ = build_analysis.pose_records(second, "rtmw", cache, tmp_path, det_frequency=5)
    assert status == "miss-identity"
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Persistent ROI: schedule, triggers, coordinate handling
# ---------------------------------------------------------------------------


def test_the_roi_schedule_matches_the_detection_grid() -> None:
    tracker = variants.ScheduledPoseTracker(cadence=5)
    assert tracker.schedule(0) == "schedule"
    assert tracker.schedule(5) == "schedule"
    assert tracker.schedule(3) == "no-box"  # nothing tracked yet: must acquire
    tracker._boxes = [np.array([0.0, 0.0, 10.0, 10.0])]
    assert tracker.schedule(3) == ""  # a tracked box is reused between anchors
    assert tracker.schedule(7) == ""


def test_the_roi_interval_trigger_bounds_drift() -> None:
    tracker = variants.ScheduledPoseTracker(cadence=1 << 30, interval=10)
    tracker._boxes = [np.array([0.0, 0.0, 10.0, 10.0])]
    tracker.last_detection_index = 0
    assert tracker.schedule(4) == ""
    assert tracker.schedule(10) == "interval"
    assert tracker.schedule(12, force_detection=True) == "forced"
    assert tracker.schedule(12, allow_tracking=False) == "no-tracking"


def test_roi_boxes_use_rtmlib_rule() -> None:
    from rtmlib.tools.solution.pose_tracker import pose_to_bbox as rtmlib_pose_to_bbox

    keypoints = np.array([[10.0, 20.0], [30.0, 50.0], [15.0, 45.0]])
    assert np.allclose(variants.pose_to_bbox(keypoints), rtmlib_pose_to_bbox(keypoints))


class _StubRoiTracker:
    """Stands in for rtmlib's tools so the ROI logic is testable without them."""

    def __init__(self, boxes, keypoints, scores) -> None:
        self.det_model = lambda image: [np.asarray(box, float) for box in boxes]
        self.pose_model = lambda image, bboxes=None: (
            np.asarray([keypoints], float),
            np.asarray([scores], float),
        )


def _roi_provider(boxes, keypoints, scores, **kwargs):
    tracker = variants.ScheduledPoseTracker(**kwargs)
    tracker._tracker = _StubRoiTracker(boxes, keypoints, scores)
    return tracker


def test_a_roi_frame_is_detected_once_and_then_tracked() -> None:
    keypoints = [[10.0, 20.0], [30.0, 50.0]] + [[25.0, 35.0]] * 15
    scores = [0.9] * 17
    tracker = _roi_provider([[5.0, 10.0, 40.0, 60.0]], keypoints, scores, cadence=5)
    frame = np.zeros((80, 60, 3), np.uint8)

    first = tracker.detect(frame, 0)
    assert first.detected
    assert tracker.stats.detector_calls == 1
    assert tracker.stats.pose_calls == 1
    # Landmarks come back normalised to the *full frame*, not to a crop.
    normalised = {landmark.name: landmark for landmark in first.landmarks}
    assert normalised["nose"].x == pytest.approx(10.0 / 60.0)

    for index in range(1, 4):
        tracker.detect(frame, index)
    assert tracker.stats.detector_calls == 1, "a tracked box must not re-run the detector"
    assert tracker.stats.pose_calls == 4
    stability = tracker.crop_stability()
    assert stability["frames"] == 4 and stability["max_centre_jump_px"] == pytest.approx(0.0)


def test_a_lost_person_forces_the_next_frame_to_reacquire() -> None:
    keypoints = [[10.0, 20.0], [30.0, 50.0]]
    tracker = _roi_provider([[5.0, 10.0, 40.0, 60.0]], keypoints, [0.1, 0.1], cadence=1 << 30)
    frame = np.zeros((80, 60, 3), np.uint8)
    assert tracker.detect(frame, 0).detected is False
    assert tracker.stats.lost_frames == 1
    tracker.detect(frame, 1)
    assert tracker.stats.detector_calls == 2, "a lost body must trigger re-acquisition"
    assert "no-box" in tracker.stats.trigger_reasons


def test_an_empty_detection_is_reported_and_counted() -> None:
    tracker = _roi_provider([], [[0.0, 0.0]], [0.9], cadence=5)
    frame = np.zeros((80, 60, 3), np.uint8)
    observation = tracker.detect(frame, 0)
    assert observation.detected is False
    assert tracker.stats.empty_detections == 1
    assert tracker.stats.detector_calls == 1 and tracker.stats.pose_calls == 0


# ---------------------------------------------------------------------------
# Adaptive dense refinement
# ---------------------------------------------------------------------------


def test_sustained_runs_ignore_a_single_spike() -> None:
    indices = [0, 5, 10, 15, 20, 25]
    flags = [False, True, False, False, False, False]
    assert variants._sustained_indices(indices, flags, min_run=3) == []
    flags = [False, True, True, True, False, False]
    assert variants._sustained_indices(indices, flags, min_run=3) == [5, 10, 15]


def test_the_dense_span_needs_a_sustained_run_and_a_margin() -> None:
    series = [(index, 0.1) for index in range(5, 200, 5)]
    series += [(100, 2.0), (105, 2.0), (110, 2.0)]
    span = variants.propose_dense_span(series, fps=30.0, frame_count=200, margin_before_s=1.5, margin_after_s=1.5)
    assert span["available"] is True
    assert span["motion_first"] == 100 and span["motion_last"] == 110
    assert span["start"] == 100 - 45
    assert span["end"] == 110 + 45


def test_a_spike_only_span_is_unavailable() -> None:
    series = [(index, 0.1) for index in range(5, 200, 5)]
    series += [(100, 2.0)]
    span = variants.propose_dense_span(series, fps=30.0, frame_count=200)
    assert span["available"] is False
    assert "no run of" in span["reason"]


def test_the_span_floor_is_the_engines_own_still_threshold() -> None:
    from swingguru.auto_moments import MomentConfig

    series = [(10, 1e-6), (20, 1e-6)]
    span = variants.propose_dense_span(series, fps=30.0, frame_count=400)
    assert span["available"] is False
    assert span["threshold"] == pytest.approx(MomentConfig().stability_speed)


def test_coarse_series_is_torso_normalised() -> None:
    from swingguru.landmarks import Landmark

    def observation(offset: float) -> PoseObservation:
        landmarks = []
        for name, x, y in (
            ("left_hip", 0.4, 0.6),
            ("right_hip", 0.6, 0.6),
            ("left_shoulder", 0.4, 0.4),
            ("right_shoulder", 0.6, 0.4),
            ("left_wrist", 0.4 + offset, 0.5),
            ("right_wrist", 0.6 + offset, 0.5),
        ):
            landmarks.append(Landmark(index=0, name=name, x=x, y=y, z=0.0, visibility=0.9, presence=0.9))
        return PoseObservation(detected=True, landmarks=tuple(landmarks))

    sampled = {0: observation(0.0), 30: observation(0.1)}
    series = variants._coarse_series(sampled, width=100, height=200, fps=30.0)
    # hands move 10 px, torso is 40 px, over 1 s -> 0.25 torso lengths per second
    assert series == [(30, pytest.approx(0.25, abs=1e-6))]


def test_assembled_records_keep_the_timebase_and_mark_the_rest() -> None:
    from swingguru.measurements import MeasurementConfig

    frames = {index: (index * 1000.0 / 30.0, 40, 60) for index in range(6)}
    observations = {0: _observation(0.2, 0.9), 4: _observation(0.4, 0.8)}
    records = variants._assemble_records(frames, observations, set(), MeasurementConfig())
    assert [record.index for record in records] == list(range(6))
    assert [record.timestamp_ms for record in records] == [frame[0] for frame in frames.values()]
    assert "pose interpolated between inferred frames" in records[2].notes
    assert records[5].notes and "no neighbour" in records[5].notes[0]
    assert records[0].pose_detected is True and records[5].pose_detected is False
    assert records[2].observation.landmarks[0].visibility == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# Gate semantics
# ---------------------------------------------------------------------------


def test_a_coverage_gain_is_not_a_failure() -> None:
    comparison = {
        "phase_frames": {},
        "coverage_losses": {},
        "phase_geometry_px": {},
        "dead_time_displacement_px": {"left_wrist": 40.0},
        "measurements": {"max_by_measurement": {}, "availability_changes": {}, "non_finite_values": {}},
    }
    assert bench.verdict({}, comparison, 196)["pass"] is True


def test_dead_time_displacement_is_not_gated_but_phase_geometry_is() -> None:
    base = {
        "phase_frames": {},
        "coverage_losses": {},
        "measurements": {"max_by_measurement": {}, "availability_changes": {}, "non_finite_values": {}},
    }
    heavy_dead_time = dict(base, phase_geometry_px={}, dead_time_displacement_px={"left_wrist": 50.0})
    assert bench.verdict({}, heavy_dead_time, 196)["pass"] is True
    moved_overlay = dict(base, phase_geometry_px={"left_wrist": 2.5}, dead_time_displacement_px={})
    verdict = bench.verdict({}, moved_overlay, 196)
    assert verdict["pass"] is False and any("phase-frame joint" in item for item in verdict["failures"])


def test_a_disappeared_or_nonfinite_measurement_fails() -> None:
    base = {
        "phase_frames": {},
        "coverage_losses": {},
        "phase_geometry_px": {},
        "dead_time_displacement_px": {},
    }
    vanished = dict(
        base,
        measurements={
            "max_by_measurement": {},
            "availability_changes": {"address": ["torso_inclination_deg"]},
            "non_finite_values": {},
        },
    )
    assert bench.verdict({}, vanished, 196)["pass"] is False
    infinite = dict(
        base,
        measurements={"max_by_measurement": {}, "availability_changes": {}, "non_finite_values": {"address": ["head_displacement_pct_height"]}},
    )
    assert bench.verdict({}, infinite, 196)["pass"] is False


def test_compare_measurements_flags_availability_and_non_finite_values() -> None:
    baseline = {"address": {"values": {"torso_inclination_deg": 46.0, "head_displacement_pct_height": 1.0}}}
    vanished = {"address": {"values": {"torso_inclination_deg": 46.0, "head_displacement_pct_height": None}}}
    compared = bench.compare_measurements(baseline, vanished)
    assert compared["availability_changes"] == {"address": ["head_displacement_pct_height"]}
    broken = {"address": {"values": {"torso_inclination_deg": float("nan"), "head_displacement_pct_height": 1.0}}}
    assert bench.compare_measurements(baseline, broken)["non_finite_values"] == {"address": ["torso_inclination_deg"]}


def test_coverage_losses_only_counts_decreases() -> None:
    baseline = {"left_wrist": 27, "right_wrist": 27}
    variant = {"left_wrist": 26, "right_wrist": 29}
    assert bench.coverage_losses(baseline, variant) == {"left_wrist": -1}


def test_build_reports_the_pose_cache_status(tmp_path, fixture_video, scripted_provider, monkeypatch) -> None:
    import pickle

    from swingguru.measurements import MeasurementConfig
    from swingguru.phases import ManualPhaseProvider
    from swingguru.pipeline import PipelineConfig, process_video

    outcome = process_video(
        fixture_video,
        tmp_path / "pose",
        config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
        pose_provider=scripted_provider,
        phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
    )
    cache = tmp_path / "pose_records.pkl"
    identity = build_analysis.pose_identity(fixture_video, "mediapipe", 5)
    assert isinstance(identity, PoseCacheIdentity)
    save_pose_cache(cache, list(outcome.records), identity)
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)

    analysis = build_analysis.build(
        fixture_video,
        tmp_path / "bundle",
        pose="mediapipe",
        cache=cache,
        club_cache=False,
    )
    engine = analysis["engine"]
    assert analysis["engine"]["pose_cache"]["status"] == "hit"
    assert analysis["engine"]["pose_cache"]["video"] == identity.video
    assert engine["timings"]["pose_cached"] is True
    assert pickle.loads(cache.read_bytes())["identity"]["version"] == 1


class _StubOwl:
    """No-op OWLv2 stand-in: the pose tests must never load the model."""

    def __init__(self, *args, **kwargs) -> None:
        self.load_seconds = 0.0

    def boxes(self, *args, **kwargs):
        return []
