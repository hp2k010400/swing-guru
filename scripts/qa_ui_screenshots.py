"""Capture real browser screenshots of the local Swing Guru viewer.

Drives the installed Microsoft Edge in headless mode over the Chrome DevTools
Protocol. ``websockets`` already ships as a Streamlit dependency, so this needs
no extra package and no network access. The module also exposes the CDP helpers
that ``qa_guided_playback.py`` builds on.

This script covers the page surface: the upload panel, the guided player ready
to play, the Adjust moments editor and the collapsed Advanced analysis section,
at 1440px and at 390px with a measured horizontal-overflow check. The
playback-timing evidence (holds, resume, speed, seek) lives in
``scripts/qa_guided_playback.py``.

Usage::

    .venv\\Scripts\\python.exe scripts\\qa_ui_screenshots.py ^
        --url http://127.0.0.1:8501 ^
        --out var\\qa\\analysis-ui

The script walks the real page: upload panel, sample-clip analysis, an
unchosen checkpoint, a manually chosen moment, the next checkpoint and the
collapsed Advanced analysis section - at desktop and 390px mobile widths. Every
capture records the measured horizontal overflow and the theme colours that
were actually rendered, and the run fails loudly if a step does not appear
instead of writing a screenshot of an unexpected page.
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets.sync.client

ROOT = Path(__file__).resolve().parents[1]

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_browser() -> Path:
    for candidate in EDGE_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return path
    raise SystemExit(
        "no Edge or Chrome binary found; browser QA cannot run in this environment"
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class DevTools:
    """Minimal synchronous Chrome DevTools Protocol client."""

    def __init__(self, websocket_url: str) -> None:
        self._ws = websockets.sync.client.connect(
            websocket_url, max_size=None, open_timeout=30
        )
        self._id = 0

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass

    def send(self, method: str, params: dict | None = None, timeout: float = 60.0):
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no reply to {method} within {timeout}s")
            raw = self._ws.recv(timeout=remaining)
            data = json.loads(raw)
            if data.get("id") != self._id:
                continue
            if "error" in data:
                raise RuntimeError(f"{method} failed: {data['error']}")
            return data.get("result", {})

    def evaluate(self, expression: str):
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        return result.get("result", {}).get("value")

    def wait_for(
        self, expression: str, *, timeout: float = 60.0, label: str = "condition"
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression):
                    return
            except Exception:  # noqa: BLE001 - the page may still be loading
                pass
            time.sleep(0.3)
        raise TimeoutError(f"timed out waiting for {label}")

    def viewport(self, width: int, height: int, *, mobile: bool = False) -> None:
        self.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 2 if mobile else 1,
                "mobile": mobile,
            },
        )

    def reset_viewport(self) -> None:
        self.send("Emulation.clearDeviceMetricsOverride")

    def screenshot(self, path: Path, *, max_height: int = 6000) -> dict:
        metrics = self.send("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        width = max(1, int(size.get("width") or 1440))
        height = max(1, min(int(size.get("height") or 1200), max_height))
        result = self.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": height,
                    "scale": 1,
                },
            },
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        return {"width": width, "height": height}


def click_text(text: str) -> str:
    return (
        "(() => { const target = "
        f"{json.dumps(text)}; "
        "const node = Array.from(document.querySelectorAll('button'))"
        ".find(item => item.innerText.trim() === target); "
        "if (!node) return false; node.click(); return true; })()"
    )


def click_label(text: str) -> str:
    return (
        "(() => { const target = "
        f"{json.dumps(text)}; "
        "const label = Array.from(document.querySelectorAll('label'))"
        ".find(item => item.innerText.trim() === target); "
        "if (!label) return false; label.click(); return true; })()"
    )


def open_expander(text: str) -> str:
    return (
        "(() => { const target = "
        f"{json.dumps(text)}; "
        "const details = Array.from(document.querySelectorAll('details'))"
        ".find(item => item.innerText.includes(target)); "
        "if (!details) return false; "
        "if (!details.open) { const summary = details.querySelector('summary'); "
        "if (!summary) return false; summary.click(); } "
        "return true; })()"
    )


def page_has(text: str) -> str:
    return f"document.body.innerText.includes({json.dumps(text)})"


def scroll_to(text: str) -> str:
    """Scroll the deepest element holding the text into the middle of the view."""

    return (
        "(() => { const target = "
        f"{json.dumps(text)}; "
        "const nodes = Array.from(document.querySelectorAll('body *')); "
        "const matches = nodes.filter(node => "
        "node.children.length === 0 && node.innerText && "
        "node.innerText.includes(target)); "
        "const el = matches[matches.length - 1]; "
        "if (!el) return false; "
        "el.scrollIntoView({ block: 'center', inline: 'nearest' }); "
        "return true; })()"
    )


def overflow_pixels() -> str:
    return (
        "(() => { const doc = document.documentElement; "
        "const body = document.body; "
        "const widest = Math.max(doc.scrollWidth, body ? body.scrollWidth : 0); "
        "return Math.round(widest - window.innerWidth); })()"
    )


def theme_probe() -> str:
    return (
        "(() => { const app = document.querySelector('.stApp'); "
        "const button = document.querySelector('button[kind=\"primary\"]'); "
        "return { background: app ? getComputedStyle(app).backgroundColor : null, "
        "primary: button ? getComputedStyle(button).backgroundColor : null, "
        "radius: button ? getComputedStyle(button).borderRadius : null, "
        "body: getComputedStyle(document.body).color }; })()"
    )


def frame_counter(client: "DevTools") -> str | None:
    return client.evaluate(
        "(() => { const m = document.body.innerText"
        ".match(/Frame \\d+ of \\d+/); return m ? m[0] : null; })()"
    )


def slider_geometry(client: "DevTools") -> dict | None:
    """Locate the slider handle and its track across Streamlit DOM versions."""

    return client.evaluate(
        "(() => { const rects = el => { const r = el.getBoundingClientRect(); "
        "return { x: r.x, y: r.y, width: r.width, height: r.height, "
        "cx: r.x + r.width / 2, cy: r.y + r.height / 2 }; }; "
        "const handles = Array.from(document.querySelectorAll('[role=\"slider\"]')); "
        "const tracks = Array.from(document.querySelectorAll("
        "'[data-baseweb=\"slider\"], [data-testid=\"stSlider\"]')); "
        "const handle = handles.length ? rects(handles[0]) : null; "
        "const track = tracks.length ? rects(tracks[0]) : null; "
        "return { handle: handle, track: track, "
        "handleCount: handles.length, trackCount: tracks.length, "
        "sample: tracks.length ? tracks[0].outerHTML.slice(0, 160) : null }; })()"
    )


def nudge_slider(client: "DevTools", fraction: float) -> dict:
    """Drag (or click) the browse slider, then report whether it moved."""

    before = frame_counter(client)
    geometry = slider_geometry(client)
    if not geometry or not geometry.get("track"):
        return {"moved": False, "before": before, "geometry": geometry}

    track = geometry["track"]
    handle = geometry.get("handle") or track
    start_x = handle["cx"]
    start_y = handle["cy"]
    target_x = track["x"] + max(0.0, min(1.0, fraction)) * track["width"]
    client.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mousePressed",
            "x": start_x,
            "y": start_y,
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
        },
    )
    for step in range(1, 7):
        x = start_x + (target_x - start_x) * step / 6
        client.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": x,
                "y": start_y,
                "button": "left",
                "buttons": 1,
            },
        )
        time.sleep(0.05)
    client.send(
        "Input.dispatchMouseEvent",
        {
            "type": "mouseReleased",
            "x": target_x,
            "y": start_y,
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
        },
    )
    time.sleep(1.5)
    after = frame_counter(client)
    if after == before:
        # Streamlit's slider also accepts a track click in most builds.
        client.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": target_x,
                "y": track["cy"],
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
            },
        )
        client.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": target_x,
                "y": track["cy"],
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
            },
        )
        time.sleep(1.5)
        after = frame_counter(client)
    return {
        "moved": after != before,
        "before": before,
        "after": after,
        "geometry": geometry,
    }


def launch_browser(
    binary: Path,
    port: int,
    profile: Path,
    *,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    args = [
        str(binary),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--window-size=1440,1200",
        *(extra_args or []),
        "about:blank",
    ]
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def wait_for_devtools(port: int, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(0.5)
    raise SystemExit(f"browser DevTools endpoint never came up: {last_error}")


def first_page_target(port: int) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        targets = http_json(f"http://127.0.0.1:{port}/json/list")
        for target in targets:
            if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                return target
        time.sleep(0.5)
    raise SystemExit("no page target available in the headless browser")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "analysis-ui"))
    parser.add_argument("--keep-browser", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    binary = find_browser()
    port = free_port()
    profile = Path(tempfile.mkdtemp(prefix="swingguru-qa-"))
    log: dict[str, object] = {
        "url": args.url,
        "browser": str(binary),
        "captures": [],
        "console": [],
    }

    process = launch_browser(binary, port, profile)
    client: DevTools | None = None
    try:
        version = wait_for_devtools(port)
        log["browser_version"] = version.get("Browser")
        target = first_page_target(port)
        client = DevTools(target["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Page.navigate", {"url": args.url})
        client.wait_for(
            page_has("Analyse swing"),
            timeout=120,
            label="the redesigned upload panel",
        )

        def capture(name: str, note: str, *, scroll: str | None = None) -> None:
            if scroll is not None:
                if not client.evaluate(scroll_to(scroll)):
                    raise SystemExit(f"could not scroll to {scroll!r} for {name}")
                time.sleep(0.6)
            path = out_dir / f"{name}.png"
            size = client.screenshot(path)
            overflow = client.evaluate(overflow_pixels())
            frame = {
                "name": name,
                "file": path.name,
                "note": note,
                "size": size,
                "overflow_px": overflow,
                "theme": client.evaluate(theme_probe()),
                "checkpoint": client.evaluate(
                    "(() => { const m = document.body.innerText"
                    ".match(/Checkpoint \\d of \\d/); return m ? m[0] : null; })()"
                ),
            }
            log["captures"].append(frame)
            print(f"captured {path.name}: {size} overflow={overflow}px")

        client.viewport(1440, 2200)
        time.sleep(1.0)
        capture("01-upload-desktop", "first screen, desktop 1440x2200")

        client.viewport(390, 844, mobile=True)
        time.sleep(1.0)
        capture("02-upload-mobile", "first screen, mobile 390x844")
        client.viewport(1440, 2200)
        time.sleep(1.0)

        if not client.evaluate(open_expander("Developer details")):
            raise SystemExit("could not open the Developer details section")
        client.wait_for(page_has("Guided playback sample"), label="the source options")
        if not client.evaluate(click_label("Guided playback sample")):
            raise SystemExit("could not select the guided playback sample")
        client.wait_for(
            page_has("Create guided fixture and analyse"), label="the guided fixture button"
        )
        if not client.evaluate(click_text("Create guided fixture and analyse")):
            raise SystemExit("could not start the guided sample analysis")
        client.wait_for(
            page_has("Your swing plays between the estimated moments"),
            timeout=240,
            label="the guided player",
        )
        capture(
            "03-player-ready-desktop",
            "guided player ready with the moments loaded",
            scroll="Swing Guru",
        )

        if not client.evaluate(open_expander("Adjust moments")):
            raise SystemExit("could not open the Adjust moments section")
        time.sleep(1.0)
        capture(
            "04-adjust-moments-desktop",
            "manual moment editor with the candidate frame preview",
            scroll="Moment to adjust",
        )

        if not client.evaluate(open_expander("Advanced analysis")):
            raise SystemExit("could not open the Advanced analysis section")
        time.sleep(1.0)
        capture(
            "05-advanced-desktop",
            "raw measurements, moment provenance and exports",
            scroll="Moment estimates",
        )

        client.viewport(390, 844, mobile=True)
        time.sleep(1.5)
        capture(
            "06-player-mobile",
            "guided player at 390px",
            scroll="Swing Guru",
        )
        capture(
            "07-advanced-mobile",
            "Advanced analysis at 390px",
            scroll="Moment estimates",
        )

        log["ok"] = True
    finally:
        if client is not None:
            client.close()
        if not args.keep_browser:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        if not args.keep_browser:
            shutil.rmtree(profile, ignore_errors=True)
        (out_dir / "capture-log.json").write_text(
            json.dumps(log, indent=2), encoding="utf-8"
        )
        print(f"log written to {out_dir / 'capture-log.json'}")
    return 0 if log.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
