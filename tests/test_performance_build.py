"""Build-level regressions for the event-window performance path.

These tests use the real entry points in ``scripts/build_analysis.py`` (the
same ones the operator runs) rather than the library alone, so they pin the
behaviour the acceptance guard depends on:

* every analysis moment is inside a merged window and is analysed;
* the OWLv2 fallback still triggers for exactly the moments the classical
  tracker cannot report - and only there;
* a replay/rebuild reuses the cached OWLv2 boxes and never reruns the model.
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from swingguru.analysis_bundle import ANALYSIS_PHASES
from swingguru.auto_moments import apply_manual_overrides, estimate_moments
from swingguru.club import ClassicalClubTracker, ClubConfig
from swingguru.club.fallback import BoxCandidate, fallback_shaft
from swingguru.club.geometry import unit_from_angle
from swingguru.club.metrics import reportable
from swingguru.club.tracker import ClubTrack, HandsContext
from swingguru.club.windows import frames_in
from swingguru.measurements import MeasurementConfig
from swingguru.phases import ManualPhaseProvider, Phase
from swingguru.pipeline import PipelineConfig, process_video

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import build_analysis  # noqa: E402  (scripts/build_analysis.py)

W, H = 240, 360
HANDS = np.array([120.0, 180.0])
TORSO = 60.0


def _record(index: int, *, hands=HANDS) -> SimpleNamespace:
    pixels = {
        "right_wrist": np.asarray(hands, float),
        "hip_mid": np.array([90.0, 200.0]),
        "shoulder_mid": np.array([90.0, 140.0]),
    }
    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=W,
        height=H,
        points=SimpleNamespace(pixels=pixels, landmarks={}),
    )


def _moments(frames: dict) -> SimpleNamespace:
    mapping = {phase: frames.get(phase) for phase in ANALYSIS_PHASES}
    return SimpleNamespace(frames=lambda: mapping)


def _canvas(index: int = 0, angle: float = 45.0) -> np.ndarray:
    rng = np.random.default_rng(17 + index)
    image = np.full((H, W, 3), 150, np.uint8)
    noise = rng.integers(-4, 5, size=(H, W, 1))
    image = np.clip(image.astype(int) + noise, 0, 255).astype(np.uint8)
    tip = HANDS + unit_from_angle(angle) * 150.0
    cv2.line(image, tuple(int(v) for v in HANDS), tuple(int(v) for v in tip), (40,) * 3, 2, cv2.LINE_AA)
    return image


PHASES = {
    ANALYSIS_PHASES[0]: 10,
    ANALYSIS_PHASES[1]: 20,
    ANALYSIS_PHASES[2]: 25,
    ANALYSIS_PHASES[3]: 35,
    ANALYSIS_PHASES[4]: 45,
    ANALYSIS_PHASES[5]: 50,
}


# ---------------------------------------------------------------------------
# Windows cover every moment
# ---------------------------------------------------------------------------


def test_club_windows_cover_every_analysis_moment() -> None:
    records = [_record(index) for index in range(60)]
    windows = build_analysis.club_windows(_moments(PHASES), records, 3)
    covered = set(frames_in(windows))
    for phase, frame in PHASES.items():
        assert frame in covered, f"{phase.value} frame {frame} is not inside any window"


def test_windowed_tracking_analyses_every_moment_frame() -> None:
    records = [_record(index) for index in range(60)]
    windows = build_analysis.club_windows(_moments(PHASES), records, 3)
    frames = {index: _canvas(index) for index in frames_in(windows)}
    track = ClassicalClubTracker(ClubConfig()).track_windows(frames, records, windows)
    analysed = {detection.frame_index for detection in track.detections}
    for frame in PHASES.values():
        assert track.at(frame) is not None, f"moment frame {frame} was not analysed"
        assert frame in analysed


def test_windows_stay_disjoint_when_the_moments_are_far_apart() -> None:
    records = [_record(index) for index in range(120)]
    far = {phase: 10 + 18 * position for position, phase in enumerate(ANALYSIS_PHASES)}
    windows = build_analysis.club_windows(_moments(far), records, 3)
    assert len(windows) == len(ANALYSIS_PHASES)
    for before, after in zip(windows, windows[1:]):
        assert after.start > before.end + 1
    assert far[ANALYSIS_PHASES[0]] == 10


def test_close_moments_merge_into_one_window() -> None:
    records = [_record(index) for index in range(60)]
    close = {phase: 20 + 2 * position for position, phase in enumerate(ANALYSIS_PHASES)}
    windows = build_analysis.club_windows(_moments(close), records, 3)
    assert len(windows) == 1
    assert windows[0].start == 17 and windows[0].end == 33


# ---------------------------------------------------------------------------
# Fallback triggering
# ---------------------------------------------------------------------------


class _StubOwl:
    """Records how often the model would have been asked for boxes."""

    instances: list["_StubOwl"] = []

    def __init__(self, boxes: list[list[float]] | None = None) -> None:
        self.load_seconds = 0.0
        self.calls: list[tuple[int, int]] = []
        self.boxes_payload = boxes
        _StubOwl.instances.append(self)

    def boxes(self, frame_bgr, hands, torso_px):
        self.calls.append((int(frame_bgr.shape[0]), int(frame_bgr.shape[1])))
        payload = self.boxes_payload
        if payload is None:
            tip = np.asarray(hands, float) + unit_from_angle(45.0) * 1.4 * torso_px
            # An axis-aligned box around the shaft segment, so the corner
            # farthest from the hands is the club-head end.
            payload = [[
                float(min(hands[0], tip[0])),
                float(min(hands[1], tip[1])),
                float(max(hands[0], tip[0])),
                float(max(hands[1], tip[1])),
                0.72,
            ]]
        return [BoxCandidate(tuple(item[:4]), float(item[4])) for item in payload]


def _track(levels: dict[int, str], *, config: ClubConfig | None = None) -> ClubTrack:
    settings = config or ClubConfig()
    frames = []
    for frame_index, level in sorted(levels.items()):
        frames.append(
            SimpleNamespace(
                frame_index=frame_index,
                timestamp_ms=frame_index * 1000.0 / 30.0,
                status="unavailable" if level == "UNAVAILABLE" else "detected",
                level=level,
                confidence=0.2 if level == "UNAVAILABLE" else 0.5,
                angle_deg=None,
                grip=None,
                tip=None,
                method="",
                interpolated=False,
                reason="",
                available=level != "UNAVAILABLE",
            )
        )
    return ClubTrack(tuple(frames), (), "test", settings)


def _video_file(tmp_path: Path) -> Path:
    """A stand-in input path; ``run_fallback`` only hashes its bytes."""

    path = tmp_path / "clip.mp4"
    if not path.exists():
        path.write_bytes(b"stand-in clip bytes")
    return path


def test_fallback_runs_only_for_moments_the_classical_tracker_cannot_report(tmp_path, monkeypatch) -> None:
    moments = _moments(PHASES)
    records = [_record(index) for index in range(60)]
    frames = {frame: _canvas(index) for index, frame in enumerate(PHASES.values())}
    levels = {frame: "HIGH" for frame in PHASES.values()}
    levels[PHASES[ANALYSIS_PHASES[2]]] = "LOW"
    levels[PHASES[ANALYSIS_PHASES[3]]] = "UNAVAILABLE"
    track = _track(levels)

    _StubOwl.instances.clear()
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    timings: dict = {}
    overrides, log = build_analysis.run_fallback(
        _video_file(tmp_path), tmp_path, records, frames, moments, track, timings
    )

    owl = _StubOwl.instances[-1]
    assert len(owl.calls) == 2, "the fallback must run once per unreportable moment"
    assert timings["owlv2_frames_run"] == 2
    assert timings["owlv2_frames_cached"] == 0
    assert [entry["phase"] for entry in log] == ["mid_back", "top"]
    assert set(overrides) == {PHASES[ANALYSIS_PHASES[2]], PHASES[ANALYSIS_PHASES[3]]}
    assert (tmp_path / "club_fallback.json").exists()


def test_fallback_does_not_run_when_the_classical_shaft_is_reportable(tmp_path, monkeypatch) -> None:
    moments = _moments(PHASES)
    records = [_record(index) for index in range(60)]
    frames = {frame: _canvas(index) for index, frame in enumerate(PHASES.values())}
    track = _track({frame: "HIGH" for frame in PHASES.values()})

    _StubOwl.instances.clear()
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    overrides, log = build_analysis.run_fallback(
        _video_file(tmp_path), tmp_path, records, frames, moments, track, {}
    )
    assert overrides == {} and log == []
    assert _StubOwl.instances[-1].calls == []


def test_a_frame_that_was_not_decoded_is_reported_instead_of_crashing(tmp_path, monkeypatch) -> None:
    moments = _moments({ANALYSIS_PHASES[0]: 10})
    records = [_record(index) for index in range(20)]
    track = _track({10: "LOW"})

    _StubOwl.instances.clear()
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    overrides, log = build_analysis.run_fallback(
        _video_file(tmp_path), tmp_path, records, {}, moments, track, {}
    )

    assert overrides == {}
    assert log and log[0]["reason"].startswith("the frame was not decoded")
    assert _StubOwl.instances[-1].calls == []


def test_replay_reuses_cached_owlv2_boxes_and_never_calls_the_model(tmp_path, monkeypatch) -> None:
    moments = _moments(PHASES)
    records = [_record(index) for index in range(60)]
    frames = {frame: _canvas(index) for index, frame in enumerate(PHASES.values())}
    levels = {frame: "LOW" for frame in PHASES.values()}
    track = _track(levels)

    class _ExplodingOwl:
        def __init__(self) -> None:
            self.load_seconds = 0.0
            self.calls = 0

        def boxes(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("OWLv2 must not run when the boxes are already cached")

    # First run writes the cache with the stub; the replay must not need it.
    _StubOwl.instances.clear()
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    build_analysis.run_fallback(_video_file(tmp_path), tmp_path, records, frames, moments, track, {})
    written = json.loads((tmp_path / "club_fallback.json").read_text(encoding="utf-8"))
    assert written, "the first run should have cached boxes for every unreportable moment"

    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _ExplodingOwl)
    timings: dict = {}
    overrides, log = build_analysis.run_fallback(
        _video_file(tmp_path), tmp_path, records, frames, moments, track, timings
    )

    assert timings["owlv2_frames_run"] == 0
    assert timings["owlv2_frames_cached"] == len(ANALYSIS_PHASES)
    assert all(entry["cached"] is True for entry in log)
    assert len(overrides) == len(ANALYSIS_PHASES)


def test_missing_hands_means_no_model_call_and_no_override(tmp_path, monkeypatch) -> None:
    moments = _moments({ANALYSIS_PHASES[2]: 25})
    records = []
    for index in range(30):
        record = _record(index)
        if index == 25:
            record.points.pixels = {}
        records.append(record)
    frames = {25: _canvas(25)}
    track = _track({25: "LOW"})

    _StubOwl.instances.clear()
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    overrides, log = build_analysis.run_fallback(
        _video_file(tmp_path), tmp_path, records, frames, moments, track, {}
    )
    assert overrides == {}
    assert _StubOwl.instances[-1].calls == []
    assert log[0]["level"] == "UNAVAILABLE"


def test_default_window_radius_matches_the_documented_value() -> None:
    assert build_analysis.DEFAULT_CLUB_WINDOW == 5


def test_open_candidate_cache_only_applies_to_the_windowed_pass(tmp_path) -> None:
    identity = build_analysis.CandidateIdentity("t", "v", "p", "c", "r")
    assert build_analysis.open_candidate_cache(
        tmp_path, identity, club_cache=True, full_sequence=True
    ) is None
    assert build_analysis.open_candidate_cache(
        tmp_path, identity, club_cache=False, full_sequence=False
    ) is None
    assert build_analysis.open_candidate_cache(
        tmp_path, identity, club_cache=True, full_sequence=False
    ) is not None


def _records_for(fixture_video, scripted_provider) -> list:
    result = process_video(
        fixture_video,
        fixture_video.parent / "pose",
        config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
        pose_provider=scripted_provider,
        phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
    )
    return list(result.records)


def _moments_with_frames(records) -> object:
    """Real moment objects placed on frames of the synthetic clip."""

    base = estimate_moments(records)
    return apply_manual_overrides(
        base,
        {
            Phase.ADDRESS: 3,
            Phase.TAKEAWAY: 6,
            Phase.MID_BACK: 9,
            Phase.TOP: 12,
            Phase.DOWNSWING: 14,
            Phase.IMPACT: 16,
        },
    )


def test_full_sequence_build_never_touches_the_candidate_cache(tmp_path, fixture_video, scripted_provider, monkeypatch) -> None:
    records = _records_for(fixture_video, scripted_provider)
    cache_path = tmp_path / "records.pkl"
    cache_path.write_bytes(pickle.dumps(records))
    out = tmp_path / "bundle"
    out.mkdir()
    sentinel = '{"version": 1, "identity": {"sentinel": true}, "frames": {}}'
    (out / "club_candidates.json").write_text(sentinel, encoding="utf-8")

    monkeypatch.setattr(build_analysis, "estimate_moments", lambda arg: _moments_with_frames(arg))
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    analysis = build_analysis.build(
        fixture_video,
        out,
        pose="mediapipe",
        cache=cache_path,
        full_sequence=True,
        club_cache=True,
    )

    assert (out / "club_candidates.json").read_text(encoding="utf-8") == sentinel
    engine = analysis["engine"]
    assert engine["club_windows"]["mode"] == "full-sequence"
    assert engine["club_windows"]["candidate_cache"] is None
    counters = engine["counters"]
    assert counters["club_frames_analysed"] == len(records)
    assert counters["club_frames_detected"] == counters["club_frames_analysed"]
    assert counters["club_frames_from_cache"] == 0


def test_windowed_build_writes_and_then_reuses_the_candidate_cache(tmp_path, fixture_video, scripted_provider, monkeypatch) -> None:
    records = _records_for(fixture_video, scripted_provider)
    cache_path = tmp_path / "records.pkl"
    cache_path.write_bytes(pickle.dumps(records))
    out = tmp_path / "bundle"

    monkeypatch.setattr(build_analysis, "estimate_moments", lambda arg: _moments_with_frames(arg))
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _StubOwl)
    first = build_analysis.build(fixture_video, out, pose="mediapipe", cache=cache_path, club_cache=True)

    assert (out / "club_candidates.json").exists()
    counters = first["engine"]["counters"]
    assert counters["club_frames_analysed"] > 0
    assert counters["club_frames_detected"] == counters["club_frames_analysed"]
    assert counters["club_frames_from_cache"] == 0
    stats = first["engine"]["club_windows"]["candidate_cache"]
    assert stats["misses"] == counters["club_frames_analysed"]
    assert stats["entries"] == counters["club_frames_analysed"]
    assert first["engine"]["club_windows"]["mode"] == "event-windows"

    second = build_analysis.build(fixture_video, out, pose="mediapipe", cache=cache_path, club_cache=True)
    again = second["engine"]["counters"]
    assert again["club_frames_from_cache"] == again["club_frames_analysed"]
    assert again["club_frames_detected"] == 0
    assert second["engine"]["club_windows"]["candidate_cache"]["hits"] == again["club_frames_analysed"]


def test_hands_context_and_fallback_shaft_agree_on_a_box_leaving_the_hands() -> None:
    hands = HandsContext(HANDS.copy(), TORSO, source="landmarks")
    image = _canvas(0, 45.0)
    box = BoxCandidate((120.0, 180.0, 200.0, 280.0), 0.8)
    shaft, diagnostics = fallback_shaft(image, 5, 166.0, hands, _track({5: "LOW"}), [box])
    assert shaft.available
    assert shaft.level == "MEDIUM"
    assert reportable(shaft)
    assert diagnostics["method"] == "owlv2-fallback"
    assert np.linalg.norm(shaft.tip - shaft.grip) == pytest.approx(
        float(np.linalg.norm(np.asarray([200.0, 280.0]) - HANDS)), rel=1e-6
    )
