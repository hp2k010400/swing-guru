"""QA the replay exactly as the mobile app's WebView loads it (headless Edge, phone viewport).

    .venv\\Scripts\\python.exe scripts\\qa_mobile_replay.py --api http://127.0.0.1:8790 --token dev:<user> [--out var\\qa\\mobile]

For the user's latest swing: fetches the Summary from the API, builds the same
viewer URL as ``mobile/src/app/replay/[id].tsx`` (signed analysis + video URLs,
``embed=1``), and checks (1) full replay starts, (2) a checkpoint deep link
lands on that check, (3) the back button posts ``close`` to the native bridge.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402

BRIDGE = """window.ReactNativeWebView = { messages: [], postMessage: function (m) { this.messages.push(JSON.parse(m)); } };"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "mobile"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {args.token}"}
    swing = httpx.get(f"{args.api}/v1/swings", headers=headers).json()["swings"][0]
    detail = httpx.get(f"{args.api}/v1/swings/{swing['id']}", headers=headers).json()
    replay = detail["replay"]
    target = detail["checkpoints"][-1]["id"].replace(".", ":", 1)
    base = {"embed": "1", "analysis": replay["analysis_url"], "video": replay["video_url"]}
    results = []

    port = ui.free_port()
    process = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-mobile-")),
                                extra_args=["--autoplay-policy=no-user-gesture-required"])
    try:
        ui.wait_for_devtools(port)
        c = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.send("Page.addScriptToEvaluateOnNewDocument", {"source": BRIDGE})
        c.viewport(390, 844, mobile=True)
        for name, extra in (("full", {"autoplay": "1"}), ("deeplink", {"check": target})):
            c.send("Page.navigate", {"url": f"{replay['viewer_url']}?{urlencode({**base, **extra})}"})
            c.wait_for("!!window.__swingViewer && window.__swingViewer.state !== 'loading'", timeout=60, label=name)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                state, check = c.evaluate("window.__swingViewer.state"), c.evaluate("window.__swingViewer.checkId")
                if (name == "full" and state in ("playing", "holding")) or (name == "deeplink" and check == target):
                    break
                time.sleep(0.3)
            time.sleep(1.0)
            c.screenshot(out / f"replay_{name}.png")
            ok = state in ("playing", "holding") if name == "full" else check == target
            results.append((f"{name}: {'plays' if name == 'full' else 'lands on ' + target}", ok, f"state={state} check={check}"))
        c.evaluate("document.getElementById('to-results').click()")
        time.sleep(0.5)
        messages = c.evaluate("window.ReactNativeWebView.messages")
        results.append(("back button posts 'close' to the app", any(m.get("type") == "close" for m in messages), json.dumps(messages)))
    finally:
        process.terminate()
    for label, ok, detail_text in results:
        print(f"{'PASS' if ok else 'FAIL'} {label} - {detail_text}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
