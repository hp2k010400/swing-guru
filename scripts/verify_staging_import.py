"""Check every swing in one STAGING test account: detail, replay video, thumbnail, progress, comparison.

    .venv\\Scripts\\python.exe scripts\\verify_staging_import.py <staging test account id>

Read-only (GET requests only). Prints counts, never keys or signed URLs.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://swing-guru-staging-production.up.railway.app"


def main(user: str) -> int:
    key = (ROOT / ".tmp" / "staging_access_key.txt").read_text(encoding="utf-8").strip()
    head = {"Authorization": f"Bearer dev:{user}", "X-SG-Access-Key": key}

    def get(path: str) -> dict:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=head), timeout=60) as r:
            return json.loads(r.read())

    def plays(url: str) -> bool:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=0-2047"}), timeout=60) as r:
                return r.status in (200, 206) and len(r.read()) > 0
        except Exception:
            return False

    swings = get("/v1/swings")["swings"]
    detail_ok = video_ok = thumb_ok = 0
    problems = []
    for s in swings:
        try:
            d = get(f"/v1/swings/{s['id']}")
            detail_ok += 1
            if plays(d["replay"]["video_url"]) and plays(d["replay"]["analysis_url"]):
                video_ok += 1
            else:
                problems.append(f"{s['id'][:8]} replay")
            if d.get("thumb_url") and plays(d["thumb_url"]):
                thumb_ok += 1
        except Exception as error:
            problems.append(f"{s['id'][:8]} detail {type(error).__name__}")
    progress = get("/v1/progress")
    events = progress["rank"]["events"]
    order_ok = [x["created_at"] for x in progress["swings"]] == sorted(x["created_at"] for x in progress["swings"])
    scored = [x for x in progress["swings"] if x["score"] is not None]
    cmp = get(f"/v1/compare?a={scored[0]['id']}&b={scored[-1]['id']}") if len(scored) >= 2 else None
    print(f"swings listed {len(swings)} | detail ok {detail_ok} | replay video + analysis ok {video_ok} | thumbnails ok {thumb_ok}")
    print(f"progress: {len(progress['swings'])} swings, pending {progress['pending']}, chronological {order_ok}, "
          f"rank {progress['rank']['rank']['name']}, counted {progress['rank']['eligible_swings']}, "
          f"not counted: {sum(e['kind'] == 'not_counted' for e in events)} (in the last {len(events)} events)")
    if cmp:
        print(f"compare oldest vs newest: rows {len(cmp['rows'])}, counts {cmp['counts']}, score {cmp['score']['result']}, "
              f"moments a/b {sorted(cmp['moments']['a'])}/{sorted(cmp['moments']['b'])}")
    print("problems:", problems or "none")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
