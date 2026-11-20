"""Audit today's rank rules by simulation (read-only): how fast do different users climb?

    .venv\\Scripts\\python.exe scripts\\rank_audit.py

Score distributions come from the validation swings (tour / elite clips vs the rest). Each scenario
feeds synthetic but realistic swing records (distinct, so duplicates don't interfere) through
swingguru.ranks.compute with today's DEFAULT_CONFIG and reports the week each tier is first reached.
"""

from __future__ import annotations

import json
import random
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from swingguru.ranks import DEFAULT_CONFIG, compute  # noqa: E402
from swingguru.swing_history import record  # noqa: E402

TOUR = {"P04", "P05", "P06", "P09", "P14", "P15", "P16", "P17", "P22", "P23", "P24", "P28", "P29", "P39", "P43",
        "P44", "P45", "P46", "P47", "N02", "N03"}


def load():
    tour, others = [], []
    for tag, root in (("P", "validation_prod"), ("N", "validation_new"), ("R", "validation_1_0_1")):
        for p in sorted((ROOT / "var/qa" / root / "candidate").glob("*/analysis.json")):
            rec = record(json.loads(p.read_text(encoding="utf-8")))
            if rec["score"]["total"] is not None:
                (tour if tag + p.parent.name in TOUR else others).append(rec)
    return tour, others


def simulate(pool, per_day, days, seed=1):
    rnd = random.Random(seed)
    start = datetime(2026, 1, 5, 9)
    swings = []
    for d in range(days):
        for k in range(per_day):
            base = rnd.choice(pool)
            rec = json.loads(json.dumps(base))
            # make each swing distinct (a real new swing), keep its score and verdicts
            for c in rec["checks"]:
                c["value"] = round(c["value"] + rnd.uniform(-0.6, 0.6), 2)
            rec["fingerprint"] = f"sim-{d}-{k}-{rnd.random()}"
            swings.append({"id": f"{d}-{k}", "created_at": (start + timedelta(days=d, minutes=10 * k)).isoformat(),
                           "club": "7 Iron", "record": rec})
    out = compute(swings)
    first = {}
    for t in out["timeline"]:
        tier = t["rank"].split()[0]
        if tier not in first:
            first[tier] = (datetime.fromisoformat(t["date"]) - start).days // 7 + 1
    return out, first


def main() -> int:
    tour, others = load()
    q = lambda v, p: sorted(v)[int(p * (len(v) - 1))]
    ts, os_ = [r["score"]["total"] for r in tour], [r["score"]["total"] for r in others]
    print(f"Scores - tour/elite ({len(ts)}): median {st.median(ts):.1f}, p25 {q(ts, .25):.1f}, p75 {q(ts, .75):.1f} | "
          f"others ({len(os_)}): median {st.median(os_):.1f}, p25 {q(os_, .25):.1f}, p75 {q(os_, .75):.1f}, p90 {q(os_, .9):.1f}")
    print("Config:", {k: DEFAULT_CONFIG[k] for k in ("points", "daily_cap", "tier_rating", "rating_window")},
          "Diamond I needs", DEFAULT_CONFIG["division_rp"][-1], "RP")
    for name, pool, per_day in (("Typical golfer, 1 swing/day", others, 1), ("Typical golfer, 5 swings/day (volume)", others, 5),
                                ("Tour clips uploaded, 5/day", tour, 5), ("Tour clips uploaded, 1/day", tour, 1)):
        out, first = simulate(pool, per_day, 26 * 7)
        print(f"- {name}: after 26 weeks {out['rank']['name']} (RP {out['rp']}, rating {out['rating']}); first week reaching "
              f"each tier: {first}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
