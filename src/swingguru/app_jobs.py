"""Upload -> analysis jobs for the *local* Swing Guru app server (``scripts/serve_app.py``).

The analysis itself is :func:`swingguru.analysis_runner.run_analysis` - the same
code the hosted worker (``swingguru.service``) runs. This module only adds the
local plumbing: files under ``var/app``, an in-memory + JSON job record, one
analysis at a time, and SHA-1 de-duplication of repeated uploads.

Stages are reported only as they really complete (each one leaves a file
behind); there is no invented percentage. A video that cannot be analysed is
rejected with a golfer-facing reason and never enters the Library.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from .analysis_runner import ERRORS, MAX_SECONDS, MIN_SECONDS, STAGES, probe, reject_reason, run_analysis, stage_states

ROOT = Path(__file__).resolve().parents[2]
APP_VAR = ROOT / "var" / "app"
JOBS = APP_VAR / "jobs"
SWINGS = APP_VAR / "swings"
META = APP_VAR / "swing_meta.json"

MAX_BYTES = 500 * 1024 * 1024

_lock = threading.Lock()
_run_lock = threading.Lock()
_jobs: dict[str, dict] = {}


# ---------------------------------------------------------------- meta store

def read_meta() -> dict:
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_meta(swing_id: str, **fields) -> dict:
    with _lock:
        meta = read_meta()
        entry = {**meta.get(swing_id, {}), **fields}
        meta[swing_id] = entry
        META.parent.mkdir(parents=True, exist_ok=True)
        tmp = META.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, META)
        return entry


# ---------------------------------------------------------------- jobs

def _save(job: dict) -> None:
    folder = JOBS / job["id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")


def _set(job: dict, **fields) -> None:
    with _lock:
        job.update(fields)
        job["updated"] = time.time()
        _save(job)


def get_job(job_id: str) -> dict | None:
    job = _jobs.get(job_id)
    if job is None:
        path = JOBS / job_id / "job.json"
        if not path.is_file():
            return None
        job = json.loads(path.read_text(encoding="utf-8"))
    out = dict(job)
    out["stages"] = stage_states(JOBS / job["id"], prepared=job.get("prepared", False),
                                 finished=job.get("state") == "done", running=job.get("state") == "running")
    out["elapsed_s"] = round((job.get("finished") or time.time()) - job["created"], 1)
    return out


def start_job(stream, length: int, *, file_name: str, modified_ms: float | None) -> dict:
    """Store the upload and start its analysis. Returns the job (or the existing swing)."""

    if length <= 0:
        return {"state": "error", "error": "unreadable", **_error_text("unreadable")}
    if length > MAX_BYTES:
        return {"state": "error", "error": "too_large", **_error_text("too_large")}
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    folder = JOBS / job_id
    folder.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_name).suffix.lower() if Path(file_name).suffix.lower() in {".mp4", ".mov", ".m4v", ".avi"} else ".mp4"
    upload = folder / f"upload{suffix}"
    digest = hashlib.sha1()
    remaining = length
    with upload.open("wb") as handle:
        while remaining > 0:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    sha = digest.hexdigest()
    existing = next((sid for sid, m in read_meta().items() if m.get("sha1") == sha and (SWINGS / sid / "bundle" / "analysis.json").exists()), None)
    captured = datetime.fromtimestamp(modified_ms / 1000.0) if modified_ms else datetime.now()
    job = {"id": job_id, "state": "running", "created": time.time(), "file_name": file_name, "sha1": sha,
           "captured": captured.isoformat(timespec="seconds"), "prepared": False, "notice": None,
           "swing_id": None, "error": None}
    if existing:
        job.update(state="done", swing_id=existing, prepared=True, duplicate=True, finished=time.time(),
                   notice="This swing is already in your Library.")
        shutil.rmtree(folder, ignore_errors=True)
        _jobs[job_id] = job
        return get_job(job_id) or job
    _jobs[job_id] = job
    _save(job)
    threading.Thread(target=_run, args=(job, upload), daemon=True).start()
    return get_job(job_id)


def _error_text(code: str) -> dict:
    title, advice = ERRORS.get(code, ERRORS["failed"])
    return {"error_title": title, "error_advice": advice}


def _fail(job: dict, code: str, detail: str = "", **extra) -> None:
    _set(job, state="error", error=code, detail=detail, finished=time.time(), **extra, **_error_text(code))


def _run(job: dict, upload: Path) -> None:
    with _run_lock:  # one analysis at a time
        try:
            _analyse(job, upload)
        except Exception as error:  # pragma: no cover - defensive: never leave a job running
            _fail(job, "failed", repr(error))


def _analyse(job: dict, upload: Path) -> None:
    result = run_analysis(upload, upload.parent, on_prepared=lambda notice: _set(job, prepared=True, notice=notice))
    if not result.ok:
        return _fail(job, result.error or "failed", result.detail, diagnostics=result.diagnostics,
                     timings=result.timings)
    swing_id = "s" + job["id"]
    target = SWINGS / swing_id
    target.mkdir(parents=True, exist_ok=True)
    shutil.move(str(result.bundle), str(target / "bundle"))
    update_meta(swing_id, created=job["captured"], uploaded=datetime.now().isoformat(timespec="seconds"),
                source_name=job["file_name"], sha1=job["sha1"], notice=result.notice, job=job["id"])
    _set(job, state="done", swing_id=swing_id, finished=time.time(), diagnostics=result.diagnostics,
         timings=result.timings)


__all__ = ["ERRORS", "MAX_SECONDS", "MIN_SECONDS", "STAGES", "get_job", "probe", "read_meta", "reject_reason",
           "start_job", "update_meta"]
