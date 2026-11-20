"""Head movement at Impact split into vertical (drop / rise) and lateral (toward / away from the ball).

    .venv\\Scripts\\python.exe scripts\\head_component_study.py

Read-only, from the candidate engine's head_direction blocks in the validation bundles. Compares tour /
elite clips with the rest, and shows how a lateral-first rule would classify them, beside today's
total-movement rule. Changes no score; writes docs/agent-work/analysis-v2/HEAD_COMPONENTS.md.
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOUR = {"P04", "P05", "P06", "P09", "P14", "P15", "P16", "P17", "P22", "P23", "P24", "P28", "P29", "P39", "P43",
        "P44", "P45", "P46", "P47", "N02", "N03"}


def rows() -> list[dict]:
    out = []
    for tag, root in (("P", "validation_prod"), ("N", "validation_new"), ("R", "validation_1_0_1")):
        for p in sorted((ROOT / "var/qa" / root / "candidate").glob("*/analysis.json")):
            a = json.loads(p.read_text(encoding="utf-8"))
            for ph in a["phases"]:
                if ph["phase"] != "impact":
                    continue
                for c in ph["checks"]:
                    hd = c.get("head_direction")
                    if c.get("kind") == "head" and c.get("guided") and hd:
                        out.append({"swing": tag + p.parent.name, "tour": tag + p.parent.name in TOUR,
                                    "total": hd["total_pct"], "down": hd["down_pct"], "ball": hd.get("toward_ball_pct"),
                                    "status": (c.get("verdict") or {}).get("status")})
    return out


def pct(values, q):
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1)))] if v else None


def main() -> int:
    data = rows()
    lines = ["# Head movement at Impact: vertical vs lateral (study only, no score change)\n",
             f"{len(data)} swings with a measured, direction-split head check "
             f"({sum(r['tour'] for r in data)} tour / elite). Values in % of body height; "
             "down = positive, toward the ball = positive.\n",
             "| Group | Total median (p90) | Drop median (p90) | Lateral, either way: median (p90) | Today in range |",
             "|---|---|---|---|---|"]
    for name, pick in (("Tour / elite", lambda r: r["tour"]), ("Others", lambda r: not r["tour"])):
        g = [r for r in data if pick(r)]
        lat = [abs(r["ball"]) for r in g if r["ball"] is not None]
        down = [r["down"] for r in g]
        tot = [r["total"] for r in g]
        good = sum(r["status"] == "good" for r in g)
        lines.append(f"| {name} ({len(g)}) | {st.median(tot):.1f} ({pct(tot, .9):.1f}) | {st.median(down):.1f} ({pct(down, .9):.1f}) | "
                     f"{st.median(lat):.1f} ({pct(lat, .9):.1f}) | {good}/{len(g)} |")
    # An illustrative lateral-first rule (NOT implemented): lateral bands like today's total bands,
    # rising penalised like lateral, a drop allowed up to DROP_OK before it counts.
    DROP_OK = 6.0
    lines.append(f"\n**Illustration only:** a lateral-first rule. Judge the larger of the lateral movement, any rise, "
                 f"and any drop beyond {DROP_OK:g}%, against today's bands (under 4% good, 4–8% slight, 8%+ clear):\n")
    lines.append("| Group | Today: good / slight / clear | Lateral-first: good / slight / clear |\n|---|---|---|")
    for name, pick in (("Tour / elite", lambda r: r["tour"]), ("Others", lambda r: not r["tour"])):
        g = [r for r in data if pick(r)]
        def band(x):
            return "good" if x < 4 else "attention" if x < 8 else "problem"
        today = [band(r["total"]) for r in g]
        alt = [band(max(abs(r["ball"] or 0), max(0.0, -r["down"]), max(0.0, r["down"] - DROP_OK))) for r in g]
        c = lambda xs: f"{xs.count('good')} / {xs.count('attention')} / {xs.count('problem')}"
        lines.append(f"| {name} ({len(g)}) | {c(today)} | {c(alt)} |")
    text = "\n".join(lines) + "\n"
    (ROOT / "docs/agent-work/analysis-v2/HEAD_COMPONENTS.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
