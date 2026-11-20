"""Extract stage timings from build_analysis runs into one durable summary.

    .venv\\Scripts\\python.exe scripts\\summarise_analysis_runs.py \\
        --out docs\\agent-work\\performance\\timings.json \\
        before-cold=var\\bench\\final-before-cold.log \\
        after-cold=var\\bench\\final-after-cold.log

Each argument is ``name=path``. The path may be a build log (the JSON summary
the script prints is found by its ``"shown"`` key) or an ``analysis.json``.
Logs matter because a warm rerun overwrites ``analysis.json`` in the same
directory, so the log is the durable record of the cold run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def extract(path: Path) -> dict:
    """The JSON summary a build run printed, or an analysis payload."""

    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = text.find('{\n  "shown"')
    if marker < 0:
        raise SystemExit(f"no analysis summary found in {path}")
    # Logs can carry engine chatter after the summary, so decode one value.
    payload, _ = json.JSONDecoder().raw_decode(text, marker)
    return payload


def summarise(path: Path) -> dict:
    payload = extract(path)
    engine = payload.get("engine") or {}
    return {
        "source": str(path),
        "pose": engine.get("pose"),
        "pose_cache": engine.get("pose_cache"),
        "club_tracker": engine.get("club_tracker"),
        "club_mode": (engine.get("club_windows") or {}).get("mode"),
        "window_radius_frames": (engine.get("club_windows") or {}).get("radius_frames"),
        "timings_s": engine.get("timings"),
        "counters": engine.get("counters"),
        "candidate_cache": (engine.get("club_windows") or {}).get("candidate_cache"),
        "club_fallback": [
            {
                "phase": entry.get("phase"),
                "frame": entry.get("frame"),
                "level": entry.get("level"),
                "angle": entry.get("angle"),
                "cached": entry.get("cached"),
            }
            for entry in (engine.get("club_fallback") or [])
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", help="name=path pairs")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    summary = {}
    for item in args.runs:
        if "=" not in item:
            parser.error(f"expected name=path, got {item!r}")
        name, raw_path = item.split("=", 1)
        summary[name] = summarise(Path(raw_path))

    rendered = json.dumps(summary, indent=2)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
