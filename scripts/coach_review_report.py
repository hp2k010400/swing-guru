"""Generate the coach-review validation report (evidence only).

    .venv\\Scripts\\python.exe scripts\\coach_review_report.py
    .venv\\Scripts\\python.exe scripts\\coach_review_report.py --batch pilot

Writes Markdown to ``docs/agent-work/swing-score/VALIDATION_REPORT.md`` and the
machine-readable payload to ``var/coach_review/report/validation_report.json``.
Nothing is rescored: the report reads the stored score snapshots and the logged
blind ratings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.coach_review import config as C  # noqa: E402
from swingguru.coach_review import report as report_module  # noqa: E402
from swingguru.coach_review import store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(C.DEFAULT_DB))
    parser.add_argument("--batch", default=None, help="restrict the report to one batch")
    parser.add_argument("--out", default=str(C.DEFAULT_REPORT))
    parser.add_argument("--json-out", default=str(C.DEFAULT_REPORT_JSON))
    parser.add_argument("--no-json", action="store_true")
    parser.add_argument("--print", dest="print_markdown", action="store_true", help="also print the Markdown")
    args = parser.parse_args(argv)

    if not Path(args.db).is_file():
        print(f"no review database at {args.db} - import a batch first "
              "(scripts/coach_review_import.py)", file=sys.stderr)
        return 2

    conn = store.connect(args.db)
    try:
        store.init_db(conn)
        try:
            result = report_module.write_report(conn, out=args.out,
                                                json_out=None if args.no_json else args.json_out,
                                                batch_name=args.batch)
        except store.StoreError as error:
            print(str(error), file=sys.stderr)
            return 2
    finally:
        conn.close()

    payload = result["payload"]
    counts, summary = payload["counts"], payload["summary"]
    print(f"swings {counts['swings']} (stored snapshot {counts['swings_with_stored_score']}, usable score "
          f"{counts['swings_with_available_score']}), reviews {counts['reviews_submitted']}, comparable pairs "
          f"{summary['scored_swings']}")
    print(f"  report: {args.out}")
    if not args.no_json:
        print(f"  detail: {args.json_out}")
    if not summary["meets_planned_minimum"]:
        print(f"  below the planned exploratory minimum: {summary['scored_swings']} of "
              f"{summary['planned_minimum']} rated+scored swings - exploratory only")
    if args.print_markdown:
        print(result["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
