"""Export a replay as a 1080 x 1920 MP4 for social video (staging / local only).

    # a local bundle (analysis.json + swing.mp4), e.g. a validation run:
    python scripts/export_replay_video.py --bundle var/qa/validation_prod/candidate/54 --out out.mp4

    # a swing analysed on STAGING (never production): token from smoke_service --token-out
    python scripts/export_replay_video.py --staging-swing <id> --token-file token.txt --out out.mp4

Plays the replay viewer from this checkout (overlay v3, ``capture=1``: clean 9:16 layout, controls
hidden, checkpoint card over the top of the picture, ending on the stored Swing Score) in headless
Edge/Chrome at 360 x 640 CSS px with a device scale of 3, records it with the DevTools screencast,
and encodes the frames at their real timestamps into a constant-30-fps H.264 MP4 with ffmpeg.

Read-only: production is never contacted. A staging swing is fetched with the staging access key
(.tmp/staging_access_key.txt) and served locally with the same viewer code staging runs. A local
bundle without a stored ``swing_score`` is scored with ``swingguru.scoring.score_swing`` (the same
function the server uses) so the closing frame shows a score.
"""

from __future__ import annotations

import argparse
import base64
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

import websockets.sync.client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import qa_ui_screenshots as ui  # noqa: E402

STAGING = "https://swing-guru-staging-production.up.railway.app"
WIDTH, HEIGHT, SCALE = 360, 640, 3


class Recorder:
    """DevTools client that keeps every event (the screencast needs each frame acknowledged)."""

    def __init__(self, url: str) -> None:
        self.ws = websockets.sync.client.connect(url, max_size=None, open_timeout=30)
        self.id = 0
        self.frames: list[tuple[float, bytes]] = []

    def _handle(self, data: dict) -> None:
        if data.get("method") == "Page.screencastFrame":
            p = data["params"]
            self.frames.append((float(p["metadata"]["timestamp"]), base64.b64decode(p["data"])))
            self.id += 1
            self.ws.send(json.dumps({"id": self.id, "method": "Page.screencastFrameAck",
                                     "params": {"sessionId": p["sessionId"]}}))

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self.id += 1
        mine = self.id
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            data = json.loads(self.ws.recv(timeout=max(0.01, deadline - time.monotonic())))
            if data.get("id") == mine:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})
            self._handle(data)

    def evaluate(self, expression: str):
        return self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                              "awaitPromise": True}).get("result", {}).get("value")

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self._handle(json.loads(self.ws.recv(timeout=max(0.01, end - time.monotonic()))))
            except TimeoutError:
                return


def stage_bundle(args, work: Path) -> None:
    if args.bundle:
        bundle = Path(args.bundle)
        analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
        shutil.copyfile(bundle / "swing.mp4", work / "swing.mp4")
        if not analysis.get("swing_score"):
            from swingguru.scoring import score_swing

            analysis["swing_score"] = score_swing(analysis)
    else:
        import httpx

        headers = {"Authorization": "Bearer " + Path(args.token_file).read_text().strip(),
                   "X-SG-Access-Key": (ROOT / ".tmp" / "staging_access_key.txt").read_text().strip()}
        detail = httpx.get(f"{STAGING}/v1/swings/{args.staging_swing}", headers=headers, timeout=30).raise_for_status().json()
        analysis = httpx.get(detail["replay"]["analysis_url"], timeout=60).raise_for_status().json()
        (work / "swing.mp4").write_bytes(httpx.get(detail["replay"]["video_url"], timeout=120).raise_for_status().content)
    analysis["video"] = "swing.mp4"
    (work / "analysis.json").write_text(json.dumps(analysis), encoding="utf-8")
    for name in ("index.html", "app.css", "app.js"):
        shutil.copyfile(ROOT / "src" / "swingguru" / "viewer" / name, work / name)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def encode(frames: list[tuple[float, bytes]], out: Path, fps: int) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="sg-export-f-"))
    lines = []
    for i, (t, jpg) in enumerate(frames):
        name = tmp / f"f{i:05d}.jpg"
        name.write_bytes(jpg)
        dur = (frames[i + 1][0] - t) if i + 1 < len(frames) else 1.0 / fps
        lines += [f"file '{name.as_posix()}'", f"duration {max(dur, 0.001):.4f}"]
    lines.append(f"file '{(tmp / f'f{len(frames) - 1:05d}.jpg').as_posix()}'")
    (tmp / "list.txt").write_text("\n".join(lines), encoding="utf-8")
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
                    "-vf", f"scale={WIDTH * SCALE}:{HEIGHT * SCALE}:flags=lanczos,fps={fps}", "-c:v", "libx264",
                    "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--bundle")
    src.add_argument("--staging-swing")
    parser.add_argument("--token-file", help="staging bearer token (with --staging-swing)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="sg-export-"))
    stage_bundle(args, work)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(work)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html?embed=1&overlay=3&capture=1"
    port = ui.free_port()
    proc = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-export-b-")),
                             extra_args=["--autoplay-policy=no-user-gesture-required"])
    try:
        ui.wait_for_devtools(port)
        rec = Recorder(ui.first_page_target(port)["webSocketDebuggerUrl"])
        rec.send("Page.enable")
        rec.send("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": SCALE, "mobile": True})
        rec.send("Page.navigate", {"url": url})
        deadline = time.monotonic() + 60
        while not rec.evaluate("!!window.__swingViewer && window.__swingViewer.state === 'ready'"):
            if time.monotonic() > deadline:
                raise TimeoutError("viewer did not load")
            rec.pump(0.2)
        rec.pump(0.5)
        rec.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": WIDTH * SCALE,
                                          "maxHeight": HEIGHT * SCALE, "everyNthFrame": 1})
        rec.pump(0.6)                                   # a still first frame before playback
        rec.evaluate("document.getElementById('play').click()")
        deadline = time.monotonic() + args.timeout
        while not rec.evaluate("!!window.__captureDone"):
            if time.monotonic() > deadline:
                raise TimeoutError("the walkthrough did not finish")
            rec.pump(0.25)
        rec.send("Page.stopScreencast")
        frames = sorted(rec.frames)
        if len(frames) < 10:
            raise RuntimeError(f"only {len(frames)} frames captured")
        span = frames[-1][0] - frames[0][0]
        print(f"captured {len(frames)} frames over {span:.1f} s ({len(frames) / max(span, 0.001):.1f} fps)", flush=True)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        encode(frames, out, args.fps)
        print("wrote", out)
    finally:
        proc.terminate()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
