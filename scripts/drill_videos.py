"""Manage the drill video catalogue (src/swingguru/drill_videos.json, or SG_DRILL_VIDEOS_PATH).

    .venv\\Scripts\\python.exe scripts\\drill_videos.py list
    .venv\\Scripts\\python.exe scripts\\drill_videos.py check [--apply]          # re-verify every video; disable broken ones
    .venv\\Scripts\\python.exe scripts\\drill_videos.py import rows.csv [--apply] # bulk add / update / replace
    .venv\\Scripts\\python.exe scripts\\drill_videos.py disable <drill-id> [--reason "..."] [--apply]
    .venv\\Scripts\\python.exe scripts\\drill_videos.py enable <drill-id> <video-id-or-url> [--apply]

Import rows (CSV header or JSON objects): drill_id, video_id (or url), start_s, status (active / pending_review /
disabled), suitability, match, optional title / creator. Every row is checked against YouTube (exists, public,
embedding allowed) before it is written. Without --apply nothing is written (a dry run). Only official embeds
are recorded; nothing is downloaded. Written drill instructions (app_drills.py) are never changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swingguru.drill_videos import catalogue_path  # noqa: E402
from swingguru.drill_videos_admin import (  # noqa: E402
    check_all, import_rows, read_rows, save, set_status, video_id, youtube_probe,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("--apply", action="store_true")
    i = sub.add_parser("import")
    i.add_argument("file", type=Path)
    i.add_argument("--apply", action="store_true")
    d = sub.add_parser("disable")
    d.add_argument("drill")
    d.add_argument("--reason")
    d.add_argument("--apply", action="store_true")
    e = sub.add_parser("enable")
    e.add_argument("drill")
    e.add_argument("video")
    e.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    path = catalogue_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    if a.cmd == "list":
        for v in data["videos"]:
            print(f"{v['drill_id']:20s} {v['status']:15s} {v['suitability']:20s} {v['video_id']}  {v['creator']} | {v['title']}")
        return 0
    if a.cmd == "check":
        new, report = check_all(data, youtube_probe)
    elif a.cmd == "import":
        new, report = import_rows(data, read_rows(a.file), youtube_probe)
    elif a.cmd == "disable":
        new, report = set_status(data, a.drill, "disabled", reason=a.reason)
    else:
        new, report = set_status(data, a.drill, "active", video=video_id(a.video))
    print("\n".join(report))
    if not a.apply:
        print("(dry run: nothing written; add --apply)")
        return 0
    problems = save(new, path)
    if problems:
        print("NOT WRITTEN:\n  " + "\n  ".join(problems))
        return 1
    print(f"written {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
