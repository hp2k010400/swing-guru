"""Side-by-side screenshots of the replay overlay: current (v1) vs ?overlay=2, per check.

    python scripts/overlay_compare.py <bundle dir> <out dir> [check ...]

Serves the bundle (analysis.json + swing.mp4) with the *current* viewer files from
src/swingguru/viewer, opens each check held (``?embed=1&check=<phase:kind>``) in headless Edge/Chrome
at phone size, once without and once with ``overlay=2``, and writes ``<check>_v1.png``,
``<check>_v2.png`` and a side-by-side ``<check>.png``. Read-only: nothing in the bundle changes.
"""

from __future__ import annotations

import functools
import http.server
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import qa_ui_screenshots as ui  # noqa: E402

DEFAULT = ["address:torso_angle", "address:trail_knee", "address:arm_hang", "mid_back:shaft", "top:posture",
           "downswing:shaft", "impact:pelvis", "impact:head"]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main(argv: list[str]) -> int:
    bundle, out = Path(argv[0]), Path(argv[1])
    checks = argv[2:] or DEFAULT
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sg-ov2-"))
    for name in ("analysis.json", "swing.mp4"):
        shutil.copyfile(bundle / name, work / name)
    for name in ("index.html", "app.css", "app.js"):
        shutil.copyfile(ROOT / "src" / "swingguru" / "viewer" / name, work / name)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(work)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    port = ui.free_port()
    proc = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-ov2-b-")),
                             extra_args=["--autoplay-policy=no-user-gesture-required"])
    try:
        ui.wait_for_devtools(port)
        c = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.viewport(390, 844, mobile=True)
        from PIL import Image

        for check in checks:
            shots = []
            for tag, extra in (("v1", ""), ("v2", "&overlay=2")):
                c.send("Page.navigate", {"url": f"{base}?embed=1&check={check}{extra}"})
                c.wait_for("!!window.__swingViewer && window.__swingViewer.state !== 'loading'", timeout=60, label=check)
                time.sleep(2.2)  # the held moment, after the draw-on sequence
                path = out / f"{check.replace(':', '_')}_{tag}.png"
                c.screenshot(path, max_height=844)
                shots.append(path)
            a, b = Image.open(shots[0]), Image.open(shots[1])
            side = Image.new("RGB", (a.width + b.width + 24, max(a.height, b.height)), (245, 247, 246))
            side.paste(a, (0, 0))
            side.paste(b, (a.width + 24, 0))
            side.save(out / f"{check.replace(':', '_')}.png")
            print("wrote", out / f"{check.replace(':', '_')}.png", flush=True)
    finally:
        proc.terminate()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
