"""Screenshot one check in developer mode (``?dev=1``) for geometry review.

    .venv\\Scripts\\python.exe scripts\\qa_dev_overlay.py var\\analysis\\real impact:pelvis \
        --out var\\qa\\pelvis\\dev-impact-tush-line.png
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402
from qa_viewer import serve  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("check_id")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    server, port = serve(Path(args.bundle))
    browser_port = ui.free_port()
    process = ui.launch_browser(ui.find_browser(), browser_port, Path(tempfile.mkdtemp()), extra_args=["--mute-audio"])
    try:
        ui.wait_for_devtools(browser_port)
        client = ui.DevTools(ui.first_page_target(browser_port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.viewport(390, 844, mobile=True)
        client.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html?dev=1"})
        for _ in range(200):
            if client.evaluate("window.__swingViewer && window.__swingViewer.state") == "ready":
                break
            time.sleep(0.1)
        client.evaluate(f"window.__swingViewer.goToCheck('{args.check_id}')")
        time.sleep(1.5)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        client.screenshot(Path(args.out), max_height=844)
        print("shown:", client.evaluate("window.__swingViewer.checkId"))
        client.close()
    finally:
        process.terminate()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
