"""Does the same swing give the same analysis when the clip is cut or encoded slightly differently?

    .venv\\Scripts\\python.exe scripts\\trim_stability.py [--set var/qa/prod_swings] [--limit 8] [--jobs 2]

For each swing video (``<set>/<id>/window.mp4``) four variants are analysed with the real engine
(``analysis_runner.run_analysis``, engine profile from SG_ENGINE_PROFILE, default ``candidate``):

* ``orig``    the clip as stored;
* ``start``   the first 0.2 s removed (a slightly later start of recording);
* ``end``     the last 0.3 s removed;
* ``reenc``   re-encoded (CRF 20), as a phone or messaging app might.

A golfer would call all four "the same swing", so a trustworthy engine gives the same verdicts and
nearly the same values. Per check it reports: verdict agreement with ``orig``, the largest value
change, and that change relative to the check's measurement noise (scoring V3_NOISE); plus moment
frame shifts (in source time) and Swing Score spread. Writes
docs/agent-work/analysis-v2/TRIM_STABILITY.md and .json. Read-only for production.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.scoring.config import CHECKS, V3_NOISE  # noqa: E402

RESUMMARISE = "--resummarise" in sys.argv   # rebuild summaries from the existing runs (no re-analysis)
VARIANTS = {"orig": [], "start": ["-ss", "0.2"], "end": None, "reenc": ["-c:v", "libx264", "-crf", "20", "-preset", "fast"],
            "crop": None, "dark": None, "small": None}
OUT = ROOT / "var" / "qa" / "trim_stability"


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def make_variant(src: Path, name: str, dst: Path) -> float:
    """Write the variant; returns its start offset in source seconds (to map frames back)."""

    if name == "orig":
        shutil.copyfile(src, dst)
        return 0.0
    if name == "start":
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", "0.2", "-i", str(src), "-c:v", "libx264", "-crf", "16", "-an", str(dst)]
        offset = 0.2
    elif name == "end":
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", f"{duration(src) - 0.3:.3f}", "-c:v", "libx264",
               "-crf", "16", "-an", str(dst)]
        offset = 0.0
    elif name == "crop":    # tighter framing: 15% off each side (the golfer still fully in frame on these clips)
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", "crop=iw*0.7:ih*0.85:iw*0.15:ih*0.075,scale=trunc(iw/2)*2:trunc(ih/2)*2",
               "-c:v", "libx264", "-crf", "16", "-an", str(dst)]
        offset = 0.0
    elif name == "dark":    # dimmer light: lower brightness and contrast
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", "eq=brightness=-0.12:contrast=0.8",
               "-c:v", "libx264", "-crf", "16", "-an", str(dst)]
        offset = 0.0
    elif name == "small":   # lower resolution: 540 px tall
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", "scale=-2:540", "-c:v", "libx264", "-crf", "20",
               "-an", str(dst)]
        offset = 0.0
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src), *VARIANTS["reenc"], "-an", str(dst)]
        offset = 0.0
    subprocess.run(cmd, check=True)
    return offset


def summarise(bundle: Path, offset: float) -> dict | None:
    path = next(bundle.rglob("analysis.json"), None)
    if path is None:
        return None
    a = json.loads(path.read_text(encoding="utf-8"))
    out = {"moments": {}, "checks": {}, "score": None}
    for p in a.get("phases", []):
        if p.get("time_s") is not None:
            # time_s is already clip time (it includes segment_start_s); adding the segment start again
            # was the 2026-11-06 report's error that showed a false "1.8 s moment jump" on swing 02.
            out["moments"][p["phase"]] = round(p["time_s"] + offset, 3)
        for c in p.get("checks", []):
            v = c.get("verdict") or {}
            m = v.get("mapping") or {}
            if c.get("guided") and m.get("input") is not None:
                out["checks"][f"{p['phase']}.{c['kind']}"] = {"value": m["input"], "status": v.get("status"),
                                                               "label": v.get("label")}
    score = a.get("swing_score") or {}
    out["score"] = score.get("total")
    out["fps"] = a.get("fps")
    return out


def analyse(swing: Path, name: str) -> tuple[str, str, dict | None]:
    from swingguru.analysis_runner import run_analysis

    work = OUT / swing.name / name
    if RESUMMARISE and (work / "run").is_dir():
        summary = summarise(work / "run", 0.2 if name == "start" else 0.0)
        (work / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return swing.name, name, summary
    if (work / "summary.json").exists():
        return swing.name, name, json.loads((work / "summary.json").read_text(encoding="utf-8"))
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        video = Path(tmp) / "upload.mp4"
        offset = make_variant(swing / "window.mp4", name, video)
        result = run_analysis(video, work / "run")
    summary = summarise(work / "run", offset) if getattr(result, "ok", True) else None
    (work / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return swing.name, name, summary


def report(results: dict) -> str:
    lines = ["# Trim and re-encode stability (same swing, slightly different clip)\n",
             f"Engine profile `{os.environ.get('SG_ENGINE_PROFILE', 'candidate')}`. {len(results)} swings, variants: "
             "first 0.2 s removed, last 0.3 s removed, re-encoded. Each variant is compared with the original clip.\n"]
    per_check = {c.id: {"pairs": 0, "same": 0, "lost": 0, "gained": 0, "max_noise_x": 0.0, "worst": None} for c in CHECKS}
    scores, shifts, failed = [], {}, []
    for swing, runs in results.items():
        base = runs.get("orig")
        if not base:
            failed.append(f"{swing}/orig")
            continue
        for name, r in runs.items():
            if name == "orig":
                continue
            if not r:
                failed.append(f"{swing}/{name}")
                continue
            if base["score"] is not None and r["score"] is not None:
                scores.append(abs(r["score"] - base["score"]))
            for m, t in base["moments"].items():
                if m in r["moments"] and base.get("fps"):
                    shifts.setdefault(m, []).append(round(abs(r["moments"][m] - t) * base["fps"], 1))
            for cid, row in per_check.items():
                a, b = base["checks"].get(cid), r["checks"].get(cid)
                if a and b:
                    row["pairs"] += 1
                    row["same"] += a["status"] == b["status"]
                    x = abs(b["value"] - a["value"]) / V3_NOISE.get(cid, 1.0)
                    if x > row["max_noise_x"]:
                        row["max_noise_x"], row["worst"] = x, f"{swing}/{name}: {a['label']} -> {b['label']} ({a['value']:.1f} -> {b['value']:.1f})"
                elif a and not b:
                    row["lost"] += 1
                elif b and not a:
                    row["gained"] += 1
    lines.append("## Verdicts per check\n")
    lines.append("| Check | Pairs | Same verdict | Lost | Gained | Largest change (x noise) | Worst case |\n|---|---|---|---|---|---|---|")
    for cid, row in per_check.items():
        same = f"{row['same']}/{row['pairs']} ({100 * row['same'] / row['pairs']:.0f}%)" if row["pairs"] else "-"
        lines.append(f"| {cid} | {row['pairs']} | {same} | {row['lost']} | {row['gained']} | {row['max_noise_x']:.1f} | {row['worst'] or ''} |")
    if scores:
        scores.sort()
        lines.append(f"\n**Swing Score change:** median {scores[len(scores) // 2]:.2f}, max {scores[-1]:.2f} "
                     f"({sum(s >= 0.3 for s in scores)} of {len(scores)} pairs moved 0.3 or more).\n")
    lines.append("## Moment frame shifts (source frames)\n")
    for m, v in shifts.items():
        v.sort()
        lines.append(f"* {m}: median {v[len(v) // 2]}, max {v[-1]}, more than 2 frames: {sum(x > 2 for x in v)}/{len(v)}")
    if failed:
        lines.append(f"\nNo analysis for: {', '.join(failed)}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", default=str(ROOT / "var" / "qa" / "prod_swings"))
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--resummarise", action="store_true", help="rebuild summaries from existing runs")
    parser.add_argument("--variants", default="orig,start,end,reenc", help="comma list from: " + ",".join(VARIANTS))
    parser.add_argument("--out", default="TRIM_STABILITY", help="report name under docs/agent-work/analysis-v2")
    parser.add_argument("--work", default=None, help="analysis work folder (default var/qa/trim_stability)")
    args = parser.parse_args(argv)
    global OUT
    if args.work:
        OUT = Path(args.work)
    os.environ.setdefault("SG_ENGINE_PROFILE", "candidate")
    swings = sorted(p for p in Path(args.set).iterdir() if (p / "window.mp4").is_file())[:args.limit]
    results: dict[str, dict] = {s.name: {} for s in swings}
    with ThreadPoolExecutor(args.jobs) as pool:
        for swing, name, summary in pool.map(lambda job: analyse(*job), [(s, v) for s in swings for v in args.variants.split(",")]):
            results[swing][name] = summary
            print(f"{swing} {name}: {'ok' if summary else 'no analysis'}", flush=True)
    out = ROOT / "docs" / "agent-work" / "analysis-v2" / f"{args.out}.md"
    out.write_text(report(results), encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
