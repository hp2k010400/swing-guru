"""Time the replay exactly as the app opens it: API detail call, viewer page, analysis, video, first frame.

    .venv\\Scripts\\python.exe scripts\\bench_replay.py --api https://<service> --token-file token.txt [--swing ID]
        [--viewer-url URL]  # load a different viewer build against the same signed URLs (before/after)

Runs headless Edge at a phone viewport, cold (fresh profile) and with a
phone-like network (70 ms latency, 12 Mbit/s down). Milestones are measured
inside the page from navigation start; the API call is measured from here.
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

PROBE = """
window.ReactNativeWebView = { messages: [], postMessage: function (m) {
  var msg = JSON.parse(m); msg.at = performance.now(); this.messages.push(msg); } };
window.__sgT = {};
['loadedmetadata', 'loadeddata', 'canplay', 'playing'].forEach(function (name) {
  document.addEventListener(name, function () { if (!(name in window.__sgT)) window.__sgT[name] = performance.now(); }, true);
});
document.addEventListener('DOMContentLoaded', function () { window.__sgT.dom = performance.now(); });
"""

NETWORKS = {
    "wifi": None,
    "phone_4g": {"offline": False, "latency": 70, "downloadThroughput": 12e6 / 8, "uploadThroughput": 5e6 / 8},
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--swing", default=None)
    parser.add_argument("--viewer-url", default=None)
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")
    headers = {"Authorization": f"Bearer {Path(args.token_file).read_text(encoding='utf-8').strip()}"}
    http = httpx.Client(timeout=60)
    swing_id = args.swing or http.get(f"{api}/v1/swings", headers=headers).json()["swings"][0]["id"]
    api_times = []
    for _ in range(3):
        began = time.monotonic()
        detail = http.get(f"{api}/v1/swings/{swing_id}", headers=headers)
        api_times.append(round((time.monotonic() - began) * 1000))
    detail.raise_for_status()
    replay = detail.json()["replay"]
    sizes = {name: int(http.head(replay[f"{name}_url"]).headers.get("content-length", 0)) for name in ("analysis", "video")}
    print(json.dumps({"swing": swing_id, "api_detail_ms": api_times, "bytes": sizes}))
    url = f"{args.viewer_url or replay['viewer_url']}?" + urlencode(
        {"embed": "1", "analysis": replay["analysis_url"], "video": replay["video_url"], "autoplay": "1"})

    for network, conditions in NETWORKS.items():
        for run in range(args.runs):
            port = ui.free_port()
            process = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-replay-")),
                                        extra_args=["--autoplay-policy=no-user-gesture-required"])
            try:
                ui.wait_for_devtools(port)
                c = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
                c.send("Page.enable")
                c.send("Network.enable")
                c.send("Network.setCacheDisabled", {"cacheDisabled": True})
                if conditions:
                    c.send("Network.emulateNetworkConditions", conditions)
                c.send("Page.addScriptToEvaluateOnNewDocument", {"source": PROBE})
                c.viewport(390, 844, mobile=True)
                c.send("Page.navigate", {"url": url})
                c.wait_for("window.__sgT && 'playing' in window.__sgT", timeout=60, label="playing")
                marks = c.evaluate("JSON.stringify(window.__sgT)")
                resources = c.evaluate("""JSON.stringify(performance.getEntriesByType('resource').map(function (e) {
                    return [e.name.split('?')[0].split('/').pop(), Math.round(e.startTime), Math.round(e.responseEnd)]; }))""")
                messages = c.evaluate("JSON.stringify(window.ReactNativeWebView.messages)")
                nav = c.evaluate("JSON.stringify((function (n) { return {first_byte: Math.round(n.responseStart), html: Math.round(n.responseEnd)}; })(performance.getEntriesByType('navigation')[0]))")
            finally:
                process.terminate()
            marks = {k: round(v) for k, v in json.loads(marks).items()}
            print(json.dumps({"network": network, "run": run + 1, "html": json.loads(nav), "marks_ms": marks,
                              "resources_start_end_ms": json.loads(resources), "messages": json.loads(messages)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
