"""Weekly league design study (simulation only; nothing in the app or backend uses this yet).

    .venv\\Scripts\\python.exe scripts\\league_sim.py [--weeks 12] [--players 60] [--seed 3]

Simulates seasons of the proposed weekly league (docs/agent-work/release-1.0.1/LEAGUE_DESIGN.md):
* real-player personas (casual, regular, improver, grinder) practise with persona-specific habits;
* persistent practice rivals (named, labelled, with their own skill, schedule and holidays) earn LP from
  simulated swings under exactly the same rules, and are promoted / relegated like anyone else;
* groups of 20 per division, filled with rivals only where real players are short; everyone starts in
  Bronze; promotion / relegation per division at the weekly reset.
Prints an example group table, where each persona ends up, and the farming check.
"""

from __future__ import annotations

import argparse
import random
import statistics as st
from dataclasses import dataclass, field

DIVISIONS = ["Bronze", "Silver", "Gold", "Platinum", "Diamond"]
# per division: (counted swings a day, baseline quantile, promote top N, relegate bottom N)
RULES = {"Bronze": (3, 0.5, 7, 0), "Silver": (3, 0.5, 6, 3), "Gold": (3, 0.55, 5, 4),
         "Platinum": (2, 0.6, 4, 5), "Diamond": (2, 0.7, 3, 5)}
# Improvement is judged ONCE A WEEK (this week's median of counted swings vs your baseline), so extra swings
# buy no extra chances; per-swing improvement made volume a lottery (grinders reached Diamond 14 of 15).
LP = {"practice_day": 6, "practice_days_cap": 3,       # approved 2026-11-09: 3 practice days (was 4) "swing_base": 2, "week_swings_cap": 9, "improve_per_point": 80,
      "improve_cap": 60, "improve_min_swings": 3, "fix": 15, "fix_cap": 3, "fix_chance_per_day": 0.12,
      "consistent_week": 20}
RIVAL_NAMES = ["Fairway Fox", "Iron Owl", "Birdie Bot", "Divot Drake", "Tempo Tern", "Pin Hawk", "Links Lynx",
               "Wedge Wren", "Draw Dingo", "Fade Falcon", "Bunker Badger", "Green Heron", "Carry Crane",
               "Loft Lark", "Spin Swift", "Par Puffin", "Eagle Elk", "Chip Chough", "Putt Pelican", "Tee Tiger"]


@dataclass
class Golfer:
    key: str
    kind: str                      # persona, or "rival"
    skill: float                   # typical swing score
    spread: float                  # swing-to-swing variation
    days_per_week: float           # average practice days
    swings_per_session: int
    improve_per_week: float        # how fast the typical score rises
    holiday: float = 0.0           # chance of a week off
    division: str = "Bronze"
    history: list = field(default_factory=list)   # recent eligible scores (baseline)
    outcomes: list = field(default_factory=list)

    @property
    def is_rival(self) -> bool:
        return self.kind == "rival"


def week_lp(g: Golfer, rnd: random.Random) -> int:
    """One week of LP under the proposed rules (identical for players and rivals)."""

    if rnd.random() < g.holiday:
        return 0
    per_day, quant, _, _ = RULES[g.division]
    hist = sorted(g.history[-60:])
    baseline = hist[int(quant * (len(hist) - 1))] if len(hist) >= 5 else 7.5
    days = min(7, max(0, round(rnd.gauss(g.days_per_week, 0.8))))
    lp, fixes, week_scores, counted, counted_scores = 0, 0, [], 0, []
    for d in range(days):
        if d < LP["practice_days_cap"]:
            lp += LP["practice_day"]
        for k in range(g.swings_per_session):
            s = min(10.0, max(3.0, rnd.gauss(g.skill, g.spread)))
            week_scores.append(s)
            if k < per_day and counted < LP["week_swings_cap"]:   # only the first counted swings earn swing LP
                counted += 1
                lp += LP["swing_base"]
                counted_scores.append(s)
        if rnd.random() < LP["fix_chance_per_day"] and fixes < LP["fix_cap"]:   # a confirmed check back in range
            fixes += 1
            lp += LP["fix"]
    if len(counted_scores) >= LP["improve_min_swings"]:
        lp += min(LP["improve_cap"], max(0.0, LP["improve_per_point"] * (st.median(counted_scores) - baseline)))
    if days >= 3 and counted_scores and sorted(counted_scores)[int(0.3 * (len(counted_scores) - 1))] >= baseline:
        lp += LP["consistent_week"]
    g.history += week_scores[:per_day * 7]
    g.skill = min(9.6, g.skill + g.improve_per_week)
    return round(lp)


def personas(n: int, rnd: random.Random) -> list[Golfer]:
    kinds = [("casual", 7.2, 0.9, 1.5, 2, 0.0), ("regular", 7.8, 0.7, 3.5, 3, 0.01),
             ("improver", 7.0, 0.7, 4.0, 3, 0.06), ("grinder", 7.6, 0.8, 6.5, 10, 0.005)]
    out = []
    for i in range(n):
        k, skill, spread, days, swings, imp = kinds[i % len(kinds)]
        out.append(Golfer(f"P{i:02d}-{k}", k, rnd.gauss(skill, 0.3), spread, days, swings, imp, holiday=0.08))
    return out


# Division-appropriate rivals: skill and practice habits scale with the division they are created for;
# afterwards they move only by results, under the same rules as players.
RIVAL_LEVEL = {"Bronze": (6.6, 7.6, 1.5, 3.5), "Silver": (7.0, 8.0, 2.0, 4.0), "Gold": (7.4, 8.4, 2.5, 4.5),
               "Platinum": (7.8, 8.8, 3.0, 5.0), "Diamond": (8.2, 9.2, 3.5, 5.5)}


def rival(i: int, rnd: random.Random, division: str = "Bronze") -> Golfer:
    lo, hi, d_lo, d_hi = RIVAL_LEVEL[division]
    skill = rnd.uniform(lo, hi)
    return Golfer(f"Rival · {RIVAL_NAMES[i % len(RIVAL_NAMES)]}{'' if i < len(RIVAL_NAMES) else ' ' + str(i // len(RIVAL_NAMES) + 1)}",
                  "rival", skill, rnd.uniform(0.5, 0.9), rnd.uniform(d_lo, d_hi), rnd.randint(2, 4),
                  rnd.uniform(-0.005, 0.02), holiday=rnd.uniform(0.03, 0.15))


def season(weeks: int, players: int, seed: int) -> None:
    rnd = random.Random(seed)
    real = personas(players, rnd)
    rivals: list[Golfer] = []
    example = None
    rival_example = None
    for week in range(1, weeks + 1):
        for div in DIVISIONS:
            members = [g for g in real if g.division == div]
            if not members:
                continue
            groups = [members[i:i + 20] for i in range(0, len(members), 20)]
            for group in groups:
                # top up with persistent rivals already in this division, then new ones
                pool = [r for r in rivals if r.division == div and all(r is not m for grp in groups for m in grp)]
                while len(group) < 20:
                    if pool:
                        group.append(pool.pop())
                    else:
                        r = rival(len(rivals), rnd, div)
                        r.division = div
                        rivals.append(r)
                        group.append(r)
                scores = [(week_lp(g, rnd), rnd.random(), g) for g in group]
                scores.sort(key=lambda x: (-x[0], x[1]))
                _, _, up, down = RULES[div]
                i = DIVISIONS.index(div)
                for pos, (lp, _, g) in enumerate(scores, 1):
                    if pos <= up and i < len(DIVISIONS) - 1:
                        g.division, outcome = DIVISIONS[i + 1], "promoted"
                    elif (pos > 20 - down or lp == 0) and i > 0:
                        g.division, outcome = DIVISIONS[i - 1], "relegated"
                    else:
                        outcome = "medal" if div == "Diamond" and pos <= 3 else "stayed"
                    g.outcomes.append((week, div, pos, lp, outcome))
                if example is None and week == 1:
                    example = (div, scores)
                if rival_example is None and week >= 3 and sum(g.is_rival for g in group) >= 6:
                    rival_example = (week, div, scores)
    div, scores = example
    print(f"Example: week 1, a {div} group (top {RULES[div][2]} promote; rivals marked)")
    for pos, (lp, _, g) in enumerate(scores, 1):
        tag = "  [practice rival]" if g.is_rival else ""
        print(f"  {pos:2d}. {g.key:24s} {lp:4d} LP{tag}")
    if rival_example:
        w, d2, sc = rival_example
        print(f"\nExample with practice rivals: week {w}, a {d2} group (top {RULES[d2][2]} promote, bottom {RULES[d2][3]} relegate)")
        for pos, (lp, _, g) in enumerate(sc, 1):
            print(f"  {pos:2d}. {g.key:28s} {lp:4d} LP{'  [practice rival]' if g.is_rival else ''}")
    print(f"\nAfter {weeks} weeks (players start in Bronze):")
    for kind in ("casual", "regular", "improver", "grinder"):
        g = [p for p in real if p.kind == kind]
        dist = {d: sum(p.division == d for p in g) for d in DIVISIONS}
        lps = [o[3] for p in g for o in p.outcomes]
        print(f"  {kind:9s}: " + ", ".join(f"{d} {n}" for d, n in dist.items() if n) +
              f" | weekly LP median {st.median(lps):.0f}, p90 {sorted(lps)[int(0.9 * (len(lps) - 1))]}")
    rv = rivals
    print(f"  rivals created: {len(rv)} (persistent); by division now: " +
          ", ".join(f"{d} {sum(r.division == d for r in rv)}" for d in DIVISIONS if any(r.division == d for r in rv)))
    reg = [o[3] for p in real if p.kind == "regular" for o in p.outcomes if o[3] > 0]
    grind = [o[3] for p in real if p.kind == "grinder" for o in p.outcomes if o[3] > 0]
    print(f"\nFarming check: a grinder uploads ~10 swings a session on ~6.5 days; a regular ~3 on ~3.5 days.\n"
          f"  median weekly LP when active: regular {st.median(reg):.0f}, grinder {st.median(grind):.0f} "
          f"(grinder uploads ~{10 * 6.5 / (3 * 3.5):.0f}x more swings)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--players", type=int, default=60)
    ap.add_argument("--seed", type=int, default=3)
    a = ap.parse_args()
    season(a.weeks, a.players, a.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
