"""Local server for the human review pack.

    .venv\\Scripts\\python.exe scripts\\serve_review.py

Serves ``var/`` on http://127.0.0.1:8770/ (with byte ranges, so videos seek)
and stores review answers in ``var/qa/human_review/reviews.json``:

* ``GET  /qa/human_review/api/reviews`` - all saved answers;
* ``POST /qa/human_review/api/reviews`` - ``{"slug": ..., "review": {...}}``.

Nothing here touches the engine or its outputs.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / "var"
REVIEWS = VAR / "qa" / "human_review" / "reviews.json"
API = "/qa/human_review/api/reviews"
PORT = 8770
_lock = threading.Lock()


def read_reviews() -> dict:
    try:
        return json.loads(REVIEWS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(VAR), **kwargs)

    def log_message(self, *args) -> None:  # quiet console
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def _json(self, payload, status=200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == API:
            return self._json(read_reviews())
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

    def do_POST(self) -> None:
        if self.path.split("?")[0] != API:
            return self._json({"error": "not found"}, 404)
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            slug, review = str(payload["slug"]), dict(payload["review"])
        except (ValueError, KeyError, TypeError):
            return self._json({"error": "expected {slug, review}"}, 400)
        with _lock:
            reviews = read_reviews()
            reviews[slug] = review
            tmp = REVIEWS.with_suffix(".tmp")
            tmp.write_text(json.dumps(reviews, indent=2), encoding="utf-8")
            os.replace(tmp, REVIEWS)
        return self._json({"saved": slug})


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    address = f"http://127.0.0.1:{PORT}/qa/human_review/index.html"
    print(f"Swing Guru review: {address}\nAnswers are saved to {REVIEWS}\nPress Ctrl+C to stop.", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
