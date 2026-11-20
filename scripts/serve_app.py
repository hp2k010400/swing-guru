"""Local server for the Swing Guru app shell.

    .venv\\Scripts\\python.exe scripts\\serve_app.py [--no-browser] [--port 8780]

Serves:

* ``/`` - the app (``src/swingguru/app/index.html``); ``/app/*`` its assets;
* ``/swings/<id>/*`` - an analysed swing bundle (the guided viewer, ``analysis.json``,
  ``swing.mp4``) with byte ranges so video seeks;
* ``/thumbs/<id>.jpg`` - a swing's Address-frame thumbnail (made once, cached in ``var/app/thumbs``);
* ``/api/*`` - the app's JSON API (see ``swingguru.app_api``); ``POST /api/analyze``
  takes a raw video body (headers ``X-File-Name``, ``X-File-Modified``) and starts
  an analysis job (``swingguru.app_jobs``); ``POST /api/swings/<id>/meta`` tags the club.

Demo stubs (score, trend, PR, "Start here") are on by default; ``--production``
shows the honest not-available states instead. Add ``?demo=0`` to any API call
to compare.

Nothing here changes the analysis engine or its outputs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import re
import sys
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

APP = ROOT / "src" / "swingguru" / "app"
VIEWER = ROOT / "src" / "swingguru" / "viewer"
VIEWER_FILES = {"index.html", "app.js", "app.css"}
DEFAULT_PORT = 8780


def swing_dirs() -> dict[str, Path]:
    """Every analysed swing bundle the app can show, by id."""

    try:
        from swingguru.app_api import bundle_dirs
    except ImportError:  # Phase 2: the API module arrives in Phase 3
        return {}
    return bundle_dirs()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet console
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    # -- routing -----------------------------------------------------------

    def translate_path(self, path: str) -> str:
        path = unquote(urlparse(path).path)
        if path in ("/", "/index.html"):
            return str(APP / "index.html")
        if path.startswith("/app/"):
            return str(self._inside(APP, path[len("/app/"):]))
        thumb = re.match(r"^/thumbs/([^/]+)\.jpg$", path)
        if thumb:
            from swingguru.app_api import thumbnail_path

            made = thumbnail_path(thumb.group(1))
            return str(made) if made else str(APP / "__missing__")
        match = re.match(r"^/swings/([^/]+)/(.*)$", path)
        # The viewer's own files always come from the source (bundles hold copies
        # made at build time); its data (analysis.json, swing.mp4) from the bundle.
        if match and (match.group(2) or "index.html") in VIEWER_FILES:
            return str(VIEWER / (match.group(2) or "index.html"))
        if match:
            base = swing_dirs().get(match.group(1))
            if base is not None:
                return str(self._inside(base, match.group(2) or "index.html"))
        return str(APP / "__missing__")

    @staticmethod
    def _inside(base: Path, relative: str) -> Path:
        target = (base / relative).resolve()
        return target if str(target).startswith(str(base.resolve())) else base / "__missing__"

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            try:
                from swingguru.app_api import handle_get
            except ImportError:
                return self._json({"error": "the app API is not available yet"}, 501)
            status, payload = handle_get(path, self.path)
            return self._json(payload, status)
        return self._serve_file()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        from swingguru.app_api import handle_post

        status, payload = handle_post(path, self.rfile, self.headers)
        return self._json(payload, status)

    # -- files with byte ranges (video seeking) ------------------------------

    def _serve_file(self) -> None:
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        path = Path(self.translate_path(self.path))
        if not match or not path.is_file():
            return super().do_GET()
        size = path.stat().st_size
        start = int(match.group(1)) if match.group(1) else max(0, size - int(match.group(2) or 0))
        end = min(int(match.group(2)), size - 1) if match.group(1) and match.group(2) else size - 1
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            return self.end_headers()
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = handle.read(min(1 << 16, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (ConnectionError, OSError):
                    return
                remaining -= len(chunk)


Handler.extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript",
                          ".mjs": "text/javascript", ".css": "text/css", ".json": "application/json"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--production", action="store_true",
                        help="no demo stubs: score, trend and PR show their not-available states")
    args = parser.parse_args(argv)
    import os

    os.environ["SG_APP_DEMO"] = "0" if args.production else os.environ.get("SG_APP_DEMO", "1")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    address = f"http://127.0.0.1:{args.port}/"
    print(f"Swing Guru app: {address}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
