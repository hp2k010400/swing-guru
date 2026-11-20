"""League tables (SQLite, staging). The production Postgres migration is docs/agent-work/release-1.0.1/league_migration.sql
(written, NOT applied). Kept in its own database file so the league can never touch swing or billing rows.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
create table if not exists league_profiles (
  user_id text primary key,
  display_name text not null,
  division text not null default 'Bronze',
  joined_at text not null
);
create table if not exists league_weeks (
  week_start text primary key,             -- Monday 00:00 UTC, ISO date
  status text not null,                    -- open | closed
  formed_at text not null,
  closed_at text
);
create table if not exists league_groups (
  id text primary key,                     -- '<week>:<division>:<n>'
  week_start text not null,
  division text not null,
  seq integer not null
);
create table if not exists league_members (
  week_start text not null,
  member_key text not null,                -- user id, or a league_rivals.id
  kind text not null,                      -- user | rival
  group_id text not null,
  division text not null,
  base text not null,                      -- frozen baseline + pre-week check statuses (JSON)
  joined_at text not null,
  final_lp integer, final_position integer, outcome text, reached_at text, breakdown text,
  primary key (week_start, member_key)
);
create index if not exists league_members_group on league_members (group_id);
create table if not exists league_rivals (
  id text primary key,
  display_name text not null,
  persona text not null,
  division text not null,
  created_division text not null,
  created_index integer not null unique,
  created_at text not null
);
create table if not exists league_meta (
  key text primary key,
  value text not null
);
-- This week's distinct swings per player, captured as they are seen (one row per swing id, insert-or-ignore).
-- A swing deleted during the week is kept as withdrawn (no more practice/swing LP, score still counts) so
-- deleting can never raise LP. Rows are purged when the week closes; only the week's result is kept.
create table if not exists league_entries (
  user_id text not null,
  week_start text not null,
  swing_id text not null,
  at text not null,
  record text not null,
  withdrawn integer not null default 0,
  primary key (user_id, swing_id)
);
"""


class LeagueStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self._lock:
            db = self._connect()
            try:
                db.executescript(SCHEMA)
            finally:
                db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("pragma journal_mode=wal")
        db.execute("pragma busy_timeout=30000")
        return db

    @contextmanager
    def tx(self):
        """One write transaction (BEGIN IMMEDIATE takes the database write lock up front)."""

        with self._lock:
            db = self._connect()
            try:
                db.execute("begin immediate")
                yield db
                db.execute("commit")
            except BaseException:
                if db.in_transaction:
                    db.execute("rollback")
                raise
            finally:
                db.close()

    # -- backup / restore ---------------------------------------------------------

    def snapshot(self, dest: Path) -> None:
        """A consistent copy of the whole database (SQLite online backup API: safe while in use, WAL included)."""

        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            src = self._connect()
            out = sqlite3.connect(dest)
            try:
                src.backup(out)
            finally:
                out.close()
                src.close()

    @staticmethod
    def verify_file(path: Path) -> str | None:
        """Problem with a database file meant for restore (None = usable): integrity and the league tables."""

        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                if db.execute("pragma integrity_check").fetchone()[0] != "ok":
                    return "integrity check failed"
                tables = {r[0] for r in db.execute("select name from sqlite_master where type = 'table'")}
            finally:
                db.close()
        except sqlite3.DatabaseError as error:
            return f"not a readable database ({type(error).__name__})"
        missing = {"league_profiles", "league_weeks", "league_members", "league_rivals"} - tables
        return f"missing tables: {', '.join(sorted(missing))}" if missing else None

    def replace_with(self, source: Path) -> None:
        """Install ``source`` as the database (restore). Single process: the store lock blocks other writers."""

        with self._lock:
            for suffix in ("-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            os.replace(source, self.path)
            db = self._connect()
            try:
                db.executescript(SCHEMA)       # any table added since the snapshot
            finally:
                db.close()

    def meta(self, key: str) -> str | None:
        row = self.one("select value from league_meta where key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as db:
            db.execute("insert into league_meta (key, value) values (?, ?) on conflict(key) do update set value = excluded.value",
                       (key, value))

    def read(self, sql: str, args: tuple = ()) -> list[dict]:
        db = self._connect()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.read(sql, args)
        return rows[0] if rows else None

    # -- profiles --------------------------------------------------------------

    def profile(self, user_id: str) -> dict | None:
        return self.one("select * from league_profiles where user_id = ?", (user_id,))

    def profiles(self) -> list[dict]:
        return self.read("select * from league_profiles order by joined_at, user_id")

    def name_taken(self, name: str, user_id: str) -> bool:
        """Taken by another player or by a practice rival (so a group never shows the same name twice)."""

        return (self.one("select 1 from league_profiles where lower(display_name) = lower(?) and user_id != ?",
                         (name, user_id)) is not None
                or self.one("select 1 from league_rivals where lower(display_name) = lower(?)", (name,)) is not None)

    def purge_user(self, user_id: str) -> None:
        """Remove everything the league holds for this user (account deletion / leaving)."""

        with self.tx() as db:
            for table, col in (("league_entries", "user_id"), ("league_members", "member_key"),
                               ("league_profiles", "user_id")):
                db.execute(f"delete from {table} where {col} = ?", (user_id,))

    # -- weeks, groups, members --------------------------------------------------

    def week(self, week: str) -> dict | None:
        return self.one("select * from league_weeks where week_start = ?", (week,))

    def latest_week(self) -> dict | None:
        return self.one("select * from league_weeks order by week_start desc limit 1")

    def member(self, week: str, key: str) -> dict | None:
        row = self.one("select * from league_members where week_start = ? and member_key = ?", (week, key))
        return _member(row) if row else None

    def group_members(self, group_id: str) -> list[dict]:
        return [_member(r) for r in self.read("select * from league_members where group_id = ? order by member_key",
                                              (group_id,))]

    def week_members(self, week: str) -> list[dict]:
        return [_member(r) for r in self.read("select * from league_members where week_start = ?", (week,))]

    def groups(self, week: str) -> list[dict]:
        return self.read("select * from league_groups where week_start = ? order by division, seq", (week,))

    def last_result(self, key: str, before_week: str) -> dict | None:
        row = self.one("select * from league_members where member_key = ? and week_start < ? and outcome is not null "
                       "order by week_start desc limit 1", (key, before_week))
        return _member(row) if row else None

    def results(self, key: str, limit: int = 12) -> list[dict]:
        return [_member(r) for r in self.read(
            "select * from league_members where member_key = ? and outcome is not null order by week_start desc limit ?",
            (key, limit))]

    # -- rivals ------------------------------------------------------------------

    def rivals(self, division: str | None = None) -> list[dict]:
        rows = (self.read("select * from league_rivals where division = ? order by created_index", (division,))
                if division else self.read("select * from league_rivals order by created_index"))
        for r in rows:
            r["persona"] = json.loads(r["persona"])
        return rows

    def rival(self, rival_id: str) -> dict | None:
        row = self.one("select * from league_rivals where id = ?", (rival_id,))
        if row:
            row["persona"] = json.loads(row["persona"])
        return row

    def rival_count(self) -> int:
        return self.one("select count(*) as n from league_rivals")["n"]

    # -- entries -----------------------------------------------------------------

    def entries(self, user_id: str, week: str) -> list[dict]:
        rows = self.read("select * from league_entries where user_id = ? and week_start = ? order by at",
                         (user_id, week))
        return [{"id": r["swing_id"], "at": r["at"], "record": json.loads(r["record"]), "withdrawn": bool(r["withdrawn"])}
                for r in rows]

    def capture(self, user_id: str, week: str, swings: list[dict], *, withdrawn: bool = False) -> None:
        """Insert-or-ignore this week's swings; a swing seen again changes nothing (idempotent)."""

        if not swings:
            return
        with self.tx() as db:
            for s in swings:
                db.execute("insert or ignore into league_entries (user_id, week_start, swing_id, at, record, withdrawn) "
                           "values (?, ?, ?, ?, ?, ?)",
                           (user_id, week, s["id"], s["at"], json.dumps(s["record"], separators=(",", ":")),
                            1 if withdrawn else 0))

    def withdraw(self, user_id: str, swing_id: str) -> None:
        with self.tx() as db:
            db.execute("update league_entries set withdrawn = 1 where user_id = ? and swing_id = ?", (user_id, swing_id))


def _member(row: dict) -> dict:
    out = dict(row)
    out["base"] = json.loads(out["base"]) if out.get("base") else {}
    out["breakdown"] = json.loads(out["breakdown"]) if out.get("breakdown") else None
    return out
