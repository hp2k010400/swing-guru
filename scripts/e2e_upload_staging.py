"""End-to-end upload test against the STAGING server, reproducing the Build 16 failure.

    .venv\\Scripts\\python.exe scripts\\e2e_upload_staging.py <video> [<video> ...]

For each video: create a job, upload it slowly in chunks while calling /start mid-upload (what
Build 16 did), and require that the server refuses to start until every byte has arrived, then
analyses the whole file to a complete swing with a playable replay. Uses a throwaway test account
(dev sign-in), never a real one, and deletes its swings afterwards. Staging only: the URL is fixed.
Prints statuses and byte counts only (no keys or signed URLs).
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://swing-guru-staging-production.up.railway.app"
KEY = (ROOT / ".tmp" / "staging_access_key.txt").read_text(encoding="utf-8").strip()
USER = f"e2e-upload-{uuid.uuid4().hex[:12]}"
HEAD = {"Authorization": f"Bearer dev:{USER}", "X-SG-Access-Key": KEY, "Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, method=method, headers=HEAD,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


SENT_ALL: dict[str, float] = {}


def slow_put(url: str, data: bytes, headers: dict, chunks: int = 8, pause: float = 0.6) -> int:
    u = urlparse(url)
    conn = http.client.HTTPSConnection(u.netloc, timeout=120)
    conn.putrequest("PUT", f"{u.path}?{u.query}")
    for k, v in {**headers, "Content-Length": str(len(data))}.items():
        conn.putheader(k, v)
    conn.endheaders()
    step = len(data) // chunks + 1
    for i in range(0, len(data), step):
        if i + step >= len(data):
            SENT_ALL[url] = time.monotonic()          # the moment the last byte leaves (before the server has it)
        conn.send(data[i:i + step])
        time.sleep(pause)
    status = conn.getresponse().status
    conn.close()
    return status


def run(video: Path) -> bool:
    data = video.read_bytes()
    ctype = "video/quicktime" if video.suffix.lower() == ".mov" else "video/mp4"
    status, created = call("POST", "/v1/analysis/jobs", {"file_name": video.name, "content_type": ctype, "size_bytes": len(data)})
    assert status == 201, (status, created)
    job = created["job"]["id"]
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault("put", slow_put(created["upload"]["url"], data, created["upload"]["headers"])))
    t.start()
    mid = []
    while t.is_alive():                                   # Build 16 behaviour: /start while uploading
        s, body = call("POST", f"/v1/analysis/jobs/{job}/start")
        mid.append((s, (body.get("error") or {}).get("code") or body.get("status"), time.monotonic()))
        time.sleep(0.5)
    t.join()
    # Early = accepted before the last byte had even been sent (a /start after that is legitimate).
    done_at = SENT_ALL.get(created["upload"]["url"], float("inf"))
    started_mid = [m for m in mid if m[0] == 200 and m[2] < done_at]
    print("  /start during upload:", [(m[0], m[1]) for m in mid])
    s, body = call("POST", f"/v1/analysis/jobs/{job}/start")
    for _ in range(120):
        s, body = call("GET", f"/v1/analysis/jobs/{job}")
        if body.get("status") in ("complete", "failed"):
            break
        time.sleep(3)
    ok = body.get("status") == "complete"
    replay = None
    if ok:
        s, detail = call("GET", f"/v1/swings/{job}")
        r = urllib.request.Request(detail["replay"]["video_url"], headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            replay = resp.status
        ok = replay in (200, 206) and len(detail["checkpoints"]) > 0
        call("DELETE", f"/v1/swings/{job}")
    print(f"{video.name}: {len(data)} bytes | PUT {result.get('put')} | mid-upload /start calls {len(mid)}, "
          f"accepted early {len(started_mid)} | final {body.get('status')} {(body.get('error') or {}).get('code') or ''} | "
          f"replay {replay} -> {'PASS' if ok and not started_mid else 'FAIL'}")
    return ok and not started_mid


if __name__ == "__main__":
    results = [run(Path(v)) for v in sys.argv[1:]]
    print(f"{sum(results)}/{len(results)} passed")
    raise SystemExit(0 if all(results) else 1)
