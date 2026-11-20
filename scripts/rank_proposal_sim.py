"""Simulate the PROPOSED rank rules beside today's (design study only; the app is unchanged).

    .venv\\Scripts\\python.exe scripts\\rank_proposal_sim.py

Proposed rules (see docs/agent-work/release-1.0.1/PROGRESSION_AND_LEAGUES.md):
* points by practice, not volume: first counted swing of a day 10, the next two 5 each, none after;
  +25 personal best (at most one a week); +15 a check back in range; +30 for a week with 3+ practice days;
* rating = 30th percentile of the last 20 verified swings (most swings must be good), needs 10 swings;
* tier promotion only at the weekly check, and only if the gate was met at two consecutive weekly checks;
* tenure: Platinum needs 6 weeks and 40 verified swings; Diamond 10 weeks and 80;
* gates (on the 30th percentile): Silver 7.0, Gold 7.8, Platinum 8.4, Diamond 8.9; divisions by points.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from rank_audit import load  # noqa: E402

TIERS = ["Bronze", "Silver", "Gold", "Platinum", "Diamond"]
GATES = {"Bronze": 0.0, "Silver": 7.0, "Gold": 7.8, "Platinum": 8.4, "Diamond": 8.9}
TENURE = {"Platinum": (6, 40), "Diamond": (10, 80)}
DIV_RP = [0, 60, 140, 250, 380, 520, 700, 900, 1120, 1400, 1700, 2050, 2450, 2900, 3400]


def p30(values):
    v = sorted(values)
    return v[int(0.3 * (len(v) - 1))]


def simulate(pool, per_day, days, seed=1):
    rnd = random.Random(seed)
    rp, scores, best, rank, history = 0, [], None, 0, []
    met_last_week: dict[str, bool] = {}
    first = {}
    week_days = set()
    for d in range(days):
        day_scores = [rnd.choice(pool)["score"]["total"] for _ in range(per_day)]
        for k, s in enumerate(day_scores):
            rp += [10, 5, 5][k] if k < 3 else 0
            if best is not None and s >= best + 0.1 and not history[-1:] == [("pb", d // 7)]:
                rp += 25
                history.append(("pb", d // 7))
            best = s if best is None else max(best, s)
            scores.append(s)
        if day_scores:
            week_days.add(d)
        if d % 7 == 6:                                  # weekly check
            if len(week_days) >= 3:
                rp += 30
            week_days = set()
            week = d // 7 + 1
            rating = p30(scores[-20:]) if len(scores) >= 10 else None
            by_rp = max(i for i in range(15) if rp >= DIV_RP[i])
            allowed = 0
            for t_index, tier in enumerate(TIERS):
                gate_ok = GATES[tier] <= 0 or (rating is not None and rating >= GATES[tier])
                weeks_ok, swings_ok = TENURE.get(tier, (0, 0))
                tenure_ok = week >= weeks_ok and len(scores) >= swings_ok
                sustained = gate_ok and (met_last_week.get(tier, False) or GATES[tier] <= 0)
                met_last_week[tier] = gate_ok
                if sustained and tenure_ok:
                    allowed = t_index
            reached = min(by_rp, allowed * 3 + 2)
            if reached > rank:
                rank = reached
            tier = TIERS[rank // 3]
            first.setdefault(tier, week)
    return rank, rp, first


def main() -> int:
    tour, others = load()
    for name, pool, per_day in (("Typical golfer, 1 swing/day", others, 1), ("Typical golfer, 5 swings/day (volume)", others, 5),
                                ("Tour clips uploaded, 5/day", tour, 5), ("Tour clips uploaded, 1/day", tour, 1)):
        results = [simulate(pool, per_day, 26 * 7, seed) for seed in range(1, 21)]
        tiers = [TIERS[r[0] // 3] for r in results]
        print(f"- {name}: tier after 26 weeks across 20 runs: " + ", ".join(f"{t} {tiers.count(t)}" for t in TIERS if tiers.count(t)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
