"""Synthetic swings and batches for tests and browser QA.

These helpers exist so the review workflow can be exercised end to end without
touching the real validation database or any real media. They write small fake
``.mp4`` files: the review server only ever streams bytes, so the content does
not matter here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from ..scoring import config as SC
from . import store


def synthetic_score(*, total: float | None = 7.0, available: bool = True, version: str = SC.VERSION,
                    points: Mapping[str, float] | None = None, confidences: Mapping[str, str] | None = None,
                    measured: int | None = None, excluded_ids: Sequence[str] = (), reason: str | None = None) -> dict:
    """A stored ``swing_score`` object in the shape the product writes."""

    picks = dict(points or {})
    levels = dict(confidences or {})
    checks, excluded = [], []
    for check in SC.CHECKS:
        if check.id in excluded_ids:
            excluded.append({"id": check.id, "phase": check.phase, "name": check.name, "reason": "not measured in fixture"})
            continue
        level = levels.get(check.id, "HIGH")
        checks.append({
            "id": check.id, "phase": check.phase, "name": check.name, "measurement": 0.0, "unit": check.unit,
            "target": {"from": check.good[0], "to": check.good[1], "ideal": check.ideal}, "status": "good",
            "label": "in range", "confidence": level, "estimated": level == "MEDIUM",
            # Points default to the snapshot's total, so a default fixture is self-consistent
            # (the weighted mean of the points equals the stored total).
            "points": float(picks.get(check.id, total if total is not None else 8.0)),
            "weight": SC.MODELS[version if version in SC.MODELS else SC.VERSION].weights[check.id] * (0.5 if level == "MEDIUM" else 1.0),
        })
    weight = sum(c["weight"] for c in checks) or 1.0
    for check in checks:
        check["share"] = round(check["weight"] / weight, 3)
    medium = sum(1 for c in checks if c["confidence"] == "MEDIUM")
    result = {
        "schema": "swing_score/1", "version": version, "source": "model", "scale": SC.SCALE,
        "measured": measured if measured is not None else len(checks), "eligible": len(SC.CHECKS),
        "coverage": round(len(checks) / len(SC.CHECKS), 2), "minimum": SC.MIN_MEASURED,
        "confidence": {"high": len(checks) - medium, "medium": medium,
                       "label": "HIGH" if medium == 0 else "MEDIUM"},
        "checks": checks, "excluded": excluded, "method": SC.METHOD,
    }
    enough = len(checks) >= SC.MIN_MEASURED
    return {**result, "available": bool(available and enough), "total": float(total) if (available and enough) else None,
            "reason": None if (available and enough) else (reason or SC.NOT_ENOUGH)}


def synthetic_analysis(score: dict | None = None) -> dict:
    """The minimum an analysis file needs for the importer: a ``phases`` list, plus any score."""

    payload = {"schema": "analysis/1", "phases": [], "width": 1080, "height": 1920}
    if score is not None:
        payload["swing_score"] = score
    return payload


def write_swing(folder: Path, *, swing_id: str, score: dict | None = None,
                video_bytes: bytes | None = None, video_source: Path | None = None) -> dict:
    """One bundle-shaped swing folder; returns a manifest entry.

    ``video_source`` copies a real playable clip (needed for browser QA); otherwise a
    small placeholder is written, which is enough for the importer and the API tests.
    """

    (folder / "bundle").mkdir(parents=True, exist_ok=True)
    video = folder / "bundle" / "swing.mp4"
    if video_source is not None:
        video.write_bytes(Path(video_source).read_bytes())
    else:
        # Distinct bytes per swing by default: the importer refuses byte-identical clips.
        video.write_bytes(video_bytes if video_bytes is not None
                          else b"\x00\x00\x00\x18ftypmp42" + str(swing_id).encode("utf-8"))
    analysis = folder / "bundle" / "analysis.json"
    analysis.write_text(json.dumps(synthetic_analysis(score)), encoding="utf-8")
    return {"swing_id": swing_id, "video": str(video), "analysis": str(analysis)}


def write_batch(root: Path, specs: Iterable[Mapping]) -> list[dict]:
    """Write one folder per spec: ``{swing_id, score, club?, golfer_id?, similar_group?, video_source?}``."""

    entries = []
    for spec in specs:
        entry = write_swing(root / str(spec["swing_id"]), swing_id=str(spec["swing_id"]), score=spec.get("score"),
                            video_source=spec.get("video_source"), video_bytes=spec.get("video_bytes"))
        for key in ("club", "golfer_id", "similar_group"):
            if spec.get(key):
                entry[key] = spec[key]
        entries.append(entry)
    return entries


def build_db(db_path: Path, batch: str, specs: Sequence[Mapping], *, root: Path | None = None) -> dict:
    """Import a synthetic batch into ``db_path`` and return the import summary."""

    root = Path(root or Path(db_path).parent / f"{batch}-media")
    root.mkdir(parents=True, exist_ok=True)
    entries = write_batch(root, specs)
    prepared = store.prepare_entries(entries, root=root)
    conn = store.connect(db_path)
    try:
        store.init_db(conn)
        summary = store.import_batch(conn, batch, prepared)
    finally:
        conn.close()
    return summary


def first_item_id(db_path: Path, batch: str) -> str:
    conn = store.connect(db_path)
    try:
        row = conn.execute("SELECT public_id FROM swing_item s JOIN batch b ON b.id = s.batch_id"
                           " WHERE b.name = ? ORDER BY s.ordinal LIMIT 1", (batch,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"no items in batch {batch!r}")
    return str(row["public_id"])


def submit(db_path: Path, batch: str, reviewer_id: str, public_id: str, *, overall: int = 8, setup: int = 7,
           backswing: int = 7, top: int = 7, downswing: int = 7, confidence: str = "HIGH", notes: str = "",
           allow_edit: bool = False) -> dict:
    conn = store.connect(db_path)
    try:
        return store.submit_review(conn, batch, reviewer_id, public_id,
                                   ratings={"setup": setup, "backswing": backswing, "top": top, "downswing": downswing,
                                            "overall": overall},
                                   confidence=confidence, notes=notes, allow_edit=allow_edit)
    finally:
        conn.close()


__all__ = ["build_db", "first_item_id", "submit", "synthetic_analysis", "synthetic_score", "write_batch", "write_swing"]
