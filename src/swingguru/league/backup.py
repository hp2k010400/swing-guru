"""League database backups off the volume, and a one-shot restore.

* **Backups:** a consistent snapshot (SQLite online backup) is verified and uploaded to the service's object storage
  (Supabase Storage in production, a private bucket) as ``league-backups/<stamp>/league.sqlite3``:
  at start-up (so every deploy and restart is covered), after every weekly reset, and at least once a day
  (checked on league requests). The newest ``KEEP`` are kept. This is independent of the Railway volume, so a
  lost or damaged volume can be restored from it.
* **Restore:** set ``SG_LEAGUE_RESTORE_KEY`` to a backup key and redeploy. At start-up the server takes a backup of
  the current database first, downloads the chosen backup, verifies it (integrity check + league tables), and only
  then installs it. A marker on the volume makes it one-shot (restarts don't restore again); remove the variable
  afterwards. Procedure: docs/agent-work/release-1.0.1/LEAGUE_BACKUP_RESTORE.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .store import LeagueStore

log = logging.getLogger("swingguru.league.backup")
PREFIX = "league-backups"
KEEP = 14
EVERY = timedelta(hours=24)


class LeagueBackups:
    def __init__(self, store: LeagueStore, storage) -> None:
        self.store = store
        self.storage = storage

    def backup(self, now: datetime, reason: str) -> str | None:
        """Snapshot, verify and upload. Returns the key, or None if it failed (never raises: leagues keep working)."""

        # Unique per backup: two backups in the same second (pre-restore, then startup) must never share a key,
        # or the second would overwrite the first.
        stamp = datetime.now(now.tzinfo).strftime("%f") if now.microsecond == 0 else now.strftime("%f")
        slug = "".join(c if c.isalnum() else "-" for c in reason.lower())[:16]
        key = f"{PREFIX}/snap-{now.strftime('%Y%m%d-%H%M%S')}-{stamp}-{slug}/league.sqlite3"
        if key in self.keys():
            key = key.replace(f"-{slug}/", f"-{slug}-{len(self.keys())}/")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "league.sqlite3"
                self.store.snapshot(path)
                problem = LeagueStore.verify_file(path)
                if problem:
                    raise RuntimeError(problem)
                self.storage.put_file(key, path, "application/vnd.sqlite3")
        except Exception:
            log.exception("league backup (%s) failed", reason)
            return None
        keys = json.loads(self.store.meta("backup_keys") or "[]") + [key]
        old, keys = keys[:-KEEP], keys[-KEEP:]
        if old:
            try:
                self.storage.delete(old)
            except Exception:
                log.exception("league backup: could not prune %d old backups", len(old))
                keys = old + keys
        self.store.set_meta("backup_keys", json.dumps(keys))
        self.store.set_meta("last_backup_at", now.isoformat(timespec="seconds"))
        log.info("league backup (%s) stored: %s", reason, key)
        return key

    def maybe_backup(self, now: datetime) -> str | None:
        last = self.store.meta("last_backup_at")
        if last and now - datetime.fromisoformat(last) < EVERY:
            return None
        return self.backup(now, "daily")

    def keys(self) -> list[str]:
        return json.loads(self.store.meta("backup_keys") or "[]")

    def restore(self, key: str, now: datetime) -> bool:
        """One-shot restore of ``key`` (see the module docstring). Returns True if it was installed now."""

        marker = Path(f"{self.store.path}.restored-{hashlib.sha256(key.encode()).hexdigest()[:16]}")
        if marker.exists():
            log.info("league restore of %s already done; remove SG_LEAGUE_RESTORE_KEY", key)
            return False
        before = self.backup(now, "pre-restore")
        if before is None:
            raise RuntimeError("league restore refused: could not back up the current database first")
        known = self.keys()
        with tempfile.TemporaryDirectory(dir=str(self.store.path.parent)) as tmp:
            path = Path(tmp) / "restore.sqlite3"
            self.storage.download_to(key, path)
            problem = LeagueStore.verify_file(path)
            if problem:
                raise RuntimeError(f"league restore refused: {problem}")
            self.store.replace_with(path)
        # Keep the list of every backup that exists (the restored file only knows the older ones).
        restored = self.keys()
        self.store.set_meta("backup_keys", json.dumps(restored + [k for k in known if k not in restored]))
        marker.write_text(json.dumps({"key": key, "at": now.isoformat(timespec="seconds"), "previous": before}))
        log.warning("league database restored from %s (previous state saved as %s)", key, before)
        return True


__all__ = ["EVERY", "KEEP", "LeagueBackups", "PREFIX"]
