"""Overlay v2 vs v3 on the identical swing: screenshots of every check, and v3's reveal step by step.

    python scripts/overlay_v3_qa.py <bundle dir> <out dir> [check ...] [--times 0,250,500,800,1100]

Serves the bundle (analysis.json + swing.mp4) with the viewer files from src/swingguru/viewer, opens
each check held (``?embed=1&check=<phase:kind>``) in headless Edge/Chrome at 390 x 844, and writes
``<check>.png``: v2 (settled) | v3 at each requested reveal time | v3 settled. The v3 times are set
with the viewer's QA hook ``__swingViewer.v3At(ms)``, so frames are deterministic. Checks default to
every walkthrough check plus every observation in the bundle. Read-only: nothing in the bundle changes.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import qa_ui_screenshots as ui  # noqa: E402


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(bundle: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    work = Path(tempfile.mkdtemp(prefix="sg-ov3-"))
    for name in ("analysis.json", "swing.mp4"):
        shutil.copyfile(bundle / name, work / name)
    for name in ("index.html", "app.css", "app.js"):
        shutil.copyfile(ROOT / "src" / "swingguru" / "viewer" / name, work / name)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(work)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/index.html"


def default_checks(bundle: Path) -> list[str]:
    analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    return [f"{p['phase']}:{c['kind']}" for p in analysis["phases"] for c in p["checks"]
            if c.get("guided") or c.get("observation")]


def open_browser():
    port = ui.free_port()
    proc = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-ov3-b-")),
                             extra_args=["--autoplay-policy=no-user-gesture-required"])
    ui.wait_for_devtools(port)
    c = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.viewport(390, 844, mobile=True)
    return proc, c


def load(c, url: str, label: str) -> None:
    c.send("Page.navigate", {"url": url})
    c.wait_for("!!window.__swingViewer && window.__swingViewer.state !== 'loading' && !!window.__swingViewer.checkId",
               timeout=60, label=label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    parser.add_argument("out")
    parser.add_argument("checks", nargs="*")
    parser.add_argument("--times", default="0,300,600,900,1200")
    args = parser.parse_args(argv)
    bundle, out = Path(args.bundle), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = args.checks or default_checks(bundle)
    times = [int(t) for t in args.times.split(",") if t.strip()]
    server, base = serve(bundle)
    proc, c = open_browser()
    from PIL import Image, ImageDraw

    report = []
    try:
        for check in checks:
            shots, labels = [], []
            load(c, f"{base}?embed=1&check={check}&overlay=2", check)
            time.sleep(2.2)
            path = out / f"{check.replace(':', '_')}__v2.png"
            c.screenshot(path, max_height=844)
            shots.append(path); labels.append("v2")
            load(c, f"{base}?embed=1&check={check}&overlay=3", check)
            time.sleep(0.3)
            info = c.evaluate("JSON.stringify(window.__swingViewer.v3)")
            end = json.loads(info)["end"] if info else 0
            for t in times + [end]:
                c.evaluate(f"window.__swingViewer.v3At({t})")
                time.sleep(0.12)
                path = out / f"{check.replace(':', '_')}__v3_{t}.png"
                c.screenshot(path, max_height=844)
                shots.append(path); labels.append(f"v3 {t} ms" if t != end else f"v3 final ({end} ms)")
            card = c.evaluate("JSON.stringify(window.__swingViewer.check)")
            report.append({"check": check, "v3_end_ms": end, "card": json.loads(card) if card else None})
            images = [Image.open(p) for p in shots]
            w = sum(i.width for i in images) + 12 * (len(images) - 1)
            sheet = Image.new("RGB", (w, max(i.height for i in images) + 26), (245, 247, 246))
            x = 0
            draw = ImageDraw.Draw(sheet)
            for img, label in zip(images, labels):
                sheet.paste(img, (x, 26))
                draw.text((x + 6, 6), label, fill=(30, 30, 30))
                x += img.width + 12
            sheet.save(out / f"{check.replace(':', '_')}.png")
            print("wrote", out / f"{check.replace(':', '_')}.png", flush=True)
    finally:
        proc.terminate()
        server.shutdown()
    (out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
