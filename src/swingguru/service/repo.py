"""The ``swings`` table: one row per swing, which is also its analysis job.

Status: ``awaiting_upload`` -> ``queued`` -> ``processing`` -> ``complete`` | ``failed``.
Only ``complete`` swings appear in the Library.

* :class:`PostgrestRepo` - Supabase Postgres through PostgREST with the service
  role key; jobs are claimed atomically by the ``claim_swing_job`` SQL
  function (``FOR UPDATE SKIP LOCKED``), so several workers are safe.
* :class:`SqliteRepo` - the same interface on SQLite for development and tests.

Every read or write a *user* triggers goes through the ``*_owned`` methods,
which always filter on ``user_id``.
"""

from __future__ import annotations

import json
import uuid
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import httpx

from .config import Settings

JSON_FIELDS = ("progress", "summary")
COLUMNS = ("id", "user_id", "status", "progress", "error_code", "notice", "club", "file_name", "content_type",
           "size_bytes", "captured_at", "created_at", "updated_at", "started_at", "finished_at", "attempts",
           "source_key", "result_key", "video_key", "thumb_key", "summary", "analysis_version", "source_deleted_at")

# The billing ledger. Kept out of ``COLUMNS`` on purpose: a swing patch must never
# touch it, and settlement goes through the atomic ``billing_finish`` path below.
BILLING_FINISH_FIELDS = ("error_code", "notice", "progress", "summary", "analysis_version", "thumb_key",
                         "result_key", "video_key")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_key(stamp: datetime) -> str:
    """The first day of ``stamp``'s UTC month ("2026-10-01").

    The month a free analysis is charged to is always derived from a server/database clock, never
    from a client-supplied date. Year/month arithmetic handles December -> January and February in
    a leap year because ``date`` does the calendar work.
    """

    return stamp.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0,
                                                  microsecond=0).date().isoformat()


def month_bounds(stamp: datetime) -> tuple[str, str]:
    """(this month's first instant, next month's first instant) as ISO UTC timestamps.

    The allowance period is half-open: [first day 00:00Z, next first day 00:00Z).
    """

    start = stamp.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start.isoformat(timespec="seconds"), start.replace(year=year, month=month).isoformat(timespec="seconds")


def parse_stamp(value) -> datetime:
    """A stored ISO timestamp as an aware UTC datetime (a clock fallback when it is unusable)."""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RepoError(Exception):
    pass


class AccountBlocked(RepoError):
    """The owner is in a delete lifecycle: this write must not happen, now or later.

    Distinct from a generic :class:`RepoError` so the API can answer 403 ``account_deleted`` instead
    of a 5xx, and so a caller can never mistake "refused on purpose" for "the database was down".
    """


class Repo(Protocol):
    def insert(self, row: dict) -> dict: ...
    def get(self, swing_id: str) -> dict | None: ...
    def get_owned(self, swing_id: str, user_id: str) -> dict | None: ...
    def update(self, swing_id: str, **fields) -> dict | None: ...
    def update_progress(self, swing_id: str, attempts: int, *, progress: list | None = None,
                        notice: str | None = None) -> bool: ...
    def update_owned(self, swing_id: str, user_id: str, **fields) -> dict | None: ...
    def finish_job(self, swing_id: str, *, status: str, attempts: int, expected_updated_at: str | None = None,
                   older_than: str | None = None, **fields) -> dict | None: ...
    def queue_if_awaiting(self, swing_id: str, user_id: str, size_bytes: int) -> dict | None: ...
    def list_owned(self, user_id: str, statuses: tuple[str, ...]) -> list[dict]: ...
    def count_since(self, user_id: str, since: str) -> int: ...
    def count_active(self, user_id: str) -> int: ...
    def claim_next(self, stale_before: str, max_attempts: int) -> dict | None: ...
    def delete_owned(self, swing_id: str, user_id: str) -> dict | None: ...
    def owned_rows(self, user_id: str) -> list[dict]: ...
    def delete_user(self, user_id: str) -> int: ...
    def where(self, status: str, older_than: str, field: str = "updated_at") -> list[dict]: ...
    def ping(self) -> bool: ...

    # durable account lifecycle (Apple credential + deletion tombstone; service-role only)
    def account_status(self, user_id: str) -> str: ...
    def account_deletion_begin(self, user_id: str) -> dict: ...
    def account_deletion_get(self, user_id: str) -> dict | None: ...
    def account_deletion_update(self, user_id: str, **fields) -> dict | None: ...
    def account_deletion_finish(self, user_id: str) -> dict | None: ...
    def account_wipe(self, user_id: str) -> int: ...
    def feedback_submit(self, *, user_id: str, kind: str, swing_id: str | None, rating: str | None,
                        category: str | None, message: str | None, context: dict,
                        app_version: str | None) -> dict: ...
    def apple_grant_put(self, user_id: str, grant_id: str, subject: str, client_id: str,
                        ciphertext: str) -> bool: ...
    def apple_grant_put_for_deletion(self, user_id: str, grant_id: str, subject: str, client_id: str,
                                     ciphertext: str) -> bool: ...
    def apple_grants_unrevoked(self, user_id: str) -> list[dict]: ...
    def apple_grant_counts(self, user_id: str) -> dict: ...
    def apple_grant_mark_revoked(self, grant_id: str, user_id: str) -> dict | None: ...
    def apple_grants_delete_revoked(self, user_id: str) -> int: ...
    def account_deletions_for_reconcile(self, *, stuck_before: str, cleanup_before: str) -> list[dict]: ...

    # billing (used only when SG_BILLING_MODE=on; the SQLite mirror keeps the
    # same atomicity as the Postgres functions)
    def billing_status(self, user_id: str, free_total: int) -> dict: ...
    def billing_admit(self, *, user_id: str, swing_id: str, kind: str, product_id: str | None,
                      size_bytes: int, free_total: int) -> dict: ...
    def billing_finish(self, swing_id: str, *, status: str, attempts: int,
                       expected_updated_at: str | None = None, older_than: str | None = None,
                       **fields) -> dict | None: ...


# ---------------------------------------------------------------- sqlite

class SqliteRepo:
    def __init__(self, path: Path, clock: Callable[[], datetime] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        # The mirror of the Postgres RPCs owns the clock too, so month arithmetic can be tested
        # deterministically (and never comes from a client).
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute(f"""CREATE TABLE IF NOT EXISTS swings (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, progress TEXT, error_code TEXT,
                notice TEXT, club TEXT, file_name TEXT, content_type TEXT, size_bytes INTEGER, captured_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, source_key TEXT, result_key TEXT, video_key TEXT,
                thumb_key TEXT, summary TEXT, analysis_version TEXT, source_deleted_at TEXT)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS swings_user ON swings (user_id, created_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS swings_status ON swings (status, created_at)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS billing_accounts (
                user_id TEXT PRIMARY KEY, free_total INTEGER NOT NULL, free_used_seed INTEGER NOT NULL DEFAULT 0,
                grandfather_seeded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS billing_usage (
                swing_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
                product_id TEXT, admitted_month TEXT, charge_month TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, settled_at TEXT)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS billing_usage_user ON billing_usage (user_id, kind, state)")
            # Durable account lifecycle: the deletion tombstone outlives the auth user (it is what
            # denies a still-cryptographically-valid JWT after deletion), and the Apple refresh token
            # is stored only as ciphertext under a server-only key.
            self.db.execute("""CREATE TABLE IF NOT EXISTS account_deletions (
                user_id TEXT PRIMARY KEY, status TEXT NOT NULL, storage_cleared_at TEXT, data_cleared_at TEXT,
                apple_revoked_at TEXT, auth_deleted_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT, apple_revocation TEXT, cleanup_until TEXT, cleanup_done_at TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS account_deletions_status ON account_deletions (status)")
            # Append-only: one row per minted Apple refresh token, each with its own revocation state.
            # The mirror of `public.apple_grants` (see the migration for why there is no single row
            # per user, and no FK to an auth table).
            self.db.execute("""CREATE TABLE IF NOT EXISTS apple_grants (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, apple_subject TEXT NOT NULL,
                client_id TEXT NOT NULL, refresh_token_ciphertext TEXT NOT NULL,
                key_version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active',
                revoked_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.db.execute("CREATE INDEX IF NOT EXISTS apple_grants_user_state"
                            " ON apple_grants (user_id, state)")
            # Mirror of public.feedback (20261006000000_feedback.sql).
            self.db.execute("""CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, swing_id TEXT, rating TEXT,
                category TEXT, message TEXT, context TEXT NOT NULL DEFAULT '{}', app_version TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS feedback_one_per_swing"
                            " ON feedback (user_id, swing_id) WHERE kind = 'analysis'")
            # The whole upgrade of a pre-monthly development database is ONE write transaction:
            # introspection, the ALTERs, the backfill and the month index. That matters twice over:
            #   * two connections opening at the same moment cannot both decide an ALTER is needed
            #     (the second waits, re-reads the schema, and finds the column already there); and
            #   * another connection cannot settle a job between the backfill's ledger snapshot and
            #     its swing snapshot - which is what let a stale `reserved` row be overwritten as
            #     `forfeited` after the job had actually settled.
            # `charge_month` must exist before the index that references it is created.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                existing = {row["name"] for row in self.db.execute("PRAGMA table_info(billing_usage)")}
                for column in ("admitted_month", "charge_month"):
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE billing_usage ADD COLUMN {column} TEXT")
                # The clock is read inside the transaction, so the upgrade's timestamps are one instant.
                self._backfill_billing_months(self._stamp())
                self.db.execute("CREATE INDEX IF NOT EXISTS billing_usage_month "
                                "ON billing_usage (user_id, kind, state, charge_month)")
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _stamp(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    def _blocked_locked(self, user_id: str) -> bool:
        """Caller holds ``self.lock``: does this owner have a delete tombstone?"""

        return self.db.execute("SELECT 1 FROM account_deletions WHERE user_id=?",
                               (user_id,)).fetchone() is not None

    @staticmethod
    def _iso(when: datetime) -> str:
        """One instant, rendered once - the stamp and the month must come from the same reading."""

        return when.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _backfill_billing_months(self, stamp: str) -> None:
        """The SQLite twin of the migration's backfill: idempotent and additive.

        Called during construction with ``self.lock`` held AND inside one ``BEGIN IMMEDIATE``
        transaction (before the month index exists, after the month columns do), so the ledger and
        swing reads come from the same snapshot as the writes. ``stamp`` is that transaction's single
        clock reading. Existing settled usage stays charged to its completion month
        (``swings.finished_at`` first, then the ledger's ``settled_at``, then ``created_at``); an
        admission month is recorded for every row; and a legacy orphan free reservation - its swing
        is gone or already terminal - is forfeited into its admission month so it can never hold a
        slot forever. Nothing is deleted and no known usage is reset.
        """

        rows = self.db.execute(
            "SELECT swing_id, kind, state, created_at, settled_at, admitted_month, charge_month"
            " FROM billing_usage").fetchall()
        if not rows:
            return
        swings = {row["id"]: {"status": row["status"], "finished_at": row["finished_at"]}
                  for row in self.db.execute("SELECT id, status, finished_at FROM swings")}
        for row in rows:
            admitted = row["admitted_month"] or month_key(parse_stamp(row["created_at"]))
            charge = row["charge_month"]
            state, settled_at = row["state"], row["settled_at"]
            if charge is None and state == "settled":
                swing = swings.get(row["swing_id"]) or {}
                completion = swing.get("finished_at") or row["settled_at"] or row["created_at"]
                charge = month_key(parse_stamp(completion))
            if state == "reserved" and row["kind"] == "free":
                live = (swings.get(row["swing_id"]) or {}).get("status")
                if live not in ("queued", "processing"):
                    state = "forfeited"
                    charge = charge or admitted
                    settled_at = settled_at or row["created_at"]
            if (state, admitted, charge, settled_at) == (row["state"], row["admitted_month"],
                                                         row["charge_month"], row["settled_at"]):
                continue
            self.db.execute(
                "UPDATE billing_usage SET state=?, admitted_month=?, charge_month=?, settled_at=?,"
                " updated_at=? WHERE swing_id=?",
                (state, admitted, charge, settled_at, stamp, row["swing_id"]))

    @staticmethod
    def _out(row) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in JSON_FIELDS:
            if out.get(key):
                out[key] = json.loads(out[key])
        return out

    @staticmethod
    def _in(fields: dict) -> dict:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        return {k: (json.dumps(v) if k in JSON_FIELDS and v is not None else v) for k, v in fields.items()}

    def insert(self, row: dict) -> dict:
        with self.lock:
            user_id = str(row.get("user_id") or "")
            if user_id and self._blocked_locked(user_id):
                raise AccountBlocked("account is being deleted")
            stamp = self._stamp()
            fields = self._in({"created_at": stamp, "updated_at": stamp, "attempts": 0, **row})
            self.db.execute(f"INSERT INTO swings ({','.join(fields)}) VALUES ({','.join('?' * len(fields))})",
                            tuple(fields.values()))
        return self.get(fields["id"])

    def get(self, swing_id: str) -> dict | None:
        with self.lock:
            return self._out(self.db.execute("SELECT * FROM swings WHERE id=?", (swing_id,)).fetchone())

    def get_owned(self, swing_id: str, user_id: str) -> dict | None:
        with self.lock:
            return self._out(self.db.execute("SELECT * FROM swings WHERE id=? AND user_id=?", (swing_id, user_id)).fetchone())

    def _update(self, where: str, args: tuple, fields: dict) -> None:
        with self.lock:
            fields = self._in({**fields, "updated_at": self._stamp()})
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(f"UPDATE swings SET {sets} WHERE {where}", tuple(fields.values()) + args)

    def update(self, swing_id: str, **fields) -> dict | None:
        self._update("id=?", (swing_id,), fields)
        return self.get(swing_id)

    def update_owned(self, swing_id: str, user_id: str, **fields) -> dict | None:
        if self.get_owned(swing_id, user_id) is None:
            return None
        self._update("id=? AND user_id=?", (swing_id, user_id), fields)
        return self.get_owned(swing_id, user_id)

    def update_progress(self, swing_id: str, attempts: int, *, progress: list | None = None,
                        notice: str | None = None) -> bool:
        """Publish progress/heartbeat for the attempt that still owns the job.

        The write only lands while the row is still ``processing`` *and* still
        carries the attempts counter this worker claimed. A job that another
        worker has re-claimed, has gone terminal, or has been deleted is left
        exactly as it is - the old attempt can never overwrite newer progress.
        """

        fields: dict = {"updated_at": self._stamp()}
        if progress is not None:
            fields["progress"] = json.dumps(progress)
        if notice is not None:
            fields["notice"] = notice
        with self.lock:
            cursor = self.db.execute(
                f"UPDATE swings SET {','.join(f'{key}=?' for key in fields)} "
                "WHERE id=? AND status='processing' AND attempts=?",
                (*fields.values(), swing_id, int(attempts)))
            return cursor.rowcount == 1

    def queue_if_awaiting(self, swing_id: str, user_id: str, size_bytes: int) -> dict | None:
        """awaiting_upload -> queued, only once: a second concurrent /start can never put a job that a
        worker already claimed back into the queue. None when it was not awaiting the upload."""

        stamp = self._stamp()
        with self.lock:
            if self._blocked_locked(user_id):
                raise AccountBlocked("account is being deleted")
            cursor = self.db.execute(
                "UPDATE swings SET status='queued', size_bytes=?, updated_at=? "
                "WHERE id=? AND user_id=? AND status='awaiting_upload'", (int(size_bytes), stamp, swing_id, user_id))
            applied = cursor.rowcount == 1
        return self.get_owned(swing_id, user_id) if applied else None

    def finish_job(self, swing_id: str, *, status: str, attempts: int, expected_updated_at: str | None = None,
                   older_than: str | None = None, **fields) -> dict | None:
        """Terminal write (billing off) guarded exactly like ``billing_finish``, without the ledger.

        Lands only while the row is ``processing`` and still carries the attempt that did the work
        (plus, for the janitor, the heartbeat it observed). A worker that lost the job - re-claimed,
        finished, or deleted meanwhile - changes nothing and gets ``None``.
        """

        if status not in ("complete", "failed"):
            raise RepoError("bad terminal status")
        unknown = set(fields) - set(BILLING_FINISH_FIELDS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        stamp = self._stamp()
        values = self._in({**fields, "status": status, "finished_at": stamp, "updated_at": stamp})
        if status == "complete":
            values["error_code"] = None
        where, guard = "id=? AND status='processing' AND attempts=?", [swing_id, int(attempts)]
        if expected_updated_at is not None:
            where += " AND updated_at=?"
            guard.append(expected_updated_at)
        if older_than is not None:
            where += " AND updated_at<?"
            guard.append(older_than)
        # Atomic account guard: a settlement that races a deletion must not land, not even for an
        # owner whose row was deleted a moment later.
        where += " AND user_id NOT IN (SELECT user_id FROM account_deletions)"
        with self.lock:
            cursor = self.db.execute(f"UPDATE swings SET {','.join(f'{k}=?' for k in values)} WHERE {where}",
                                     (*values.values(), *guard))
            applied = cursor.rowcount == 1
        return self.get(swing_id) if applied else None

    def list_owned(self, user_id: str, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM swings WHERE user_id=? AND status IN ({marks}) "
                                   "ORDER BY created_at DESC", (user_id, *statuses)).fetchall()
        return [self._out(r) for r in rows]

    def count_since(self, user_id: str, since: str) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM swings WHERE user_id=? AND created_at>=?", (user_id, since)).fetchone()[0]

    def count_active(self, user_id: str) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM swings WHERE user_id=? AND status IN "
                                   "('awaiting_upload','queued','processing')", (user_id,)).fetchone()[0]

    def claim_next(self, stale_before: str, max_attempts: int) -> dict | None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT id FROM swings WHERE (status='queued' OR (status='processing' AND updated_at<?)) "
                    "AND attempts<? AND user_id NOT IN (SELECT user_id FROM account_deletions) "
                    "ORDER BY created_at LIMIT 1", (stale_before, max_attempts)).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return None
                stamp = self._stamp()
                self.db.execute("UPDATE swings SET status='processing', attempts=attempts+1, started_at=?, "
                                "updated_at=? WHERE id=?", (stamp, stamp, row["id"]))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.get(row["id"])

    def delete_owned(self, swing_id: str, user_id: str) -> dict | None:
        """Delete one owned swing, forfeiting a still-reserved free slot in the deletion month.

        A queued/processing free job whose bytes are deleted can never finish, so its reservation
        would otherwise hold a slot forever (and a late worker failure must not return it). A
        completed (settled) row is untouched: deleting an analysed swing never returns an analysis.
        """

        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                found = self.db.execute("SELECT * FROM swings WHERE id=? AND user_id=?",
                                        (swing_id, user_id)).fetchone()
                if found is not None:
                    # One instant inside the transaction for the forfeit month and its timestamps.
                    instant = self._clock()
                    stamp, month = self._iso(instant), month_key(instant)
                    self.db.execute("UPDATE billing_usage SET state='forfeited', charge_month=?,"
                                    " settled_at=?, updated_at=?"
                                    " WHERE swing_id=? AND kind='free' AND state='reserved'",
                                    (month, stamp, stamp, swing_id))
                    self.db.execute("DELETE FROM swings WHERE id=? AND user_id=?", (swing_id, user_id))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self._out(found)

    def owned_rows(self, user_id: str) -> list[dict]:
        with self.lock:
            return [self._out(r) for r in self.db.execute("SELECT * FROM swings WHERE user_id=?", (user_id,)).fetchall()]

    def delete_user(self, user_id: str) -> int:
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                removed = self.db.execute("DELETE FROM swings WHERE user_id=?", (user_id,)).rowcount
                self.db.execute("DELETE FROM billing_usage WHERE user_id=?", (user_id,))
                self.db.execute("DELETE FROM billing_accounts WHERE user_id=?", (user_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return removed

    def where(self, status: str, older_than: str, field: str = "updated_at") -> list[dict]:
        if field not in ("updated_at", "finished_at", "created_at"):
            raise RepoError("bad field")
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM swings WHERE status=? AND {field}<?", (status, older_than)).fetchall()
        return [self._out(r) for r in rows]

    def ping(self) -> bool:
        with self.lock:
            self.db.execute("SELECT 1")
        return True

    # -- billing ledger --------------------------------------------------------

    def _billing_account_locked(self, user_id: str, free_total: int, stamp: str) -> sqlite3.Row:
        """Create the per-user quota account once.

        The lifetime grandfather seed (``free_used_seed``) is deliberately not read or written any
        more: pre-ledger completed swings are grandfathered and the current allowance comes from the
        monthly ledger. Caller holds ``self.lock`` and an open ``BEGIN IMMEDIATE`` transaction.

        ``stamp`` is the transaction's single clock reading, so the account row cannot carry a
        timestamp from a different instant than the ledger write beside it.
        """

        row = self.db.execute("SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)).fetchone()
        if row is not None:
            if row["free_total"] != free_total:
                self.db.execute("UPDATE billing_accounts SET free_total=?, updated_at=? WHERE user_id=?",
                                (free_total, stamp, user_id))
                row = self.db.execute("SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)).fetchone()
            return row
        self.db.execute(
            "INSERT OR IGNORE INTO billing_accounts (user_id, free_total, free_used_seed, grandfather_seeded,"
            " created_at, updated_at) VALUES (?,?,0,1,?,?)",
            (user_id, free_total, stamp, stamp))
        return self.db.execute("SELECT * FROM billing_accounts WHERE user_id=?", (user_id,)).fetchone()

    def _billing_counts_locked(self, user_id: str, month: str) -> tuple[int, int, int]:
        """(charged this month, forfeited this month, outstanding reservations of any month).

        One statement, one snapshot - the mirror of the single aggregate in the Postgres RPCs, so a
        reservation being settled by another connection can never be seen as neither charged nor
        reserved. Caller holds ``self.lock`` and an open ``BEGIN IMMEDIATE`` transaction.
        """

        row = self.db.execute(
            "SELECT"
            " COALESCE(SUM(CASE WHEN kind='free' AND state='settled' AND charge_month=? THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN kind='free' AND state='forfeited' AND charge_month=? THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN kind='free' AND state='reserved' THEN 1 ELSE 0 END), 0)"
            " FROM billing_usage WHERE user_id=?",
            (month, month, user_id)).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def billing_status(self, user_id: str, free_total: int) -> dict:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # ONE instant, read after the write lock is held: the month counted and the period
                # reported can never come from either side of midnight.
                stamp = self._clock()
                account = self._billing_account_locked(user_id, free_total, self._iso(stamp))
                settled, forfeited, reserved = self._billing_counts_locked(user_id, month_key(stamp))
                period_start, period_resets = month_bounds(stamp)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return {"free_total": account["free_total"], "free_settled": settled, "free_forfeited": forfeited,
                "free_reserved": reserved, "allowance_period_start": period_start,
                "allowance_resets_at": period_resets, "allowance_timezone": "UTC",
                "allowance_model": "calendar_month"}

    def billing_admit(self, *, user_id: str, swing_id: str, kind: str, product_id: str | None,
                      size_bytes: int, free_total: int) -> dict:
        if kind not in ("free", "paid"):
            raise RepoError("bad billing kind")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self._blocked_locked(user_id):
                    self.db.execute("COMMIT")
                    return {"status": "blocked"}
                stamp = self._clock()          # one instant for the month AND the queue timestamp
                month, iso = month_key(stamp), self._iso(stamp)
                row = self.db.execute("SELECT user_id, status FROM swings WHERE id=?", (swing_id,)).fetchone()
                if row is None or row["user_id"] != user_id:
                    self.db.execute("COMMIT")
                    return {"status": "not_found"}
                if row["status"] != "awaiting_upload":
                    self.db.execute("COMMIT")
                    return {"status": "not_awaiting", "swing_status": row["status"]}
                account = self._billing_account_locked(user_id, free_total, iso)
                existing = self.db.execute("SELECT kind, state FROM billing_usage WHERE swing_id=?", (swing_id,)).fetchone()
                if existing is not None:
                    self.db.execute("COMMIT")
                    return {"status": "already", "kind": existing["kind"], "state": existing["state"]}
                if kind == "free":
                    settled, forfeited, reserved = self._billing_counts_locked(user_id, month)
                    used = settled + forfeited
                    if used >= account["free_total"]:
                        self.db.execute("COMMIT")
                        return {"status": "exhausted", "free_total": account["free_total"],
                                "free_used": used, "free_reserved": reserved}
                    if used + reserved >= account["free_total"]:
                        self.db.execute("COMMIT")
                        return {"status": "in_progress", "free_total": account["free_total"],
                                "free_used": used, "free_reserved": reserved}
                self.db.execute(
                    "INSERT INTO billing_usage (swing_id, user_id, kind, state, product_id, admitted_month,"
                    " created_at, updated_at) VALUES (?,?,?,'reserved',?,?,?,?)",
                    (swing_id, user_id, kind, product_id, month, iso, iso))
                self.db.execute("UPDATE swings SET status='queued', size_bytes=COALESCE(?, size_bytes), updated_at=?"
                                " WHERE id=?", (size_bytes, iso, swing_id))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return {"status": "admitted", "kind": kind}

    def billing_finish(self, swing_id: str, *, status: str, attempts: int,
                       expected_updated_at: str | None = None, older_than: str | None = None,
                       **fields) -> dict | None:
        """Terminal write guarded by the attempt that started the work.

        The ledger only moves when this call really flipped the job out of
        ``processing``: a deleted swing, or one another worker has since
        re-claimed, keeps its credit exactly as it was. ``expected_updated_at``
        additionally pins the heartbeat a caller observed (the janitor re-reads
        it), so a heartbeat that lands between the read and this write wins.
        """

        if status not in ("complete", "failed"):
            raise RepoError("bad billing status")
        unknown = set(fields) - set(BILLING_FINISH_FIELDS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        stored = self._in(fields)
        applied = False
        lease = ""
        guard: tuple = (swing_id, int(attempts))
        if expected_updated_at is not None:
            lease += " AND updated_at=?"
            guard += (expected_updated_at,)
        if older_than is not None:
            # Second-resolution timestamps make an exact match insufficient on its own: requiring
            # the row to still be older than the caller's staleness cutoff is what makes a
            # heartbeat in the same second win.
            lease += " AND updated_at<?"
            guard += (older_than,)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                # One instant inside the transaction: `finished_at` and the charge month can never
                # straddle midnight (or a lock wait) because both come from this single reading.
                instant = self._clock()
                stamp, month = self._iso(instant), month_key(instant)
                cursor = self.db.execute(
                    "UPDATE swings SET status=?, error_code=CASE WHEN ?='failed' THEN ? ELSE NULL END,"
                    " notice=COALESCE(?, notice), progress=COALESCE(?, progress), summary=COALESCE(?, summary),"
                    " analysis_version=COALESCE(?, analysis_version), thumb_key=COALESCE(?, thumb_key),"
                    " result_key=COALESCE(?, result_key), video_key=COALESCE(?, video_key),"
                    f" finished_at=?, updated_at=? WHERE id=? AND status='processing' AND attempts=?"
                    " AND user_id NOT IN (SELECT user_id FROM account_deletions)" + lease,
                    (status, status, stored.get("error_code"), stored.get("notice"), stored.get("progress"),
                     stored.get("summary"), stored.get("analysis_version"), stored.get("thumb_key"),
                     stored.get("result_key"), stored.get("video_key"), stamp, stamp, *guard))
                applied = cursor.rowcount == 1
                if applied:
                    self.db.execute(
                        "UPDATE billing_usage SET"
                        " state=CASE WHEN ?='complete' THEN 'settled' ELSE 'released' END,"
                        " charge_month=CASE WHEN ?='complete' THEN ? ELSE NULL END,"
                        " settled_at=?, updated_at=? WHERE swing_id=? AND state='reserved'",
                        (status, status, month, stamp, stamp, swing_id))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return self.get(swing_id) if applied else None

    # -- durable account lifecycle (service-role only) -------------------------

    _DELETION_FIELDS = ("apple_revoked_at", "storage_cleared_at", "data_cleared_at", "auth_deleted_at",
                        "last_error_code", "apple_revocation", "cleanup_until", "cleanup_done_at")

    def account_status(self, user_id: str) -> str:
        """``active`` / ``deleting`` / ``deleted`` for the API's access guard."""

        with self.lock:
            row = self.db.execute("SELECT status FROM account_deletions WHERE user_id=?", (user_id,)).fetchone()
        return row["status"] if row else "active"

    def account_deletion_begin(self, user_id: str) -> dict:
        """Create (or re-read) the tombstone and count this attempt. One transaction, one clock."""

        stamp = self._stamp()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT OR IGNORE INTO account_deletions (user_id, status, attempts, created_at, updated_at)"
                    " VALUES (?, 'deleting', 0, ?, ?)", (user_id, stamp, stamp))
                self.db.execute("UPDATE account_deletions SET attempts=attempts+1, updated_at=? WHERE user_id=?",
                                (stamp, user_id))
                row = self.db.execute("SELECT * FROM account_deletions WHERE user_id=?", (user_id,)).fetchone()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return dict(row)

    def account_deletion_get(self, user_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM account_deletions WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def account_deletion_update(self, user_id: str, **fields) -> dict | None:
        unknown = set(fields) - set(self._DELETION_FIELDS)
        if unknown:
            raise RepoError(f"unknown deletion columns {sorted(unknown)}")
        stamp = self._stamp()
        # The callers pass ``True`` for "this step is done now" so the SQLite mirror and the
        # Postgres RPC (which uses the same booleans) share one call shape.
        values = {**{key: (stamp if value is True else value) for key, value in fields.items()},
                  "updated_at": stamp}
        with self.lock:
            # The monotonic rules must hold here too, not only in the Postgres function: a confirmed
            # revocation is terminal, a completed step's timestamp is written once, and a cleanup
            # deadline is not moved by a later call.
            self.db.execute("BEGIN IMMEDIATE")
            try:
                current = self.db.execute(
                    "SELECT apple_revocation, apple_revoked_at, cleanup_until, cleanup_done_at"
                    " FROM account_deletions WHERE user_id=?", (user_id,)).fetchone()
                if current is not None:
                    if current["apple_revocation"] == "revoked" or (
                            values.get("apple_revocation") is None and "apple_revocation" in values):
                        values.pop("apple_revocation", None)
                    for column in ("apple_revoked_at", "cleanup_until", "cleanup_done_at"):
                        if current[column]:
                            values.pop(column, None)
                sets = ",".join(f"{key}=?" for key in values)
                self.db.execute(f"UPDATE account_deletions SET {sets} WHERE user_id=?",
                                (*values.values(), user_id))
                row = self.db.execute("SELECT * FROM account_deletions WHERE user_id=?", (user_id,)).fetchone()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return dict(row) if row else None

    def account_deletion_finish(self, user_id: str) -> dict | None:
        refusal: str | None = None
        row = None
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT * FROM account_deletions WHERE user_id=?",
                                      (user_id,)).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return None
                # The same prerequisites as the Postgres function: completion is refused while any
                # grant is still active, and a malformed/empty answer can never read as success.
                active = self.db.execute(
                    "SELECT COUNT(*) FROM apple_grants WHERE user_id=? AND state<>'revoked'",
                    (user_id,)).fetchone()[0]
                if row["apple_revocation"] is None or row["storage_cleared_at"] is None \
                        or row["data_cleared_at"] is None:
                    self.db.execute("COMMIT")
                    return None
                if active:
                    refusal = "unrevoked_grant"
                else:
                    stamp = self._stamp()
                    self.db.execute("UPDATE account_deletions SET status='deleted', last_error_code=NULL,"
                                    " updated_at=? WHERE user_id=?", (stamp, user_id))
                    row = self.db.execute("SELECT * FROM account_deletions WHERE user_id=?",
                                          (user_id,)).fetchone()
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        if refusal is not None:
            raise RepoError(refusal)
        return dict(row) if row else None

    def account_wipe(self, user_id: str) -> int:
        """Remove the golfer's app data. The tombstone and the Apple credential are separate steps."""

        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                stamp = self._stamp()
                # Mirror of the Postgres lock order: the account row first, then the ledger, then the
                # swing rows (so a concurrent admission/settlement cannot interleave).
                self.db.execute(
                    "INSERT INTO billing_accounts (user_id, free_total, free_used_seed, grandfather_seeded,"
                    " created_at, updated_at) VALUES (?,1,0,1,?,?)"
                    " ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at",
                    (user_id, stamp, stamp))
                self.db.execute("DELETE FROM billing_usage WHERE user_id=?", (user_id,))
                cursor = self.db.execute("DELETE FROM swings WHERE user_id=?", (user_id,))
                deleted = cursor.rowcount
                self.db.execute("DELETE FROM billing_accounts WHERE user_id=?", (user_id,))
                # Postgres removes feedback through the auth.users cascade; there is no auth table here.
                self.db.execute("DELETE FROM feedback WHERE user_id=?", (user_id,))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return int(deleted)

    def feedback_submit(self, *, user_id: str, kind: str, swing_id: str | None, rating: str | None,
                        category: str | None, message: str | None, context: dict,
                        app_version: str | None) -> dict:
        """Mirror of ``public.feedback_submit``: refused during deletion; one analysis row per swing."""

        with self.lock:
            stamp = self._stamp()
            if self.db.execute("SELECT 1 FROM account_deletions WHERE user_id=?", (user_id,)).fetchone():
                return {"blocked": True}
            payload = json.dumps(context or {}, separators=(",", ":"))
            if kind == "analysis":
                owned = self.db.execute("SELECT 1 FROM swings WHERE id=? AND user_id=?", (swing_id, user_id)).fetchone()
                if not owned:
                    return {"not_found": True}
                row = self.db.execute("SELECT id FROM feedback WHERE kind='analysis' AND user_id=? AND swing_id=?",
                                      (user_id, swing_id)).fetchone()
                if row:
                    self.db.execute("UPDATE feedback SET rating=?, message=?, context=?, app_version=?, updated_at=?"
                                    " WHERE id=?", (rating, message, payload, app_version, stamp, row["id"]))
                    return {"id": row["id"]}
            new_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO feedback (id, user_id, kind, swing_id, rating, category, message, context,"
                            " app_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (new_id, user_id, kind, swing_id if kind == "analysis" else None, rating,
                             category, message, payload, app_version, stamp, stamp))
            return {"id": new_id}

    def _insert_grant_locked(self, user_id: str, grant_id: str, subject: str, client_id: str,
                             ciphertext: str, stamp: str) -> None:
        """Caller holds ``self.lock``: append one grant. Existing rows are never overwritten."""

        self.db.execute(
            "INSERT OR IGNORE INTO apple_grants (id, user_id, apple_subject, client_id,"
            " refresh_token_ciphertext, key_version, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,1,'active',?,?)",
            (grant_id, user_id, subject, client_id, ciphertext, stamp, stamp))

    def apple_grant_put(self, user_id: str, grant_id: str, subject: str, client_id: str,
                        ciphertext: str) -> bool:
        """Append one minted grant, unless a deletion owns this account.

        Append-only: a second exchange can never overwrite the first token, and each row carries its
        own revocation state, so an account-level "revoked" cannot vouch for a newer grant.
        """

        stamp = self._stamp()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM account_deletions WHERE user_id=?",
                                   (user_id,)).fetchone() is not None:
                    self.db.execute("COMMIT")
                    return False
                self._insert_grant_locked(user_id, grant_id, subject, client_id, ciphertext, stamp)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return True

    def apple_grant_put_for_deletion(self, user_id: str, grant_id: str, subject: str, client_id: str,
                                     ciphertext: str) -> bool:
        """Append a grant minted while a deletion is running, so it can be revoked on retry."""

        stamp = self._stamp()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("SELECT 1 FROM account_deletions WHERE user_id=?",
                                   (user_id,)).fetchone() is None:
                    self.db.execute("COMMIT")
                    return False
                self._insert_grant_locked(user_id, grant_id, subject, client_id, ciphertext, stamp)
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
        return True

    def apple_grants_unrevoked(self, user_id: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM apple_grants WHERE user_id=? AND state<>'revoked'"
                                   " ORDER BY created_at, id", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def apple_grant_counts(self, user_id: str) -> dict:
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) FILTER (WHERE state<>'revoked') AS active,"
                " COUNT(*) FILTER (WHERE state='revoked') AS revoked"
                " FROM apple_grants WHERE user_id=?", (user_id,)).fetchone()
        return {"active": int(row["active"] or 0), "revoked": int(row["revoked"] or 0)}

    def apple_grant_mark_revoked(self, grant_id: str, user_id: str) -> dict | None:
        """Per-grant compare-and-set: only a confirmed revocation flips a row, and never back."""

        stamp = self._stamp()
        with self.lock:
            cursor = self.db.execute(
                "UPDATE apple_grants SET state='revoked', revoked_at=?, updated_at=?"
                " WHERE id=? AND user_id=? AND state='active'", (stamp, stamp, grant_id, user_id))
            applied = cursor.rowcount == 1
            row = self.db.execute("SELECT * FROM apple_grants WHERE id=? AND user_id=?",
                                  (grant_id, user_id)).fetchone()
        return dict(row) if applied and row else None

    def apple_grants_delete_revoked(self, user_id: str) -> int:
        """Remove revoked rows only; a live grant can never leave the table this way."""

        with self.lock:
            cursor = self.db.execute("DELETE FROM apple_grants WHERE user_id=? AND state='revoked'",
                                     (user_id,))
            deleted = cursor.rowcount
        return int(deleted)

    def account_deletions_for_reconcile(self, *, stuck_before: str, cleanup_before: str) -> list[dict]:
        """Half-finished deletions, plus finished ones whose late-upload window is still open."""

        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM account_deletions WHERE"
                " (status='deleting' AND updated_at<?)"
                " OR (status='deleted' AND cleanup_done_at IS NULL AND cleanup_until IS NOT NULL"
                "     AND cleanup_until<=?)"
                " OR EXISTS (SELECT 1 FROM apple_grants g WHERE g.user_id=account_deletions.user_id"
                "            AND g.state<>'revoked')"
                " ORDER BY created_at LIMIT 200", (stuck_before, cleanup_before)).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------- supabase / postgrest

class PostgrestRepo:
    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.base = f"{settings.supabase_url}/rest/v1"
        self.http = client or httpx.Client(timeout=30.0, headers={
            "Authorization": f"Bearer {settings.supabase_service_key}", "apikey": settings.supabase_service_key,
            "Content-Type": "application/json"})

    def _ok(self, response: httpx.Response, what: str) -> httpx.Response:
        if response.status_code >= 400:
            raise RepoError(f"{what} failed ({response.status_code})")
        return response

    def _rows(self, params: dict) -> list[dict]:
        return self._ok(self.http.get(f"{self.base}/swings", params={"select": "*", **params}), "select").json()

    def insert(self, row: dict) -> dict:
        """Create one swing through the lifecycle-guarded function, never a bare table insert."""

        # swing_create builds the row with jsonb_populate_record, which turns every omitted column into
        # an explicit NULL (column defaults do not apply). Send the NOT NULL defaulted columns.
        stamp = now()
        payload = {"attempts": 0, "created_at": stamp, "updated_at": stamp, **row}
        user_id, swing_id = str(payload["user_id"]), str(payload["id"])
        data = self._rpc("swing_create", {"p_user_id": user_id, "p_swing_id": swing_id,
                                          "p_fields": payload})
        if isinstance(data, dict) and data.get("blocked"):
            raise AccountBlocked("account is being deleted")
        created = self.get(swing_id)
        if created is None:
            raise RepoError("swing_create did not return the created row")
        return created

    def get(self, swing_id: str) -> dict | None:
        rows = self._rows({"id": f"eq.{swing_id}"})
        return rows[0] if rows else None

    def get_owned(self, swing_id: str, user_id: str) -> dict | None:
        rows = self._rows({"id": f"eq.{swing_id}", "user_id": f"eq.{user_id}"})
        return rows[0] if rows else None

    def _patch(self, params: dict, fields: dict) -> dict | None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        response = self._ok(self.http.patch(f"{self.base}/swings", params=params, json={**fields, "updated_at": now()},
                                            headers={"Prefer": "return=representation"}), "update")
        rows = response.json()
        return rows[0] if rows else None

    def update(self, swing_id: str, **fields) -> dict | None:
        return self._patch({"id": f"eq.{swing_id}"}, fields)

    def update_owned(self, swing_id: str, user_id: str, **fields) -> dict | None:
        return self._patch({"id": f"eq.{swing_id}", "user_id": f"eq.{user_id}"}, fields)

    def update_progress(self, swing_id: str, attempts: int, *, progress: list | None = None,
                        notice: str | None = None) -> bool:
        """The PostgREST mirror of the attempt-guarded progress write."""

        fields: dict = {}
        if progress is not None:
            fields["progress"] = progress
        if notice is not None:
            fields["notice"] = notice
        response = self._ok(self.http.patch(
            f"{self.base}/swings",
            params={"id": f"eq.{swing_id}", "status": "eq.processing", "attempts": f"eq.{int(attempts)}"},
            json={**fields, "updated_at": now()}, headers={"Prefer": "return=representation"}), "update")
        rows = response.json()
        return bool(rows)

    def queue_if_awaiting(self, swing_id: str, user_id: str, size_bytes: int) -> dict | None:
        """PostgREST mirror: the status filter makes the transition happen at most once."""

        data = self._rpc("swing_queue_if_awaiting", {"p_swing_id": swing_id, "p_user_id": user_id,
                                                     "p_size_bytes": int(size_bytes)})
        if isinstance(data, dict) and data.get("blocked"):
            raise AccountBlocked("account is being deleted")
        return data if isinstance(data, dict) and data.get("id") else None

    def finish_job(self, swing_id: str, *, status: str, attempts: int, expected_updated_at: str | None = None,
                   older_than: str | None = None, **fields) -> dict | None:
        """The PostgREST mirror of the guarded terminal write (billing off)."""

        if status not in ("complete", "failed"):
            raise RepoError("bad terminal status")
        unknown = set(fields) - set(BILLING_FINISH_FIELDS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        # swing_finish only takes the status, error code and progress. The result fields (keys,
        # summary, notice, version) are written first, while the job is still `processing` and only
        # by the attempt that owns it, so "complete" is never visible without its results.
        results = {k: v for k, v in fields.items() if k not in ("error_code", "progress") and v is not None}
        if status == "complete" and results:
            params = {"id": f"eq.{swing_id}", "status": "eq.processing", "attempts": f"eq.{int(attempts)}"}
            response = self._ok(self.http.patch(f"{self.base}/swings", params=params, json=results,
                                                headers={"Prefer": "return=representation"}), "finish results")
            if not response.json():
                return None  # this attempt no longer owns the job
        # The guarded terminal write lives in SQL so the account-lifecycle predicate is atomic with
        # the attempt/heartbeat guard; a PostgREST PATCH cannot express `not exists (...)`.
        data = self._rpc("swing_finish", {
            "p_swing_id": swing_id, "p_status": status, "p_attempts": int(attempts),
            "p_expected_updated_at": expected_updated_at, "p_older_than": older_than,
            "p_error_code": fields.get("error_code"), "p_progress": fields.get("progress")})
        rows = data if isinstance(data, list) else []
        return rows[0] if rows else None

    def list_owned(self, user_id: str, statuses: tuple[str, ...]) -> list[dict]:
        return self._rows({"user_id": f"eq.{user_id}", "status": f"in.({','.join(statuses)})", "order": "created_at.desc"})

    def _count(self, params: dict) -> int:
        response = self._ok(self.http.get(f"{self.base}/swings", params={"select": "id", **params},
                                          headers={"Prefer": "count=exact", "Range": "0-0"}), "count")
        total = response.headers.get("content-range", "*/0").split("/")[-1]
        return int(total) if total.isdigit() else 0

    def count_since(self, user_id: str, since: str) -> int:
        return self._count({"user_id": f"eq.{user_id}", "created_at": f"gte.{since}"})

    def count_active(self, user_id: str) -> int:
        return self._count({"user_id": f"eq.{user_id}", "status": "in.(awaiting_upload,queued,processing)"})

    def claim_next(self, stale_before: str, max_attempts: int) -> dict | None:
        response = self._ok(self.http.post(f"{self.base}/rpc/claim_swing_job",
                                           json={"stale_before": stale_before, "max_attempts": max_attempts}), "claim")
        rows = response.json()
        return rows[0] if rows else None

    def delete_owned(self, swing_id: str, user_id: str) -> dict | None:
        """One RPC so ownership, the deletion-month forfeit and the delete are atomic.

        PostgREST cannot express that as a single REST delete (forfeiting a reserved slot is a second
        statement), so the whole operation is the ``billing_delete`` function.
        """

        data = self._rpc("billing_delete", {"p_user_id": user_id, "p_swing_id": swing_id})
        if not isinstance(data, dict) or data.get("status") != "deleted":
            return None
        row = data.get("swing")
        return row if isinstance(row, dict) else None

    def owned_rows(self, user_id: str) -> list[dict]:
        return self._rows({"user_id": f"eq.{user_id}"})

    def delete_user(self, user_id: str) -> int:
        response = self._ok(self.http.delete(f"{self.base}/swings", params={"user_id": f"eq.{user_id}"},
                                             headers={"Prefer": "return=representation"}), "delete")
        return len(response.json())

    def where(self, status: str, older_than: str, field: str = "updated_at") -> list[dict]:
        return self._rows({"status": f"eq.{status}", field: f"lt.{older_than}"})

    def ping(self) -> bool:
        self._ok(self.http.get(f"{self.base}/swings", params={"select": "id", "limit": "1"}), "ping")
        return True

    # -- billing ledger (narrow service-role RPCs; no client table access) ------

    def _rpc(self, name: str, payload: dict) -> object:
        response = self._ok(self.http.post(f"{self.base}/rpc/{name}", json=payload), name)
        try:
            return response.json()
        except ValueError:
            raise RepoError(f"{name} returned a non-JSON body") from None

    def billing_status(self, user_id: str, free_total: int) -> dict:
        data = self._rpc("billing_status", {"p_user_id": user_id, "p_free_total": free_total})
        if not isinstance(data, dict):
            raise RepoError("billing_status returned an unexpected shape")
        return data

    def billing_admit(self, *, user_id: str, swing_id: str, kind: str, product_id: str | None,
                      size_bytes: int, free_total: int) -> dict:
        data = self._rpc("billing_admit", {
            "p_user_id": user_id, "p_swing_id": swing_id, "p_kind": kind, "p_product_id": product_id,
            "p_size_bytes": size_bytes, "p_free_total": free_total})
        if not isinstance(data, dict):
            raise RepoError("billing_admit returned an unexpected shape")
        if data.get("status") == "blocked":
            raise AccountBlocked("account is being deleted")
        return data

    def billing_finish(self, swing_id: str, *, status: str, attempts: int,
                       expected_updated_at: str | None = None, older_than: str | None = None,
                       **fields) -> dict | None:
        if status not in ("complete", "failed"):
            raise RepoError("bad billing status")
        unknown = set(fields) - set(BILLING_FINISH_FIELDS)
        if unknown:
            raise RepoError(f"unknown columns {sorted(unknown)}")
        data = self._rpc("billing_finish", {
            "p_swing_id": swing_id, "p_status": status, "p_attempts": int(attempts),
            "p_expected_updated_at": expected_updated_at,
            "p_older_than": older_than,
            "p_error_code": fields.get("error_code"), "p_notice": fields.get("notice"),
            "p_progress": fields.get("progress"), "p_summary": fields.get("summary"),
            "p_analysis_version": fields.get("analysis_version"), "p_thumb_key": fields.get("thumb_key"),
            "p_result_key": fields.get("result_key"), "p_video_key": fields.get("video_key")})
        rows = data if isinstance(data, list) else []
        return rows[0] if rows else None

    # -- durable account lifecycle (narrow service-role RPCs) -------------------

    _DELETION_FLAGS = ("apple_revoked_at", "storage_cleared_at", "data_cleared_at", "auth_deleted_at",
                       "last_error_code", "apple_revocation", "cleanup_until", "cleanup_done_at")

    def account_status(self, user_id: str) -> str:
        """A single service-role RPC on the tombstone; the client never reads the table directly."""

        data = self._rpc("account_status", {"p_user_id": user_id})
        if not isinstance(data, str) or data not in ("active", "deleting", "deleted"):
            raise RepoError("account_status returned an unexpected shape")
        return data

    def account_deletion_begin(self, user_id: str) -> dict:
        data = self._rpc("account_deletion_begin", {"p_user_id": user_id})
        if not isinstance(data, dict):
            raise RepoError("account_deletion_begin returned an unexpected shape")
        return data

    def account_deletion_get(self, user_id: str) -> dict | None:
        data = self._rpc("account_deletion_get", {"p_user_id": user_id})
        return data if isinstance(data, dict) else None

    def account_deletion_update(self, user_id: str, **fields) -> dict | None:
        unknown = set(fields) - set(self._DELETION_FLAGS)
        if unknown:
            raise RepoError(f"unknown deletion columns {sorted(unknown)}")
        data = self._rpc("account_deletion_update", {
            "p_user_id": user_id,
            # A present-but-None value must not stamp the timestamp: the flag means "this step is
            # done", and only a truthy value says so.
            "p_apple_revoked": bool(fields.get("apple_revoked_at")),
            "p_storage_cleared": bool(fields.get("storage_cleared_at")),
            "p_data_cleared": bool(fields.get("data_cleared_at")),
            "p_auth_deleted": bool(fields.get("auth_deleted_at")),
            "p_cleanup_until": fields.get("cleanup_until"),
            "p_cleanup_done": bool(fields.get("cleanup_done_at")),
            "p_apple_revocation": fields.get("apple_revocation"),
            "p_error_code": fields.get("last_error_code"),
        })
        if not isinstance(data, dict) or not data:
            raise RepoError("account_deletion_update returned no row")
        return data

    def account_deletion_finish(self, user_id: str) -> dict | None:
        data = self._rpc("account_deletion_finish", {"p_user_id": user_id})
        if not isinstance(data, dict) or not data:
            raise RepoError("account_deletion_finish returned no row")
        if data.get("error"):
            # The function refuses to finalise an account whose checkpoints are missing.
            raise RepoError(f"account_deletion_finish refused ({data['error']})")
        if data.get("status") != "deleted":
            raise RepoError("account_deletion_finish did not confirm deletion")
        return data

    def account_wipe(self, user_id: str) -> int:
        data = self._rpc("account_wipe", {"p_user_id": user_id})
        if isinstance(data, dict):
            return int(data.get("deleted") or 0)
        return int(data) if isinstance(data, int) else 0

    def feedback_submit(self, *, user_id: str, kind: str, swing_id: str | None, rating: str | None,
                        category: str | None, message: str | None, context: dict,
                        app_version: str | None) -> dict:
        data = self._rpc("feedback_submit", {
            "p_user_id": user_id, "p_kind": kind, "p_swing_id": swing_id, "p_rating": rating,
            "p_category": category, "p_message": message, "p_context": context or {}, "p_app_version": app_version})
        if not isinstance(data, dict):
            raise RepoError("feedback_submit returned an unexpected shape")
        return data

    def _apple_grant_put(self, rpc: str, user_id: str, grant_id: str, subject: str, client_id: str,
                         ciphertext: str) -> bool:
        data = self._rpc(rpc, {"p_user_id": user_id, "p_grant_id": grant_id, "p_subject": subject,
                               "p_client_id": client_id, "p_token": ciphertext})
        if not isinstance(data, dict):
            raise RepoError(f"{rpc} returned an unexpected shape")
        return bool(data.get("stored"))

    def apple_grant_put(self, user_id: str, grant_id: str, subject: str, client_id: str,
                        ciphertext: str) -> bool:
        return self._apple_grant_put("apple_grant_put", user_id, grant_id, subject, client_id, ciphertext)

    def apple_grant_put_for_deletion(self, user_id: str, grant_id: str, subject: str, client_id: str,
                                     ciphertext: str) -> bool:
        return self._apple_grant_put("apple_grant_put_for_deletion", user_id, grant_id, subject,
                                     client_id, ciphertext)

    def apple_grants_unrevoked(self, user_id: str) -> list[dict]:
        data = self._rpc("apple_grants_unrevoked", {"p_user_id": user_id})
        if not isinstance(data, list):
            # A malformed answer must never read as "no live grant".
            raise RepoError("apple_grants_unrevoked returned an unexpected shape")
        rows = [row for row in data if isinstance(row, dict)]
        if len(rows) != len(data):
            raise RepoError("apple_grants_unrevoked returned an unexpected row")
        return rows

    def apple_grant_counts(self, user_id: str) -> dict:
        data = self._rpc("apple_grant_counts", {"p_user_id": user_id})
        if not isinstance(data, dict) or not isinstance(data.get("active"), int) \
                or not isinstance(data.get("revoked"), int):
            raise RepoError("apple_grant_counts returned an unexpected shape")
        return {"active": data["active"], "revoked": data["revoked"]}

    def apple_grant_mark_revoked(self, grant_id: str, user_id: str) -> dict | None:
        data = self._rpc("apple_grant_mark_revoked", {"p_grant_id": grant_id, "p_user_id": user_id})
        if data is None or data == {}:
            return None            # already revoked: an idempotent no-op
        if not isinstance(data, dict) or data.get("id") != grant_id:
            raise RepoError("apple_grant_mark_revoked returned an unexpected shape")
        return data

    def apple_grants_delete_revoked(self, user_id: str) -> int:
        data = self._rpc("apple_grants_delete_revoked", {"p_user_id": user_id})
        if not isinstance(data, dict) or not isinstance(data.get("deleted"), int):
            raise RepoError("apple_grants_delete_revoked returned an unexpected shape")
        if data.get("active"):
            # The function reports any live grant it could not remove; never ignore that.
            raise RepoError("apple_grants_delete_revoked left a live grant")
        return data["deleted"]

    def account_deletions_for_reconcile(self, *, stuck_before: str, cleanup_before: str) -> list[dict]:
        data = self._rpc("account_deletions_for_reconcile", {
            "p_stuck_before": stuck_before, "p_cleanup_before": cleanup_before})
        rows = data if isinstance(data, list) else []
        return [row for row in rows if isinstance(row, dict)]


def make_repo(settings: Settings) -> Repo:
    if settings.db_mode == "supabase":
        return PostgrestRepo(settings)
    return SqliteRepo(settings.data_dir / "swings.sqlite3")


__all__ = ["BILLING_FINISH_FIELDS", "COLUMNS", "PostgrestRepo", "Repo", "RepoError", "SqliteRepo",
           "make_repo", "now"]
