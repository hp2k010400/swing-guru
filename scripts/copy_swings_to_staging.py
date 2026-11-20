"""Copy ONE production account's analysed swings to a staging test account (read-only on production).

    .venv\\Scripts\\python.exe scripts\\copy_swings_to_staging.py <production user id> <staging test account id>

Environment (never printed): SB_URL, SB_KEY (production service key), SB_BUCKET. The staging import key
is read from .tmp/staging_import_key.txt. Production is only read: rows of that one user with
status 'complete', and their analysis, replay video and thumbnail. Prints counts only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = "https://swing-guru-staging-production.up.railway.app"
COLUMNS = ("id", "club", "notice", "captured_at", "created_at", "finished_at", "analysis_version", "summary",
           "file_name", "result_key", "video_key", "thumb_key")


def main(prod_user: str, test_user: str) -> int:
    url, key, bucket = os.environ["SB_URL"], os.environ["SB_KEY"], os.environ["SB_BUCKET"]
    import_key = (ROOT / ".tmp" / "staging_import_key.txt").read_text(encoding="utf-8").strip()
    auth = {"apikey": key, "Authorization": f"Bearer {key}"}

    def fetch(path: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(url + path, headers=auth), timeout=120) as r:
            return r.read()

    query = urllib.parse.urlencode({"select": ",".join(COLUMNS), "user_id": f"eq.{prod_user}",
                                    "status": "eq.complete", "order": "created_at.asc"})
    rows = json.loads(fetch(f"/rest/v1/swings?{query}"))
    done = {"imported": 0, "exists": 0, "skipped": 0}
    for row in rows:
        if not row.get("result_key") or not row.get("video_key"):
            done["skipped"] += 1
            continue
        files = {}
        for name, col in (("analysis", "result_key"), ("video", "video_key"), ("thumb", "thumb_key")):
            if row.get(col):
                files[name] = base64.b64encode(fetch(f"/storage/v1/object/{bucket}/{row[col]}")).decode()
        swing = {k: row.get(k) for k in COLUMNS if not k.endswith("_key")}
        payload = json.dumps({"user_id": test_user, "swing": swing, "files": files}).encode()
        request = urllib.request.Request(f"{STAGING}/v1/admin/import", data=payload, method="POST",
                                         headers={"Content-Type": "application/json", "X-SG-Import-Key": import_key})
        with urllib.request.urlopen(request, timeout=300) as r:
            done[json.loads(r.read())["status"]] += 1
        print(f"{sum(done.values())}/{len(rows)}", end="\r", flush=True)
    print(f"\ncomplete swings for that account: {len(rows)}; {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:3]))
