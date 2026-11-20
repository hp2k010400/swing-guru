"""Slow-motion benchmark: the production pipeline (run_analysis) per engine profile (read-only for data).

    python scripts/bench_slowmo.py <folder of NAME_xK.mp4> <out dir> [--profiles next,candidate] [--jobs 2]

Each clip is analysed exactly as the server does (swing window -> engine subprocess -> rejection
rules) under each engine profile. Per run: success / error code, the slow-motion factor found,
moments located, scored checks and their values, Swing Score v3, and processing time. The 1x clip of
each swing is the reference the slowed copies are compared with.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WORKER = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from swingguru.analysis_runner import run_analysis
from swingguru.scoring import score_swing
t0 = time.monotonic()
r = run_analysis(Path(sys.argv[1]), Path(sys.argv[2]))
out = {"ok": r.ok, "error": r.error, "detail": (r.detail or "")[:200], "seconds": round(time.monotonic() - t0, 1)}
a = getattr(r, "analysis", None)
if a is None and (Path(sys.argv[2]) / "bundle" / "analysis.json").exists():
    a = json.loads((Path(sys.argv[2]) / "bundle" / "analysis.json").read_text(encoding="utf-8"))
if a:
    s = score_swing(a, "swing_score_v3")
    out.update({"time_scale": (a.get("time_scale") or {}).get("factor"),
                "located": sum(1 for p in a["phases"] if p.get("frame_index") is not None),
                "measured": s.get("measured"), "score": s.get("total"),
                "checks": {c["id"]: round(c["value"], 2) for c in s.get("checks", []) if c.get("value") is not None}})
print("RESULT " + json.dumps(out))
"""


def run(video: Path, out: Path, profile: str) -> dict:
    work = out / profile / video.stem
    work.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SG_ENGINE_PROFILE": profile, "PYTHONIOENCODING": "utf-8"}
    done = subprocess.run([sys.executable, "-c", WORKER, str(video), str(work), str(ROOT / "src")],
                          cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    result = json.loads(line[7:]) if line else {"ok": False, "error": "harness", "detail": done.stderr[-300:]}
    result.update({"clip": video.stem, "profile": profile})
    print(json.dumps({k: result.get(k) for k in ("clip", "profile", "ok", "error", "time_scale", "located", "measured", "score", "seconds")}), flush=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("clips")
    parser.add_argument("out")
    parser.add_argument("--profiles", default="next,candidate")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args(argv)
    out = Path(args.out)
    videos = sorted(Path(args.clips).glob("*.mp4"))
    jobs = [(v, p) for p in args.profiles.split(",") for v in videos]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda j: run(j[0], out, j[1]), jobs))
    (out / "results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
