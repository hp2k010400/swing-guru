"""Structured run diagnostics: honest categories, no invented values, no leaks."""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field

import pytest

from swingguru import diagnostics as diag
from swingguru import progress
from swingguru.analysis_runner import ERRORS


def _analysis(*, phases=6, guided=True, signals=None, hidden=None) -> dict:
    rows = []
    for index in range(phases):
        checks = [{"kind": "torso_angle", "guided": guided, "level": "HIGH"}] if index == 0 else []
        rows.append({"phase": "address", "frame_index": index, "time_s": 0.1, "source": "estimated", "checks": checks})
    return {"phases": rows, "hidden": hidden or [],
            "camera_check": {"signals": {name: {"status": status} for name, status in (signals or {}).items()}},
            "engine": {"club_tracker": "classical-ridge-v1",
                       "club_windows": {"mode": "event-windows", "candidate_cache": {"hits": 3}},
                       "counters": {"owlv2_frames_run": 0, "owlv2_frames_cached": 2},
                       "club_fallback": [], "pose_cache": {"status": "hit"}}}


def test_every_public_error_maps_to_a_real_category() -> None:
    expected = {"unreadable": "decode", "too_short": "insufficient_frames", "too_long": "other",
                "too_large": "other", "upload_missing": "other", "no_clear_swing": "window",
                "no_swing": "window", "no_setup": "phases", "camera_angle": "camera", "framing": "framing",
                "too_far": "framing", "timeout": "other", "failed": "other"}
    for code in ERRORS:
        block = diag.failure_block(error=code, detail="internal detail")
        assert block is not None, code
        assert block["category"] == expected[code], code
        assert block["category"] in diag.CATEGORIES
        assert block["public_code"] == code
        assert block["stage"] in dict(progress.STAGES)


def test_no_swing_distinguishes_person_phases_and_confidence() -> None:
    no_person = {"value": 0.0, "detected_frames": 0}
    pose = diag.failure_block(error="no_swing", analysis=_analysis(), pose=no_person)
    # Zero retained poses is not proof that nobody was in frame.
    assert pose["category"] == "pose" and pose["stage"] == "positions"
    assert "no usable pose was retained" in pose["observed"]
    # Without an analysis at all the window stage is what stopped the run.
    window = diag.failure_block(error="no_swing", window={"reason": "no swing-shaped hand movement found"})
    assert window["category"] == "window" and window["stage"] == "moments"


def test_internal_stage_attributes_the_real_boundary() -> None:
    rows = [{"id": "probe", "state": "done"}, {"id": "window", "state": "done"},
            {"id": "cut", "state": "pending"}, {"id": "pose", "state": "pending"},
            {"id": "phase_detection", "state": "pending"}, {"id": "club_tracking", "state": "active"},
            {"id": "camera", "state": "pending"}, {"id": "render", "state": "pending"}]
    block = diag.failure_block(error="failed", internal_rows=rows)
    assert block["internal_stage"] == "club_tracking" and block["category"] == "club" and block["stage"] == "club"
    # Nothing in progress: name the last completed boundary, do not invent one.
    stopped = [{"id": "probe", "state": "done"}, {"id": "window", "state": "done"},
               {"id": "cut", "state": "pending"}]
    block = diag.failure_block(error="failed", stage_rows=[{"id": "club", "state": "active"}], internal_rows=stopped)
    assert block["internal_stage"] == "window" and block["stage"] == "club"
    assert "after the window stage" in block["observed"]


def test_phase_confidence_keeps_moment_and_check_confidence_apart() -> None:
    block = diag.phase_confidence(_analysis())
    assert block["moment_confidence"]["value"] is None
    assert "does not retain per-phase moment confidence" in block["moment_confidence"]["unavailable"]
    assert block["check_confidence_levels"] == {"HIGH": 1}
    assert "check_levels" not in block, "check confidence must not be labelled as phase confidence"


def test_club_source_is_never_invented() -> None:
    analysis = _analysis()
    analysis["phases"][0]["checks"] = [{"kind": "shaft", "guided": False, "level": "HIGH"}]
    block = diag.club_coverage(analysis)
    assert block["sources"]["address"] is None and block["source_unknown_phases"] == ["address"]
    short = diag.failure_block(error="no_swing", analysis=_analysis(phases=3))
    assert short["category"] == "phases" and "3 of 6" in short["observed"]
    unguided = diag.failure_block(error="no_swing", analysis=_analysis(guided=False))
    assert unguided["category"] == "confidence"


def test_camera_and_framing_name_the_signals_that_failed() -> None:
    block = diag.failure_block(error="camera_angle", analysis=_analysis(signals={"orientation_width": "INVALID"}))
    assert block["category"] == "camera" and "orientation_width" in block["observed"]
    block = diag.failure_block(error="too_far", analysis=_analysis(signals={"golfer_size": "INVALID"}))
    assert block["category"] == "framing" and "golfer_size" in block["observed"]


def test_inferred_cause_is_only_reported_when_the_engine_said_so() -> None:
    assert diag.failure_block(error="failed", detail="internal detail")["inferred_cause"] is None
    block = diag.failure_block(error="failed", detail="VideoOpenError: could not open video for reading")
    assert block["inferred_cause"] == "the decoder could not open the clip"


def test_engine_failure_stage_comes_from_the_recorded_progress(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.DONE)
    progress.record(tmp_path, "moments", progress.DONE)
    progress.record(tmp_path, "club", progress.ACTIVE)
    block = diag.failure_block(error="failed", stage_rows=progress.stage_rows(tmp_path))
    assert block["stage"] == "club" and block["category"] == "club"


def test_person_confidence_is_never_substituted_with_a_pose_score() -> None:
    block = diag.person_confidence({"phases": []})
    assert block["value"] is None and "person-detector" in block["unavailable"]


@dataclass
class _Landmark:
    name: str
    visibility: float
    presence: float


@dataclass
class _Points:
    landmarks: dict = field(default_factory=dict)


@dataclass
class _Record:
    index: int
    detected: bool
    landmarks: dict

    @property
    def pose_detected(self) -> bool:
        return self.detected

    @property
    def points(self) -> _Points:
        return _Points(landmarks=self.landmarks)


def test_pose_coverage_is_measured_from_the_retained_records(tmp_path) -> None:
    records = []
    for index in range(4):
        landmarks = {name: _Landmark(name, 0.9 if index < 2 else 0.1, 0.9 if index < 2 else 0.1)
                     for name in diag.COVERAGE_LANDMARKS}
        records.append(_Record(index, detected=index < 3, landmarks=landmarks))
    path = tmp_path / "pose_records.pkl"
    path.write_bytes(pickle.dumps(records))
    coverage = diag.pose_coverage(path)
    assert coverage["detected_frame_coverage"] == 0.75
    assert coverage["reliable_landmark_coverage"] == 0.5, "reliable coverage is all keypoints per frame"
    assert coverage["detected_frames"] == 3 and coverage["frames"] == 4
    assert coverage["landmark_presence"]["left_wrist"] == 0.5
    assert coverage["unavailable"] is None


def test_missing_pose_records_report_an_unavailable_reason(tmp_path) -> None:
    coverage = diag.pose_coverage(tmp_path / "nothing.pkl")
    assert coverage["detected_frame_coverage"] is None and "no pose records" in coverage["unavailable"]


def test_pose_coverage_reads_the_keyed_cache_format(tmp_path) -> None:
    """The runner's ``--records`` file is a keyed cache, not a bare list."""

    from swingguru.pose_cache import PoseCacheIdentity, save_pose_cache

    records = [_Record(index, detected=True,
                       landmarks={name: _Landmark(name, 0.9, 0.9) for name in diag.COVERAGE_LANDMARKS})
               for index in range(3)]
    path = tmp_path / "pose_records.pkl"
    save_pose_cache(path, records, identity=PoseCacheIdentity(video="x", provider="rtmw"))
    coverage = diag.pose_coverage(path)
    assert coverage["detected_frame_coverage"] == 1.0 and coverage["reliable_landmark_coverage"] == 1.0
    assert coverage["detected_frames"] == 3
    assert coverage["unavailable"] is None


def test_diagnostics_sanitize_paths_urls_and_credentials() -> None:
    dirty = ("failed at C:\\Users\\harry\\swing.mp4 and /home/user/secret.mp4 "
             "see https://x.example/y?token=abc123 sig=deadbeef token=zzz")
    clean = diag.sanitize(dirty)
    assert "C:\\Users" not in clean and "/home/user" not in clean
    assert "https://x.example" not in clean
    assert "deadbeef" not in clean and "zzz" not in clean and "abc123" not in clean
    assert "<path>" in clean and "<url>" in clean


def test_sanitize_handles_spaced_paths_and_authorization_headers() -> None:
    dirty = ("work=C:\\Users\\harry phillips\\swing guru\\var\\run\\upload.mp4 "
             "home=/Users/harry phillips/secret clip.mp4 tmp=/tmp/a b/c.mp4 "
             "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def "
             "client_secret: hunter2 cookie=session%3Dxyz")
    clean = diag.sanitize(dirty)
    assert "harry phillips" not in clean and "swing guru" not in clean
    assert "/Users" not in clean and "/tmp" not in clean
    assert "eyJhbGciOiJIUzI1NiJ9" not in clean and "hunter2" not in clean and "xyz" not in clean
    assert "<path>" in clean and "<redacted>" in clean


def test_a_bearer_header_is_redacted_on_its_own() -> None:
    clean = diag.sanitize("Authorization: Bearer abc123def456ghi header was rejected")
    assert clean.startswith("Bearer <redacted>")
    assert "abc123def456ghi" not in clean
    assert "deadbeef" not in diag.sanitize("token=deadbeef&user=1")


def test_strip_paths_removes_a_known_spaced_root_exactly() -> None:
    root = "C:\\Users\\harry phillips.GOLFCLUBS4CASH\\swing guru"
    clean = diag.sanitize(f"failed opening {root}\\var\\run\\upload.mp4", strip_paths=(root,))
    assert root not in clean and "upload.mp4" not in clean


def test_sanitize_never_raises_on_odd_input() -> None:
    assert diag.sanitize(None) == ""
    assert diag.sanitize(b"bytes") == "b'bytes'"
    assert isinstance(diag.sanitize({"a": 1}), str)


def test_video_block_never_invents_geometry() -> None:
    block = diag.video_block(None)
    assert all(block[key] is None for key in ("width", "height", "fps", "frames", "duration_s"))
    assert "could not be probed" in block["unavailable"]


def test_build_records_only_measured_timings_and_stages(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.DONE)
    record = diag.build(ok=False, error="no_clear_swing", detail="", work=tmp_path,
                        window={"reason": "1 swing-shaped event(s) but none with a still set-up"},
                        timings={"probe_s": 0.02, "locate_swing_s": 1.5}, analysis_version="engine-test")
    assert record["schema"] == diag.SCHEMA and record["outcome"] == "failed"
    assert record["public_code"] == "no_clear_swing"
    assert record["timings"]["probe_s"] == 0.02 and record["timings"]["locate_swing_s"] == 1.5
    assert "total_s" not in record["timings"], "an unmeasured total must stay unknown"
    assert record["failure"]["stage"] == "moments"
    assert [row["id"] for row in record["stages"]] == [key for key, _ in progress.STAGES]
    assert record["stages"][0]["state"] == "done"
    assert record["signals"]["phase_confidence"]["value"] is None


def test_successful_record_has_no_failure_block(tmp_path) -> None:
    record = diag.build(ok=True, error=None, work=tmp_path, analysis=_analysis(), timings={})
    assert record["outcome"] == "ok" and record["failure"] is None and record["public_code"] is None
    assert record["signals"]["phase_confidence"]["located"] == 6


def test_failure_evidence_separates_confirmed_from_inferred() -> None:
    from swingguru import progress

    rows_done = [{"id": "prepare", "state": progress.DONE}, {"id": "moments", "state": "pending"}]
    # An explicit rejection rule: the stage is confirmed.
    rejected = diag.failure_block(error="camera_angle", analysis={"camera_check": {"signals": {}}}, stage_rows=rows_done)
    assert rejected["evidence"] == {"stage": "confirmed", "category": "confirmed", "cause": "none"}
    # The engine stopped while a boundary was active: stage and category are confirmed.
    active = diag.failure_block(error="failed", stage_rows=rows_done,
                                internal_rows=[{"id": "pose", "state": "active"}])
    assert active["evidence"]["stage"] == "confirmed" and active["evidence"]["category"] == "confirmed"
    # Stopped between boundaries: the category is only a deduction.
    between = diag.failure_block(error="failed", stage_rows=rows_done,
                                 internal_rows=[{"id": "window", "state": "done"}])
    assert between["evidence"]["category"] == "inferred"
    # No progress evidence at all: nothing is claimed.
    blind = diag.failure_block(error="failed")
    assert blind["evidence"] == {"stage": "unknown", "category": "unknown", "cause": "none"}
    # A timeout names where it stopped but never a cause category.
    timeout = diag.failure_block(error="timeout", detail="engine timed out", stage_rows=rows_done)
    assert timeout["evidence"]["category"] == "unknown" and timeout["evidence"]["cause"] == "engine_message"
