"""Interpretation debug report for one analysis bundle.

    .venv\\Scripts\\python.exe scripts\\interpretation_report.py var\\analysis\\real\\analysis.json \
        --out docs\\agent-work\\interpretation\\REAL_SWING_DEBUG.md

One row per (rule, required measurement): phase, measurement, raw value,
reference, confidence, whether the rule was evaluated, its result, the reason,
evidence strength and calibration status. Recomputes interpretations from the
bundle's facts so the report always matches the current rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.interpretation import interpret  # noqa: E402
from swingguru.interpretation.rules import RULES  # noqa: E402

PHASES = {"address": "Address", "takeaway": "Takeaway", "mid_back": "Mid-Back", "top": "Top",
          "downswing": "Mid-Down", "impact": "Impact"}
UNITS = {"deg": "°", "pct_body_height": "% body height"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    report = interpret(analysis)
    facts = {(f["key"], f["phase"]): f for f in report["facts"]}
    results = {r["rule_id"]: r for r in report["results"]}

    lines = [
        f"# Interpretation debug — {analysis.get('source', '')}",
        "",
        f"Interpretation version {report['version']}; camera view `{analysis.get('camera_view')}`; "
        f"direction toward the ball known: {analysis.get('geometry_context', {}).get('toward_ball_known')}.",
        "",
        f"**Main focus:** {report['focus']['text'] if report['focus'] else report['focus_fallback']}",
        "",
        "| Phase | Measurement | Raw value | Reference | Confidence | Rule | Evaluated? | Result | Reason | Evidence | Calibration |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for rule in RULES:
        result = results[rule.rule_id]
        evaluated = "no" if result["status"] == "blocked" else "yes"
        strength = ", ".join(sorted({e.strength for e in rule.evidence})) or "-"
        outcome = result["status"] + (f" / {result['classification']}" if result["classification"] else "")
        for key, phase in rule.requires:
            fact = facts.get((key, phase))
            raw = "-" if fact is None else f"{fact['value']:+.1f}{UNITS.get(fact['unit'], '')}"
            lines.append(
                f"| {PHASES.get(phase, phase)} | {key} | {raw} | {'-' if fact is None else fact['reference']} | "
                f"{'-' if fact is None else fact['confidence']} | `{rule.rule_id}` v{rule.version} | {evaluated} | "
                f"{outcome} | {result['reason']} | {strength} | {rule.calibration} |"
            )
    lines += ["", "## Golfer-facing text produced", ""]
    for result in report["results"]:
        if result["user_text"]:
            lines.append(f"* `{result['rule_id']}` ({result['status']}): {result['user_text']}")
    lines += ["", "## All facts", "", "| Phase | Key | Value | Unit | Reference | Confidence | Source |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for fact in report["facts"]:
        lines.append(f"| {PHASES.get(fact['phase'], fact['phase'])} | {fact['key']} | {fact['value']:+.3f} | "
                     f"{fact['unit']} | {fact['reference']} | {fact['confidence']} | {fact['source']} |")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
