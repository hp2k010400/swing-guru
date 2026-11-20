"""End-to-end smoke test of a running analysis service, exactly as the phone uses it.

    # local dev service (python -m swingguru.service with defaults):
    .venv\\Scripts\\python.exe scripts\\smoke_service.py --api http://127.0.0.1:8790 --video swing.mp4

    # production: signs in anonymously through Supabase Auth first
    .venv\\Scripts\\python.exe scripts\\smoke_service.py --api https://<service> --video swing.mp4 ^
        --supabase-url https://<project>.supabase.co --anon-key <public anon key>

Creates a job, uploads the video to the returned signed URL, starts it, polls
until it ends, fetches the Summary, checks the replay URLs, then deletes the
swing (unless --keep). Exits non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--video", required=True)
    parser.add_argument("--token", default=None, help="bearer token (default: a fresh dev token)")
    parser.add_argument("--supabase-url", default=None)
    parser.add_argument("--anon-key", default=None)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--club", default=None, help="club stored with the job before analysis")
    parser.add_argument("--token-out", default=None, help="write the bearer token here (to benchmark the replay later)")
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")
    video = Path(args.video)

    token = args.token
    if args.supabase_url:
        signup = httpx.post(f"{args.supabase_url.rstrip('/')}/auth/v1/signup", json={},
                            headers={"apikey": args.anon_key, "Content-Type": "application/json"}, timeout=30)
        signup.raise_for_status()
        token = signup.json()["access_token"]
        print("signed in anonymously as", signup.json()["user"]["id"])
    token = token or f"dev:smoke-{uuid.uuid4().hex[:12]}"
    if args.token_out:
        Path(args.token_out).write_text(token, encoding="utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    http = httpx.Client(timeout=120)

    print("health:", http.get(f"{api}/health").json(), "ready:", http.get(f"{api}/ready").json())
    content_type = "video/quicktime" if video.suffix.lower() == ".mov" else "video/mp4"
    created = http.post(f"{api}/v1/analysis/jobs", headers=headers, json={
        "file_name": video.name, "content_type": content_type, "size_bytes": video.stat().st_size, "club": args.club})
    created.raise_for_status()
    job, upload = created.json()["job"], created.json()["upload"]
    began = time.monotonic()
    with video.open("rb") as handle:
        put = http.request(upload["method"], upload["url"], content=handle.read(), headers=upload["headers"])
    put.raise_for_status()
    print(f"uploaded {video.stat().st_size / 1e6:.1f} MB in {time.monotonic() - began:.1f}s")
    http.post(f"{api}/v1/analysis/jobs/{job['id']}/start", headers=headers).raise_for_status()
    last = None
    while time.monotonic() - began < args.timeout:
        status = http.get(f"{api}/v1/analysis/jobs/{job['id']}", headers=headers).json()
        stages = " ".join(f"{s['id']}:{s['state']}" for s in status["stages"])
        if (status["status"], stages) != last:
            print(f"{time.monotonic() - began:6.1f}s {status['status']:<11} {stages}")
            last = (status["status"], stages)
        if status["status"] in ("complete", "failed"):
            break
        time.sleep(2)
    if status["status"] != "complete":
        print("FAILED:", json.dumps(status.get("error")))
        return 1
    detail = http.get(f"{api}/v1/swings/{job['id']}", headers=headers)
    detail.raise_for_status()
    d = detail.json()
    print("club:", d["club"])
    print("summary:", d["summary_text"])
    print("headline:", d["headline"], "| score available:", d["score"]["available"], "| focus:", d["focus"]["source"])
    assert http.get(d["replay"]["analysis_url"]).status_code == 200, "analysis URL"
    assert http.get(d["replay"]["video_url"]).status_code == 200, "video URL"
    other = http.get(f"{api}/v1/swings/{job['id']}", headers={"Authorization": f"Bearer dev:other-{uuid.uuid4().hex[:8]}"})
    print("another user gets:", other.status_code)
    if not args.keep:
        http.delete(f"{api}/v1/swings/{job['id']}", headers=headers).raise_for_status()
        print("deleted the swing")
    print(f"OK in {time.monotonic() - began:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
