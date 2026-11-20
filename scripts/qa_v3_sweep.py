"""Overlay v3 robustness sweep over many bundles (read-only).

    python scripts/qa_v3_sweep.py <run dir> [<run dir> ...] [--out report.json]

For every bundle (analysis.json + swing.mp4) in the run directories, opens the viewer with
``?embed=1&overlay=3`` in headless Edge/Chrome at 390 x 844 and, for every walkthrough check
(scored + observations), jumps to it, shows its final state and records:

* JavaScript errors (``window.onerror`` / unhandled rejections, installed before the page loads). The
  browser's "ResizeObserver loop completed with undelivered notifications" notice is counted
  separately, not as a fault: it is benign and occurs intermittently with v1 and v2 (production) too;
* invalid geometry: any SVG path / circle attribute containing NaN or Infinity;
* drawn geometry outside the video box (lines are clipped to the frame; more than 2 px outside is a
  fault);
* a reveal that never reaches its end (``__swingViewer.v3``).

It also plays the whole walkthrough once per bundle at 1x and checks it reaches the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import overlay_v3_qa as q  # noqa: E402

ERRORS = """window.__errs = [];
window.addEventListener('error', function (e) { window.__errs.push(String(e.message || e)); });
window.addEventListener('unhandledrejection', function (e) { window.__errs.push('rejection: ' + String(e.reason)); });"""

PROBE = r"""(function () {
  var box = window.__swingViewer.overlay.box, bad = 0, outside = 0, n = 0;
  document.querySelectorAll('#overlay path, #overlay circle, #overlay text').forEach(function (el) {
    ['d', 'cx', 'cy', 'r', 'x', 'y'].forEach(function (a) {
      var v = el.getAttribute(a); if (v && /NaN|Infinity/.test(v)) bad++;
    });
    if (el.tagName === 'path' && el.getAttribute('d') && box) {
      var L = 0; try { L = el.getTotalLength(); } catch (e) {}
      for (var k = 0; L > 0 && k <= 8; k++) {
        var p = el.getPointAtLength(L * k / 8); n++;
        if (p.x < box.x - 2 || p.x > box.x + box.width + 2 || p.y < box.y - 2 || p.y > box.y + box.height + 2) outside++;
      }
    }
  });
  var v3 = window.__swingViewer.v3;
  return JSON.stringify({ nan: bad, outside: outside, sampled: n, done: v3 ? v3.elapsed >= v3.end : null, errors: window.__errs.slice() });
})()"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    bundles = [d for r in args.runs for d in sorted(Path(r).glob("*")) if (d / "analysis.json").is_file() and (d / "swing.mp4").is_file()]
    report = []
    proc = None
    try:
        for bundle in bundles:
            checks = q.default_checks(bundle)
            server, base = q.serve(bundle)
            # A fresh browser per bundle: one long-lived DevTools socket can drop on a long sweep.
            proc, c = q.open_browser()
            c.send("Page.addScriptToEvaluateOnNewDocument", {"source": ERRORS})
            row = {"bundle": f"{bundle.parent.parent.name}/{bundle.name}", "checks": len(checks), "faults": []}
            try:
                if not checks:
                    row["faults"].append("no walkthrough checks")
                for check in checks:
                    c.send("Page.navigate", {"url": f"{base}?embed=1&overlay=3&check={check}"})
                    c.wait_for("!!window.__swingViewer && window.__swingViewer.checkId === " + json.dumps(check), timeout=60, label=check)
                    time.sleep(2.0)                       # the reveal runs to its end on its own
                    r = json.loads(c.evaluate(PROBE))
                    benign = [e for e in r["errors"] if "ResizeObserver loop" in e]
                    r["errors"] = [e for e in r["errors"] if e not in benign]
                    row["resize_observer_notices"] = row.get("resize_observer_notices", 0) + len(benign)
                    if r["nan"] or r["outside"] or r["errors"] or r["done"] is False:
                        row["faults"].append({"check": check, **r})
                # One full walkthrough at 1x.
                c.send("Page.navigate", {"url": f"{base}?embed=1&overlay=3&autoplay=1"})
                deadline = time.time() + 120
                state = None
                while time.time() < deadline:
                    state = c.evaluate("window.__swingViewer ? window.__swingViewer.state : null")
                    if state == "ended":
                        break
                    time.sleep(0.5)
                errs = json.dumps([e for e in json.loads(c.evaluate("JSON.stringify(window.__errs || [])"))
                                   if "ResizeObserver loop" not in e])
                if state != "ended":
                    row["faults"].append(f"walkthrough did not finish (state {state})")
                if errs and errs != "[]":
                    row["faults"].append(f"errors during playback: {errs}")
            except Exception as error:  # noqa: BLE001
                row["faults"].append(f"harness: {type(error).__name__}: {error}")
            finally:
                server.shutdown()
                proc.terminate()
            report.append(row)
            print(json.dumps({"bundle": row["bundle"], "checks": row["checks"], "faults": len(row["faults"])}), flush=True)
    finally:
        if proc is not None:
            proc.terminate()
    # A swing with no walkthrough checks at all (no swing moments found) is reported, not a fault.
    for r in report:
        r["faults"] = [f for f in r["faults"] if f != "no walkthrough checks"]
    faulty = [r for r in report if r["faults"]]
    summary = {"bundles": len(report), "checks": sum(r["checks"] for r in report), "bundles_with_faults": len(faulty),
               "resize_observer_notices": sum(r.get("resize_observer_notices", 0) for r in report), "faults": faulty}
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "faults"}))
    return 1 if faulty else 0


if __name__ == "__main__":
    raise SystemExit(main())
