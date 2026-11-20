"""Add / refresh golfer-facing verdicts in existing analysis bundles (no CV re-run).

    .venv\\Scripts\\python.exe scripts\\refresh_verdicts.py var\\qa\\test_swings var\\analysis\\real

Verdicts are a presentation layer (``swingguru.verdicts``) over checks the bundle
already contains; refreshing them never changes a measurement. Each argument is
a bundle folder (with ``analysis.json``) or a folder searched for bundles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.verdicts import attach_verdicts  # noqa: E402


def refresh(analysis_path: Path) -> dict:
    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    attach_verdicts(analysis)
    analysis_path.write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    return analysis


def main(argv: list[str] | None = None) -> int:
    targets = [Path(a) for a in (argv if argv is not None else sys.argv[1:])] or [ROOT / "var" / "qa" / "test_swings"]
    paths: list[Path] = []
    for target in targets:
        paths += [target / "analysis.json"] if (target / "analysis.json").exists() else sorted(target.glob("**/bundle/analysis.json"))
    for path in paths:
        analysis = refresh(path)
        shown = [c["verdict"] for p in analysis["phases"] for c in p["checks"]]
        counts = {s: sum(1 for v in shown if v["status"] == s) for s in ("good", "attention", "problem", "info")}
        print(path.parent.parent.name if path.parent.name == "bundle" else path.parent.name, counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
