"""Screenshot QA for the Swing Guru app shell (headless Edge, phone viewport).

    .venv\\Scripts\\python.exe scripts\\qa_app_screens.py [--url http://127.0.0.1:8780/] [--routes home,library]
        [--out var\\qa\\app] [--width 390 --height 844]

For every route: loads it, waits for the screen, then captures the viewport at
the top and after each scroll of the screen's scroller (so long screens are
reviewed end to end). Records JS errors, horizontal overflow and the rendered
tab. Exits non-zero if any route errors or overflows.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402

DEFAULT_ROUTES = "home,library,drills,profile,system"

ERROR_HOOK = """
window.__errs = [];
window.addEventListener('error', e => window.__errs.push(String(e.message || e)));
window.addEventListener('unhandledrejection', e => window.__errs.push('promise: ' + String(e.reason)));
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8780/")
    parser.add_argument("--routes", default=DEFAULT_ROUTES)
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "app"))
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--max-shots", type=int, default=6)
    parser.add_argument("--query", default="", help="extra query string, e.g. dev=1")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    port = ui.free_port()
    profile = Path(tempfile.mkdtemp(prefix="swingguru-app-qa-"))
    process = ui.launch_browser(ui.find_browser(), port, profile)
    report, failed = [], False
    try:
        ui.wait_for_devtools(port)
        client = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Page.addScriptToEvaluateOnNewDocument", {"source": ERROR_HOOK})
        client.viewport(args.width, args.height, mobile=True)
        base = args.url.rstrip("/") + "/" + (f"?{args.query}" if args.query else "")
        for route in [r for r in args.routes.split(",") if r]:
            # "home@analyze" opens the route, then clicks [data-action=analyze] (sheets).
            path_part, _, action = route.partition("@")
            client.send("Page.navigate", {"url": f"{base}#/{path_part}"})
            client.wait_for("!!document.querySelector('.view .scroll, .view')", timeout=30, label=f"route {route}")
            time.sleep(1.2)  # entrance animation + images
            if action:
                selector = action if action.startswith("[") else f'[data-action="{action}"]'
                client.evaluate(f"document.querySelector({json.dumps(selector)}).click()")
                time.sleep(0.8)
                route = path_part + "__" + "".join(ch for ch in action if ch.isalnum() or ch == "-")
            shots = []
            for index in range(args.max_shots):
                path = out / f"{route.replace('/', '_')}_{index}.png"
                client.screenshot(path)
                shots.append(path.name)
                moved = client.evaluate(
                    "(() => { const s = document.querySelector('.view .scroll'); if (!s) return false;"
                    " const before = s.scrollTop; s.scrollTop += s.clientHeight * 0.8;"
                    " return s.scrollTop > before; })()")
                if not moved:
                    break
                time.sleep(0.5)
            probe = client.evaluate(
                "(() => { const s = document.querySelector('.view .scroll');"
                " return { errors: window.__errs || [],"
                " overflow: s ? s.scrollWidth - s.clientWidth : 0,"
                " page_overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,"
                " tab: (document.querySelector('.tab[aria-current=page] span') || {}).textContent || null,"
                " title: document.title }; })()")
            entry = {"route": route, "shots": shots, **probe}
            bad = bool(probe["errors"]) or probe["overflow"] > 0 or probe["page_overflow"] > 0
            failed |= bad
            report.append(entry)
            print(f"{'FAIL' if bad else 'ok  '} {route}: {len(shots)} shots, overflow {probe['overflow']}px, "
                  f"errors {probe['errors']}", flush=True)
    finally:
        process.terminate()
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
