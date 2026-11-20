"""Repeatable analysis validation: run the real engine over a fixed swing set and compare.

    python scripts/validate_analysis.py run   [--runs current,next,next:refined_club,...] [--only 01,04] [--jobs 3]
    python scripts/validate_analysis.py report [--baseline docs/agent-work/analysis-quality/baseline_current.json]

``run`` analyses every swing in ``var/qa/test_swings/*/window.mp4`` with ``scripts/build_analysis.py``
once per requested run (``current`` = production engine, ``next`` = all 1.0.1 fixes, ``next:<fix>`` =
one fix alone, for attribution). Each run gets its own output folder under ``var/qa/validation``;
the swing's pose / club-candidate / club-fallback caches are copied in first, so a rerun only redoes
the cheap stages. Nothing is uploaded and production is never touched.

Per swing and run it records: the moments and their frames, every one of the 8 scored checks with
its value, level, confidence and verdict, every unavailable check with a reason code, the reason
and the deciding numbers (camera correction, club level), handedness, the camera check, and the
Swing Score (v2; v3 is reported as ``null`` until it exists).

``report`` writes ``results.json`` and ``REPORT.md``: coverage per run, what each fix gained or
lost, value and score changes, and a regression list (a check measured before and not after, or a
value that moved without a fix that explains it).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.engine_profile import FIXES, reason_code  # noqa: E402
from swingguru.scoring import score_swing  # noqa: E402
from swingguru.scoring.config import CHECKS  # noqa: E402

SWINGS = Path(os.environ.get("SG_VALIDATION_SWINGS") or ROOT / "var" / "qa" / "test_swings")
OUT = Path(os.environ.get("SG_VALIDATION_OUT") or ROOT / "var" / "qa" / "validation")
DOCS = ROOT / "docs" / "agent-work" / "analysis-quality"
CACHE_FILES = ("pose_records.pkl", "club_candidates.json", "club_fallback.json")
SCORED = [c.id for c in CHECKS]
DEFAULT_RUNS = ["current", "next"] + [f"next:{fix}" for fix in FIXES]


def swings(only: set[str] | None = None) -> list[tuple[str, Path]]:
    out = []
    for folder in sorted(SWINGS.iterdir()):
        video = folder / "window.mp4"
        if folder.is_dir() and video.is_file() and (only is None or folder.name[:2] in only):
            out.append((folder.name[:2], folder))
    return out


def run_one(run: str, name: str, folder: Path) -> dict:
    out = OUT / run.replace(":", "__") / name
    out.mkdir(parents=True, exist_ok=True)
    for cache in CACHE_FILES:
        source = folder / "bundle" / cache
        if source.is_file() and not (out / cache).is_file():
            shutil.copyfile(source, out / cache)
    profile, _, fix = run.partition(":")
    command = [sys.executable, str(ROOT / "scripts" / "build_analysis.py"), str(folder / "window.mp4"),
               "--out", str(out), "--records", str(out / "pose_records.pkl"), "--engine-profile", profile]
    if fix:
        command += ["--engine-fixes", fix]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("SG_ENGINE_PROFILE", None)  # the command line decides
    began = time.monotonic()
    done = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)
    (out / "build.log").write_text(done.stdout + "\n--- stderr ---\n" + done.stderr, encoding="utf-8")
    return {"run": run, "swing": name, "ok": done.returncode == 0, "seconds": round(time.monotonic() - began, 1),
            "error": None if done.returncode == 0 else done.stderr.strip().splitlines()[-1:]}


def summarise(analysis: dict) -> dict:
    """The validation record for one analysis.json."""

    checks = {}
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            key = f"{phase['phase']}.{check['kind']}"
            verdict = check.get("verdict") or {}
            mapping = verdict.get("mapping") or {}
            if key in SCORED and check.get("guided"):
                checks[key] = {"value": mapping.get("input"), "level": check.get("level"),
                               "confidence": check.get("confidence"), "status": verdict.get("status"),
                               "label": verdict.get("label"), "camera": check.get("camera"),
                               "source": check.get("source")}
    correction = analysis.get("camera_correction") or {}
    unavailable = {}
    for item in analysis.get("hidden", []):
        key = f"{item.get('phase')}.{item.get('kind')}"
        if key not in SCORED or key in checks or key in unavailable:
            continue
        detail = {"code": item.get("code") or reason_code(item.get("reason", "")), "reason": item.get("reason")}
        if detail["code"] == "camera_uncorrected":
            c = correction.get(item.get("phase")) or {}
            detail["numbers"] = {k: c.get(k) for k in ("status", "reason", "foot_residual_pct", "foot_raw_pct",
                                                       "shift_pct_body", "residual_px")}
        unavailable[key] = detail
    # Measured but not scored: the verdict layer refused the reading (e.g. outside the plausible range,
    # which catches the club tracker's false flat lines).
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            key = f"{phase['phase']}.{check['kind']}"
            if key in SCORED and key not in checks and key not in unavailable:
                mapping = (check.get("verdict") or {}).get("mapping") or {}
                reason = mapping.get("reason") or "measured but not scored"
                unavailable[key] = {"code": "implausible_value" if "plausible" in reason else "not_scored",
                                    "reason": reason, "numbers": {"raw": check.get("raw"), "level": check.get("level")}}
    for key in SCORED:
        if key not in checks and key not in unavailable:
            unavailable[key] = {"code": "not_reported", "reason": "check absent from the analysis"}
    motion = ((analysis.get("camera_check") or {}).get("signals") or {}).get("camera_motion") or {}
    score = score_swing(analysis, "swing_score_v2")
    v3 = score_swing(analysis, "swing_score_v3")
    v2_points = {c["id"]: c["points"] for c in score.get("checks", [])}
    return {
        "moments": {p["phase"]: {"frame": p.get("frame_index"), "source": p.get("source")}
                    for p in analysis.get("phases", [])},
        "measured": sorted(checks), "coverage": len(checks), "checks": checks, "unavailable": unavailable,
        "handedness": analysis.get("handedness") or {"value": "right", "source": "default (current engine)"},
        "camera": {"motion": motion.get("status"), "motion_value": motion.get("value"),
                   "correction": {k: (v or {}).get("status") for k, v in correction.items()}},
        "body_height_px": (analysis.get("geometry_context") or {}).get("body_height_px"),
        "score_v2": {"total": score.get("total"), "measured": score.get("measured"), "version": score.get("version"),
                     "points": v2_points},
        "score_v3": {"total": v3.get("total"), "measured": v3.get("measured"), "version": v3.get("version"),
                     "checks": {c["id"]: {"points": c["points"], "class": c["class"], "d": c["d"], "noise": c["noise"],
                                          "deviation": c["deviation"], "share": c["share"]} for c in v3.get("checks", [])},
                     "families": v3.get("families"), "deductions": v3.get("deductions")},
    }


def collect(runs: list[str]) -> dict:
    results: dict = {}
    for run in runs:
        for folder in sorted((OUT / run.replace(":", "__")).glob("*")):
            path = folder / "analysis.json"
            if path.is_file():
                results.setdefault(folder.name, {})[run] = summarise(json.loads(path.read_text(encoding="utf-8")))
    return results


def explain(record: dict) -> list[str]:
    """Why v3 differs from v2 for one swing, from the actual terms of both calculations."""

    v2, v3 = record["score_v2"], record["score_v3"]
    if v2["total"] is None or v3["total"] is None:
        return ["no score in one model"]
    reasons = []
    weight_used = (v3["deductions"] or {}).get("weight_used") or 1.0
    effects = []
    for cid, c3 in v3["checks"].items():
        p2 = v2["points"].get(cid)
        if p2 is None:
            continue
        effect = c3["share"] * (c3["points"] - p2)
        value = record["checks"].get(cid, {}).get("value")
        why = {"within_noise": f"outside the band by less than its noise ({c3['noise']:g}), so softened",
               "slightly_out": f"slightly out (d={c3['d']:.2f}): smooth curve, edge softened by noise {c3['noise']:g}",
               "clearly_out": f"clearly out (d={c3['d']:.2f}): significant fault",
               "in_range": "in range (v3 gives 9.5-10 in range, v2 9-10)"}[c3["class"]]
        effects.append((abs(effect), f"{cid} {_fmt(value)}: {p2:.1f} -> {c3['points']:.1f} pts ({why}), {effect:+.2f}"))
    for _, text in sorted(effects, reverse=True)[:3]:
        if not text.endswith("+0.00") and not text.endswith("-0.00"):
            reasons.append(text)
    for family in v3["families"] or []:
        relief = 10.0 * (family["independent_sum"] - family["loss"]) / weight_used
        if relief >= 0.05:
            reasons.append(f"{family['label']}: related faults counted once (+{relief:.2f})")
    penalty = (v3["deductions"] or {}).get("multi_fault_penalty") or 0
    if penalty:
        reasons.append(f"{v3['deductions']['significant_families']} families with significant faults (-{penalty:.2f})")
    return reasons or ["same result"]


def _fmt(value) -> str:
    return "-" if value is None else (f"{value:.1f}" if isinstance(value, float) else str(value))


def report(results: dict, runs: list[str], baseline: dict | None) -> str:
    lines = ["# Analysis validation report", "",
             f"Swings: {len(results)}. Runs: {', '.join(runs)}. Score v3: not implemented yet.", ""]
    # Coverage per run.
    lines += ["## Coverage (scored checks measured, of 8)", "",
              "| Swing | " + " | ".join(runs) + " |", "|---" * (len(runs) + 1) + "|"]
    totals = {run: 0 for run in runs}
    for swing, by_run in sorted(results.items()):
        cells = []
        for run in runs:
            cov = by_run.get(run, {}).get("coverage")
            totals[run] += cov or 0
            cells.append(_fmt(cov))
        lines.append(f"| {swing} | " + " | ".join(cells) + " |")
    lines.append("| **total** | " + " | ".join(str(totals[r]) for r in runs) + " |")
    dist = {run: {} for run in runs}
    for by_run in results.values():
        for run in runs:
            if run in by_run:
                n = by_run[run]["coverage"]
                dist[run][n] = dist[run].get(n, 0) + 1
    lines += ["", "Coverage distribution: " + "; ".join(
        f"{run}: " + ", ".join(f"{k}/8 x{v}" for k, v in sorted(dist[run].items(), reverse=True)) for run in runs), ""]

    # Per-fix attribution against current.
    if "current" in runs:
        lines += ["## Effect of each run against current", ""]
        for run in runs:
            if run == "current":
                continue
            gained, lost, moved, rescored = [], [], [], []
            for swing, by_run in sorted(results.items()):
                a, b = by_run.get("current"), by_run.get(run)
                if not a or not b:
                    continue
                for key in SCORED:
                    va, vb = a["checks"].get(key), b["checks"].get(key)
                    if va is None and vb is not None:
                        gained.append(f"{swing} {key} ({_fmt(vb['value'])}, {vb['level']}, was {a['unavailable'][key]['code']})")
                    elif va is not None and vb is None:
                        lost.append(f"{swing} {key} (now {b['unavailable'][key]['code']})")
                    elif va and vb and va["value"] is not None and vb["value"] is not None and abs(va["value"] - vb["value"]) > 0.05:
                        moved.append(f"{swing} {key} {va['value']:.2f} -> {vb['value']:.2f}")
                sa, sb = a["score_v2"]["total"], b["score_v2"]["total"]
                if sa != sb:
                    rescored.append(f"{swing} {_fmt(sa)} -> {_fmt(sb)}")
            lines += [f"### {run}", f"- Gained ({len(gained)}): " + ("; ".join(gained) or "none"),
                      f"- **Lost ({len(lost)}): " + ("; ".join(lost) or "none") + "**",
                      f"- Values changed ({len(moved)}): " + ("; ".join(moved) or "none"),
                      f"- Swing Score v2 changed ({len(rescored)}): " + ("; ".join(rescored) or "none"), ""]

    # Why checks are unavailable, per run.
    lines += ["## Unavailable checks by reason", ""]
    for run in runs:
        counts: dict = {}
        for by_run in results.values():
            for key, item in (by_run.get(run) or {}).get("unavailable", {}).items():
                counts[(key, item["code"])] = counts.get((key, item["code"]), 0) + 1
        lines.append(f"- **{run}**: " + (", ".join(f"{k} {c} x{n}" for (k, c), n in sorted(counts.items())) or "none"))
    lines.append("")

    # Detail table for current vs next.
    if "current" in runs and "next" in runs:
        lines += ["## Per swing: current -> next", "",
                  "| Swing | Hand | Camera | Score v2 | Measured (current -> next) | Unavailable in next (code) |",
                  "|---|---|---|---|---|---|"]
        for swing, by_run in sorted(results.items()):
            a, b = by_run.get("current"), by_run.get("next")
            if not a or not b:
                continue
            un = ", ".join(f"{k} ({v['code']})" for k, v in sorted(b["unavailable"].items())) or "none"
            lines.append(f"| {swing} | {b['handedness']['value']} ({b['handedness']['source']}) | {b['camera']['motion']} | "
                         f"{_fmt(a['score_v2']['total'])} -> {_fmt(b['score_v2']['total'])} | {a['coverage']} -> {b['coverage']} | {un} |")
        lines.append("")

    # Swing Score v2 vs v3 (v3 is calibrated on the ``next`` profile, i.e. with C6).
    if "next" in runs:
        lines += ["## Swing Score v2 vs v3 (next profile)", "",
                  "| Swing | Coverage | v2 (production engine) | v2 (next) | v3 (next) | v3 - v2 | Why |",
                  "|---|---|---|---|---|---|---|"]
        pairs = []
        for swing, by_run in sorted(results.items()):
            b = by_run.get("next")
            if not b:
                continue
            prod = (by_run.get("current") or {}).get("score_v2", {}).get("total")
            s2, s3 = b["score_v2"]["total"], b["score_v3"]["total"]
            delta = None if s2 is None or s3 is None else round(s3 - s2, 1)
            pairs.append((swing, s2, s3, delta, prod))
            lines.append(f"| {swing} | {b['coverage']}/8 | {_fmt(prod)} | {_fmt(s2)} | {_fmt(s3)} | "
                         f"{'-' if delta is None else f'{delta:+.1f}'} | " + "<br>".join(explain(b)) + " |")
        scored = [p for p in pairs if p[1] is not None and p[2] is not None]
        if scored:
            import statistics as st
            v2s, v3s = [p[1] for p in scored], [p[2] for p in scored]
            lines += ["", "Distribution (next profile): "
                      f"v2 mean {st.mean(v2s):.2f}, sd {st.pstdev(v2s):.2f}, range {min(v2s)}-{max(v2s)}; "
                      f"v3 mean {st.mean(v3s):.2f}, sd {st.pstdev(v3s):.2f}, range {min(v3s)}-{max(v3s)}.",
                      "Largest changes: " + ", ".join(f"{p[0]} {p[3]:+.1f}" for p in sorted(scored, key=lambda p: -abs(p[3]))[:4]),
                      "Rank order (best first): v2 " + " ".join(p[0] for p in sorted(scored, key=lambda p: -p[1]))
                      + " | v3 " + " ".join(p[0] for p in sorted(scored, key=lambda p: -p[2])), ""]
        lines += ["## Per-check measurements and classifications (next profile)", "",
                  "| Swing | Check | Value | Verdict (current -> next) | v2 pts | v3 pts | v3 class |", "|---|---|---|---|---|---|---|"]
        for swing, by_run in sorted(results.items()):
            a, b = by_run.get("current") or {}, by_run.get("next")
            if not b:
                continue
            for cid in SCORED:
                c = b["checks"].get(cid)
                if not c:
                    lines.append(f"| {swing} | {cid} | not measured ({b['unavailable'][cid]['code']}) | | | | |")
                    continue
                before = (a.get("checks") or {}).get(cid, {}).get("status", "not measured")
                changed = "" if before == c["status"] else " **changed**"
                v3c = b["score_v3"]["checks"].get(cid, {})
                lines.append(f"| {swing} | {cid} | {_fmt(c['value'])} ({c['level']}) | {before} -> {c['status']}{changed} | "
                             f"{_fmt(b['score_v2']['points'].get(cid))} | {_fmt(v3c.get('points'))} | {v3c.get('class', '-')} |")
        lines.append("")

    if baseline:
        lines += ["## Regressions against the saved baseline", ""]
        problems = []
        for swing, by_run in sorted(results.items()):
            for run, now in by_run.items():
                then = (baseline.get(swing) or {}).get(run)
                if not then:
                    continue
                for key in SCORED:
                    if key in then["checks"] and key not in now["checks"]:
                        problems.append(f"{swing} [{run}] {key} no longer measured ({now['unavailable'][key]['code']})")
                    elif key in then["checks"] and key in now["checks"]:
                        x, y = then["checks"][key]["value"], now["checks"][key]["value"]
                        if x is not None and y is not None and abs(x - y) > 0.05:
                            problems.append(f"{swing} [{run}] {key} value {x:.2f} -> {y:.2f}")
                if then["score_v2"]["total"] != now["score_v2"]["total"]:
                    problems.append(f"{swing} [{run}] score v2 {then['score_v2']['total']} -> {now['score_v2']['total']}")
        lines += [f"- {p}" for p in problems] or ["- none"]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("run", "report"))
    parser.add_argument("--runs", default=",".join(DEFAULT_RUNS))
    parser.add_argument("--only", default=None, help="comma-separated swing numbers (01,04)")
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--baseline", default=str(DOCS / "baseline_current.json"))
    parser.add_argument("--save-baseline", action="store_true", help="write the current results as the baseline")
    args = parser.parse_args(argv)
    runs = [r.strip() for r in args.runs.split(",") if r.strip()]
    if args.mode == "run":
        jobs = [(run, name, folder) for run in runs for name, folder in
                swings(set(args.only.split(",")) if args.only else None)]
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for result in pool.map(lambda job: run_one(*job), jobs):
                print(json.dumps(result), flush=True)
    results = collect(runs)
    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.is_file() else None
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (OUT / "REPORT.md").write_text(report(results, runs, baseline), encoding="utf-8")
    if args.save_baseline:
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"wrote {OUT / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
