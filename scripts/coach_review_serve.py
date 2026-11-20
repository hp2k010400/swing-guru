"""Serve the blind coach-review UI on loopback only.

    .venv\\Scripts\\python.exe scripts\\coach_review_serve.py --batch pilot \\
        --reviewer-id coach1 --reviewer-name "Coach One"

One launch fixes one reviewer and one batch. The reviewer identity never comes
from the request body or query string. Start a second server (different port)
for a second reviewer; both write to the same database.
"""

from __future__ import annotations

import argparse
import secrets
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.coach_review import config as C  # noqa: E402
from swingguru.coach_review import store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--reviewer-id", required=True, help="short id stored with every rating")
    parser.add_argument("--reviewer-name", default=None, help="display name (defaults to the id)")
    parser.add_argument("--db", default=str(C.DEFAULT_DB))
    parser.add_argument("--port", type=int, default=C.DEFAULT_PORT)
    parser.add_argument("--token", default=None, help="request token (generated per process by default)")
    parser.add_argument("--edit-mode", action="store_true",
                        help="allow editing after submission (each edit is a new, clearly post-reveal revision)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    reviewer_id = args.reviewer_id.strip()
    if not reviewer_id or len(reviewer_id) > C.MAX_REVIEWER_ID:
        print(f"reviewer id must be 1-{C.MAX_REVIEWER_ID} characters", file=sys.stderr)
        return 2
    reviewer_name = (args.reviewer_name or reviewer_id).strip()[:C.MAX_REVIEWER_NAME]
    token = args.token or secrets.token_urlsafe(24)

    conn = store.connect(args.db)
    try:
        store.init_db(conn)
        batch = store.get_batch(conn, args.batch)
        if batch is None:
            names = ", ".join(row["name"] for row in store.list_batches(conn)) or "none imported yet"
            print(f"unknown batch {args.batch!r} in {args.db}. Imported batches: {names}", file=sys.stderr)
            return 2
        store.ensure_order(conn, batch["id"], reviewer_id)
        store.remember_reviewer(conn, args.batch, reviewer_id, reviewer_name)
        total = batch["item_count"]
        drift = store.video_integrity_problems(conn, args.batch)
    finally:
        conn.close()

    if drift:
        print(f"{len(drift)} video file(s) changed since this batch was imported. The score snapshot belongs to the "
              "imported bytes, so the review will not start:", file=sys.stderr)
        for problem in drift[:10]:
            print(f"  - {problem}", file=sys.stderr)
        print("  Re-import the batch from the current files (a new batch name is required if it already has reviews).",
              file=sys.stderr)
        return 3

    import uvicorn  # imported late so --help stays fast

    from swingguru.coach_review.api import create_app

    app = create_app(db_path=args.db, batch=args.batch, reviewer_id=reviewer_id, reviewer_name=reviewer_name,
                     token=token, edit_mode=args.edit_mode)
    address = f"http://{C.DEFAULT_HOST}:{args.port}/"
    print(f"Swing Guru coach review - batch {args.batch!r}, reviewer {reviewer_name!r}, {total} swing(s)")
    print(f"  open: {address}")
    print(f"  edit mode: {'on' if args.edit_mode else 'off'}")
    print(f"  database: {args.db}")
    print("  Blindness is enforced by this server. Anyone who can read the files on this computer can still "
          "open the analysis files and see a score, so keep this to trusted operators.")
    print("  Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        uvicorn.run(app, host=C.DEFAULT_HOST, port=args.port, log_level="warning", access_log=False)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
