"""Maintenance for the drill video catalogue (used by scripts/drill_videos.py; never imported by the API).

Bulk import (match to drill ids, validate, de-duplicate, replace), availability checks, enable / disable.
Network access goes through a ``Probe`` so tests run offline. Nothing here downloads a video: only YouTube's
public oEmbed endpoint and the watch page's player flags are read.
"""

from __future__ import annotations

import csv
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .app_drills import DRILLS
from .drill_videos import YOUTUBE_ID, validate

ID_IN_URL = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})")


@dataclass
class ProbeResult:
    ok: bool                       # exists, public, playable
    embeddable: bool               # the owner allows embedding
    title: str | None = None
    creator: str | None = None
    channel_id: str | None = None
    duration_s: int | None = None
    reason: str | None = None


Probe = Callable[[str], ProbeResult]


def video_id(value: str) -> str | None:
    value = (value or "").strip()
    if YOUTUBE_ID.match(value):
        return value
    m = ID_IN_URL.search(value)
    return m.group(1) if m else None


def youtube_probe(vid: str) -> ProbeResult:
    """oEmbed (401 = embedding disabled, 404 = removed/private) + the watch page's playability flags."""

    try:
        with urllib.request.urlopen(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}",
                                    timeout=20) as r:
            meta = json.loads(r.read())
    except urllib.error.HTTPError as e:
        reason = {401: "embedding disabled by the owner", 403: "embedding disabled by the owner",
                  404: "removed or private"}.get(e.code, f"oEmbed HTTP {e.code}")
        return ProbeResult(False, e.code not in (401, 403), reason=reason)
    except OSError as e:
        return ProbeResult(False, False, reason=f"network error ({type(e).__name__})")
    req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-GB,en;q=0.9",
                                          "Cookie": "CONSENT=YES+1"})
    try:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except OSError as e:
        return ProbeResult(False, False, meta.get("title"), meta.get("author_name"), reason=f"watch page ({type(e).__name__})")

    def grab(pattern: str) -> str | None:
        m = re.search(pattern, html)
        return m.group(1) if m else None

    status = grab(r'"playabilityStatus":\{"status":"([A-Z_]+)"')
    embeddable = grab(r'"playableInEmbed":(true|false)') == "true"
    length = grab(r'"lengthSeconds":"(\d+)"')
    ok = status == "OK"
    return ProbeResult(ok and embeddable, embeddable, meta.get("title"), meta.get("author_name"),
                       grab(r'"channelId":"(UC[A-Za-z0-9_-]{22})"'), int(length) if length else None,
                       None if ok and embeddable else ("embedding disabled by the owner" if ok else f"not playable ({status})"))


def read_rows(path: Path) -> list[dict]:
    """Import rows from .json (a list, or {"videos": [...]}) or .csv (header row)."""

    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as f:
            return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["videos"] if isinstance(data, dict) else data)


def import_rows(catalogue: dict, rows: Iterable[Mapping], probe: Probe, today: str | None = None) -> tuple[dict, list[str]]:
    """Upsert rows into a copy of the catalogue. Returns (new catalogue, report lines).

    * a row must name an existing drill id and a valid video (id or URL);
    * the same (drill, video) updates the existing record (fields not given are kept), never duplicates it;
    * a row made active disables the drill's previous active video ("replaced by ...");
    * every row is probed: a video that is unavailable or not embeddable is imported as ``disabled``;
    * written drill instructions are never touched (they live in app_drills.py).
    """

    today = today or date.today().isoformat()
    known = {d.id for d in DRILLS}
    out = json.loads(json.dumps(catalogue))
    videos: list[dict] = out.setdefault("videos", [])
    report: list[str] = []
    for n, row in enumerate(rows, 1):
        drill, vid = str(row.get("drill_id") or "").strip(), video_id(str(row.get("video_id") or row.get("url") or ""))
        if drill not in known:
            report.append(f"row {n}: skipped, unknown drill id {drill!r}")
            continue
        if not vid:
            report.append(f"row {n}: skipped, no valid YouTube video id or URL")
            continue
        result = probe(vid)
        existing = next((v for v in videos if v["drill_id"] == drill and v["video_id"] == vid), None)
        record = dict(existing or {"drill_id": drill, "provider": "youtube", "video_id": vid, "rights": "youtube_embed",
                                   "suitability": "unverified", "status": "pending_review", "start_s": 0})
        for key in ("title", "creator", "match", "suitability", "status", "rights", "aspect"):
            if row.get(key):
                record[key] = str(row[key]).strip()
        if row.get("start_s") not in (None, ""):
            record["start_s"] = int(row["start_s"])
        record["title"] = record.get("title") or result.title or ""
        record["creator"] = record.get("creator") or result.creator or ""
        if result.channel_id:
            record["channel_id"] = result.channel_id
        if result.duration_s:
            record["duration_s"] = result.duration_s
        record["embeddable"] = result.embeddable
        record["checked_at"] = today
        if not result.ok:
            if record["status"] == "active":
                record["status"] = "disabled"
            record["disabled_reason"] = result.reason
            report.append(f"row {n}: {drill} {vid} imported as {record['status']}: {result.reason}")
        else:
            record.pop("disabled_reason", None)
        if record["status"] == "active":
            for other in videos:
                if other["drill_id"] == drill and other["video_id"] != vid and other["status"] == "active":
                    other["status"] = "disabled"
                    other["disabled_reason"] = f"replaced by {vid} on {today}"
                    report.append(f"row {n}: {drill} previous video {other['video_id']} disabled (replaced)")
        if existing:
            existing.clear()
            existing.update(record)
            report.append(f"row {n}: {drill} {vid} updated ({record['status']})")
        else:
            videos.append(record)
            report.append(f"row {n}: {drill} {vid} added ({record['status']})")
    out["updated"] = today
    return out, report


def check_all(catalogue: dict, probe: Probe, today: str | None = None) -> tuple[dict, list[str]]:
    """Re-check every record; an active video that is gone or no longer embeddable is disabled (kept, with the reason)."""

    today = today or date.today().isoformat()
    out = json.loads(json.dumps(catalogue))
    report = []
    for v in out["videos"]:
        result = probe(v["video_id"])
        v["checked_at"] = today
        v["embeddable"] = result.embeddable
        if result.ok:
            report.append(f"{v['drill_id']:20s} {v['video_id']} OK ({v['status']})")
            continue
        if v["status"] == "active":
            v["status"] = "disabled"
        v["disabled_reason"] = result.reason
        report.append(f"{v['drill_id']:20s} {v['video_id']} PROBLEM: {result.reason} -> {v['status']}")
    out["updated"] = today
    return out, report


def set_status(catalogue: dict, drill: str, status: str, *, video: str | None = None, reason: str | None = None) -> tuple[dict, list[str]]:
    out = json.loads(json.dumps(catalogue))
    targets = [v for v in out["videos"] if v["drill_id"] == drill and (video is None or v["video_id"] == video)]
    if status == "active":
        targets = targets[:1] if video else []
        if not targets:
            return out, [f"name the video to enable for {drill}"]
        for v in out["videos"]:
            if v["drill_id"] == drill and v is not targets[0] and v["status"] == "active":
                v["status"], v["disabled_reason"] = "disabled", f"replaced by {targets[0]['video_id']}"
    for v in targets:
        v["status"] = status
        if status == "disabled":
            v["disabled_reason"] = reason or "disabled manually"
        else:
            v.pop("disabled_reason", None)
    return out, [f"{v['drill_id']} {v['video_id']} -> {status}" for v in targets] or [f"no video for {drill}"]


def save(catalogue: dict, path: Path) -> list[str]:
    """Validate, then write (stable order, pretty-printed). Returns problems; writes nothing if any."""

    problems = validate(catalogue, (d.id for d in DRILLS))
    if problems:
        return problems
    order = {d.id: i for i, d in enumerate(DRILLS)}
    catalogue["videos"].sort(key=lambda v: (order.get(v["drill_id"], 99), v["status"] != "active", v["video_id"]))
    path.write_text(json.dumps(catalogue, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return []


__all__ = ["ProbeResult", "check_all", "import_rows", "read_rows", "save", "set_status", "video_id", "youtube_probe"]
