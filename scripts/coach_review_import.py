"""Import a coach-review batch into the review database.

    .venv\\Scripts\\python.exe scripts\\coach_review_import.py --batch test-swings-2026-09-29 \\
        --from-test-swings var\\qa\\test_swings
    .venv\\Scripts\\python.exe scripts\\coach_review_import.py --batch pilot --manifest var\\coach_review\\pilot.json

The manifest is a JSON list (or ``{"items": [...]}``) or a CSV. Each item needs
``swing_id``, ``video`` (local clean video) and ``analysis`` (the stored analysis
JSON); ``club``, ``golfer_id`` and ``similar_swing_group`` are optional. A stored
``swing_score`` inside the analysis is snapshotted as it is. Nothing is
recomputed: an analysis without a stored score stays "score unavailable".

Whole-batch validation: every entry is checked (duplicate ids, missing or
unreadable media, malformed score data) before anything is written, so a bad
manifest never leaves a partial batch behind.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.coach_review import config as C  # noqa: E402
from swingguru.coach_review import store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", required=True, help="batch name (unique in the database)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", help="JSON or CSV manifest of swings")
    source.add_argument("--from-test-swings", help="build the manifest from <dir>/<swing>/bundle/analysis.json")
    parser.add_argument("--db", default=str(C.DEFAULT_DB))
    parser.add_argument("--replace", action="store_true", help="overwrite an existing batch with the same name")
    parser.add_argument("--note", default=None, help="free-text note stored with the batch")
    parser.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    args = parser.parse_args(argv)

    try:
        if args.manifest:
            entries = store.read_manifest(args.manifest)
            base = Path(args.manifest).parent
        else:
            entries = store.scan_test_swings(args.from_test_swings)
            base = Path(args.from_test_swings)
        prepared = store.prepare_entries(entries, root=base)
    except store.ManifestError as error:
        print(f"manifest rejected: {len(error.problems)} problem(s)", file=sys.stderr)
        for problem in error.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    stored = sum(1 for row in prepared if row.get("score_stored"))
    usable = sum(1 for row in prepared if row.get("score_available"))
    print(f"{len(prepared)} swing(s) validated: {stored} with a stored score snapshot "
          f"({usable} of them usable), {len(prepared) - stored} with none (kept unavailable, never recomputed)")
    if args.dry_run:
        for row in prepared:
            state = "usable" if row.get("score_available") else ("unavailable" if row.get("score_stored") else "no snapshot")
            version = row.get("model_version") or "-"
            print(f"  {row['swing_id']}: club={row.get('club') or '-'} model={version} score={state} "
                  f"video={Path(row['video_path']).name}")
        print("dry run: nothing written")
        return 0

    conn = store.connect(args.db)
    try:
        store.init_db(conn)
        summary = store.import_batch(conn, args.batch, prepared, replace=args.replace, note=args.note)
    except store.StoreError as error:
        print(f"import failed: {error}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    print(f"batch {summary['batch']!r}: {summary['items']} item(s) "
          f"({summary['scores_stored']} with a stored snapshot, {summary['scores_available']} usable, "
          f"{summary['scores_unavailable']} without a usable score)"
          + (" - replaced the previous batch" if summary["replaced"] else ""))
    print(f"database: {args.db}")
    print("next: .venv\\Scripts\\python.exe scripts\\coach_review_serve.py --batch "
          f"{args.batch} --reviewer-id coach1 --reviewer-name \"Coach One\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
