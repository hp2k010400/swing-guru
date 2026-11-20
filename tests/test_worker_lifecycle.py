"""Worker lifecycle: the progress watcher always stops, failures keep their evidence."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from swingguru import diagnostics as diag, progress
from swingguru.analysis_runner import RunResult
from swingguru.service.config import Settings
from swingguru.service.worker import Worker


class FakeRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.updates: list[dict] = []
        self.refused: list[dict] = []

    def seed(self, row: dict) -> dict:
        self.rows[row["id"]] = dict(row)
        return self.rows[row["id"]]

    def account_status(self, user_id: str) -> str:
        """The worker refuses to store results unless the owner is provably active."""

        return "active"

    def get(self, swing_id: str) -> dict | None:
        return self.rows.get(swing_id)

    def update(self, swing_id: str, **fields) -> dict | None:
        self.updates.append({"id": swing_id, **fields})
        self.rows.setdefault(swing_id, {"id": swing_id}).update(fields)
        return self.rows[swing_id]

    def update_progress(self, swing_id: str, attempts: int, *, progress=None, notice=None) -> bool:
        """The same attempt guard the real repositories implement."""

        row = self.rows.get(swing_id)
        if row is None or row.get("status") != "processing" or int(row.get("attempts") or 0) != int(attempts):
            self.refused.append({"id": swing_id, "attempts": attempts})
            return False
        fields = {"progress": progress} if progress is not None else {}
        if notice is not None:
            fields["notice"] = notice
        self.updates.append({"id": swing_id, **fields})
        row.update(fields)
        return True

    def finish_job(self, swing_id: str, *, status: str, attempts: int, expected_updated_at=None,
                   older_than=None, **fields) -> dict | None:
        """The same terminal guard the real repositories implement."""

        row = self.rows.get(swing_id)
        if row is None or row.get("status") != "processing" or int(row.get("attempts") or 0) != int(attempts):
            self.refused.append({"id": swing_id, "attempts": attempts, "status": status})
            return None
        fields = {**fields, "status": status, "finished_at": "now"}
        self.updates.append({"id": swing_id, **fields})
        row.update(fields)
        return row


class FakeStorage:
    def __init__(self, blob: bytes = b"\x00\x00\x00\x18ftypmp42" + b"x" * 500) -> None:
        self.blob = blob

    def size(self, key: str) -> int:
        return len(self.blob)

    def download_to(self, key: str, path: Path) -> None:
        Path(path).write_bytes(self.blob)

    def delete(self, keys) -> None:
        return None

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        return None

    def signed_url(self, key: str) -> str:
        return f"https://local.test/{key}"


def _settings(tmp_path: Path) -> Settings:
    return replace(Settings(data_dir=tmp_path), worker_poll_s=0.01, stale_after_s=1.0, max_attempts=2,
                   source_retention_hours=0.0, analysis_timeout_s=5.0)


def _row() -> dict:
    return {"id": "job-1", "user_id": "user-1", "status": "processing", "attempts": 1,
            "source_key": "user-1/job-1/source.mp4", "content_type": "video/mp4",
            "captured_at": "2026-10-03T10:00:00+00:00"}


def _tiny_mp4(path: Path) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 96))
    for _ in range(10):
        writer.write(np.full((96, 64, 3), 128, np.uint8))
    writer.release()
    return path


def _use_repo_local_workdir(monkeypatch, tmp_path: Path) -> Path:
    """tempfile.mkdtemp() sets a mode-0o700 ACL that is not writable on this account."""

    from swingguru.service import worker as worker_module

    target = tmp_path / "worker-work"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worker_module.tempfile, "mkdtemp", lambda prefix="swing-": str(target))
    return target


def _analysis() -> dict:
    verdict = {"status": "good", "label": "Hip hinge good", "support": "ok", "verdict_id": "x",
               "mapping": {"id": "address.hip_hinge", "input": 40.0, "validation": "provisional_ui_mapping"}}
    return {"video": "swing.mp4", "duration_s": 1.0,
            "phases": [{"phase": "address", "label": "Address", "frame_index": 0, "time_s": 0.1,
                        "checks": [{"kind": "torso_angle", "guided": True, "checkpoint": "Hip hinge",
                                    "value": "40°", "time_s": 0.1, "level": "HIGH", "verdict": verdict}]}],
            "hidden": [], "interpretations": {"focus": None, "results": []}, "camera_check": {"signals": {}}}


def test_watcher_is_joined_when_the_runner_raises(tmp_path, monkeypatch, caplog) -> None:
    created = []
    real_thread = threading.Thread

    def spy(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(threading, "Thread", spy)
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())

    def runner(*args, **kwargs):
        raise RuntimeError("boom while loading C:/models/rtmw.onnx")

    worker = Worker(_settings(tmp_path), repo, storage, runner=runner)
    with caplog.at_level(logging.INFO, logger="swingguru.worker"):
        worker.process(row)

    assert created, "the watcher thread should have been started"
    assert not any(thread.is_alive() for thread in created), "the progress watcher must not outlive the job"
    assert repo.get("job-1")["status"] == "failed" and repo.get("job-1")["error_code"] == "failed"
    assert any("diagnostics" in record.getMessage() for record in caplog.records)
    structured = "\n".join(record.getMessage() for record in caplog.records
                           if "diagnostics" in record.getMessage() or "failure detail" in record.getMessage())
    assert "rtmw.onnx" not in structured, "the structured records must be sanitized"
    assert "<path>" in structured


def test_failure_persists_the_real_stage_rows(tmp_path, monkeypatch) -> None:
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())
    stages = [{"id": "prepare", "label": "Preparing your swing", "state": "done", "at_ms": 1.0},
              {"id": "moments", "label": "Finding key moments", "state": "active", "at_ms": 5.0},
              {"id": "club", "label": "Tracking the club", "state": "pending", "at_ms": None},
              {"id": "positions", "label": "Checking your positions", "state": "pending", "at_ms": None},
              {"id": "build", "label": "Building your analysis", "state": "pending", "at_ms": None}]
    result = RunResult(False, "no_swing", detail="no person detected in the sampled frames",
                       timings={"probe_s": 0.1, "total_s": 2.0},
                       diagnostics={"schema": diag.SCHEMA, "outcome": "failed",
                                    "failure": {"stage": "moments", "category": "person"}, "stages": stages})
    worker = Worker(_settings(tmp_path), repo, storage, runner=lambda *a, **k: result)
    worker.process(row)
    assert repo.get("job-1")["progress"] == stages
    assert repo.get("job-1")["error_code"] == "no_swing"


def test_successful_job_scores_once_at_storage_and_logs_timings(tmp_path, caplog, monkeypatch) -> None:
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "analysis.json").write_text(json.dumps(_analysis()), encoding="utf-8")
    _tiny_mp4(bundle / "swing.mp4")
    result = RunResult(True, bundle=bundle, timings={"probe_s": 0.1, "total_s": 2.0},
                       diagnostics={"schema": diag.SCHEMA, "outcome": "ok", "stages": []})
    worker = Worker(_settings(tmp_path), repo, storage, runner=lambda *a, **k: result)
    with caplog.at_level(logging.INFO, logger="swingguru.worker"):
        worker.process(row)
    assert repo.get("job-1")["status"] == "complete"
    assert repo.get("job-1")["analysis_version"]
    stored = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    assert stored["swing_score"]["version"] == "swing_score_v3"
    timings_line = next(record.getMessage() for record in caplog.records if "timings" in record.getMessage())
    payload = json.loads(timings_line.split("timings ", 1)[1])
    assert "score_s" in payload and "store_s" in payload and "thumbnail_s" in payload


def test_watcher_reports_progress_while_the_runner_is_working(tmp_path, monkeypatch) -> None:
    from swingguru.service import worker as worker_module

    monkeypatch.setattr(worker_module, "WATCH_POLL_S", 0.02)
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())

    def runner(upload: Path, work: Path, *, on_prepared=None, timeout_s=10.0):
        progress.record(work, "prepare", progress.DONE)
        progress.record(work, "moments", progress.ACTIVE)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any(any(stage.get("state") == "done" for stage in (update.get("progress") or []))
                   for update in repo.updates):
                break
            threading.Event().wait(0.01)
        progress.record(work, "moments", progress.DONE)
        return RunResult(False, "no_swing", timings={"probe_s": 0.1, "total_s": 1.0},
                         diagnostics={"schema": diag.SCHEMA, "outcome": "failed", "failure": None, "stages": []})

    worker = Worker(_settings(tmp_path), repo, storage, runner=runner)
    worker.process(row)
    assert any("progress" in update for update in repo.updates), "the watcher should write real stage progress"
    states = [stage["state"] for update in repo.updates if update.get("progress") for stage in update["progress"]]
    assert "active" in states and "done" in states


def test_a_failed_job_never_writes_progress_after_the_terminal_transition(tmp_path, monkeypatch) -> None:
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())
    stages = [{"id": "prepare", "label": "Preparing your swing", "state": "done", "at_ms": 1.0},
              {"id": "moments", "label": "Finding key moments", "state": "active", "at_ms": 5.0}]
    result = RunResult(False, "no_swing", timings={"probe_s": 0.1},
                       diagnostics={"schema": diag.SCHEMA, "outcome": "failed", "failure": None, "stages": stages})
    worker = Worker(_settings(tmp_path), repo, storage, runner=lambda *a, **k: result)
    worker.process(row)
    terminal = [index for index, update in enumerate(repo.updates) if update.get("status") == "failed"]
    assert terminal, "the job must have been failed"
    with_progress = [index for index, update in enumerate(repo.updates) if "progress" in update]
    assert with_progress[-1] == terminal[-1], "no progress write may follow the terminal transition"
    assert all("progress" not in update for update in repo.updates[terminal[-1] + 1:])
    assert repo.updates[terminal[-1]]["progress"] == stages, \
        "the terminal transition carries the last real stage list"


def test_an_interrupt_is_not_swallowed_into_an_ordinary_failure(tmp_path, monkeypatch) -> None:
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed(_row())

    def runner(*args, **kwargs):
        raise KeyboardInterrupt

    worker = Worker(_settings(tmp_path), repo, storage, runner=runner)
    with pytest.raises(KeyboardInterrupt):
        worker.process(row)
    assert repo.get("job-1")["status"] == "processing", "an interrupt must not be rewritten as a failure"
    assert not any(update.get("status") == "failed" for update in repo.updates)


def test_stale_attempt_heartbeat_and_progress_cannot_touch_a_replacement(tmp_path, monkeypatch) -> None:
    from swingguru.service import worker as worker_module

    monkeypatch.setattr(worker_module, "WATCH_POLL_S", 0.01)
    _use_repo_local_workdir(monkeypatch, tmp_path)
    repo, storage = FakeRepo(), FakeStorage()
    row = repo.seed({**_row(), "attempts": 1})
    assert repo.update_progress("job-1", 1, progress=[{"id": "prepare"}]) is True

    # Another worker re-claims the stale job: same row, attempt 2.
    repo.update("job-1", attempts=2, progress=None)
    repo.updates.clear()
    assert repo.update_progress("job-1", 1) is False
    assert repo.update_progress("job-1", 1, progress=[{"id": "prepare", "state": "done"}]) is False
    assert repo.get("job-1")["progress"] is None

    # The old worker's watcher must stop on its own rather than fight the claim.
    work = tmp_path / "work"
    progress.record(work / "run", "prepare", progress.DONE)
    progress.record(work / "run", "moments", progress.ACTIVE)
    state = {"prepared": True, "notice": None, "done": False, "wake": threading.Event()}
    worker = Worker(_settings(tmp_path), repo, storage, runner=lambda *a, **k: None)
    watcher = threading.Thread(target=worker._watch, args=("job-1", 1, work, state), daemon=True)
    watcher.start()
    deadline = time.monotonic() + 3.0
    while not repo.refused and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    state["done"] = True
    state["wake"].set()
    watcher.join(timeout=5)
    assert repo.refused, "the guarded write must be refused for the replaced attempt"
    assert repo.get("job-1")["progress"] is None, "the replaced attempt's progress is untouched"


def test_the_watcher_never_publishes_all_done_before_the_result_is_committed(tmp_path, monkeypatch) -> None:
    """Service-facing: a real repository row never reads ready mid-job."""

    from swingguru.service.repo import SqliteRepo, now as repo_now

    monkeypatch.setattr(__import__("swingguru.service.worker", fromlist=["x"]), "WATCH_POLL_S", 0.01)
    repo = SqliteRepo(tmp_path / "swings.sqlite3")
    repo.insert({"id": "job-00000001", "user_id": "user-aaaaaaaa", "status": "queued"})
    claimed = repo.claim_next(repo_now(), 2)
    work = tmp_path / "work"
    for stage, _ in progress.STAGES:
        progress.record(work / "run", stage, progress.DONE)
    state = {"prepared": True, "notice": None, "done": False, "wake": threading.Event()}
    worker = Worker(_settings(tmp_path), repo, FakeStorage(), runner=lambda *a, **k: None)
    watcher = threading.Thread(target=worker._watch, args=(claimed["id"], claimed["attempts"], work, state),
                               daemon=True)
    watcher.start()
    deadline = time.monotonic() + 3.0
    stored = None
    while time.monotonic() < deadline:
        stored = repo.get(claimed["id"]).get("progress")
        if stored:
            break
        threading.Event().wait(0.01)
    state["done"] = True
    state["wake"].set()
    watcher.join(timeout=5)
    assert stored, "the watcher should publish progress"
    assert [row["state"] for row in stored][-1] == "active", "ready is only advertised after the commit"
    assert not all(row["state"] == "done" for row in stored)
