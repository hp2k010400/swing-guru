"""Reproducible batch of analysis runs for the launch-polish diagnostics baseline.

Takes a drop-in manifest (JSON) or a folder of clips, runs the production
runner on each, and writes a JSON/CSV/Markdown summary: accepted/failed, the
failure stage and category, located phases, score eligibility, club/pose
coverage, real timings, source metadata, cache mode and the implementation
identity. A missing clip is an explicit error row, never a crash.

    python scripts/launch_polish_batch.py --folder var/test_swings --out var/qa/launch-polish/batch-modern
    python scripts/launch_polish_batch.py --manifest var/qa/launch-polish/degraded/variants.json --out ...

Manifest shapes accepted: a list of entries, ``{"clips": [...]}`` or the
degraded-variant manifest ``{"variants": [...]}``. Each entry needs ``path``
and may carry ``id``, ``kind`` ("source" | "degraded"), ``notes`` and
``synthetic: true``. Synthetic entries are always marked in the report and are
never presented as archive footage.

Nothing is downloaded and no source file is modified: each run happens in its
own throwaway folder under ``--out``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru import diagnostics as diag  # noqa: E402
from swingguru.analysis_runner import ANALYSIS_VERSION, run_analysis  # noqa: E402

IDENTITY_FILES = ("src/swingguru/analysis_runner.py", "src/swingguru/progress.py", "src/swingguru/diagnostics.py",
                  "scripts/build_analysis.py")
COLD_CACHE_STATUSES = ("miss", "miss-missing", "miss-identity", "miss-corrupt", "miss-version", "miss-legacy",
                       "miss-no-path")
WARM_CACHE_STATUSES = ("hit", "hit-legacy")


class ManifestError(ValueError):
    """A manifest that cannot be read as a clip list."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def implementation() -> dict:
    files = {}
    for name in IDENTITY_FILES:
        path = ROOT / name
        files[name] = sha256(path)[:16] if path.is_file() else "missing"
    return {"analysis_version": ANALYSIS_VERSION, "python": platform.python_version(),
            "platform": platform.system(), "root": ROOT.name, "files": files}


def load_manifest(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise ManifestError(f"could not read {path.name}") from None
    except ValueError:
        raise ManifestError(f"{path.name} is not valid JSON") from None
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = next((data[key] for key in ("clips", "variants") if isinstance(data.get(key), list)), None)
        if items is None:
            raise ManifestError(f"{path.name}: expected a list, 'clips' or 'variants'")
    else:
        raise ManifestError(f"{path.name}: expected a JSON list or object")
    entries: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ManifestError(f"{path.name}: entry {index} is not an object")
        if not str(item.get("path") or "").strip():
            raise ManifestError(f"{path.name}: entry {index} has no path")
        entries.append(item)
    return entries


def folder_manifest(folder: Path) -> list[dict]:
    return [{"id": clip.stem, "path": str(clip), "kind": "source"} for clip in
            sorted(p for p in folder.rglob("*") if p.suffix.lower() in {".mp4", ".mov"})]


def run_ident(entry: dict) -> str:
    """A filesystem-safe, non-empty identifier for one clip."""

    raw = str(entry.get("id") or Path(str(entry.get("path") or "clip")).stem)
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw).strip("-_")[:40]
    return slug or f"clip-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"


def cache_mode(diagnostics: dict) -> str:
    """The pose cache status as measured, with an explicit unknown."""

    status = (diagnostics.get("cache") or {}).get("pose_cache")
    if status in WARM_CACHE_STATUSES:
        return "warm"
    if status in COLD_CACHE_STATUSES:
        return "cold"
    return "unknown"


def remove_run_dir(work: Path, work_root: Path, *, source: Path | None) -> str | None:
    """Delete one run folder, refusing anything outside or containing the source."""

    resolved_root = work_root.resolve()
    resolved = work.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return "refused: run folder is outside the output work root"
    if resolved == resolved_root:
        return "refused: the run folder is the output work root"
    if source is not None and resolved in source.resolve().parents:
        return "kept: the source clip lives inside this run folder"
    shutil.rmtree(resolved, ignore_errors=True)
    return None


def _evidence(evidence: dict | None) -> str | None:
    """"stage=confirmed category=inferred cause=none": how each failure conclusion is known."""

    if not evidence:
        return None
    return " ".join(f"{key}={evidence.get(key)}" for key in ("stage", "category", "cause"))


def score_view(analysis: dict) -> dict | None:
    stored = analysis.get("swing_score")
    if stored:
        return {"source": "stored", "available": stored.get("available"), "total": stored.get("total"),
                "version": stored.get("version"), "measured": stored.get("measured"),
                "eligible": stored.get("eligible"), "coverage": stored.get("coverage")}
    from swingguru.scoring import score_swing

    computed = score_swing(analysis)
    return {"source": "computed_for_report", "available": computed.get("available"), "total": computed.get("total"),
            "version": computed.get("version"), "measured": computed.get("measured"),
            "eligible": computed.get("eligible"), "coverage": computed.get("coverage")}


def run_one(entry: dict, work_root: Path, *, timeout_s: float) -> dict:
    raw_path = entry.get("path")
    ident = run_ident(entry)
    row: dict = {"id": ident, "kind": entry.get("kind", "source"), "synthetic": bool(entry.get("synthetic")),
                 "notes": entry.get("notes") or entry.get("description"),
                 "variant": (entry.get("variant") if isinstance(entry.get("variant"), str) else None),
                 "parameters": entry.get("parameters")}
    clip = Path(raw_path)
    if not clip.is_absolute():
        clip = (ROOT / clip).resolve()
    if not clip.is_file():
        return {**row, "status": "error", "error": "missing_clip", "detail": f"no file at {clip.name}"}
    row.update({"clip": clip.name, "bytes": clip.stat().st_size, "sha256": sha256(clip),
                "source_sha256": entry.get("source", {}).get("sha256") if isinstance(entry.get("source"), dict) else None})
    # Unique child folder: never the work root itself, and never shared with
    # another run, however the manifest ids collide or sanitize to nothing.
    work_root.mkdir(parents=True, exist_ok=True)
    work = work_root / f"{ident}-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    try:
        result = run_analysis(clip, work, timeout_s=timeout_s)
    except Exception as error:  # the runner should never raise; keep the evidence if it does
        try:
            record = diag.build(ok=False, error="failed", detail=repr(error), work=work,
                                timings={"total_s": round(time.monotonic() - started, 2)})
        except Exception:  # pragma: no cover - diagnostics never mask the failure
            record = {}
        row.update({"status": "error", "error": "runner_raised",
                    "detail": diag.sanitize(repr(error), strip_paths=(work, ROOT)),
                    "wall_s": round(time.monotonic() - started, 2),
                    "failure": record.get("failure"),
                    "failure_stage": (record.get("failure") or {}).get("stage"),
                    "internal_stage": (record.get("failure") or {}).get("internal_stage"),
                    "failure_category": (record.get("failure") or {}).get("category"),
                    "failure_evidence": _evidence((record.get("failure") or {}).get("evidence")),
                    "inferred_cause": (record.get("failure") or {}).get("inferred_cause"),
                    "stages": record.get("stages"), "internal_stages": record.get("internal_stages"),
                    "cache_mode": "unknown", "diagnostics_schema": record.get("schema")})
        cleanup = remove_run_dir(work, work_root, source=clip)
        if cleanup:
            row["cleanup"] = cleanup
        return row
    diagnostics = result.diagnostics or {}
    row.update({
        "status": "accepted" if result.ok else "rejected",
        "error": result.error,
        "detail": diag.sanitize(result.detail),
        "notice": result.notice,
        "wall_s": round(time.monotonic() - started, 2),
        "timings": result.timings,
        "window": {key: result.window.get(key) for key in ("status", "confidence", "trimmed")},
        "video": diag.video_block(result.video_info),
        "failure": diagnostics.get("failure"),
        "failure_stage": (diagnostics.get("failure") or {}).get("stage"),
        "internal_stage": (diagnostics.get("failure") or {}).get("internal_stage"),
        "failure_category": (diagnostics.get("failure") or {}).get("category"),
        "failure_evidence": _evidence((diagnostics.get("failure") or {}).get("evidence")),
        "inferred_cause": (diagnostics.get("failure") or {}).get("inferred_cause"),
        "stages": diagnostics.get("stages"),
        "internal_stages": diagnostics.get("internal_stages"),
        "pose_coverage": (diagnostics.get("signals") or {}).get("pose_coverage"),
        "phase_confidence": (diagnostics.get("signals") or {}).get("phase_confidence"),
        "club_coverage": (diagnostics.get("signals") or {}).get("club_coverage"),
        "person_confidence": (diagnostics.get("signals") or {}).get("person_confidence"),
        "cache_mode": cache_mode(diagnostics),
        "cache": diagnostics.get("cache"),
        "diagnostics_schema": diagnostics.get("schema"),
    })
    bundle = result.bundle
    if bundle and (bundle / "analysis.json").is_file():
        analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
        row["score"] = score_view(analysis)
        row["phases_located"] = sum(1 for p in analysis.get("phases", []) if p.get("frame_index") is not None)
        row["guided_checks"] = sum(1 for p in analysis.get("phases", []) for c in p.get("checks", []) if c.get("guided"))
        row["engine_timings"] = (analysis.get("engine") or {}).get("timings")
        row["engine_counters"] = (analysis.get("engine") or {}).get("counters")
        row["analysis_bytes"] = (bundle / "analysis.json").stat().st_size
    cleanup = remove_run_dir(work, work_root, source=clip)
    if cleanup:
        row["cleanup"] = cleanup
    return row


def write_reports(out: Path, label: str, rows: list[dict], identity: dict, budget: dict) -> None:
    payload = {"schema": "swingguru.launch_polish_batch/1", "label": label, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "identity": identity, "budget": budget, "clips": rows}
    (out / "results.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    columns = ("id", "kind", "synthetic", "variant", "clip", "bytes", "status", "error", "failure_stage",
               "internal_stage", "failure_category", "failure_evidence", "inferred_cause", "wall_s", "phases_located", "guided_checks", "score_total", "score_available",
               "score_measured", "score_eligible", "pose_coverage", "club_phases", "cache_mode")
    with (out / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            score = row.get("score") or {}
            writer.writerow({
                "id": row.get("id"), "kind": row.get("kind"), "synthetic": row.get("synthetic"),
                "variant": row.get("variant"), "clip": row.get("clip"), "bytes": row.get("bytes"),
                "status": row.get("status"), "error": row.get("error"), "failure_stage": row.get("failure_stage"),
                "internal_stage": row.get("internal_stage") or (row.get("failure") or {}).get("internal_stage"),
                "failure_category": row.get("failure_category"), "failure_evidence": row.get("failure_evidence"),
                "inferred_cause": row.get("inferred_cause"), "wall_s": row.get("wall_s"),
                "phases_located": row.get("phases_located"), "guided_checks": row.get("guided_checks"),
                "score_total": score.get("total"), "score_available": score.get("available"),
                "score_measured": score.get("measured"), "score_eligible": score.get("eligible"),
                "pose_coverage": (row.get("pose_coverage") or {}).get("detected_frame_coverage"),
                "club_phases": (row.get("club_coverage") or {}).get("phases_with_club"),
                "cache_mode": row.get("cache_mode"),
            })
    lines = [f"# Launch-polish batch: {label}", "",
             f"- ran_at: {payload['ran_at']}  budget: {budget.get('spent_s')}s of {budget.get('max_seconds')}s",
             f"- analysis_version: {identity['analysis_version']}  python: {identity['python']}",
             f"- implementation: " + ", ".join(f"{name.split('/')[-1]}={digest}" for name, digest in identity["files"].items()),
             "- evidence: `confirmed` = an explicit rule or the boundary that was active; `inferred` = deduced from"
             " where the run stopped; `unknown` = no evidence. An inferred cause is never a confirmed fact.",
             "", "| id | kind | status | stage | internal | category | evidence | inferred cause | phases | score | pose | club | wall s |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        score = row.get("score") or {}
        total = score.get("total")
        lines.append("| {id} | {kind}{synthetic} | {status} | {stage} | {internal} | {category} | {evidence} | {cause} | {phases} | "
                     "{score} | {pose} | {club} | {wall} |".format(
                         id=row.get("id"), kind=row.get("kind"),
                         synthetic=" (synthetic)" if row.get("synthetic") else "",
                         status=row.get("status"), stage=row.get("failure_stage") or "-",
                         internal=(row.get("failure") or {}).get("internal_stage") or "-",
                         category=row.get("failure_category") or "-", evidence=row.get("failure_evidence") or "-",
                         cause=row.get("inferred_cause") or "-", phases=row.get("phases_located") or "-",
                         score=("-" if total is None else total),
                         pose=(row.get("pose_coverage") or {}).get("detected_frame_coverage") or "-",
                         club=(row.get("club_coverage") or {}).get("phases_with_club") or "-", wall=row.get("wall_s")))
    (out / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--folder", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", default="batch")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--max-seconds", type=float, default=1800.0,
                        help="wall budget; remaining clips are reported as not_run")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offline-models", action="store_true",
                        help="resolve models from the local cache only (no hub network retries); inherited "
                             "by the engine subprocess and recorded in the report")
    args = parser.parse_args(argv)
    if args.offline_models:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if not args.manifest and not args.folder:
        print("one of --manifest or --folder is required", file=sys.stderr)
        return 2
    try:
        entries = load_manifest(Path(args.manifest)) if args.manifest else folder_manifest(Path(args.folder))
    except ManifestError as error:
        print(f"manifest error: {error}", file=sys.stderr)
        return 2
    for entry in entries:
        entry["id"] = run_ident(entry)
    if args.limit:
        entries = entries[:args.limit]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work_root = out / "work"
    identity = implementation()
    started = time.monotonic()
    rows: list[dict] = []
    for entry in entries:
        if time.monotonic() - started >= args.max_seconds:
            rows.append({"id": entry.get("id"), "kind": entry.get("kind", "source"),
                         "synthetic": bool(entry.get("synthetic")), "status": "not_run",
                         "detail": "wall budget reached before this clip"})
            continue
        row = run_one(entry, work_root, timeout_s=args.timeout)
        rows.append(row)
        print(json.dumps({"id": row.get("id"), "status": row.get("status"), "error": row.get("error"),
                          "stage": row.get("failure_stage"), "wall_s": row.get("wall_s")}))
    budget = {"max_seconds": args.max_seconds, "spent_s": round(time.monotonic() - started, 1),
              "offline_models": bool(os.environ.get("HF_HUB_OFFLINE"))}
    write_reports(out, args.label, rows, identity, budget)
    accepted = sum(1 for row in rows if row.get("status") == "accepted")
    print(f"{accepted}/{len(rows)} accepted -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
