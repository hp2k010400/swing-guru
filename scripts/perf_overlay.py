"""Overlay animation cost, v2 vs v3, under CPU throttling (a stand-in for older iPhones).

    python scripts/perf_overlay.py <bundle dir> [--throttle 1,4,6] [--repeats 3]

For each check of the walkthrough, opens it held (``?embed=1&check=...``) and records every
animation frame for 1.8 s from the moment the overlay starts drawing: frame intervals (a 60 Hz
display gives ~16.7 ms; anything over 33 ms is a visibly dropped frame), plus long tasks. Chrome's
``Emulation.setCPUThrottlingRate`` slows the main thread by the given factor (4-6x approximates an
iPhone 8 / XR class device against this desktop). This is an estimate; it is not a measurement on a
real iPhone (GPU compositing and WebKit differ).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import overlay_v3_qa as q  # noqa: E402

PROBE = """(function () {
  window.__perf = { frames: [], long: 0 };
  try { new PerformanceObserver(function (l) { window.__perf.long += l.getEntries().length; }).observe({ entryTypes: ['longtask'] }); } catch (e) {}
  var last = performance.now(), until = last + 1800;
  function f(t) { window.__perf.frames.push(t - last); last = t; if (t < until) requestAnimationFrame(f); }
  requestAnimationFrame(f);
  return true;
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    parser.add_argument("--throttle", default="1,4,6")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args(argv)
    bundle = Path(args.bundle)
    checks = q.default_checks(bundle)
    server, base = q.serve(bundle)
    proc, c = q.open_browser()
    table = []
    try:
        for rate in [float(x) for x in args.throttle.split(",")]:
            c.send("Emulation.setCPUThrottlingRate", {"rate": rate})
            for mode in ("2", "3"):
                frames, longs = [], 0
                for _ in range(args.repeats):
                    for check in checks:
                        if mode == "2" and check not in q.default_checks(bundle)[:0] and "observation" in json.dumps(
                                [x for p in json.loads((bundle / "analysis.json").read_text())["phases"] for x in p["checks"]
                                 if f"{p['phase']}:{x['kind']}" == check and x.get("observation") and not x.get("guided")]):
                            continue   # observations are a v3 feature
                        c.send("Page.navigate", {"url": f"{base}?embed=1&check={check}&overlay={mode}"})
                        c.wait_for("!!window.__swingViewer && !!window.__swingViewer.checkId", timeout=90, label=check)
                        # Restart this check's draw-on and record frames through it.
                        c.evaluate(f"{PROBE}; window.__swingViewer.goToCheck({json.dumps(check)})")
                        time.sleep(1.9 * max(1.0, rate / 3))
                        got = json.loads(c.evaluate("JSON.stringify(window.__perf)"))
                        frames += got["frames"][1:]
                        longs += got["long"]
                frames = [f for f in frames if f > 0]
                table.append({"throttle": rate, "overlay": f"v{mode}", "frames": len(frames),
                              "median_ms": round(statistics.median(frames), 1),
                              "p95_ms": round(sorted(frames)[int(0.95 * (len(frames) - 1))], 1),
                              "over_33ms_pct": round(100 * sum(f > 33.4 for f in frames) / len(frames), 1),
                              "long_tasks": longs})
                print(json.dumps(table[-1]), flush=True)
    finally:
        proc.terminate()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
