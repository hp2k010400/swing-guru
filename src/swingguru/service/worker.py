"""The analysis worker: claims queued swings from the database and analyses them.

Runs as a background thread inside the service process (one analysis at a
time per process; more replicas = more throughput, claims are atomic).

For each job:

1. download the original upload from private storage to a temp dir;
2. :func:`swingguru.analysis_runner.run_analysis` (the unchanged engine), while
   a watcher writes the real stage progress - which is also the heartbeat;
3. on success store ``analysis.json``, the replay video and a thumbnail as
   private objects and mark the swing ``complete``; on failure store only a
   golfer-facing error code;
4. delete the original upload (``SG_SOURCE_RETENTION_HOURS``, default 0).

A worker that dies mid-job leaves a stale heartbeat; the job is claimed again
(up to ``max_attempts``). The janitor removes uploads that never arrived and
sources past their retention.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .. import diagnostics as diag
from ..analysis_runner import ANALYSIS_VERSION, run_analysis, stage_states
from ..app_api import product_summary, render_thumbnail
from ..scoring import score_swing
from ..swing_history import record
from .billing import BillingService
from .config import Settings
from .repo import Repo, now
from .storage import Storage

log = logging.getLogger("swingguru.worker")

# The progress watcher polls every 2 s; a healthy shutdown needs less than one
# poll interval. The join is bounded so a blocked backend write cannot hang the
# job - if the thread is still alive afterwards the worker says so honestly.
WATCH_POLL_S = 2.0
WATCHER_JOIN_S = 5.0
REPO_ROOT = Path(__file__).resolve().parents[3]


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def result_keys(user_id: str, swing_id: str, attempts: int | None = None) -> dict[str, str]:
    """Storage keys for one attempt's results. Each attempt writes its own file names, so a worker
    that lost the job can never overwrite the files the winning attempt's row points at."""

    base, tag = f"{user_id}/{swing_id}", (f"a{int(attempts)}-" if attempts else "")
    return {"result_key": f"{base}/{tag}analysis.json", "video_key": f"{base}/{tag}replay.mp4",
            "thumb_key": f"{base}/{tag}thumb.jpg"}


class Worker:
    def __init__(self, settings: Settings, repo: Repo, storage: Storage,
                 runner: Callable = run_analysis, billing: BillingService | None = None,
                 accounts=None) -> None:
        self.settings, self.repo, self.storage, self.runner = settings, repo, storage, runner
        self.billing = billing
        # Optional so the worker stays usable on its own in tests; the app wires the real service.
        self.accounts = accounts
        self._stop = threading.Event()
        # Set when a job is queued in this process, so it is claimed at once instead of at the
        # next poll (the poll remains the fallback for other replicas and restarts).
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_janitor = 0.0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="analysis-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """A job was just queued: look for work now rather than after the poll interval."""

        self._wake.set()

    def _loop(self) -> None:
        log.info("worker started")
        while not self._stop.is_set():
            try:
                ran = self.run_once()
                if time.time() - self._last_janitor > 600:
                    self.janitor()
                    self._last_janitor = time.time()
            except Exception as error:  # never let the loop die
                log.error("worker loop error type=%s detail=%s", type(error).__name__,
                          diag.sanitize(str(error), strip_paths=(REPO_ROOT,)))
                ran = False
            if not ran:
                self._wake.wait(self.settings.worker_poll_s)
                self._wake.clear()

    # -- one job ---------------------------------------------------------------

    def run_once(self) -> bool:
        row = self.repo.claim_next(_ago(self.settings.stale_after_s), self.settings.max_attempts)
        if row is None:
            return False
        started = time.monotonic()
        log.info("job %s claimed (attempt %s)", row["id"], row["attempts"])
        try:
            self.process(row)
        except Exception as error:
            # Structured and bounded: no raw traceback (it can carry paths and
            # engine internals) and no exception text beyond the sanitised form.
            log.error("job %s crashed type=%s detail=%s", row["id"], type(error).__name__,
                      diag.sanitize(str(error), strip_paths=(REPO_ROOT,)))
            self._fail(row, "failed")
        log.info("job %s finished in %.1f s", row["id"], time.monotonic() - started)
        return True

    def process(self, row: dict) -> None:
        work = Path(tempfile.mkdtemp(prefix="swing-"))
        try:
            suffix = ".mov" if row.get("content_type") == "video/quicktime" else ".mp4"
            upload = work / f"upload{suffix}"
            size = self.storage.size(row["source_key"])
            if not size:
                return self._fail(row, "upload_missing")
            if size > self.settings.max_upload_bytes:
                return self._fail(row, "too_large")
            mark = time.monotonic()
            self.storage.download_to(row["source_key"], upload)
            timings = {"download_s": round(time.monotonic() - mark, 2), "upload_bytes": upload.stat().st_size}
            if upload.stat().st_size > self.settings.max_upload_bytes:
                return self._fail(row, "too_large")

            state = {"prepared": False, "notice": None, "done": False, "wake": threading.Event()}
            attempts = int(row.get("attempts") or 0)
            watcher = threading.Thread(target=self._watch, args=(row["id"], attempts, work, state), daemon=True)
            watcher.start()

            def prepared(notice: str | None) -> None:
                state.update(prepared=True, notice=notice)

            result = None
            raised: Exception | None = None
            run_dir = work / "run"
            try:
                result = self.runner(upload, run_dir, on_prepared=prepared,
                                     timeout_s=self.settings.analysis_timeout_s)
            except Exception as error:  # an ordinary crash is a failed job
                raised = error
                log.error("job %s crashed type=%s detail=%s", row["id"], type(error).__name__,
                          diag.sanitize(str(error), strip_paths=(work, REPO_ROOT)))
            except BaseException:
                # KeyboardInterrupt / SystemExit are never converted into an
                # ordinary job failure: stop the watcher and propagate.
                state["done"] = True
                state["wake"].set()
                raise
            finally:
                state["done"] = True
                state["wake"].set()
                watcher.join(timeout=WATCHER_JOIN_S)
                if watcher.is_alive():
                    # The thread checks the stop flag before every write, so it
                    # stops at its next boundary; a blocked backend call is the
                    # only way it outlives this join. Its writes are
                    # attempt-guarded, so it cannot touch a newer attempt.
                    log.warning("job %s: progress watcher still running after %.0f s "
                                "(blocked on backend I/O; writes remain attempt-guarded)",
                                row["id"], WATCHER_JOIN_S)
            if raised is not None:
                record = self._diagnostics(run_dir, error="failed", detail=repr(raised), timings=timings)
                log.error("job %s failure detail: %s", row["id"],
                          diag.sanitize(repr(raised), strip_paths=(work, REPO_ROOT)))
                log.error("job %s diagnostics %s", row["id"], json.dumps(record))
                return self._fail(row, "failed", progress_rows=record.get("stages"))
            timings.update(result.timings, video=result.video_info)
            record = result.diagnostics or self._diagnostics(run_dir, error=result.error or "failed",
                                                             detail=result.detail, timings=timings)
            if not result.ok:
                log.info("job %s rejected: %s (%s)", row["id"], result.error, diag.sanitize(result.detail, 200))
                log.info("job %s timings %s", row["id"], json.dumps(timings))
                log.info("job %s diagnostics %s", row["id"], json.dumps(record))
                return self._fail(row, result.error or "failed", progress_rows=record.get("stages"))
            self._store_result(row, result, timings)
            log.info("job %s timings %s", row["id"], json.dumps(timings))
            log.info("job %s diagnostics %s", row["id"], json.dumps(record))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self._maybe_delete_source(row["id"])

    @staticmethod
    def _diagnostics(run_dir: Path, *, error: str, detail: str = "", timings: dict | None = None) -> dict:
        """A structured record for a runner that crashed before returning one."""

        try:
            return diag.build(ok=False, error=error, detail=detail, work=run_dir, timings=timings or {},
                              analysis_version=ANALYSIS_VERSION)
        except Exception as failure:  # pragma: no cover - diagnostics never break a job
            log.error("diagnostics could not be built type=%s detail=%s", type(failure).__name__,
                      diag.sanitize(str(failure)))
            return {"schema": diag.SCHEMA, "outcome": "failed", "public_code": error,
                    "error": "diagnostics unavailable"}

    def _watch(self, swing_id: str, attempts: int, work: Path, state: dict) -> None:
        """Publish real progress every 2 s (also the job's heartbeat).

        Every write goes through the attempt-guarded repository helper, and the
        stop flag is checked before each one, so this thread can never mutate a
        job that has been re-claimed, deleted, or finished by a later attempt.
        """

        last = None
        while not state["done"]:
            if state["wake"].is_set():
                break
            rows = stage_states(work / "run", prepared=state["prepared"], finished=False, running=True)
            if state["wake"].is_set():
                break
            if rows != last:
                applied = self.repo.update_progress(swing_id, attempts, progress=rows, notice=state["notice"])
                last = rows
            else:
                applied = self.repo.update_progress(swing_id, attempts)  # heartbeat
            if not applied:
                log.warning("job %s: progress write refused (attempt %s no longer owns the job)",
                            swing_id, attempts)
                return
            state["wake"].wait(WATCH_POLL_S)

    def _store_result(self, row: dict, result, timings: dict | None = None) -> None:
        timings = {} if timings is None else timings
        bundle: Path = result.bundle
        analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
        timings["engine"] = (analysis.get("engine") or {}).get("timings")
        # Scored once, here, and stored with the result: a later model version never rewrites this swing.
        mark = time.monotonic()
        # Production: the code default (scoring.config.VERSION). Staging may select another model.
        if self.settings.score_version:
            analysis["swing_score"] = score_swing(analysis, self.settings.score_version)
        else:
            analysis["swing_score"] = score_swing(analysis)
        timings["score_s"] = round(time.monotonic() - mark, 3)
        (bundle / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
        attempts = int(row.get("attempts") or 0)
        if not self._still_owns(row):
            log.warning("job %s: attempt %s no longer owns the job - result not stored", row["id"], attempts)
            return
        if self._account_blocked(row.get("user_id")):
            # A deleting/deleted account must not gain new private objects, even if its swing row
            # somehow outlived the tombstone.
            log.warning("job %s: account is not active - result not stored", row["id"])
            return
        keys = result_keys(row["user_id"], row["id"], attempts)
        replay = bundle / analysis.get("video", "swing.mp4")
        mark = time.monotonic()
        thumb = render_thumbnail(replay, analysis, bundle / "thumb.jpg")
        timings["thumbnail_s"] = round(time.monotonic() - mark, 2)
        mark = time.monotonic()
        self.storage.put_file(keys["result_key"], bundle / "analysis.json", "application/json")
        self.storage.put_file(keys["video_key"], replay, "video/mp4")
        if thumb:
            self.storage.put_file(keys["thumb_key"], thumb, "image/jpeg")
        timings["store_s"] = round(time.monotonic() - mark, 2)
        timings["replay_bytes"] = replay.stat().st_size
        if self._account_blocked(row.get("user_id")):
            # The delete raced this attempt: remove exactly the objects this attempt just wrote.
            log.warning("job %s: account changed during storage - objects removed", row["id"])
            self.storage.delete([keys["result_key"], keys["video_key"]] +
                                ([keys["thumb_key"]] if thumb else []))
            return
        when = datetime.fromisoformat(row["captured_at"]) if row.get("captured_at") else datetime.now(timezone.utc)
        summary = product_summary(analysis, swing_id=row["id"], when=when, meta={"notice": result.notice}, demo=False)
        score = summary["score"]
        lite = {"counts": summary["counts"], "headline": summary["headline"], "summary_text": summary["summary_text"],
                "score": {"available": score["available"], "total": score["total"], "version": score["version"],
                          "measured": score["measured"], "eligible": score["eligible"]}}
        try:
            # Progress, comparisons and ranks read this compact record (additive; older swings are
            # backfilled from their stored analysis by GET /v1/progress).
            lite["record"] = record(analysis, score)
        except Exception:
            log.exception("swing %s: history record failed (analysis unaffected)", row["id"][:8])
        mark = time.monotonic()
        fields = {"notice": result.notice, "analysis_version": ANALYSIS_VERSION,
                  "progress": stage_states(bundle.parent, prepared=True, finished=True, running=False),
                  "summary": lite, "thumb_key": keys["thumb_key"] if thumb else None,
                  "result_key": keys["result_key"], "video_key": keys["video_key"]}
        if self.billing and self.billing.enabled:
            # Job state and the usage ledger settle in one transaction, guarded by the
            # attempt that produced this result.
            applied = self.billing.settle(row["id"], attempts, **fields)
        else:
            applied = self.repo.finish_job(row["id"], status="complete", attempts=attempts, **fields)
        timings["db_write_s"] = round(time.monotonic() - mark, 2)
        if applied is None:
            # Lost ownership between the check and the write: these objects carry this
            # attempt's name and nothing references them, so remove them.
            log.warning("job %s: completion refused (attempt %s no longer owns the job)", row["id"], attempts)
            self.storage.delete([keys["result_key"], keys["video_key"]] + ([keys["thumb_key"]] if thumb else []))

    def _still_owns(self, row: dict) -> bool:
        fresh = self.repo.get(row["id"])
        return bool(fresh and fresh.get("status") == "processing"
                    and int(fresh.get("attempts") or 0) == int(row.get("attempts") or 0))

    def _account_blocked(self, user_id) -> bool:
        """Fail closed: unless the owner is provably active, no new private object is created."""

        if not user_id:
            return True
        try:
            return self.repo.account_status(str(user_id)) != "active"
        except Exception:
            log.exception("job owner status could not be read")
            return True

    def _fail(self, row: dict, code: str, expected_updated_at: str | None = None,
              older_than: str | None = None, progress_rows: list[dict] | None = None) -> None:
        if self.billing and self.billing.enabled:
            # Terminal failure releases the reservation exactly once, atomically with
            # the job state, and only if this attempt still owns the job.
            self.billing.release(row["id"], int(row.get("attempts") or 0), code,
                                 expected_updated_at=expected_updated_at, older_than=older_than)
        else:
            # One guarded statement: the terminal transition and the last real stage list
            # land together, and only while this attempt still owns the job - a stale
            # worker can never fail (or overwrite) a newer attempt's job.
            extra = {"progress": progress_rows} if progress_rows else {}
            applied = self.repo.finish_job(row["id"], status="failed", attempts=int(row.get("attempts") or 0),
                                           expected_updated_at=expected_updated_at, older_than=older_than,
                                           error_code=code, **extra)
            if applied is None:
                log.warning("job %s: failure not recorded (attempt %s no longer owns the job)",
                            row["id"], row.get("attempts"))

    # -- retention ---------------------------------------------------------------

    def _maybe_delete_source(self, swing_id: str) -> None:
        if self.settings.source_retention_hours > 0:
            return
        row = self.repo.get(swing_id)
        if row and row.get("source_key") and not row.get("source_deleted_at") and row["status"] in ("complete", "failed"):
            self.storage.delete([row["source_key"]])
            self.repo.update(swing_id, source_deleted_at=now())

    def janitor(self) -> None:
        """Remove uploads that never arrived, and sources past their retention."""

        for row in self.repo.where("awaiting_upload", _ago(self.settings.awaiting_upload_ttl_hours * 3600), "created_at"):
            self.storage.delete([row["source_key"]])
            self.repo.delete_owned(row["id"], row["user_id"])
        # A worker that keeps dying can exhaust its attempts and leave the job
        # "processing" forever: fail it (and release any reservation) once no
        # further attempt is allowed.
        for row in self.repo.where("processing", _ago(self.settings.stale_after_s), "updated_at"):
            if (row.get("attempts") or 0) >= self.settings.max_attempts:
                # Re-read: the row may have heartbeat or been re-claimed since the
                # query, and a terminal write must never clobber that.
                fresh = self.repo.get(row["id"])
                if not fresh or fresh.get("status") != "processing":
                    continue
                if fresh.get("updated_at") != row.get("updated_at") or (fresh.get("attempts") or 0) < self.settings.max_attempts:
                    log.info("job %s was refreshed - not failing", row["id"])
                    continue
                log.warning("job %s stale after %s attempts - failing", fresh["id"], fresh.get("attempts"))
                # Pin the heartbeat we just read: if it moves again before the write, that write
                # (and the credit release) must not happen.
                self._fail(fresh, "failed", expected_updated_at=fresh.get("updated_at"),
                           older_than=_ago(self.settings.stale_after_s))
        cutoff = _ago(self.settings.source_retention_hours * 3600)
        for status in ("complete", "failed"):
            for row in self.repo.where(status, cutoff, "finished_at"):
                if row.get("source_key") and not row.get("source_deleted_at"):
                    self.storage.delete([row["source_key"]])
                    self.repo.update(row["id"], source_deleted_at=now())
        self._reconcile_deletions()

    def _reconcile_deletions(self) -> None:
        """Finish half-completed deletions and re-sweep recently deleted owner prefixes.

        A golfer's JWT expires, or the auth row is already gone, and the deletion can no longer be
        retried from the app: this pass is what keeps such a tombstone from becoming permanent. It
        also covers upload URLs issued before the sweep (they stay valid for their TTL) and a worker
        that crashed after writing objects.
        """

        if self.accounts is None:
            return
        try:
            self.accounts.reconcile(stuck_before=_ago(self.settings.deletion_reconcile_hours * 3600),
                                    cleanup_before=_ago(0))
        except Exception:  # the janitor must never die on one bad row
            log.exception("deletion reconciliation failed")


__all__ = ["Worker", "result_keys"]
