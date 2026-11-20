"""Runner-level progress and diagnostics: real boundaries, retained failure timings.

The engine subprocess is replaced by a small deterministic stand-in here, so
these are plumbing fixtures, not accuracy evidence - the outcomes they assert
are about the runner's own stage and diagnostics contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from swingguru import analysis_runner, diagnostics as diag, progress
from swingguru.synthetic import SyntheticClip, make_synthetic_clip
from swingguru.swing_window import SINGLE_SWING, WindowDecision


@pytest.fixture()
def fixture_video(tmp_path) -> Path:
    """A decodable clip longer than the 1.5 s minimum the runner enforces."""

    return make_synthetic_clip(tmp_path / "long.mp4", clip=SyntheticClip(width=320, height=180, fps=24.0,
                                                                        frame_count=60))


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _decision(upload_seconds: float = 3.0, *, status: str = SINGLE_SWING, confidence: str = "HIGH",
              trimmed: bool = False) -> WindowDecision:
    return WindowDecision(status=status, duration_s=upload_seconds, start_s=0.0, end_s=upload_seconds,
                          trimmed=trimmed, confidence=confidence, reason="one complete swing", chosen=None,
                          candidates=[], scene_cuts=[])


def _patch_window(monkeypatch, decision: WindowDecision | None = None) -> None:
    import swingguru.swing_window as window

    monkeypatch.setattr(window, "locate_swing", lambda video, config=None: (decision or _decision(), {"fps": 24.0}))


def _analysis(*, phases: int = 6, guided: bool = True, signals: dict | None = None) -> dict:
    rows = []
    for index in range(phases):
        rows.append({"phase": "address" if index == 0 else "impact", "frame_index": index, "time_s": 0.1,
                     "source": "estimated",
                     "checks": [{"kind": "torso_angle", "guided": guided, "level": "HIGH"}] if index == 0 else []})
    return {"video": "swing.mp4", "phases": rows, "hidden": [],
            "camera_check": {"signals": {name: {"status": status} for name, status in (signals or {}).items()}}}


def _engine_fake(*, returncode: int = 0, analysis: dict | None = None, stages: list[tuple[str, str]] = (),
                 internal: list[tuple[str, str]] = ()):
    """A stand-in for ``scripts/build_analysis.py`` that honours --out and --progress."""

    def run(command, **kwargs):
        out = Path(command[command.index("--out") + 1])
        progress_dir = Path(command[command.index("--progress") + 1])
        for stage, state in internal:
            progress.record(progress_dir, stage, state, source="engine", internal=True)
        for stage, state in stages:
            progress.record(progress_dir, stage, state, source="engine")
        if returncode == 0:
            out.mkdir(parents=True, exist_ok=True)
            (out / "analysis.json").write_text(json.dumps(analysis or _analysis()), encoding="utf-8")
        return _Completed(returncode=returncode, stdout="engine output", stderr="engine stderr")

    return run


def test_bad_media_is_a_decode_failure_with_timings(tmp_path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not a video" * 200)
    result = analysis_runner.run_analysis(bad, tmp_path / "work")
    assert not result.ok and result.error == "unreadable"
    assert result.timings["probe_s"] >= 0 and "total_s" in result.timings
    assert result.diagnostics["failure"]["category"] == "decode"
    assert result.diagnostics["failure"]["stage"] == "prepare"
    assert result.diagnostics["failure"]["internal_stage"] == "probe"


@pytest.fixture()
def long_video(tmp_path) -> Path:
    """Longer than DEFER_WINDOW_MAX_S: the window finder's rejection still applies."""

    return make_synthetic_clip(tmp_path / "longer.mp4", clip=SyntheticClip(width=160, height=96, fps=24.0,
                                                                          frame_count=24 * 13))


def test_no_swing_stops_at_the_moments_stage(monkeypatch, long_video, tmp_path) -> None:
    _patch_window(monkeypatch, _decision(13.0, status="NO_SWING_FOUND", confidence="NONE"))
    result = analysis_runner.run_analysis(long_video, tmp_path / "work")
    assert not result.ok and result.error == "no_swing"
    assert result.timings["locate_swing_s"] >= 0 and "total_s" in result.timings
    assert result.diagnostics["failure"]["stage"] == "moments"
    assert result.diagnostics["failure"]["internal_stage"] == "window"
    rows = progress.stage_rows(tmp_path / "work")
    assert [row["state"] for row in rows[:2]] == ["done", "active"]


def test_timeout_keeps_the_timings_it_measured(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr(analysis_runner.subprocess, "run", boom)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work", timeout_s=12)
    assert not result.ok and result.error == "timeout"
    assert result.timings["engine_process_s"] >= 0 and result.timings["total_s"] >= 0
    assert result.timings["locate_swing_s"] >= 0
    failure = result.diagnostics["failure"]
    assert failure["stage"] == "moments" and "exceeded" in failure["observed"]


def test_engine_failure_reports_the_stage_the_engine_reached(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)
    monkeypatch.setattr(analysis_runner.subprocess, "run",
                        _engine_fake(returncode=1, stages=[("moments", "done"), ("club", "active")],
                                     internal=[("pose", "done"), ("phase_detection", "done"),
                                               ("club_tracking", "active")]))
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "failed"
    failure = result.diagnostics["failure"]
    assert failure["stage"] == "club" and failure["category"] == "club"
    assert failure["internal_stage"] == "club_tracking"
    assert "engine stderr" in failure["observed"]
    assert result.timings["engine_process_s"] >= 0


def test_successful_run_records_a_full_stage_timeline(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)
    monkeypatch.setattr(analysis_runner.subprocess, "run", _engine_fake(
        stages=[("moments", "done"), ("club", "done"), ("positions", "done"), ("build", "done")],
        internal=[("pose", "active"), ("pose", "done"), ("phase_detection", "active"),
                  ("phase_detection", "done"), ("club_tracking", "active"), ("club_tracking", "done"),
                  ("camera", "active"), ("camera", "done"), ("render", "active"), ("render", "done")]))
    work = tmp_path / "work"
    seen: list[str | None] = []
    result = analysis_runner.run_analysis(fixture_video, work, on_prepared=seen.append)
    assert result.ok and seen == [None]
    record = result.diagnostics
    assert record["schema"] == diag.SCHEMA and record["outcome"] == "ok" and record["failure"] is None
    assert [row["state"] for row in record["stages"]] == ["done"] * len(progress.STAGES)
    internal = {row["id"]: row["state"] for row in record["internal_stages"]}
    # Every boundary the run really crossed is done; "cut" is pending because
    # this decision needed no trim, and nothing invents it.
    assert internal["pose"] == internal["phase_detection"] == internal["club_tracking"] == "done"
    assert internal["camera"] == internal["render"] == "done"
    assert internal["cut"] == "pending"
    assert internal["probe"] == internal["window"] == "done"
    assert record["timings"]["stage_durations_ms"]["pose"] is not None
    assert analysis_runner.stage_states(work, prepared=True, finished=True, running=False) == [
        {"id": key, "label": label, "state": "done"} for key, label in progress.STAGES]


def test_rejection_from_the_engine_keeps_the_analysis_evidence(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)
    analysis = _analysis(phases=2)
    monkeypatch.setattr(analysis_runner.subprocess, "run",
                        _engine_fake(analysis=analysis, stages=[("moments", "done"), ("club", "done"),
                                                                ("positions", "done"), ("build", "done")]))
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "no_swing"
    failure = result.diagnostics["failure"]
    assert failure["category"] == "phases" and "2 of 6" in failure["observed"]


def test_camera_rejection_names_the_signal(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)
    monkeypatch.setattr(analysis_runner.subprocess, "run",
                        _engine_fake(analysis=_analysis(signals={"full_body": "INVALID"}),
                                     stages=[("moments", "done"), ("club", "done"), ("positions", "done"),
                                             ("build", "done")]))
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert result.error == "framing"
    assert result.diagnostics["failure"]["category"] == "framing"
    assert "full_body" in result.diagnostics["failure"]["observed"]


def test_locate_swing_exception_is_a_failed_result_not_a_crash(monkeypatch, fixture_video, tmp_path) -> None:
    import swingguru.swing_window as window

    def boom(video, config=None):
        raise RuntimeError("sampling failed")

    monkeypatch.setattr(window, "locate_swing", boom)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "failed"
    assert result.timings["locate_swing_s"] >= 0 and "total_s" in result.timings
    failure = result.diagnostics["failure"]
    assert failure["internal_stage"] == "window" and failure["category"] == "window"


def test_cut_window_exception_keeps_its_timing_and_stage(monkeypatch, fixture_video, tmp_path) -> None:
    import swingguru.swing_window as window

    _patch_window(monkeypatch, _decision(trimmed=True))

    def boom(*args, **kwargs):
        raise RuntimeError("ffmpeg missing")

    monkeypatch.setattr(window, "cut_window", boom)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "failed"
    assert result.timings["cut_window_s"] >= 0 and result.timings["locate_swing_s"] >= 0
    assert result.diagnostics["failure"]["internal_stage"] == "cut"


def test_corrupt_analysis_json_is_reported_with_timings(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)

    def run(command, **kwargs):
        out = Path(command[command.index("--out") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / "analysis.json").write_text("{not json", encoding="utf-8")
        return _Completed(returncode=0)

    monkeypatch.setattr(analysis_runner.subprocess, "run", run)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "failed"
    assert "analysis.json unreadable" in result.diagnostics["failure"]["observed"]
    assert result.timings["engine_process_s"] >= 0 and result.timings["total_s"] >= 0


def test_a_failing_diagnostics_build_never_changes_the_outcome(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch)
    monkeypatch.setattr(analysis_runner.subprocess, "run", _engine_fake(
        stages=[("moments", "done"), ("club", "done"), ("positions", "done"), ("build", "done")]))

    def boom(**kwargs):
        raise RuntimeError("diagnostics exploded")

    monkeypatch.setattr(analysis_runner.diag, "build", boom)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert result.ok, "a diagnostics bug must never turn a good analysis into a failure"
    assert result.diagnostics["error"].startswith("diagnostics failed: RuntimeError")



# ---------------------------------------------------------------- tolerant discovery for short clips

@pytest.mark.parametrize("status,confidence", [("NO_SWING_FOUND", "NONE"), ("SINGLE_SWING", "LOW"), ("MULTIPLE_SWINGS", "LOW")])
def test_a_short_clip_with_an_inconclusive_window_goes_to_the_engine(monkeypatch, fixture_video, tmp_path,
                                                                     status, confidence) -> None:
    """A tightly trimmed swing (no 0.25 s still set-up) is not rejected by the cheap finder."""

    _patch_window(monkeypatch, _decision(2.5, status=status, confidence=confidence))
    calls = []
    fake = _engine_fake()

    def run(command, **kwargs):
        calls.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(analysis_runner.subprocess, "run", run)
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert calls, "the engine ran on the whole clip"
    assert str(fixture_video) in calls[0], "never a cut window for a deferred clip"
    assert result.ok and result.window["deferred_to_engine"] is True
    assert result.notice is None, "no 'analysed the clearest swing' notice for a whole-clip analysis"


def test_the_engine_gates_still_reject_a_deferred_clip(monkeypatch, fixture_video, tmp_path) -> None:
    """Measurements stay strict: too few phases from the engine is still 'no swing'."""

    _patch_window(monkeypatch, _decision(2.5, status="NO_SWING_FOUND", confidence="NONE"))
    monkeypatch.setattr(analysis_runner.subprocess, "run", _engine_fake(analysis=_analysis(phases=3)))
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "no_swing"
    assert result.window["deferred_to_engine"] is True


def test_a_long_clip_keeps_the_window_rejection(monkeypatch, long_video, tmp_path) -> None:
    _patch_window(monkeypatch, _decision(13.0, status="SINGLE_SWING", confidence="LOW"))
    monkeypatch.setattr(analysis_runner.subprocess, "run", lambda *a, **k: pytest.fail("engine must not run"))
    result = analysis_runner.run_analysis(long_video, tmp_path / "work")
    assert not result.ok and result.error == "no_clear_swing"


def test_a_clip_that_starts_after_address_says_so(monkeypatch, fixture_video, tmp_path) -> None:
    _patch_window(monkeypatch, _decision(2.5, status="SINGLE_SWING", confidence="LOW"))
    late = {"video": "swing.mp4", "hidden": [], "camera_check": {"signals": {}}, "phases": [
        {"phase": "address", "frame_index": None, "checks": []}, {"phase": "takeaway", "frame_index": None, "checks": []},
        {"phase": "mid_back", "frame_index": None, "checks": []}, {"phase": "top", "frame_index": 14, "checks": []},
        {"phase": "downswing", "frame_index": 20, "checks": []}, {"phase": "impact", "frame_index": 22, "checks": []}]}
    monkeypatch.setattr(analysis_runner.subprocess, "run", _engine_fake(analysis=late))
    result = analysis_runner.run_analysis(fixture_video, tmp_path / "work")
    assert not result.ok and result.error == "no_setup"
    title, advice = analysis_runner.error_text("no_setup")
    assert "set-up" in title and "before your set-up" in advice
    assert result.diagnostics["failure"]["category"] == "phases"
