"""Explicit, atomic run-progress events for the analysis stages.

Two vocabularies live in one run-local, versioned JSON artifact:

    work/progress.json   (schema ``swingguru.run_progress/1``)

* the **five public stages** the mobile client renders (unchanged ids/labels),
  set only at real code boundaries; and
* **internal substages** (``probe``, ``window``, ``cut``, ``pose``,
  ``phase_detection``, ``club_tracking``, ``camera``, ``render``) that say
  exactly where a run stopped and which timings are real. They are diagnostics,
  never client copy.

Writes are atomic: a uniquely named temp file in the same directory is written
and then :func:`os.replace`d over the target, so a reader never sees a partial
artifact and a failed write cannot touch the real one. Events are append-only
and merged by furthest state per stage, so a late or replayed event can never
regress a completed stage.

**Single writer handoff.** The runner writes the artifact until it starts the
engine subprocess, then writes nothing until that process exits; the engine is
the only writer while it runs. That is the supported usage and what the
implementation assumes: concurrent writers from different processes are *not*
serialised (there is no file lock), so a caller that needs true concurrency
must serialise it. Within one process the merge is still correct.

Nothing here fabricates an elapsed-time percentage: the artifact carries the
monotonic timestamp of each real event, and nothing else.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

SCHEMA = "swingguru.run_progress/1"
VERSION = 1
FILENAME = "progress.json"
MAX_EVENTS = 200

# The public stage ids/labels. Stable for mobile phase D: extend by adding new
# ids, never by renaming or reordering these.
STAGES: tuple[tuple[str, str], ...] = (
    ("prepare", "Preparing your swing"),
    ("moments", "Finding key moments"),
    ("club", "Tracking the club"),
    ("positions", "Checking your positions"),
    ("build", "Building your analysis"),
)
STAGE_IDS: tuple[str, ...] = tuple(key for key, _ in STAGES)
STAGE_LABELS: dict[str, str] = dict(STAGES)

# Internal boundaries: real stages for diagnostics and failure attribution.
# They are not part of the client contract and may be renamed/extended freely.
INTERNAL_STAGES: tuple[tuple[str, str], ...] = (
    ("probe", "probe"),
    ("window", "swing window"),
    ("cut", "window cut"),
    ("pose", "pose"),
    ("phase_detection", "phase detection"),
    ("club_tracking", "club tracking"),
    ("camera", "camera check"),
    ("render", "bundle render"),
)
INTERNAL_IDS: tuple[str, ...] = tuple(key for key, _ in INTERNAL_STAGES)
INTERNAL_LABELS: dict[str, str] = dict(INTERNAL_STAGES)

PENDING, ACTIVE, DONE = "pending", "active", "done"
_ORDER = {PENDING: 0, ACTIVE: 1, DONE: 2}


class UnknownStage(ValueError):
    """Raised for a stage id that is not part of the public contract."""


def artifact_path(work: Path | str) -> Path:
    return Path(work) / FILENAME


def _now_ms() -> float:
    return round(time.monotonic() * 1000.0, 1)


def _valid_event(event: object) -> dict | None:
    """A usable event, or ``None`` for anything malformed.

    A corrupt artifact must never break an analysis: unknown stages, unknown
    states, non-dict entries, missing timestamps and non-numeric timestamps are
    all dropped rather than raised.
    """

    if not isinstance(event, dict):
        return None
    stage, state = event.get("stage"), event.get("state")
    if stage not in STAGE_LABELS and stage not in INTERNAL_LABELS:
        return None
    if state not in _ORDER:
        return None
    at_ms = event.get("at_ms")
    if at_ms is not None:
        if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)):
            return None
        at_ms = float(at_ms)
    scope = event.get("scope", "public")
    if scope not in ("public", "internal"):
        scope = "public"
    detail = event.get("detail")
    return {"stage": stage, "state": state, "at_ms": at_ms,
            "detail": detail if isinstance(detail, str) else None,
            "source": event.get("source") if isinstance(event.get("source"), str) else None,
            "scope": scope}


def _events(data: object) -> list[dict]:
    """Validated events from a decoded artifact (never raises)."""

    if not isinstance(data, dict):
        return []
    raw = data.get("events")
    if not isinstance(raw, list):
        return []
    out = [event for event in (_valid_event(item) for item in raw) if event is not None]
    return out[-MAX_EVENTS:]


def read(work: Path | str) -> dict | None:
    """The artifact for ``work``, or ``None`` when it is missing or unusable."""

    path = artifact_path(work)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    return data


def _derive(events: list[dict], *, scope: str = "public") -> list[dict]:
    """Ordered stage rows from the append-only events, furthest state per stage."""

    stages = STAGES if scope == "public" else INTERNAL_STAGES
    best: dict[str, dict] = {}
    for event in events:
        if (event.get("scope") or "public") != scope:
            continue
        stage, state = event.get("stage"), event.get("state")
        if stage not in (STAGE_LABELS if scope == "public" else INTERNAL_LABELS) or state not in _ORDER:
            continue
        current = best.get(stage)
        if current is None or _ORDER[state] > _ORDER[current["state"]] or (
                _ORDER[state] == _ORDER[current["state"]] and event.get("at_ms", 0) < current["at_ms"]):
            best[stage] = {"state": state, "at_ms": event.get("at_ms") or 0.0,
                           "detail": event.get("detail") or None, "source": event.get("source") or None}
    rows = []
    for stage, label in stages:
        found = best.get(stage, {"state": PENDING, "at_ms": None, "detail": None, "source": None})
        rows.append({"id": stage, "label": label, "state": found["state"], "at_ms": found["at_ms"],
                     "detail": found["detail"], "source": found["source"]})
    return rows


def _replace(source: str, target: Path, attempts: int = 8) -> None:
    """os.replace with a short bounded retry: on Windows a reader (or a virus scanner) holding the
    target open makes the replace fail transiently; on POSIX the first call succeeds."""

    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _read_existing(work: Path) -> dict:
    """The current artifact. A transiently unreadable file is retried rather than treated as empty,
    which would otherwise drop every earlier event on the next write."""

    path = artifact_path(work)
    for attempt in range(8):
        data = read(work)
        if data is not None or not path.exists():
            return data or {}
        time.sleep(0.01 * (attempt + 1))
    return {}


def record(work: Path | str, stage: str, state: str, *, detail: str | None = None,
           source: str = "runner", internal: bool = False) -> dict | None:
    """Append one real event and rewrite the artifact atomically.

    Returns the written artifact (or ``None`` when the write failed - progress
    must never break an analysis). ``detail`` is a short internal note such as
    ``"pose cache: hit"``; it must not contain paths, URLs or user data.
    ``internal=True`` records a diagnostic substage instead of a public stage.
    """

    known = INTERNAL_LABELS if internal else STAGE_LABELS
    if stage not in known:
        raise UnknownStage(stage)
    if state not in _ORDER:
        raise ValueError(f"state must be one of {sorted(_ORDER)}")
    work = Path(work)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    previous = _read_existing(work)
    events = _events(previous)
    events.append({"stage": stage, "state": state, "at_ms": _now_ms(), "detail": detail,
                   "source": source, "scope": "internal" if internal else "public"})
    payload = {"schema": SCHEMA, "version": VERSION, "updated_at": time.time(),
               "stages": _derive(events, scope="public"),
               "internal": _derive(events, scope="internal"),
               "events": events[-MAX_EVENTS:]}
    path = artifact_path(work)
    # A unique temp name: two writers can never share (and then replace) the
    # same partial file. The handoff is still single-writer by contract.
    temp_name: str | None = None
    try:
        handle_fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        _replace(temp_name, path)
        temp_name = None
    except OSError:
        return None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
    return payload


def timeline(work: Path | str) -> dict | None:
    """The recorded stage timeline for diagnostics (real events only)."""

    data = read(work)
    if data is None:
        return None
    events = _events(data)
    rows = _derive(events, scope="public")
    internal = _derive(events, scope="internal")
    return {"schema": data.get("schema"), "version": data.get("version"),
            "stages": [{k: row.get(k) for k in ("id", "label", "state", "at_ms", "detail", "source")} for row in rows],
            "internal": [{k: row.get(k) for k in ("id", "label", "state", "at_ms", "detail", "source")}
                         for row in internal],
            "events": events}


def stage_rows(work: Path | str) -> list[dict] | None:
    """Derived stage rows (with ``at_ms``) or ``None`` when nothing was recorded."""

    data = read(work)
    if data is None:
        return None
    return _derive(_events(data), scope="public")


def internal_rows(work: Path | str) -> list[dict] | None:
    """Derived internal substage rows, or ``None`` when nothing was recorded."""

    data = read(work)
    if data is None:
        return None
    return _derive(_events(data), scope="internal")


def stage_durations_ms(work: Path | str) -> dict[str, float | None] | None:
    """Real per-stage durations from the recorded start and done events.

    A stage that only ever recorded ``done`` (cached or skipped work) has no
    measured duration, so its value is ``None`` - never a fabricated number.
    """

    data = read(work)
    if data is None:
        return None
    events = _events(data)
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for event in events:
        if event["at_ms"] is None:
            continue
        stage = event["stage"]
        # A start is a real non-done event; a lone "done" (cached/skipped work)
        # has no measured duration and must stay unknown.
        if event["state"] != DONE:
            starts.setdefault(stage, event["at_ms"])
        if event["state"] == DONE and stage not in ends:
            ends[stage] = event["at_ms"]
    out: dict[str, float | None] = {}
    for stage in STAGE_IDS + INTERNAL_IDS:
        end = ends.get(stage)
        start = starts.get(stage)
        out[stage] = None if end is None or start is None or end < start else round(end - start, 1)
    return out


def _legacy_states(work: Path, *, prepared: bool, finished: bool, running: bool) -> list[dict]:
    """The original filename inference, kept for old bundles and fake runners."""

    if finished:
        return [{"id": key, "label": label, "state": DONE} for key, label in STAGES]
    bundle = Path(work) / "bundle"
    done = {
        "prepare": prepared,
        "moments": (bundle / "pose_records.pkl").exists(),
        "club": (bundle / "club_fallback.json").exists(),
        "positions": (bundle / "swing.mp4").exists(),
        "build": False,
    }
    out, active_set = [], False
    for key, label in STAGES:
        state = DONE if done[key] else PENDING
        if state == PENDING and not active_set and running:
            state, active_set = ACTIVE, True
        out.append({"id": key, "label": label, "state": state})
    return out


def stage_states(work: Path | str, *, prepared: bool, finished: bool, running: bool) -> list[dict]:
    """The five public stage rows for the mobile client.

    Reads the explicit artifact when the run wrote one; falls back to the
    original file inference for bundles from older builds and for test/plugin
    runners that never record events. A finished run is always all-done.

    With an explicit artifact nothing is invented: stages that never recorded an
    event stay ``pending`` (the old fallback's "first pending looks active"
    heuristic applies only to legacy bundles). While ``running`` is true a stage
    list is never all-done, because ready is only advertised after the worker
    has committed the result.
    """

    if finished:
        return [{"id": key, "label": label, "state": DONE} for key, label in STAGES]
    data = read(work)
    if data is None:
        return _legacy_states(work, prepared=prepared, finished=finished, running=running)
    rows = _derive(_events(data), scope="public")
    out = [{"id": row["id"], "label": row["label"], "state": row["state"]} for row in rows]
    if running and out and all(row["state"] == DONE for row in out):
        # Everything the engine does is finished, but the result is not stored
        # yet: keep the final public stage active so "ready" is never premature.
        out[-1]["state"] = ACTIVE
    return out


__all__ = ["ACTIVE", "DONE", "FILENAME", "INTERNAL_IDS", "INTERNAL_LABELS", "INTERNAL_STAGES", "PENDING", "SCHEMA",
           "STAGE_IDS", "STAGE_LABELS", "STAGES", "VERSION", "UnknownStage", "artifact_path", "internal_rows",
           "read", "record", "stage_durations_ms", "stage_rows", "stage_states", "timeline"]
