"""Practice-rival behaviour report, through the REAL league service (temporary database; touches nothing else).

    .venv\\Scripts\\python.exe scripts\\league_rivals_report.py [--seeds 3]

1. An example 20-player leaderboard (Thursday evening and Sunday night) as the app would show it.
2. Rival activity through the week, zero-point weeks, identical totals, and caps, over many weeks and seeds.
3. LP by rival style.
4. Growth: real golfers joining week by week (5 -> 60), and how many rivals remain in the groups.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swingguru.league import League, LeagueStore, rivals, rules  # noqa: E402
from swingguru.scoring.config import CHECKS  # noqa: E402
from swingguru.swing_history import fingerprint  # noqa: E402

MON = datetime(2026, 11, 16, tzinfo=timezone.utc)
KINDS = {"casual": (7.0, 1.5, 2), "regular": (7.6, 3.5, 3), "improver": (6.9, 4.0, 3), "grinder": (7.4, 6.5, 8)}


def record(score: float, rnd: random.Random, weak: tuple[str, ...] = ()) -> dict:
    """A simulated real golfer's swing: their weak checks are usually out of range, the rest usually in range."""

    checks = [{"id": c.id, "status": "good" if rnd.random() < (0.25 if c.id in weak else 0.93) else "attention",
               "value": round(rnd.uniform(0, 60), 2), "unit": c.unit} for c in CHECKS]
    return {"version": 2, "score": {"total": score, "measured": 8}, "video": {"long_side": 1920}, "checks": checks,
            "fingerprint": fingerprint(score, checks)}


class Players:
    def __init__(self, seed: int) -> None:
        self.rnd = random.Random(seed)
        self.swings: dict[str, list[dict]] = defaultdict(list)
        self.people: list[dict] = []

    def add_person(self, i: int) -> dict:
        kind = list(KINDS)[i % 4]
        skill, days, per = KINDS[kind]
        p = {"id": f"user-{kind}-{i:03d}", "kind": kind, "skill": self.rnd.gauss(skill, 0.2), "days": days, "per": per,
             "weak": tuple(self.rnd.sample([c.id for c in CHECKS], 2))}
        self.people.append(p)
        for w in range(1, 4):                                   # some history before joining
            self.practise(p, MON - timedelta(days=7 * w))
        return p

    def practise(self, p: dict, week: datetime) -> None:
        for d in sorted(self.rnd.sample(range(7), min(7, max(1, round(self.rnd.gauss(p["days"], 0.8)))))):
            for k in range(p["per"]):
                at = week + timedelta(days=d, hours=self.rnd.randint(7, 20), minutes=3 * k)
                self.swings[p["id"]].append({"id": f"{p['id']}:{len(self.swings[p['id']])}", "created_at": at.isoformat(),
                                             "record": record(round(min(10, max(3, self.rnd.gauss(p["skill"], 0.7))), 1),
                                                              self.rnd, p["weak"])})


def show(view: dict, title: str) -> None:
    g = view["group"]
    print(f"\n{title}: {g['division']} league, top {g['promote']} promote"
          + (f", bottom {g['relegate']} relegate" if g["relegate"] else "") + " ('o' = practice rival)")
    for m in g["members"]:
        mark = "o" if m["is_rival"] else " "
        you = "  <- you" if m["is_you"] else ""
        print(f"  {m['position']:2d}. {mark} {m['name']:22s} {m['lp']:4d} LP  {m['zone']}{you}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    a = ap.parse_args()

    # 1. Example leaderboard: one real golfer joins a fresh league.
    with tempfile.TemporaryDirectory() as tmp:
        players = Players(1)
        me = players.add_person(1)
        league = League(LeagueStore(Path(tmp) / "league.sqlite3"), lambda uid: players.swings.get(uid, []))
        league.join(me["id"], "Harry P", MON + timedelta(hours=1))
        players.practise(me, MON)
        show(league.view(me["id"], MON + timedelta(days=3, hours=20)), "Example, Thursday 20:00")
        show(league.view(me["id"], MON + timedelta(days=6, hours=23, minutes=50)), "Same group, Sunday 23:50")

    # 2/3. Rival activity across many weeks and seeds (each rival through the shared LP function).
    by_day, zero, distinct, by_style, caps, drops = defaultdict(list), [], [], defaultdict(list), True, []
    for seed in range(a.seeds):
        for w in range(10):
            week = MON + timedelta(days=7 * w)
            totals = []
            for i in range(seed * 1000, seed * 1000 + 200):
                div = rules.DIVISIONS[i % 5]
                p = rivals.persona(i, div, MON - timedelta(days=35))
                base = rules.baseline(rivals.history_before(p, week), div, week)
                entries = rivals.week_swings(p, week)
                daily = [rules.week_lp(entries, div, base, now=week + timedelta(days=d, hours=23, minutes=59))["lp"] for d in range(7)]
                for d in range(7):
                    by_day[d].append(daily[d] > 0)
                biggest_drop = max((daily[d - 1] - daily[d] for d in range(1, 7)), default=0)
                drops.append(biggest_drop)
                final = rules.week_lp(entries, div, base)
                b = final["breakdown"]
                caps &= b["practice_day"] <= 18 and b["swing"] <= 18 and b["improvement"] <= 60 and b["fix"] <= 45
                totals.append(final["lp"])
                by_style[p["style"]].append(final["lp"])
            zero.append(sum(t == 0 for t in totals) / len(totals))
            distinct += [len(set(totals[g:g + 20])) / 20 for g in range(0, len(totals), 20)]
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print(f"\nRival activity ({a.seeds} seeds x 10 weeks x 200 rivals):")
    print("  share with points by the end of: " + ", ".join(f"{days[d]} {100 * sum(v) / len(v):.0f}%" for d, v in by_day.items()))
    print(f"  zero-point weeks: {100 * statistics.mean(zero):.0f}%   distinct totals in a group of 20: "
          f"{statistics.mean(distinct) * 20:.1f} of 20   caps respected: {caps}")
    print(f"  weeks where the total fell from one day to the next (provisional weekly bonuses re-judged): "
          f"{100 * sum(d > 0 for d in drops) / len(drops):.0f}%, falls of 20+ LP: {100 * sum(d >= 20 for d in drops) / len(drops):.0f}%, "
          f"largest {max(drops)} LP")
    print("  weekly LP by style (median / p90):")
    for style, lps in sorted(by_style.items()):
        s = sorted(lps)
        print(f"    {style:12s} {statistics.median(s):5.0f} / {s[int(0.9 * (len(s) - 1))]:3d}  ({len(s)} rival-weeks)")

    # 4. Growth: real golfers join over 8 weeks (5 -> 60); rivals only fill the gaps.
    print("\nGrowth (real golfers joining week by week; groups re-formed each Monday):")
    with tempfile.TemporaryDirectory() as tmp:
        players = Players(7)
        league = League(LeagueStore(Path(tmp) / "league.sqlite3"), lambda uid: players.swings.get(uid, []))
        schedule = [5, 10, 15, 25, 35, 45, 60, 60]
        for w, target in enumerate(schedule):
            week = MON + timedelta(days=7 * w)
            while len(players.people) < target:
                p = players.add_person(len(players.people))
                league.join(p["id"], f"Golfer {len(players.people):03d}", week + timedelta(hours=2))
            for p in players.people:
                players.practise(p, week)
            league.ensure_week(week + timedelta(hours=3))
            members = league.store.week_members(week.date().isoformat())
            groups = len({m["group_id"] for m in members})
            real = sum(m["kind"] == "user" for m in members)
            print(f"  week {w + 1}: {real:2d} real golfers placed, {groups} groups, "
                  f"{sum(m['kind'] == 'rival' for m in members):3d} rivals ({100 * (1 - real / max(1, len(members))):.0f}% of places)")
            league.ensure_week(week + timedelta(days=7, minutes=31))
        divs = defaultdict(int)
        for p in league.store.profiles():
            divs[p["division"]] += 1
        print("  real golfers by division after 8 weeks: " + ", ".join(f"{d} {divs[d]}" for d in rules.DIVISIONS if divs[d]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
