"""JSON API behind the Swing Guru app shell (served by ``scripts/serve_app.py``).

Real data, read from analysed swing bundles: dates, thumbnails, verdicts,
supported checkpoint counts, hand position, camera state, streaks and
per-checkpoint consistency across swings.

Stubbed data (every object carries ``source: "stub"``; the app tags it in
``?dev=1``): Swing Score, "Start here" (unless an interpretation rule is
eligible), PR (based on the stub score) and the score trend. Stubs are only
produced in demo mode (``SG_APP_DEMO=1``, the default for now); with demo
off the same fields come back in their honest "not available" state.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import app_drills, app_jobs
from .app_model import (
    PHASE_LABELS, PHASE_ORDER, SOURCE_STUB, StubScorer, checkpoints, focus_for, status_counts, swing_score,
    summary_text, swing_datetime, withheld,
)

# The club picker, in order. "I'm not sure" is stored as no club (null).
CLUBS = (
    "Driver", "3 Wood", "5 Wood", "7 Wood",
    "2 Hybrid", "3 Hybrid", "4 Hybrid", "5 Hybrid",
    "2 Iron", "3 Iron", "4 Iron", "5 Iron", "6 Iron", "7 Iron", "8 Iron", "9 Iron",
    "Pitching Wedge", "Gap Wedge", "Sand Wedge", "Lob Wedge",
    "Other",
)
LEGACY_CLUBS = ("Hybrid", "Irons", "Wedges")  # the first picker's groups; still valid on older swings

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var"
SWING_ROOTS = (VAR / "qa" / "test_swings", VAR / "app" / "swings")
THUMBS = VAR / "app" / "thumbs"
META = VAR / "app" / "swing_meta.json"
EXCLUDED = {"03", "05"}  # robustness clips that are not real test swings (empty bay, edited montage)


def demo_mode() -> bool:
    return os.environ.get("SG_APP_DEMO", "1") != "0"


# ---------------------------------------------------------------- bundles

def bundle_dirs() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in SWING_ROOTS:
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            bundle = folder / "bundle" if (folder / "bundle").is_dir() else folder
            if (bundle / "analysis.json").is_file() and folder.name[:2] not in EXCLUDED:
                found[folder.name] = bundle
    return found


_cache: dict[str, tuple[float, dict]] = {}


def load_analysis(swing_id: str) -> dict | None:
    bundle = bundle_dirs().get(swing_id)
    if bundle is None:
        return None
    path = bundle / "analysis.json"
    mtime = path.stat().st_mtime
    cached = _cache.get(swing_id)
    if cached and cached[0] == mtime:
        return cached[1]
    analysis = json.loads(path.read_text(encoding="utf-8"))
    _cache[swing_id] = (mtime, analysis)
    return analysis


def swing_meta() -> dict:
    return app_jobs.read_meta()


def set_club(swing_id: str, club: str | None) -> tuple[int, dict]:
    if swing_id not in bundle_dirs():
        return 404, {"error": "unknown swing"}
    if club is not None and club not in CLUBS + LEGACY_CLUBS:
        return 400, {"error": f"club must be one of {', '.join(CLUBS)}"}
    app_jobs.update_meta(swing_id, club=club)
    return 200, {"id": swing_id, "club": club}


# ---------------------------------------------------------------- one swing

def _camera(analysis: dict) -> dict:
    motion = ((analysis.get("camera_check") or {}).get("signals") or {}).get("camera_motion") or {}
    correction = analysis.get("camera_correction") or {}
    corrected = sorted(p for p, r in correction.items() if (r or {}).get("status") == "CORRECTED_VALID")
    return {"moved": motion.get("status") == "INVALID", "corrected_phases": corrected}


# Below this LONG-side resolution, angle readings get noisier (CAMERA_STABILITY.md: at 540 px tall knee
# bend and Top posture changed verdicts; head, hips and arm hang held; 1024-1080 px tall portrait clips
# were the unaffected baseline). A notice only; nothing is withheld. Configurable: SG_LOW_RES_LONG_SIDE.
DEFAULT_LOW_RES_LONG_SIDE = 960
LOW_RES_NOTICE = "This video is lower resolution, so some measurements may be less precise. Recording at 1080p gives the clearest analysis."


def low_res_long_side() -> int:
    try:
        return int(os.environ.get("SG_LOW_RES_LONG_SIDE", DEFAULT_LOW_RES_LONG_SIDE))
    except ValueError:
        return DEFAULT_LOW_RES_LONG_SIDE


def quality_notice(analysis: dict, long_side_min: int | None = None) -> str | None:
    """A gentle note when the analysed clip's longest side is below the threshold (default 960 px)."""

    w, h = analysis.get("width"), analysis.get("height")
    if not w or not h:
        return None
    limit = low_res_long_side() if long_side_min is None else long_side_min
    return LOW_RES_NOTICE if max(int(w), int(h)) < limit else None


def _hand_position(analysis: dict) -> dict | None:
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            if check.get("kind") == "hand_depth":
                return {"value": check.get("value"), "text": check.get("summary") or check.get("detail"),
                        "status": "info"}
    return None


def summary(swing_id: str, *, demo: bool | None = None) -> dict | None:
    analysis = load_analysis(swing_id)
    if analysis is None:
        return None
    demo = demo_mode() if demo is None else demo
    bundle = bundle_dirs()[swing_id]
    meta = swing_meta().get(swing_id, {})
    folder = bundle.parent.name if bundle.name == "bundle" else bundle.name
    fallback = datetime.fromtimestamp((bundle / "analysis.json").stat().st_mtime)
    when = datetime.fromisoformat(meta["created"]) if meta.get("created") else \
        swing_datetime(analysis.get("source", ""), folder, fallback=fallback)
    return product_summary(analysis, swing_id=swing_id, when=when, meta=meta, demo=demo)


def product_summary(analysis: dict, *, swing_id: str, when: datetime, meta: dict, demo: bool) -> dict:
    """Everything the Summary / Library / Home show for one swing, from ``analysis.json`` alone.

    Shared by the local server and the hosted service (which always passes
    ``demo=False``: no stub score, no stub Start here in production)."""

    items = checkpoints(analysis)
    missing = withheld(analysis)
    focus = focus_for(analysis, demo=demo)
    focus_drills = app_drills.drills_for(focus.drill_mapping, focus.phase) if focus.available else []
    hand = _hand_position(analysis)
    return {
        "id": swing_id,
        "date": when.isoformat(timespec="seconds"),
        # When it entered the app (uploads), else its capture time: Home's "latest" is what you just added.
        "added": meta.get("uploaded") or when.isoformat(timespec="seconds"),
        "source": meta.get("source_name") or analysis.get("source"),
        "club": meta.get("club"),
        "clubs": list(CLUBS),
        "notice": meta.get("notice"),
        "thumb": f"/thumbs/{swing_id}.jpg",
        "replay": f"#/replay/{swing_id}",
        "duration_s": analysis.get("duration_s"),
        "checkpoints": [{**c, "drills": [d["id"] for d in app_drills.drills_for(c["mapping"], c["phase"])]} for c in items],
        "withheld": missing,
        "counts": status_counts(items),
        "headline": {"phases_analysed": sum(1 for p in analysis.get("phases", []) if p.get("frame_index") is not None),
                     **status_counts(items)},
        "summary_text": summary_text(items, missing),
        "breakdown": _breakdown(items, missing, hand),
        "recommended": _recommended(items),
        "hand_position": hand,
        "camera": _camera(analysis),
        "score": StubScorer().score(analysis, swing_id=swing_id).to_dict() if demo else swing_score(analysis),
        "focus": {**focus.to_dict(), "drills": focus_drills},
    }


def _breakdown(items: list[dict], missing: list[dict], hand: dict | None) -> list[dict]:
    """Rows grouped by phase, in swing order. Takeaway has no scored check yet, so it is omitted."""

    groups = []
    for phase in PHASE_ORDER:
        rows = [{**c, "kind": "check"} for c in items if c["phase"] == phase]
        rows += [{**w, "kind": "withheld"} for w in missing if w["phase"] == phase]
        if phase == "top" and hand:
            rows.append({"id": "top.hand_depth", "kind": "info", "name": "Hand position", "phase": "top",
                         "phase_label": "Top", "label": "Descriptive", "status": "info", "value": hand["value"],
                         "support": hand["text"]})
        if rows:
            groups.append({"phase": phase, "label": PHASE_LABELS[phase], "rows": rows})
    return groups


def _recommended(items: list[dict], limit: int = 2) -> list[dict]:
    """Drills mapped to this swing's out-of-range checks, in swing order (a mapping, not a priority).

    Clear findings always get their drill; slight ones only on checks whose bands hold up on good
    players (coaching.LOW_TRUST lists the ones that don't yet), so practice is never pushed on a
    reading we can't stand behind."""

    from .coaching import _key, practise_this

    out, seen = [], set()
    for c in items:
        if not c.get("mapping") or not practise_this(_key(c["mapping"], c["phase"]), c["status"]):
            continue
        for d in app_drills.drills_for(c["mapping"], c["phase"]):
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            out.append({"drill": d, "checkpoint": {k: c[k] for k in ("id", "name", "phase_label", "label", "status")}})
    return out[:limit]


# ---------------------------------------------------------------- collections

def library(*, demo: bool | None = None) -> list[dict]:
    items = [s for s in (summary(i, demo=demo) for i in bundle_dirs()) if s]
    items.sort(key=lambda s: s["date"], reverse=True)
    # PR only for the flagged demo score: no badges or comparisons from the real Swing Score yet.
    scored = [s for s in items if s["score"]["available"] and s["score"]["total"] is not None
              and s["score"].get("source") == SOURCE_STUB]
    best = max(scored, key=lambda s: s["score"]["total"], default=None)
    for s in items:
        s["pr"] = {"value": best is not None and s["id"] == best["id"],
                   "source": best["score"]["source"] if best else "none"}
    return items


def week_streak(dates: list[datetime], today: date) -> int:
    """Consecutive ISO weeks with a swing, counting back from this week (or last week)."""

    weeks = {d.isocalendar()[:2] for d in dates}
    cursor = today
    if (cursor.isocalendar()[:2]) not in weeks:
        cursor -= timedelta(days=7)
    streak = 0
    while cursor.isocalendar()[:2] in weeks:
        streak += 1
        cursor -= timedelta(days=7)
    return streak


def consistency(items: list[dict]) -> list[dict]:
    """Per checkpoint: how often it was in range across the swings (real)."""

    rows: dict[str, dict] = {}
    seen: dict[str, int] = {}
    for s in items:
        for position, c in enumerate(s["checkpoints"]):
            seen.setdefault(c["id"], position)
            row = rows.setdefault(c["id"], {"id": c["id"], "name": c["name"], "phase": c["phase"],
                                           "phase_label": c["phase_label"], "good": 0, "attention": 0,
                                           "problem": 0, "total": 0})
            row[c["status"]] += 1
            row["total"] += 1
    order = {p: i for i, p in enumerate(("address", "takeaway", "mid_back", "top", "downswing", "impact"))}
    return sorted(rows.values(), key=lambda r: (order.get(r["phase"], 9), seen[r["id"]]))


def home(*, demo: bool | None = None, today: date | None = None) -> dict:
    demo = demo_mode() if demo is None else demo
    items = library(demo=demo)
    dates = [datetime.fromisoformat(s["date"]) for s in items]
    today = today or date.today()
    by_added = sorted(items, key=lambda s: s["added"], reverse=True)
    latest = by_added[0] if by_added else None
    trend = [{"id": s["id"], "date": s["date"], "total": s["score"]["total"]}
             for s in reversed(items[:8]) if s["score"]["available"] and s["score"].get("source") == SOURCE_STUB]
    return {
        "demo": demo,
        "count": len(items),
        "latest": latest,
        "recent": by_added[:6],
        "streak_weeks": week_streak(dates, today),
        "consistency": consistency(items),
        "trend": {"source": SOURCE_STUB if demo and trend else "none", "points": trend,
                  "reason": None if trend else "The score trend appears once the Swing Score is calibrated."},
        "program": {"unlock_after": 3, "swings": len(items), "source": SOURCE_STUB},
    }


def drills(*, demo: bool | None = None) -> dict:
    latest = max(library(demo=demo), key=lambda s: s["added"], default=None)
    out = app_drills.catalogue()
    out["priority"] = None
    out["for_your_swing"] = []
    if latest:
        focus = latest["focus"]
        out["priority"] = {"focus": focus, "drills": focus.get("drills", [])} if focus["available"] else \
            {"focus": focus, "drills": []}
        seen = set()
        for c in latest["checkpoints"]:
            if c["status"] == "good":
                continue
            for d in app_drills.drills_for(c["mapping"], c["phase"]):
                if d["id"] not in seen:
                    seen.add(d["id"])
                    out["for_your_swing"].append({"drill": d, "checkpoint": {k: c[k] for k in (
                        "id", "name", "phase_label", "label", "status")}})
        out["latest_id"] = latest["id"]
    return out


def profile_stats(*, demo: bool | None = None) -> dict:
    items = library(demo=demo)
    totals = [s["score"]["total"] for s in items if s["score"]["available"] and s["score"].get("source") == SOURCE_STUB]
    source = items[0]["score"]["source"] if totals else "none"
    return {
        "swings": len(items),
        "average": {"value": round(sum(totals) / len(totals), 1) if totals else None, "source": source},
        "best": {"value": max(totals) if totals else None, "source": source},
        "checkpoints_in_range": {"good": sum(s["counts"]["good"] for s in items),
                                 "total": sum(s["counts"]["total"] for s in items)},
    }


# ---------------------------------------------------------------- thumbnails

def thumbnail_path(swing_id: str) -> Path | None:
    """A 3:4 JPEG of the Address frame, made once and cached."""

    bundle = bundle_dirs().get(swing_id)
    analysis = load_analysis(swing_id)
    if bundle is None or analysis is None:
        return None
    out = THUMBS / f"{swing_id}.jpg"
    if out.is_file() and out.stat().st_mtime >= (bundle / "analysis.json").stat().st_mtime:
        return out
    return render_thumbnail(bundle / analysis.get("video", "swing.mp4"), analysis, out)


def render_thumbnail(video: Path, analysis: dict, out: Path) -> Path | None:
    """Write a 3:4 JPEG of the swing's Address frame to ``out``."""

    import cv2

    t = next((p.get("time_s") for p in analysis.get("phases", []) if p["phase"] == "address"), None) or 0.0
    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
    ok, frame = capture.read()
    capture.release()
    if not ok:
        return None
    h, w = frame.shape[:2]
    target_h = int(round(w * 4.1 / 3))           # the card's aspect
    if target_h < h:
        top = int((h - target_h) * 0.55)          # keep a little more below centre (feet, ball)
        frame = frame[top:top + target_h]
    scale = 420 / frame.shape[1]
    frame = cv2.resize(frame, (420, int(frame.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 84])
    return out


# ---------------------------------------------------------------- routing

def handle_get(path: str, raw: str = "") -> tuple[int, object]:
    query = parse_qs(urlparse(raw).query)
    demo = None if "demo" not in query else query["demo"][0] != "0"
    parts = [p for p in path.split("/") if p][1:]  # drop "api"
    if parts == ["home"]:
        return 200, home(demo=demo)
    if parts == ["swings"]:
        return 200, {"demo": demo_mode() if demo is None else demo, "swings": library(demo=demo)}
    if len(parts) == 2 and parts[0] == "swings":
        item = summary(parts[1], demo=demo)
        return (200, item) if item else (404, {"error": "unknown swing"})
    if parts == ["drills"]:
        return 200, drills(demo=demo)
    if parts == ["profile"]:
        return 200, profile_stats(demo=demo)
    if len(parts) == 2 and parts[0] == "jobs":
        job = app_jobs.get_job(parts[1])
        return (200, job) if job else (404, {"error": "unknown job"})
    if parts == ["clubs"]:
        return 200, {"clubs": list(CLUBS)}
    return 404, {"error": "unknown endpoint"}


def handle_post(path: str, stream, headers) -> tuple[int, object]:
    parts = [p for p in path.split("/") if p][1:]
    length = int(headers.get("Content-Length") or 0)
    if parts == ["analyze"]:
        from urllib.parse import unquote

        modified = headers.get("X-File-Modified")
        job = app_jobs.start_job(stream, length, file_name=unquote(headers.get("X-File-Name") or "swing.mp4"),
                                 modified_ms=float(modified) if modified else None)
        return (200 if job.get("state") != "error" else 400), job
    if len(parts) == 3 and parts[0] == "swings" and parts[2] == "meta":
        try:
            body = json.loads(stream.read(length) or b"{}")
        except ValueError:
            return 400, {"error": "expected JSON"}
        return set_club(parts[1], body.get("club"))
    return 404, {"error": "unknown endpoint"}


__all__ = ["bundle_dirs", "consistency", "demo_mode", "drills", "handle_get", "home", "library",
           "profile_stats", "summary", "thumbnail_path", "week_streak"]
