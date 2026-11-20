"""Viewer regression: v1 and v2 must render exactly as the production baseline.

    python scripts/viewer_regression.py <bundle dir> [<bundle dir> ...] [--baseline-ref main]

For every bundle, every walkthrough check and both production overlays (none = v1, ``overlay=2``),
the baseline viewer (``git show <ref>:src/swingguru/viewer/*``, i.e. what production serves) and the
working-tree viewer are opened on the same bundle in headless Edge/Chrome at 390 x 844 and compared:

* the overlay SVG markup once the check has settled and the layout is stable (every path, circle,
  label and attribute except the animation bookkeeping ``class`` / ``style``);
* the card text (phase, verdict, value, detail, badge);
* the walkthrough plan (check ids in order, moment times).

Exit code 1 on any difference. Read-only.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import qa_ui_screenshots as ui  # noqa: E402

FILES = ("index.html", "app.css", "app.js")
PROBE = """JSON.stringify({
  svg: document.getElementById('overlay').innerHTML,
  card: ['card-phase','card-title','card-value','card-detail','card-badge'].map(function (id) {
    var e = document.getElementById(id); return e ? (e.hidden ? '[hidden]' : '') + e.textContent : null; }),
  status: document.getElementById('card').getAttribute('data-status'),
  plan: window.__swingViewer.checkIds, times: window.__swingViewer.eventTimes, check: window.__swingViewer.checkId,
  box: window.__swingViewer.overlay.box
})"""


def normalise(probe: dict) -> dict:
    """Drop animation bookkeeping (class / style attributes); every geometry, colour and text stays."""

    import re

    probe = dict(probe)
    probe["svg"] = re.sub(r' (class|style)="[^"]*"', "", probe["svg"])
    return probe


def stable(c, timeout: float = 8.0) -> dict:
    """Probe until two consecutive readings 0.6 s apart agree (the card can relayout the video box late)."""

    last = normalise(json.loads(c.evaluate(PROBE)))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.6)
        now = normalise(json.loads(c.evaluate(PROBE)))
        if now == last:
            return now
        last = now
    return last


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def site(bundle: Path, viewer: dict[str, bytes]) -> tuple[http.server.ThreadingHTTPServer, str]:
    work = Path(tempfile.mkdtemp(prefix="sg-reg-"))
    for name in ("analysis.json", "swing.mp4"):
        shutil.copyfile(bundle / name, work / name)
    for name, data in viewer.items():
        (work / name).write_bytes(data)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(work)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/index.html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundles", nargs="+")
    parser.add_argument("--baseline-ref", default="main")
    parser.add_argument("--settle", type=float, default=2.4)
    parser.add_argument("--current-ref", default=None,
                        help="compare against this git ref instead of the working tree (main vs main = flakiness check)")
    parser.add_argument("--diff", action="store_true", help="print the first differing SVG fragment")
    parser.add_argument("--modes", nargs="+", default=["", "&overlay=2"],
                        help="query suffixes to compare (default v1 and v2; add '&overlay=3' for Build 14)")
    args = parser.parse_args(argv)
    baseline = {n: subprocess.run(["git", "show", f"{args.baseline_ref}:src/swingguru/viewer/{n}"], cwd=ROOT,
                                  capture_output=True, check=True).stdout for n in FILES}
    current = ({n: subprocess.run(["git", "show", f"{args.current_ref}:src/swingguru/viewer/{n}"], cwd=ROOT,
                                  capture_output=True, check=True).stdout for n in FILES} if args.current_ref else
               {n: (ROOT / "src" / "swingguru" / "viewer" / n).read_bytes() for n in FILES})
    port = ui.free_port()
    proc = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-reg-b-")),
                             extra_args=["--autoplay-policy=no-user-gesture-required"])
    compared, problems = 0, []
    try:
        ui.wait_for_devtools(port)
        c = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.viewport(390, 844, mobile=True)
        for bundle in map(Path, args.bundles):
            analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
            checks = [f"{p['phase']}:{c2['kind']}" for p in analysis["phases"] for c2 in p["checks"] if c2.get("guided")]
            servers = [site(bundle, baseline), site(bundle, current)]
            try:
                for mode in args.modes:
                    for check in checks:
                        # The video box can settle at slightly different sizes between page loads of the
                        # *same* viewer (pre-existing layout timing); compare only like-for-like boxes.
                        for attempt in range(5):
                            seen = []
                            for _, base in servers:
                                c.send("Page.navigate", {"url": f"{base}?embed=1&check={check}{mode}"})
                                c.wait_for("!!window.__swingViewer && !!window.__swingViewer.checkId", timeout=60, label=check)
                                time.sleep(args.settle)
                                seen.append(stable(c))
                            if seen[0]["box"] == seen[1]["box"]:
                                break
                        else:
                            problems.append(f"{bundle.name} {check} {mode or 'v1'}: video box never matched (layout timing)")
                            continue
                        compared += 1
                        if seen[0] != seen[1]:
                            diff = [k for k in seen[0] if seen[0][k] != seen[1][k]]
                            problems.append(f"{bundle.name} {check} {mode or 'v1'}: differs in {diff}")
                            if args.diff and "svg" in diff:
                                a, b = seen[0]["svg"], seen[1]["svg"]
                                i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
                                print("   baseline:", a[max(0, i - 120):i + 160])
                                print("   current: ", b[max(0, i - 120):i + 160], flush=True)
                print(f"{bundle}: {len(checks)} checks x 2 overlays compared", flush=True)
            finally:
                for server, _ in servers:
                    server.shutdown()
    finally:
        proc.terminate()
    print(json.dumps({"compared": compared, "differences": problems}, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
