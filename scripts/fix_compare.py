"""Before / after one engine fix on every validation swing (real engine, same cached pose).

    .venv\\Scripts\\python.exe scripts\\fix_compare.py address_recheck [--jobs 3]

For each swing in the three validation sources, scripts/build_analysis.py runs twice with the
candidate profile: without the named fix ("before") and with it ("after"), sharing the swing's pose /
club caches so only the fix differs. Compares moments (frames), every scored check (value, verdict,
confidence), withheld checks and the Swing Score. Writes docs/agent-work/analysis-v2/FIX_<fix>.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.engine_profile import CANDIDATE_FIXES  # noqa: E402
from swingguru.scoring import score_swing  # noqa: E402

SOURCES = {"prod": "var/qa/prod_swings", "new": "var/qa/prod_swings_new", "1_0_1": "var/qa/test_swings"}
CACHES = ("pose_records.pkl", "club_candidates.json", "club_fallback.json")


def run(source: Path, out: Path, fixes: list[str]) -> None:
    if (out / "analysis.json").is_file():
        return
    out.mkdir(parents=True, exist_ok=True)
    for c in CACHES:
        if (source / "bundle" / c).is_file() and not (out / c).is_file():
            shutil.copyfile(source / "bundle" / c, out / c)
    cmd = [sys.executable, str(ROOT / "scripts" / "build_analysis.py"), str(source / "window.mp4"), "--out", str(out),
           "--records", str(out / "pose_records.pkl"), "--engine-profile", "candidate", "--engine-fixes", ",".join(fixes)]
    done = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    (out / "build.log").write_text(done.stdout[-4000:] + "\n--- stderr ---\n" + done.stderr[-4000:], encoding="utf-8")


def summary(path: Path) -> dict | None:
    if not path.is_file():
        return None
    a = json.loads(path.read_text(encoding="utf-8"))
    checks = {}
    for p in a["phases"]:
        for c in p["checks"]:
            v = c.get("verdict") or {}
            m = v.get("mapping") or {}
            if c.get("guided") and m.get("input") is not None:
                checks[f"{p['phase']}.{c['kind']}"] = (round(m["input"], 2), v.get("label"), v.get("status"), c.get("level"))
    score = (a.get("swing_score") or score_swing(a)).get("total")
    return {"moments": {p["phase"]: p.get("frame_index") for p in a["phases"]}, "checks": checks, "score": score,
            "fps": a.get("fps"), "_recheck": a.get("address_recheck")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fix")
    ap.add_argument("--jobs", type=int, default=3)
    args = ap.parse_args(argv)
    before = [f for f in CANDIDATE_FIXES if f != args.fix]
    after = before + [args.fix]
    jobs = []
    for tag, src in SOURCES.items():
        for folder in sorted((ROOT / src).iterdir()):
            if (folder / "window.mp4").is_file() and (folder / "bundle" / "pose_records.pkl").is_file():
                for side, fixes in (("before", before), ("after", after)):
                    jobs.append((folder, ROOT / "var/qa/fix_compare" / args.fix / tag / side / folder.name, fixes))
    with ThreadPoolExecutor(args.jobs) as pool:
        list(pool.map(lambda j: run(*j), jobs))
    rows, failed = [], []
    for tag in SOURCES:
        base = ROOT / "var/qa/fix_compare" / args.fix / tag
        for b in sorted((base / "before").glob("*")):
            s0, s1 = summary(b / "analysis.json"), summary(base / "after" / b.name / "analysis.json")
            if s0 is None or s1 is None:
                failed.append(f"{tag}/{b.name}")
                continue
            rows.append((f"{tag}/{b.name}", s0, s1))
    same = lambda a, b: {k: v for k, v in a.items() if k != "_recheck"} == {k: v for k, v in b.items() if k != "_recheck"}
    identical = [r for r in rows if same(r[1], r[2])]
    changed = [r for r in rows if not same(r[1], r[2])]
    guarded = [(r[0], r[2]["_recheck"]) for r in rows if r[2].get("_recheck")]
    lines = [f"# Fix `{args.fix}`: before / after on every validation swing\n",
             f"Real engine, candidate profile, same cached pose and club data; only `{args.fix}` differs.\n",
             f"**{len(rows)} swings compared: {len(identical)} identical in every moment, measurement, verdict and score; "
             f"{len(changed)} changed.**" + (f" No result for: {', '.join(failed)}." if failed else "") + "\n"]
    if guarded:
        lines.append("**Re-check decisions (every swing where it triggered):**\n")
        lines += [f"* {name}: {g.get('status')} - {g.get('guard', '')} (Address {g.get('hand_speed_address_s')} s -> "
                  f"{g.get('address_s')} s, takeaway {g.get('takeaway_s')} s)" for name, g in guarded]
        lines.append("")
    for name, s0, s1 in changed:
        lines.append(f"## {name}\n")
        mv = {k: (s0['moments'].get(k), s1['moments'].get(k)) for k in s0["moments"] if s0["moments"].get(k) != s1["moments"].get(k)}
        lines.append(f"* Moments (frame before -> after): {mv or 'unchanged'}")
        for cid in sorted(set(s0["checks"]) | set(s1["checks"])):
            a, b = s0["checks"].get(cid), s1["checks"].get(cid)
            if a != b:
                lines.append(f"* {cid}: {a[1] + ' (' + str(a[0]) + ')' if a else 'not measured'} -> "
                             f"{b[1] + ' (' + str(b[0]) + ')' if b else 'not measured'}")
        lines.append(f"* Swing Score: {s0['score']} -> {s1['score']}\n")
    out = ROOT / "docs/agent-work/analysis-v2" / f"FIX_{args.fix}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
