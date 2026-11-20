"""Practice rivals: persistent, computer-generated league participants.

* **Identity:** an ordinary, unique name (``names.name_for``), stored with the rival, so it is stable across
  weeks, requests and restarts. Rivals are always flagged ``is_rival`` in the API and marked in the app.
* **Behaviour:** a fixed persona with one of six styles (casual, regular, improver, inconsistent, highly active,
  inactive), its own preferred practice days and times, skill, swing-to-swing spread and holiday chance. A
  rival's swings are generated deterministically from (seed, week), so the same rival always has the same
  history and its points appear through the week as its sessions happen: nothing changes on refresh or restart.
* **Same rules:** its LP comes from those simulated swings through exactly the same ``rules.week_lp`` as real
  golfers, with the same caps and eligibility. Nothing here looks at, or adjusts for, any real player's standing.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from ..scoring.config import CHECKS
from ..swing_history import fingerprint
from . import rules
from .names import name_for

PERSONA_VERSION = 2
LABEL = "Practice rival"
PROFILE_LINE = ("Practice rival: a computer-generated competitor. It earns points from simulated practice "
                "under exactly the same rules and limits as everyone else.")
# Division a rival is created for -> (skill low, skill high, extra practice days for the division).
LEVEL = {"Bronze": (6.6, 7.6, 0.0), "Silver": (7.0, 8.0, 0.25), "Gold": (7.4, 8.4, 0.5),
         "Platinum": (7.8, 8.8, 0.75), "Diamond": (8.2, 9.2, 1.0)}
# style -> weight, practice days a week (low, high), swings a session (low, high), weekly trend (low, high),
#          holiday chance, chance of a fully inactive week, skill offset
STYLES = {
    "casual":       (0.20, (1.0, 2.2), (1, 3), (0.000, 0.010), 0.12, 0.10, -0.15),
    "regular":      (0.30, (2.5, 4.0), (2, 4), (0.000, 0.012), 0.06, 0.03, 0.00),
    "improver":     (0.15, (3.0, 4.5), (2, 4), (0.020, 0.040), 0.05, 0.02, -0.20),
    "inconsistent": (0.15, (1.5, 4.5), (1, 5), (-0.005, 0.015), 0.08, 0.08, 0.00),
    "active":       (0.12, (4.5, 6.0), (3, 5), (0.005, 0.020), 0.03, 0.01, 0.15),
    "inactive":     (0.08, (0.8, 2.0), (1, 2), (0.000, 0.005), 0.10, 0.45, -0.10),
}
SESSION_STYLES = {"early": (6, 8), "lunch": (12, 13), "afternoon": (14, 16), "evening": (17, 20), "late": (20, 21)}
HISTORY_WEEKS = 5


def identity(index: int) -> tuple[str, str]:
    """(id, name) for the n-th rival ever created. Ids are internal and never shown."""

    return f"rival:{index:05d}", name_for(index)


def persona(index: int, division: str, created_week: datetime | str, seed: str | None = None) -> dict:
    """A persona for the n-th rival (deterministic for its index and the division it was created for)."""

    rnd = random.Random(f"rival-persona-v2:{index}")
    style = rnd.choices(list(STYLES), weights=[s[0] for s in STYLES.values()])[0]
    _, days, swings, trend, holiday, inactive, skill_offset = STYLES[style]
    lo, hi, extra_days = LEVEL[division]
    weekday_bias, weekend_bias = rnd.uniform(0.6, 1.4), rnd.uniform(0.4, 2.2)
    favourite = rnd.randrange(7)
    day_weights = [round((weekend_bias if d >= 5 else weekday_bias) * (1.8 if d == favourite else 1.0), 2) for d in range(7)]
    week = created_week if isinstance(created_week, str) else rules.week_start(created_week).date().isoformat()
    return {
        "version": PERSONA_VERSION, "seed": seed or f"rival:{index}", "style": style,
        "skill": round(min(9.3, max(6.0, rnd.uniform(lo, hi) + skill_offset)), 2),
        "spread": round(rnd.uniform(0.5, 0.75) if style != "inconsistent" else rnd.uniform(0.8, 1.05), 2),
        "days_per_week": round(min(6.5, rnd.uniform(*days) + extra_days), 2),
        "swings_min": swings[0], "swings_max": swings[1],
        "trend_per_week": round(rnd.uniform(*trend), 3), "holiday": holiday, "inactive_share": inactive,
        "session_style": rnd.choice(sorted(SESSION_STYLES)), "day_weights": day_weights,
        "weak_checks": rnd.sample([c.id for c in CHECKS], 2), "created_week": week, "created_division": division,
    }


def _record(rnd: random.Random, score: float, weak: list[str], weak_good: float | None = None) -> dict:
    """One simulated swing. With ``weak_good`` (v2 personas) faults persist like a real golfer's: the rival's two
    weak checks are usually out of range (improvers fix them slowly), the others usually in range."""

    checks = []
    for c in CHECKS:
        if weak_good is None:       # v1 personas: independent per swing
            p_good = min(0.95, max(0.15, 0.45 + 0.12 * (score - 7.0) - (0.25 if c.id in weak else 0.0)))
        elif c.id in weak:
            p_good = weak_good
        else:
            p_good = min(0.98, max(0.85, 0.93 + 0.03 * (score - 7.5)))
        r = rnd.random()
        status = "good" if r < p_good else ("attention" if r < p_good + (1 - p_good) * 0.65 else "problem")
        checks.append({"id": c.id, "phase": c.phase, "name": c.name, "status": status, "label": status,
                       "value": round(rnd.uniform(0, 60), 2), "unit": c.unit, "confidence": "HIGH", "estimated": False})
    return {"version": 2, "simulated": True,
            "score": {"available": True, "total": score, "version": "simulated", "measured": len(checks), "eligible": True},
            "video": {"long_side": 1920}, "checks": checks, "fingerprint": fingerprint(score, checks)}


def week_swings(p: dict, week: datetime) -> list[dict]:
    """The rival's simulated swings in ``week`` (entries ``{id, at, record}``), deterministic."""

    start = rules.week_start(week)
    rnd = random.Random(f"{p['seed']}:{start.date().isoformat()}")
    if p.get("version", 1) < 2:                       # legacy persona (before names v2); kept for old tests
        return _week_swings_v1(p, start, rnd)
    if rnd.random() < p["inactive_share"] or rnd.random() < p["holiday"]:
        return []
    form, skill_shift = 1.0, 0.0
    if p["style"] == "inconsistent":
        form, skill_shift = rnd.choice(((0.45, -0.35), (1.0, 0.0), (1.0, 0.0), (1.35, 0.25)))
    days = min(7, max(0, round(rnd.gauss(p["days_per_week"] * form, 0.7))))
    pool, chosen = list(range(7)), []
    for _ in range(days):
        pick = rnd.choices(pool, weights=[p["day_weights"][d] for d in pool])[0]
        pool.remove(pick)
        chosen.append(pick)
    created = datetime.fromisoformat(p["created_week"]).replace(tzinfo=start.tzinfo)
    weeks_on = max(0.0, (start - created).days / 7)
    skill = min(9.4, p["skill"] + p["trend_per_week"] * weeks_on + skill_shift)
    lo, hi = SESSION_STYLES[p["session_style"]]
    weak_good = min(0.75, 0.2 + (0.04 if p["style"] == "improver" else 0.01) * weeks_on)
    out = []
    for d in sorted(chosen):
        hour = min(22, max(5, rnd.randint(lo, hi) + rnd.choice((-1, 0, 0, 0, 1))))
        at = start + timedelta(days=d, hours=hour, minutes=rnd.randint(0, 55))
        for k in range(rnd.randint(p["swings_min"], p["swings_max"])):
            score = round(min(10.0, max(3.0, rnd.gauss(skill, p["spread"]))), 1)
            out.append({"id": f"{p['seed']}:{start.date()}:{d}:{k}", "at": (at + timedelta(minutes=2 * k + rnd.randint(0, 2))).isoformat(),
                        "record": _record(rnd, score, p["weak_checks"], weak_good)})
    return out


def _week_swings_v1(p: dict, start: datetime, rnd: random.Random) -> list[dict]:
    if rnd.random() < p["holiday"]:
        return []
    days = min(7, max(0, round(rnd.gauss(p["days_per_week"], 0.8))))
    weights = [1.0] * 5 + [1.0 + 3 * p.get("weekend_bias", 0.3)] * 2
    pool, chosen = list(range(7)), []
    for _ in range(days):
        pick = rnd.choices(pool, weights=[weights[d] for d in pool])[0]
        pool.remove(pick)
        chosen.append(pick)
    created = datetime.fromisoformat(p["created_week"]).replace(tzinfo=start.tzinfo)
    skill = min(9.6, p["skill"] + p["trend_per_week"] * (start - created).days / 7)
    lo, hi = SESSION_STYLES.get(p["session_style"], (17, 20))
    out = []
    for d in sorted(chosen):
        at = start + timedelta(days=d, hours=rnd.randint(lo, hi), minutes=rnd.randint(0, 50))
        for k in range(max(1, p["swings_per_session"] + rnd.choice((-1, 0, 0, 1)))):
            score = round(min(10.0, max(3.0, rnd.gauss(skill, p["spread"]))), 1)
            out.append({"id": f"{p['seed']}:{start.date()}:{d}:{k}", "at": (at + timedelta(minutes=2 * k)).isoformat(),
                        "record": _record(rnd, score, p["weak_checks"])})
    return out


def history_before(p: dict, week: datetime) -> list[dict]:
    """Simulated swings in the weeks before ``week`` (enough for the 60-swing baseline)."""

    start = rules.week_start(week)
    out: list[dict] = []
    for back in range(HISTORY_WEEKS, 0, -1):
        out += week_swings(p, start - timedelta(days=7 * back))
    return out


def standing(p: dict, division: str, week: datetime, now: datetime | None = None) -> dict:
    """The rival's LP in ``week`` under the shared rules, counting its sessions up to ``now``."""

    base = rules.baseline(history_before(p, week), division, week)
    return rules.week_lp(week_swings(p, week), division, base, now=now)


__all__ = ["LABEL", "LEVEL", "PERSONA_VERSION", "PROFILE_LINE", "STYLES", "history_before", "identity", "persona",
           "standing", "week_swings"]
