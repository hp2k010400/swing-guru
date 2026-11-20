"""A simulated league season through the REAL league service (rules, rivals, store, weekly reset).

    .venv\\Scripts\\python.exe scripts\\league_season.py [--players 60] [--weeks 12] [--seed 7]

Unlike scripts/league_sim.py (the design study), this drives swingguru.league with fake swing histories and
time travel: players join (5 counted swings needed), practise with persona habits, the Monday reset runs,
and promotion / relegation happen exactly as on staging. Uses a temporary database; touches nothing else.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swingguru.league import League, LeagueStore, rules  # noqa: E402
from swingguru.scoring.config import CHECKS  # noqa: E402
from swingguru.swing_history import fingerprint  # noqa: E402

MON = datetime(2026, 11, 16, tzinfo=timezone.utc)
KINDS = {"casual": (7.0, 1.5, 2, 0.0), "regular": (7.6, 3.5, 3, 0.01), "improver": (6.9, 4.0, 3, 0.07),
         "grinder": (7.4, 6.5, 10, 0.005)}


def record(score: float, rnd: random.Random) -> dict:
    checks = []
    for c in CHECKS:
        p = min(0.95, max(0.15, 0.45 + 0.12 * (score - 7.0)))
        checks.append({"id": c.id, "status": "good" if rnd.random() < p else "attention",
                       "value": round(rnd.uniform(0, 60), 2), "unit": c.unit})
    return {"version": 2, "score": {"total": score, "measured": 8}, "video": {"long_side": 1920}, "checks": checks,
            "fingerprint": fingerprint(score, checks)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", type=int, default=60)
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    people = []
    for i in range(a.players):
        kind = list(KINDS)[i % 4]
        skill, days, per, trend = KINDS[kind]
        people.append({"id": f"user-{kind}-{i:03d}", "kind": kind, "skill": rnd.gauss(skill, 0.2), "days": days,
                       "per": per, "trend": trend})
    swings: dict[str, list[dict]] = {}

    def practise(week: datetime) -> None:
        for p in people:
            if rnd.random() < 0.08:
                continue
            for d in sorted(rnd.sample(range(7), min(7, max(0, round(rnd.gauss(p["days"], 0.8)))))):
                for k in range(p["per"]):
                    s = round(min(10.0, max(3.0, rnd.gauss(p["skill"], 0.7))), 1)
                    swings.setdefault(p["id"], []).append({
                        "id": f"{p['id']}:{len(swings.get(p['id'], []))}",
                        "created_at": (week + timedelta(days=d, hours=18, minutes=3 * k)).isoformat(),
                        "record": record(s, rnd)})
            p["skill"] = min(9.6, p["skill"] + p["trend"])

    with tempfile.TemporaryDirectory() as tmp:
        league = League(LeagueStore(Path(tmp) / "league.sqlite3"), lambda uid: swings.get(uid, []))
        for w in range(4, 0, -1):
            practise(MON - timedelta(days=7 * w))
        joined = []
        for p in people:
            if len(rules.counted(swings.get(p["id"], []))) >= 5:
                league.join(p["id"], f"P{p['id'][-3:]} {p['kind']}"[:20], MON + timedelta(hours=1))
                joined.append(p)
        print(f"{len(joined)} of {len(people)} players joined in week 1 (5 counted swings needed); all start in Bronze.")
        example = None
        for w in range(a.weeks):
            week = MON + timedelta(days=7 * w)
            practise(week)
            if w == 2:
                example = league.view(joined[1]["id"], week + timedelta(days=6, hours=23))
            league.ensure_week(week + timedelta(days=7, minutes=31))
        if example:
            g = example["group"]
            print(f"\nWeek 3, a {g['division']} group seen by {example['profile']['display_name']} on Sunday night "
                  f"(top {g['promote']} promote, bottom {g['relegate']} relegate; {g['rival_count']} practice rivals):")
            for m in g["members"]:
                tag = "  [Practice rival]" if m["is_rival"] else ""
                you = "  <- you" if m["is_you"] else ""
                print(f"  {m['position']:2d}. {m['name']:26s} {m['lp']:4d} LP  {m['zone']}{tag}{you}")
        profiles = {p["user_id"]: p["division"] for p in league.store.profiles()}
        print(f"\nAfter {a.weeks} weeks:")
        for kind in KINDS:
            group = [p for p in joined if p["kind"] == kind]
            dist = Counter(profiles[p["id"]] for p in group)
            lps = [r["final_lp"] for p in group for r in league.store.results(p["id"], 100)]
            uploads = statistics.mean(len(swings.get(p["id"], [])) for p in group) / (a.weeks + 4)
            print(f"  {kind:9s} ({len(group):2d}, ~{uploads:.0f} uploads/wk): "
                  + ", ".join(f"{d} {dist[d]}" for d in rules.DIVISIONS if dist[d])
                  + f" | weekly LP median {statistics.median(lps):.0f}, p90 {sorted(lps)[int(0.9 * (len(lps) - 1))]}")
        rivals = league.store.rivals()
        print(f"  practice rivals created (persistent roster): {len(rivals)}; by division now: "
              + ", ".join(f"{d} {sum(r['division'] == d for r in rivals)}" for d in rules.DIVISIONS))
        reg = [r["final_lp"] for p in joined if p["kind"] == "regular" for r in league.store.results(p["id"], 100) if r["final_lp"]]
        grind = [r["final_lp"] for p in joined if p["kind"] == "grinder" for r in league.store.results(p["id"], 100) if r["final_lp"]]
        print(f"\nFarming check (active weeks): regular median {statistics.median(reg):.0f} LP, "
              f"grinder median {statistics.median(grind):.0f} LP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
