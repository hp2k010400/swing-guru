"""Drill demonstration videos: one centralised catalogue, kept apart from the analysis engine.

* The catalogue is ``drill_videos.json`` next to this file, or the file named by ``SG_DRILL_VIDEOS_PATH``
  (so staging can swap videos without a deploy or an app build).
* Only official provider embeds are recorded (YouTube's IFrame player); nothing is downloaded or rehosted,
  and the creator's title and channel are always shown with the video.
* A record is shown in the app only when ``status == "active"``. ``disabled`` and ``pending_review`` records
  stay in the file (with the reason) so a drill keeps its written instructions and nothing is deleted.
* The API exposes it as the drill's ``demo_video``. The older ``video`` field (a direct MP4 played natively
  by builds up to 1.0.1 (20)) stays ``None``, so older app versions are unaffected.
* Availability (removed / private / embedding switched off) is checked offline by
  ``scripts/drill_videos.py check``, which disables a failing record; the app also falls back to the written
  steps and a "Watch on YouTube" link if the player reports an error.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

DEFAULT_PATH = Path(__file__).with_name("drill_videos.json")
PROVIDERS = {"youtube"}
STATUSES = {"active", "disabled", "pending_review"}
RIGHTS = {"youtube_embed", "owned", "licensed"}
SUITABILITY = {"transcript_verified", "visually_verified", "coach_reviewed", "partial_match", "unverified"}
YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
REQUIRED = ("drill_id", "provider", "video_id", "status", "rights", "suitability", "checked_at")
REQUIRED_ACTIVE = ("title", "creator")     # attribution shown with every video


class CatalogueError(ValueError):
    pass


def catalogue_path() -> Path:
    raw = os.environ.get("SG_DRILL_VIDEOS_PATH", "").strip()
    return Path(raw) if raw else DEFAULT_PATH


def validate(data: Mapping, drill_ids: Iterable[str]) -> list[str]:
    """Problems with a catalogue (empty list = valid)."""

    known = set(drill_ids)
    problems: list[str] = []
    if not isinstance(data.get("videos"), list):
        return ["catalogue must have a 'videos' list"]
    active_per_drill: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for i, v in enumerate(data["videos"]):
        where = f"videos[{i}]"
        missing = [k for k in REQUIRED if not v.get(k)]
        if missing:
            problems.append(f"{where}: missing {', '.join(missing)}")
            continue
        if v["drill_id"] not in known:
            problems.append(f"{where}: unknown drill_id {v['drill_id']!r}")
        if v["provider"] not in PROVIDERS:
            problems.append(f"{where}: provider must be one of {sorted(PROVIDERS)}")
        elif v["provider"] == "youtube" and not YOUTUBE_ID.match(str(v["video_id"])):
            problems.append(f"{where}: invalid YouTube video id")
        if v["status"] not in STATUSES:
            problems.append(f"{where}: status must be one of {sorted(STATUSES)}")
        if v["rights"] not in RIGHTS:
            problems.append(f"{where}: rights must be one of {sorted(RIGHTS)}")
        if v["suitability"] not in SUITABILITY:
            problems.append(f"{where}: suitability must be one of {sorted(SUITABILITY)}")
        start = v.get("start_s", 0)
        if not isinstance(start, int) or start < 0 or (v.get("duration_s") and start >= v["duration_s"]):
            problems.append(f"{where}: start_s must be a whole number of seconds inside the video")
        if v.get("aspect") not in (None, "16:9", "9:16"):
            problems.append(f"{where}: aspect must be 16:9 or 9:16")
        key = (v["drill_id"], v["video_id"])
        if key in seen:
            problems.append(f"{where}: duplicate record for {key}")
        seen.add(key)
        if v["status"] == "active":
            if any(not v.get(k) for k in REQUIRED_ACTIVE):
                problems.append(f"{where}: an active video needs its title and creator (attribution)")
            if v.get("embeddable") is not True:
                problems.append(f"{where}: an active video must be confirmed embeddable")
            if v["suitability"] in ("partial_match", "unverified"):
                problems.append(f"{where}: an active video must have verified content")
            active_per_drill[v["drill_id"]] = active_per_drill.get(v["drill_id"], 0) + 1
    problems += [f"drill {d!r} has {n} active videos (max 1)" for d, n in active_per_drill.items() if n > 1]
    return problems


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    from .app_drills import DRILLS

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate(data, (d.id for d in DRILLS))
    if problems:
        raise CatalogueError("; ".join(problems))
    return data


def load() -> dict:
    path = catalogue_path()
    return _load(str(path), path.stat().st_mtime)


def public(v: Mapping) -> dict:
    """What the app receives: the official embed, the creator attribution and nothing else."""

    vid, start = v["video_id"], int(v.get("start_s") or 0)
    return {
        "provider": v["provider"],
        "video_id": vid,
        "start_s": start,
        "title": v["title"],
        "creator": v["creator"],
        "watch_url": f"https://www.youtube.com/watch?v={vid}" + (f"&t={start}s" if start else ""),
        "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}?playsinline=1&rel=0" + (f"&start={start}" if start else ""),
        "thumbnail_url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "suitability": v["suitability"],
        "duration_s": v.get("duration_s"),
        "aspect": v.get("aspect") or "16:9",
    }


def active_by_drill() -> dict[str, dict]:
    """drill id -> public video, active records only. A broken catalogue never breaks the drills API."""

    try:
        data = load()
    except (OSError, ValueError):
        import logging

        logging.getLogger("swingguru.drill_videos").exception("drill video catalogue unavailable; drills shown without video")
        return {}
    return {v["drill_id"]: public(v) for v in data["videos"] if v["status"] == "active"}


__all__ = ["CatalogueError", "DEFAULT_PATH", "active_by_drill", "catalogue_path", "load", "public", "validate"]
