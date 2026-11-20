"""SQLite storage and manifest intake for the coach-review workflow.

Two rules the rest of the package relies on:

* the stored Swing Score is **snapshotted at import** and never recomputed, so a
  later model version cannot move the answer under a submitted review;
* a reviewer's first submission is immutable. Edits are extra rows with
  ``blind = 0``; the primary comparison always uses revision 1.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import config as C

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    note TEXT,
    manifest_sha256 TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swing_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    public_id TEXT NOT NULL UNIQUE,
    swing_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    club TEXT,
    golfer_id TEXT,
    similar_group TEXT,
    video_path TEXT NOT NULL,
    analysis_path TEXT NOT NULL,
    video_sha256 TEXT NOT NULL,
    video_size INTEGER NOT NULL,
    video_mtime_ns INTEGER NOT NULL DEFAULT 0,
    analysis_sha256 TEXT NOT NULL,
    model_version TEXT,
    score_stored INTEGER NOT NULL DEFAULT 0,
    score_available INTEGER NOT NULL DEFAULT 0,
    score_total REAL,
    score_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(batch_id, swing_id)
);

CREATE TABLE IF NOT EXISTS reviewer (
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    reviewer_id TEXT NOT NULL,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer_id)
);

CREATE TABLE IF NOT EXISTS review_order (
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    reviewer_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES swing_item(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer_id, item_id)
);

CREATE TABLE IF NOT EXISTS review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    reviewer_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES swing_item(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL DEFAULT 1,
    blind INTEGER NOT NULL DEFAULT 1,
    setup_rating INTEGER NOT NULL,
    backswing_rating INTEGER NOT NULL,
    top_rating INTEGER NOT NULL,
    downswing_rating INTEGER NOT NULL,
    overall_rating INTEGER NOT NULL,
    reviewer_confidence TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    first_submitted_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, reviewer_id, item_id)
);

CREATE TABLE IF NOT EXISTS review_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES review(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    blind INTEGER NOT NULL,
    ratings_json TEXT NOT NULL,
    reviewer_confidence TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    saved_at TEXT NOT NULL,
    UNIQUE (review_id, revision)
);

CREATE TABLE IF NOT EXISTS skip (
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    reviewer_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES swing_item(id) ON DELETE CASCADE,
    skipped_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, reviewer_id, item_id)
);

CREATE TABLE IF NOT EXISTS reveal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batch(id) ON DELETE CASCADE,
    reviewer_id TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES swing_item(id) ON DELETE CASCADE,
    revealed_at TEXT NOT NULL
);
"""


class StoreError(Exception):
    """A caller-visible problem (bad request, already submitted, unknown item)."""


class ManifestError(Exception):
    """The manifest cannot be imported. Carries every problem, not just the first."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "invalid manifest")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Additive migrations: a database created by an earlier build keeps working.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(swing_item)")}
    if "score_stored" not in columns:
        conn.execute("ALTER TABLE swing_item ADD COLUMN score_stored INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE swing_item SET score_stored = CASE WHEN score_json IS NULL THEN 0 ELSE 1 END")
    if "video_mtime_ns" not in columns:
        conn.execute("ALTER TABLE swing_item ADD COLUMN video_mtime_ns INTEGER NOT NULL DEFAULT 0")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(C.SCHEMA_VERSION),))


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterable[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Manifest intake
# ---------------------------------------------------------------------------


def read_manifest(path: str | Path) -> list[dict]:
    """Read a manifest file: JSON (list or ``{"items": [...]}``) or CSV with headers."""

    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if file.suffix.lower() == ".csv":
        return [dict(row) for row in csv.DictReader(text.splitlines())]
    try:
        payload = json.loads(text)
    except ValueError as error:
        raise ManifestError([f"{file.name}: not valid JSON ({error})"]) from None
    if isinstance(payload, Mapping):
        payload = payload.get("items")
    if not isinstance(payload, list):
        raise ManifestError([f"{file.name}: expected a JSON list of items or {{'items': [...]}}"])
    rows = []
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise ManifestError([f"{file.name}: item {index} is not an object"])
        rows.append(dict(row))
    return rows


def scan_test_swings(folder: str | Path) -> list[dict]:
    """Build manifest entries from a batch of ``<swing>/bundle/analysis.json`` folders.

    Used for the existing ``var/qa/test_swings`` real batch: the clean video is the
    analysed ``bundle/swing.mp4`` (falling back to ``window.mp4``), the analysis is the
    bundle's own file. Metadata such as the club is only taken from ``report.json``
    when it is present, and never guessed.
    """

    base = Path(folder).resolve()
    entries: list[dict] = []
    for swing_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        analysis = swing_dir / "bundle" / "analysis.json"
        if not analysis.is_file():
            continue
        video = None
        for candidate in (swing_dir / "bundle" / "swing.mp4", swing_dir / "window.mp4"):
            if candidate.is_file():
                video = candidate
                break
        if video is None:
            continue
        entry = {"swing_id": swing_dir.name, "video": str(video.resolve()), "analysis": str(analysis.resolve())}
        report = swing_dir / "report.json"
        if report.is_file():
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
            except ValueError:
                payload = {}
            for key in ("club", "golfer_id", "similar_swing_group"):
                value = payload.get(key) if isinstance(payload, Mapping) else None
                # Only plain strings: report.json's "club" holds per-phase club geometry, not a label.
                if isinstance(value, str) and value.strip():
                    entry["similar_group" if key == "similar_swing_group" else key] = value.strip()
        entries.append(entry)
    return entries


def _clean_optional(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (Mapping, list, tuple, set, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def _validate_swing_id(value: Any) -> str | None:
    if value is None:
        return "an item is missing 'swing_id'"
    text = str(value).strip()
    if not text:
        return "swing_id is empty"
    if len(text) > 120:
        return f"swing_id too long ({len(text)} characters): {text[:40]}..."
    if any(ord(ch) < 32 for ch in text):
        return "swing_id contains control characters"
    return None


def _is_number(value: Any) -> bool:
    """True for a real, finite number (never a bool, never NaN/Infinity)."""

    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _score_problems(score: Any) -> list[str]:
    """Every reason a stored ``swing_score`` snapshot is unusable. Never recomputes the score.

    Both shapes must be well formed: an *available* snapshot (a finite total inside the
    scale, checks that add up) and an *unavailable* one (no total, a reason). The stored
    total is authoritative and is not compared with a recomputation.
    """

    if not isinstance(score, Mapping):
        return ["stored 'swing_score' is not an object"]
    problems: list[str] = []

    version = score.get("version")
    if not isinstance(version, str) or not version.strip():
        problems.append("stored 'swing_score' has no version string")
    if "schema" in score and not (isinstance(score["schema"], str) and score["schema"].strip()):
        problems.append("stored 'swing_score' schema is not a non-empty string")

    available = score.get("available")
    if not isinstance(available, bool):
        problems.append("stored 'swing_score' 'available' is not a boolean")
        return problems

    scale_value = score.get("scale")
    if not _is_number(scale_value) or float(scale_value) <= 0:
        problems.append("stored 'swing_score' needs a positive, finite numeric 'scale'")
        scale_value = 10.0
    scale = float(scale_value)

    eligible = score.get("eligible")
    if not isinstance(eligible, int) or isinstance(eligible, bool) or eligible <= 0:
        problems.append("stored 'swing_score' needs a positive integer 'eligible'")
        eligible = None
    minimum = score.get("minimum")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0:
        problems.append("stored 'swing_score' needs a positive integer 'minimum'")
        minimum = None
    if eligible is not None and minimum is not None and minimum > eligible:
        problems.append("stored 'swing_score' 'minimum' exceeds 'eligible'")

    checks = score.get("checks")
    if not isinstance(checks, list):
        problems.append("stored 'swing_score' 'checks' is not a list")
        checks = []
    excluded = score.get("excluded", [])
    if not isinstance(excluded, list):
        problems.append("stored 'swing_score' 'excluded' is not a list")
        excluded = []

    seen: set[str] = set()
    for index, check in enumerate(checks):
        where = f"checks[{index}]"
        if not isinstance(check, Mapping):
            problems.append(f"stored 'swing_score' {where} is not an object")
            continue
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id.strip():
            problems.append(f"stored 'swing_score' {where} has no check id")
            continue
        if check_id in seen:
            problems.append(f"stored 'swing_score' repeats check id {check_id!r}")
        seen.add(check_id)
        points = check.get("points")
        if not _is_number(points) or not 0.0 <= float(points) <= scale:
            problems.append(f"stored 'swing_score' {where} ({check_id}) points must be a finite number in "
                            f"0..{scale:g}")
        weight = check.get("weight")
        if not _is_number(weight) or float(weight) <= 0:
            problems.append(f"stored 'swing_score' {where} ({check_id}) weight must be a finite positive number")
        if check.get("confidence") not in ("HIGH", "MEDIUM"):
            problems.append(f"stored 'swing_score' {where} ({check_id}) confidence must be HIGH or MEDIUM")

    for index, row in enumerate(excluded):
        where = f"excluded[{index}]"
        if not isinstance(row, Mapping):
            problems.append(f"stored 'swing_score' {where} is not an object")
            continue
        check_id = row.get("id")
        if not isinstance(check_id, str) or not check_id.strip():
            problems.append(f"stored 'swing_score' {where} has no check id")
        elif check_id in seen:
            problems.append(f"stored 'swing_score' check id {check_id!r} is both measured and excluded")
        reason = row.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"stored 'swing_score' {where} needs a reason string")

    measured = score.get("measured")
    if not isinstance(measured, int) or isinstance(measured, bool) or measured < 0:
        problems.append("stored 'swing_score' needs a non-negative integer 'measured'")
        measured = None
    elif measured != len(checks):
        problems.append(f"stored 'swing_score' 'measured' ({measured}) does not match {len(checks)} check(s)")
    if measured is not None and eligible is not None and measured > eligible:
        problems.append("stored 'swing_score' 'measured' exceeds 'eligible'")

    coverage = score.get("coverage")
    if measured is not None and eligible:
        expected = round(measured / eligible, 2)
        if not _is_number(coverage) or abs(float(coverage) - expected) > 0.02:
            problems.append(f"stored 'swing_score' coverage should be about {expected:.2f} "
                            f"for {measured}/{eligible} checks")
    elif not _is_number(coverage):
        problems.append("stored 'swing_score' needs a finite numeric coverage")

    confidence = score.get("confidence")
    if not isinstance(confidence, Mapping):
        problems.append("stored 'swing_score' needs a 'confidence' object")
    else:
        high, medium, label = confidence.get("high"), confidence.get("medium"), confidence.get("label")
        counts_ok = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (high, medium))
        if not counts_ok:
            problems.append("stored 'swing_score' confidence 'high'/'medium' must be non-negative integers")
        elif measured is not None and int(high) + int(medium) != measured:
            problems.append("stored 'swing_score' confidence counts do not add up to 'measured'")
        if label is not None and label not in ("HIGH", "MEDIUM"):
            problems.append("stored 'swing_score' confidence label must be HIGH, MEDIUM or null")

    if available:
        total = score.get("total")
        if not _is_number(total) or not 0.0 <= float(total) <= scale:
            problems.append(f"stored 'swing_score' is available but 'total' is not a finite number in 0..{scale:g}")
        if minimum is not None and len(checks) < minimum:
            problems.append(f"stored 'swing_score' is available with {len(checks)} check(s), below its minimum "
                            f"of {minimum}")
    else:
        if score.get("total") is not None:
            problems.append("stored 'swing_score' is unavailable but carries a total")
        reason = score.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append("stored 'swing_score' is unavailable but has no reason")
    return problems


def prepare_entries(entries: Sequence[Mapping], *, root: str | Path | None = None) -> list[dict]:
    """Validate every manifest entry and return the rows ready for the database.

    Raises :class:`ManifestError` with all problems found; nothing is written.
    """

    base = Path(root).resolve() if root else Path.cwd()
    problems: list[str] = []
    prepared: list[dict] = []
    seen_ids: dict[str, int] = {}
    seen_videos: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            problems.append(f"item {index}: not an object")
            continue
        label = f"item {index}"
        problem = _validate_swing_id(entry.get("swing_id"))
        if problem:
            problems.append(f"{label}: {problem}")
            continue
        swing_id = str(entry["swing_id"]).strip()
        label = f"item {index} ({swing_id})"
        if swing_id in seen_ids:
            problems.append(f"{label}: duplicate swing_id (also item {seen_ids[swing_id]})")
            continue
        seen_ids[swing_id] = index

        video_raw, analysis_raw = entry.get("video"), entry.get("analysis")
        if not video_raw:
            problems.append(f"{label}: missing 'video' path")
            continue
        if not analysis_raw:
            problems.append(f"{label}: missing 'analysis' path")
            continue
        video = (base / str(video_raw)).resolve() if not Path(str(video_raw)).is_absolute() else Path(str(video_raw)).resolve()
        analysis = ((base / str(analysis_raw)).resolve() if not Path(str(analysis_raw)).is_absolute()
                    else Path(str(analysis_raw)).resolve())

        if not video.is_file():
            problems.append(f"{label}: video not readable: {video}")
            continue
        if video.suffix.lower() not in C.VIDEO_SUFFIXES:
            problems.append(f"{label}: unsupported video type {video.suffix or '(none)'} (allowed: "
                            f"{', '.join(C.VIDEO_SUFFIXES)})")
            continue
        size = video.stat().st_size
        if size <= 0:
            problems.append(f"{label}: video is empty: {video}")
            continue
        try:
            with video.open("rb") as handle:
                handle.read(1)
        except OSError as error:
            problems.append(f"{label}: video not readable ({error.__class__.__name__})")
            continue
        key = str(video)
        if key in seen_videos:
            problems.append(f"{label}: same video file as item {seen_videos[key]} ({video})")
            continue
        seen_videos[key] = index
        video_sha = sha256_file(video)
        if video_sha in seen_hashes:
            problems.append(f"{label}: this clip is byte-identical to item {seen_hashes[video_sha]} "
                            "(the same video would be counted twice)")
            continue
        seen_hashes[video_sha] = index
        mtime_ns = video.stat().st_mtime_ns

        if not analysis.is_file():
            problems.append(f"{label}: analysis not readable: {analysis}")
            continue
        try:
            payload = json.loads(analysis.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append(f"{label}: analysis is not valid JSON ({error.__class__.__name__})")
            continue
        if not isinstance(payload, Mapping) or not isinstance(payload.get("phases"), list):
            problems.append(f"{label}: analysis has no 'phases' list")
            continue

        score = payload.get("swing_score")
        if score is not None:
            score_problems = _score_problems(score)
            if score_problems:
                problems.extend(f"{label}: {problem}" for problem in score_problems)
                continue
        prepared.append({
            "swing_id": swing_id,
            "club": _clean_optional(entry.get("club"), 60),
            "golfer_id": _clean_optional(entry.get("golfer_id"), 60),
            "similar_group": _clean_optional(entry.get("similar_group") or entry.get("similar_swing_group"), 60),
            "video_path": str(video),
            "analysis_path": str(analysis),
            "video_sha256": video_sha,
            "video_size": size,
            "video_mtime_ns": mtime_ns,
            "analysis_sha256": sha256_file(analysis),
            "model_version": (score.get("version") if isinstance(score, Mapping) else None),
            "score_stored": score is not None,
            "score_available": bool(isinstance(score, Mapping) and score.get("available")),
            "score": dict(score) if isinstance(score, Mapping) else None,
        })

    if problems:
        raise ManifestError(problems)
    if not prepared:
        raise ManifestError(["the manifest has no items"])
    return prepared


def manifest_fingerprint(prepared: Sequence[Mapping]) -> str:
    canonical = json.dumps(
        [{k: row.get(k) for k in ("swing_id", "club", "golfer_id", "similar_group", "video_path", "analysis_path",
                                   "video_sha256", "analysis_sha256")} for row in prepared],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def import_batch(conn: sqlite3.Connection, name: str, prepared: Sequence[Mapping], *, replace: bool = False,
                 note: str | None = None) -> dict:
    """Insert a validated batch. Either the whole batch lands or nothing does.

    ``ordinal`` keeps manifest order (the stable listing/report order). The
    per-reviewer blind order is drawn separately, once, by :func:`ensure_order`.

    Two guards protect the evidence: a batch that already has reviews, skips or
    reveals is never replaced (a fresh batch name is required), and a swing id or
    video file that is already imported anywhere is refused, so the same swing can
    never be counted twice as an independent blind sample.
    """

    name = str(name).strip()
    if not name or len(name) > C.MAX_BATCH_NAME:
        raise StoreError(f"batch name must be 1-{C.MAX_BATCH_NAME} characters")
    rows = list(prepared)
    if not rows:
        raise StoreError("no items to import")
    stamp = now_iso()
    with transaction(conn):
        existing = conn.execute("SELECT id FROM batch WHERE name = ?", (name,)).fetchone()
        if existing is not None:
            if not replace:
                raise StoreError(f"batch {name!r} already exists (use --replace to overwrite it)")
            used = conn.execute(
                "SELECT (SELECT COUNT(*) FROM review WHERE batch_id = ?)"
                " + (SELECT COUNT(*) FROM skip WHERE batch_id = ?)"
                " + (SELECT COUNT(*) FROM reveal WHERE batch_id = ?) AS n",
                (existing["id"], existing["id"], existing["id"])).fetchone()["n"]
            if used:
                raise StoreError(f"batch {name!r} already holds {used} review(s), skip(s) or reveal(s); replacing it "
                                 "would delete that evidence. Import a new batch name instead.")
            conn.execute("DELETE FROM batch WHERE id = ?", (existing["id"],))
        clashes = _identity_clashes(conn, rows)
        if clashes:
            shown = "; ".join(clashes[:5]) + (" …" if len(clashes) > 5 else "")
            raise StoreError(f"already imported: {shown}. Import into the same batch to add another reviewer "
                             "instead of a new batch.")
        cursor = conn.execute(
            "INSERT INTO batch(name, note, manifest_sha256, item_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, note, manifest_fingerprint(rows), len(rows), stamp))
        batch_id = int(cursor.lastrowid)
        for position, index in enumerate(range(len(rows)), start=1):
            row = rows[index]
            conn.execute(
                "INSERT INTO swing_item(batch_id, public_id, swing_id, ordinal, club, golfer_id, similar_group,"
                " video_path, analysis_path, video_sha256, video_size, video_mtime_ns, analysis_sha256, model_version,"
                " score_stored, score_available, score_total, score_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, secrets.token_hex(8), row["swing_id"], position, row.get("club"), row.get("golfer_id"),
                 row.get("similar_group"), row["video_path"], row["analysis_path"], row["video_sha256"],
                 int(row["video_size"]), int(row["video_mtime_ns"]), row["analysis_sha256"], row.get("model_version"),
                 1 if row.get("score_stored") else 0, 1 if row.get("score_available") else 0,
                 (float(row["score"]["total"]) if row.get("score") and row["score"].get("available") else None),
                 json.dumps(row["score"], sort_keys=True) if row.get("score") else None, stamp))
    stored = sum(1 for row in rows if row.get("score_stored"))
    available = sum(1 for row in rows if row.get("score_available"))
    return {"batch": name, "items": len(rows), "scores_stored": stored, "scores_available": available,
            "scores_unavailable": len(rows) - available, "replaced": bool(existing is not None),
            "manifest_sha256": manifest_fingerprint(rows)}


def _identity_clashes(conn: sqlite3.Connection, rows: Sequence[Mapping]) -> list[str]:
    """Swing ids or video files already imported into any other batch."""

    clashes: list[str] = []
    for key, column, label in (("swing_id", "swing_id", "swing id"), ("video_sha256", "video_sha256", "video file")):
        values = [str(row[key]) for row in rows]
        for start in range(0, len(values), 200):
            chunk = values[start:start + 200]
            marks = ",".join("?" * len(chunk))
            rows_found = conn.execute(
                "SELECT DISTINCT s." + column + " AS value, b.name AS batch FROM swing_item s"
                " JOIN batch b ON b.id = s.batch_id WHERE s." + column + " IN (" + marks + ")", chunk).fetchall()
            clashes.extend(f"{label} {row['value']!r} is already in batch {row['batch']!r}" for row in rows_found)
    return sorted(set(clashes))


# ---------------------------------------------------------------------------
# Review session
# ---------------------------------------------------------------------------


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, note, item_count, manifest_sha256, created_at FROM batch ORDER BY name").fetchall()
    return [dict(row) for row in rows]


def get_batch(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT id, name, note, item_count, manifest_sha256, created_at FROM batch WHERE name = ?",
                       (name,)).fetchone()
    if row is None:
        return None
    return dict(row)


def _batch_or_error(conn: sqlite3.Connection, name: str) -> dict:
    batch = get_batch(conn, name)
    if batch is None:
        raise StoreError(f"unknown batch {name!r}")
    return batch


def ensure_order(conn: sqlite3.Connection, batch_id: int, reviewer_id: str) -> None:
    """A reviewer's swing order is randomized once and then never changes."""

    existing = conn.execute("SELECT COUNT(*) AS n FROM review_order WHERE batch_id = ? AND reviewer_id = ?",
                            (batch_id, reviewer_id)).fetchone()["n"]
    if existing:
        return
    items = [row["id"] for row in conn.execute("SELECT id FROM swing_item WHERE batch_id = ? ORDER BY ordinal", (batch_id,))]
    seed = hashlib.sha256(f"{batch_id}|{reviewer_id}".encode("utf-8")).hexdigest()
    rng = random.Random(int(seed, 16))
    rng.shuffle(items)
    stamp = now_iso()
    with transaction(conn):
        for position, item_id in enumerate(items, start=1):
            conn.execute("INSERT OR IGNORE INTO review_order(batch_id, reviewer_id, item_id, position, created_at)"
                         " VALUES (?, ?, ?, ?, ?)", (batch_id, reviewer_id, item_id, position, stamp))


def _review_map(conn: sqlite3.Connection, batch_id: int, reviewer_id: str) -> dict[int, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM review WHERE batch_id = ? AND reviewer_id = ?", (batch_id, reviewer_id)).fetchall()
    return {int(row["item_id"]): row for row in rows}


def _skip_set(conn: sqlite3.Connection, batch_id: int, reviewer_id: str) -> set[int]:
    rows = conn.execute("SELECT item_id FROM skip WHERE batch_id = ? AND reviewer_id = ?", (batch_id, reviewer_id)).fetchall()
    return {int(row["item_id"]) for row in rows}


def session_view(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, reviewer_name: str,
                 *, edit_mode: bool = False) -> dict:
    """Everything the browser may know before a submission: no score, no paths, no swing ids."""

    batch = _batch_or_error(conn, batch_name)
    ensure_order(conn, batch["id"], reviewer_id)
    reviews = _review_map(conn, batch["id"], reviewer_id)
    skipped = _skip_set(conn, batch["id"], reviewer_id)
    rows = conn.execute(
        "SELECT s.id AS item_id, s.public_id, s.club, o.position"
        " FROM review_order o JOIN swing_item s ON s.id = o.item_id"
        " WHERE o.batch_id = ? AND o.reviewer_id = ? ORDER BY o.position", (batch["id"], reviewer_id)).fetchall()
    items = []
    for row in rows:
        item_id = int(row["item_id"])
        review = reviews.get(item_id)
        state = "submitted" if review else ("skipped" if item_id in skipped else "new")
        items.append({
            "item_id": row["public_id"],
            "ordinal": int(row["position"]),
            "club": row["club"],
            "state": state,
            "revision": int(review["revision"]) if review else None,
            "locked": bool(review) and not edit_mode,
            "video": f"/media/{row['public_id']}",
        })
    reviewed = sum(1 for item in items if item["state"] == "submitted")
    return {
        "reviewer": {"id": reviewer_id, "name": reviewer_name},
        "batch": {"name": batch["name"], "items": len(items), "reviewed": reviewed,
                  "skipped": sum(1 for item in items if item["state"] == "skipped")},
        "edit_mode": bool(edit_mode),
        "ratings": [{"id": field, "label": C.RATING_LABELS[field], "min": C.RATING_MIN, "max": C.RATING_MAX}
                    for field in C.RATING_FIELDS],
        "confidence_levels": list(C.CONFIDENCE_LEVELS),
        "items": items,
    }


def _item_row(conn: sqlite3.Connection, batch_id: int, public_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM swing_item WHERE batch_id = ? AND public_id = ?", (batch_id, public_id)).fetchone()
    if row is None:
        raise StoreError("unknown swing")
    return row


def _review_payload(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "setup": row["setup_rating"], "backswing": row["backswing_rating"], "top": row["top_rating"],
        "downswing": row["downswing_rating"], "overall": row["overall_rating"],
        "confidence": row["reviewer_confidence"], "notes": row["notes"],
        "revision": int(row["revision"]), "blind": bool(row["blind"]),
        "first_submitted_at": row["first_submitted_at"], "updated_at": row["updated_at"],
    }


def item_view(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, public_id: str,
              *, edit_mode: bool = False) -> dict:
    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    order = conn.execute("SELECT position FROM review_order WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                         (batch["id"], reviewer_id, item["id"])).fetchone()
    review = conn.execute("SELECT * FROM review WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                          (batch["id"], reviewer_id, item["id"])).fetchone()
    skipped = conn.execute("SELECT 1 FROM skip WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                           (batch["id"], reviewer_id, item["id"])).fetchone() is not None
    state = "submitted" if review else ("skipped" if skipped else "new")
    return {
        "item_id": item["public_id"],
        "ordinal": int(order["position"]) if order else int(item["ordinal"]),
        "club": item["club"],
        "state": state,
        "locked": bool(review) and not edit_mode,
        "video": f"/media/{item['public_id']}",
        "review": _review_payload(review),
    }


def validate_ratings(payload: Any) -> dict[str, int]:
    if not isinstance(payload, Mapping):
        raise StoreError("ratings must be an object")
    missing = [field for field in C.RATING_FIELDS if field not in payload]
    if missing:
        raise StoreError("missing ratings: " + ", ".join(missing))
    extra = [key for key in payload if key not in C.RATING_FIELDS]
    if extra:
        raise StoreError("unexpected rating fields: " + ", ".join(sorted(extra)))
    out: dict[str, int] = {}
    for field in C.RATING_FIELDS:
        value = payload[field]
        # bool is an int subclass; floats and strings are rejected rather than coerced.
        if isinstance(value, bool) or not isinstance(value, int):
            raise StoreError(f"{field} rating must be a whole number from {C.RATING_MIN} to {C.RATING_MAX}")
        if not C.RATING_MIN <= value <= C.RATING_MAX:
            raise StoreError(f"{field} rating must be from {C.RATING_MIN} to {C.RATING_MAX}")
        out[field] = value
    return out


def submit_review(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, public_id: str, *,
                  ratings: Any, confidence: Any, notes: Any = "", allow_edit: bool = False) -> dict:
    """Store a blind submission, or (edit mode only) append a clearly post-reveal revision."""

    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    clean = validate_ratings(ratings)
    level = str(confidence or "").upper()
    if level not in C.CONFIDENCE_LEVELS:
        raise StoreError("confidence must be one of " + ", ".join(C.CONFIDENCE_LEVELS))
    text = "" if notes is None else str(notes).strip()
    if len(text) > C.MAX_NOTES:
        raise StoreError(f"notes must be {C.MAX_NOTES} characters or fewer")

    stamp = now_iso()
    with transaction(conn):
        existing = conn.execute("SELECT * FROM review WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                                (batch["id"], reviewer_id, item["id"])).fetchone()
        if existing is not None and not allow_edit:
            raise StoreError("this swing is already submitted and locked")
        revision = 1 if existing is None else int(existing["revision"]) + 1
        blind = 1 if existing is None else 0
        first = stamp if existing is None else existing["first_submitted_at"]
        values = [clean[field] for field in C.RATING_FIELDS]
        if existing is None:
            cursor = conn.execute(
                "INSERT INTO review(batch_id, reviewer_id, item_id, revision, blind, setup_rating, backswing_rating,"
                " top_rating, downswing_rating, overall_rating, reviewer_confidence, notes, first_submitted_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (batch["id"], reviewer_id, item["id"], revision, blind, *values, level, text, first, stamp))
            review_id = int(cursor.lastrowid)
        else:
            review_id = int(existing["id"])
            conn.execute(
                "UPDATE review SET revision = ?, blind = ?, setup_rating = ?, backswing_rating = ?, top_rating = ?,"
                " downswing_rating = ?, overall_rating = ?, reviewer_confidence = ?, notes = ?, updated_at = ?"
                " WHERE id = ?",
                (revision, blind, *values, level, text, stamp, review_id))
        conn.execute(
            "INSERT INTO review_revision(review_id, revision, blind, ratings_json, reviewer_confidence, notes, saved_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (review_id, revision, blind, json.dumps(clean, sort_keys=True), level, text, stamp))
        conn.execute("DELETE FROM skip WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                     (batch["id"], reviewer_id, item["id"]))
    return {"item_id": public_id, "revision": revision, "blind": bool(blind), "first_submitted_at": first,
            "updated_at": stamp}


def skip_item(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, public_id: str) -> dict:
    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    with transaction(conn):
        review = conn.execute("SELECT 1 FROM review WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                              (batch["id"], reviewer_id, item["id"])).fetchone()
        if review is not None:
            raise StoreError("this swing is already submitted")
        conn.execute("INSERT OR IGNORE INTO skip(batch_id, reviewer_id, item_id, skipped_at) VALUES (?, ?, ?, ?)",
                     (batch["id"], reviewer_id, item["id"], now_iso()))
    return {"item_id": public_id, "state": "skipped"}


def unskip_item(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, public_id: str) -> dict:
    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    with transaction(conn):
        conn.execute("DELETE FROM skip WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                     (batch["id"], reviewer_id, item["id"]))
    return {"item_id": public_id, "state": "new"}


def score_view(conn: sqlite3.Connection, batch_name: str, reviewer_id: str, public_id: str) -> dict:
    """The stored score, only once this reviewer has submitted a rating for this swing."""

    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    review = conn.execute("SELECT 1 FROM review WHERE batch_id = ? AND reviewer_id = ? AND item_id = ?",
                          (batch["id"], reviewer_id, item["id"])).fetchone()
    if review is None:
        raise StoreError("the score stays hidden until you submit this swing")
    with transaction(conn):
        conn.execute("INSERT INTO reveal(batch_id, reviewer_id, item_id, revealed_at) VALUES (?, ?, ?, ?)",
                     (batch["id"], reviewer_id, item["id"], now_iso()))
    score = json.loads(item["score_json"]) if item["score_json"] else None
    return {"item_id": public_id, "swing_id": item["swing_id"], "model_version": item["model_version"],
            "stored": score is not None, "score": score}


def video_path_for(conn: sqlite3.Connection, batch_name: str, public_id: str) -> Path:
    """The registered video file for a public id; the only media the server will ever send."""

    batch = _batch_or_error(conn, batch_name)
    item = _item_row(conn, batch["id"], public_id)
    return Path(item["video_path"])


def video_integrity_problems(conn: sqlite3.Connection, batch_name: str) -> list[str]:
    """Videos that no longer match the bytes imported with this batch's score snapshot.

    The cheap signature (size, and mtime when it was recorded) is checked first; the
    file is re-hashed only when that signature moved. A changed file would pair a new
    swing video with an old score, which is exactly what the snapshot must prevent.
    """

    batch = _batch_or_error(conn, batch_name)
    problems: list[str] = []
    rows = conn.execute("SELECT swing_id, video_path, video_sha256, video_size, video_mtime_ns"
                        " FROM swing_item WHERE batch_id = ? ORDER BY ordinal", (batch["id"],)).fetchall()
    for row in rows:
        path = Path(row["video_path"])
        if not path.is_file():
            problems.append(f"{row['swing_id']}: the video file is missing ({path.name})")
            continue
        stat = path.stat()
        if stat.st_size != int(row["video_size"]):
            problems.append(f"{row['swing_id']}: video size changed since import "
                            f"({row['video_size']} -> {stat.st_size} bytes)")
            continue
        if row["video_mtime_ns"] and stat.st_mtime_ns == int(row["video_mtime_ns"]):
            continue
        if sha256_file(path) != row["video_sha256"]:
            problems.append(f"{row['swing_id']}: video content changed since import (hash differs)")
    return problems


def remember_reviewer(conn: sqlite3.Connection, batch_name: str, reviewer_id: str,
                      display_name: str | None = None) -> None:
    """Record the operator-supplied display name so reports can show it, not just the id."""

    batch = _batch_or_error(conn, batch_name)
    clean = (display_name or "").strip()[:C.MAX_REVIEWER_NAME] or None
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO reviewer(batch_id, reviewer_id, display_name, first_seen_at)"
                     " VALUES (?, ?, ?, ?)", (batch["id"], reviewer_id, clean, now_iso()))
        if clean:
            conn.execute("UPDATE reviewer SET display_name = ? WHERE batch_id = ? AND reviewer_id = ?"
                         " AND (display_name IS NULL OR display_name <> ?)",
                         (clean, batch["id"], reviewer_id, clean))


# ---------------------------------------------------------------------------
# Report dataset
# ---------------------------------------------------------------------------


def report_dataset(conn: sqlite3.Connection, batch_name: str | None = None) -> dict:
    if batch_name is not None:
        batch = get_batch(conn, batch_name)
        if batch is None:
            raise StoreError(f"unknown batch {batch_name!r}")
        batches = [batch]
    else:
        batches = [dict(row) for row in conn.execute("SELECT id, name, note, item_count, manifest_sha256, created_at"
                                                     " FROM batch ORDER BY name")]
    where, params = ("WHERE b.name = ?", (batch_name,)) if batch_name else ("", ())
    items = [dict(row) for row in conn.execute(
        "SELECT s.id, s.public_id, s.swing_id, s.club, s.golfer_id, s.similar_group, s.model_version,"
        " s.score_stored, s.score_available, s.score_total, s.score_json, s.analysis_sha256, s.video_sha256,"
        " s.video_size, b.name AS batch"
        " FROM swing_item s JOIN batch b ON b.id = s.batch_id " + where + " ORDER BY b.name, s.ordinal", params)]
    by_id = {int(row["id"]): row for row in items}
    for row in items:
        row["score"] = json.loads(row.pop("score_json")) if row["score_json"] else None

    reviews = []
    for row in conn.execute(
        "SELECT r.*, b.name AS batch, s.swing_id, s.club FROM review r"
        " JOIN batch b ON b.id = r.batch_id JOIN swing_item s ON s.id = r.item_id " + where +
            " ORDER BY b.name, s.ordinal", params):
        payload = dict(row)
        payload["ratings"] = {field: row[f"{field}_rating"] for field in C.RATING_FIELDS}
        reviews.append(payload)

    revisions = [dict(row) for row in conn.execute(
        "SELECT v.review_id, v.revision, v.blind, v.ratings_json, v.reviewer_confidence, v.notes, v.saved_at"
        " FROM review_revision v JOIN review r ON r.id = v.review_id JOIN batch b ON b.id = r.batch_id " + where +
        " ORDER BY v.review_id, v.revision", params)]
    for row in revisions:
        row["ratings"] = json.loads(row.pop("ratings_json"))

    skips = [dict(row) for row in conn.execute(
        "SELECT k.batch_id, k.reviewer_id, k.item_id, k.skipped_at, s.swing_id FROM skip k"
        " JOIN swing_item s ON s.id = k.item_id JOIN batch b ON b.id = k.batch_id " + where +
        " ORDER BY s.ordinal", params)]

    counts_by_reviewer: dict[str, int] = {}
    for row in reviews:
        counts_by_reviewer[str(row["reviewer_id"])] = counts_by_reviewer.get(str(row["reviewer_id"]), 0) + 1

    reviewers = [dict(row) for row in conn.execute(
        "SELECT r.batch_id, r.reviewer_id, r.display_name, r.first_seen_at, b.name AS batch FROM reviewer r"
        " JOIN batch b ON b.id = r.batch_id " + where + " ORDER BY b.name, r.reviewer_id", params)]

    return {
        "batches": batches,
        "items": items,
        "items_by_id": by_id,
        "reviews": reviews,
        "revisions": revisions,
        "skips": skips,
        "reviewers": reviewers,
        "reviewer_counts": counts_by_reviewer,
    }


__all__ = ["ManifestError", "StoreError", "connect", "ensure_order", "get_batch", "import_batch", "init_db",
           "item_view", "list_batches", "manifest_fingerprint", "now_iso", "prepare_entries", "read_manifest",
           "remember_reviewer", "report_dataset", "scan_test_swings", "score_view", "session_view", "sha256_file",
           "skip_item", "submit_review", "transaction", "unskip_item", "validate_ratings", "video_integrity_problems",
           "video_path_for"]
