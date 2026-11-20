"""Weekly league points (LP): pure functions, used for real players and practice rivals alike.

A week's LP is *derived* from the golfer's distinct swings that week every time it is needed, never added up
from counters, so a retry, a background re-run, a repeated /start or a re-upload has nothing to add: each
swing contributes at most once by construction, and a near-duplicate of an earlier swing (the same video
again, ``swing_history.near_duplicate``) earns nothing. The baseline and the pre-week check statuses are
frozen when the golfer is placed in the week's group, so deleting older swings cannot move them; a swing
deleted during the week is "withdrawn": it stops earning practice-day and swing LP but its score still
counts for improvement / consistency / fixes, so deleting can never raise LP. A closed week is frozen.

Rules (docs/agent-work/release-1.0.1/LEAGUE_DESIGN.md, approved 2026-11-09 with practice days capped at 3):
* practice day (at least one counted swing): +6, at most 3 days a week;
* counted swing: +2, the first ``per_day`` of each day (3; 2 in Platinum and Diamond), at most 9 a week;
* improvement, judged ONCE a week: +80 x (median of this week's eligible swings - baseline), capped at 60,
  needs at least 3 eligible swings (so extra swings buy no extra chances);
* check back in range: +15 when a check out of range before the week is in range on two consecutive
  eligible swings this week, at most 3 a week;
* consistent week: +20 for 3+ practice days and this week's 30th percentile >= the baseline.
Baseline = the division's quantile of the last 60 eligible swings before the week (division default with
fewer than 5). Eligibility (measurement quality) is the rank rules' (``ranks.eligibility``).
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .. import ranks
from ..swing_history import near_duplicate

DIVISIONS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond")
GROUP_SIZE = 20
GRACE = timedelta(minutes=30)      # an analysis running at the reset still counts for the week it was uploaded in

# division -> (counted swings a day, baseline quantile, default baseline, promote top N, relegate bottom N)
DIVISION_RULES: dict[str, tuple[int, float, float, int, int]] = {
    "Bronze": (3, 0.50, 7.0, 7, 0),
    "Silver": (3, 0.50, 7.4, 6, 3),
    "Gold": (3, 0.55, 7.8, 5, 4),
    "Platinum": (2, 0.60, 8.2, 4, 5),
    "Diamond": (2, 0.70, 8.6, 3, 5),
}
LP = {
    "practice_day": 6, "practice_days_cap": 3,
    "swing": 2, "week_swings_cap": 9,
    "improve_per_point": 80, "improve_cap": 60, "improve_min_swings": 3,
    "fix": 15, "fix_cap": 3,
    "consistent_week": 20, "consistent_days": 3,
    "baseline_swings": 60, "baseline_min": 5,
}
OUT_OF_RANGE = ("attention", "problem")
PENDING = ("improvement", "consistent_week")      # weekly bonuses, added to the table at the reset


def week_start(when: datetime) -> datetime:
    """Monday 00:00 UTC of the week containing ``when``."""

    day = when.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def league_week(now: datetime) -> datetime:
    """The league week that is open at ``now`` (it stays open for ``GRACE`` after Sunday midnight)."""

    return week_start(now - GRACE)


def parse(stamp) -> datetime:
    when = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _quantile(values: Sequence[float], q: float) -> float:
    v = sorted(values)
    return v[int(q * (len(v) - 1))]


def _score(rec: Mapping) -> float | None:
    return (rec.get("score") or {}).get("total")


def is_eligible(rec: Mapping, config: Mapping | None = None) -> bool:
    return ranks.eligibility(rec, config or ranks.DEFAULT_CONFIG) == "eligible"


def counted(history: Sequence[Mapping]) -> list[dict]:
    """Scored swings, oldest first, without re-uploads of an earlier one."""

    out: list[dict] = []
    for swing in sorted(history, key=lambda s: parse(s["created_at"] if "created_at" in s else s["at"])):
        rec = swing.get("record") or {}
        if _score(rec) is None or any(near_duplicate(rec, c["record"]) for c in out):
            continue
        out.append({"id": swing.get("id"), "at": parse(swing.get("created_at") or swing.get("at")), "record": rec})
    return out


def baseline(history: Sequence[Mapping], division: str, week: datetime, config: Mapping | None = None) -> dict:
    """The frozen per-week reference: baseline score and each check's last status before the week."""

    start = week_start(week)
    before = [c for c in counted(history) if c["at"] < start and is_eligible(c["record"], config)]
    before = before[-LP["baseline_swings"]:]
    scores = [_score(c["record"]) for c in before]
    _, quantile, default, _, _ = DIVISION_RULES[division]
    statuses: dict[str, str] = {}
    for c in before:
        for chk in c["record"].get("checks", []):
            statuses[chk["id"]] = chk["status"]
    value = _quantile(scores, quantile) if len(scores) >= LP["baseline_min"] else default
    return {"baseline": round(value, 2), "statuses": statuses, "swings": len(scores),
            "default": len(scores) < LP["baseline_min"]}


def week_lp(entries: Sequence[Mapping], division: str, base: Mapping, *, now: datetime | None = None,
            config: Mapping | None = None) -> dict:
    """LP for one golfer's week from that week's swing entries ``{id, at, record, withdrawn?}``.

    ``base`` is :func:`baseline` frozen at placement. Entries after ``now`` are ignored (a rival's sessions
    appear as the week goes on). Returns the total, the breakdown and when the total was reached.
    """

    per_day = DIVISION_RULES[division][0]
    items = sorted((e for e in entries if _score(e.get("record") or {}) is not None
                    and (now is None or parse(e["at"]) < now)), key=lambda e: parse(e["at"]))
    stats: list[Mapping] = []           # distinct swings (withdrawn ones included): improvement, consistency, fixes
    awarded: list[Mapping] = []         # distinct swings still present: practice-day and swing LP
    replaced: set[int] = set()
    for e in items:
        twin = next((i for i, s in enumerate(stats) if near_duplicate(e["record"], s["record"])), None)
        if twin is None:
            stats.append(e)
            if not e.get("withdrawn"):
                awarded.append(e)
        elif stats[twin].get("withdrawn") and not e.get("withdrawn") and twin not in replaced:
            replaced.add(twin)          # deleted then uploaded again: counts once, at the re-upload
            awarded.append(e)
    events: list[tuple[datetime, str, int]] = []
    per_day_count: dict[str, int] = {}
    swing_awards = 0
    for e in sorted(awarded, key=lambda e: parse(e["at"])):
        at = parse(e["at"])
        day = at.date().isoformat()
        if day not in per_day_count:
            per_day_count[day] = 0
            if len(per_day_count) <= LP["practice_days_cap"]:
                events.append((at, "practice_day", LP["practice_day"]))
        if per_day_count[day] < per_day and swing_awards < LP["week_swings_cap"]:
            per_day_count[day] += 1
            swing_awards += 1
            events.append((at, "swing", LP["swing"]))
    days = len(per_day_count)
    eligible = [e for e in stats if is_eligible(e["record"], config)]
    scores = [_score(e["record"]) for e in eligible]
    ref = base["baseline"]
    week_median = statistics.median(scores) if scores else None
    if len(scores) >= LP["improve_min_swings"] and week_median > ref:
        lp = int(min(LP["improve_cap"], round(LP["improve_per_point"] * (week_median - ref))))
        if lp > 0:
            events.append((parse(eligible[-1]["at"]), "improvement", lp))
    streak: dict[str, int] = {}
    fixed: list[str] = []
    for e in eligible:
        for chk in e["record"].get("checks", []):
            cid = chk["id"]
            if cid in fixed or base.get("statuses", {}).get(cid) not in OUT_OF_RANGE:
                continue
            streak[cid] = streak.get(cid, 0) + 1 if chk["status"] == "good" else 0
            if streak[cid] >= 2 and len(fixed) < LP["fix_cap"]:
                fixed.append(cid)
                events.append((parse(e["at"]), "fix", LP["fix"]))
    if days >= LP["consistent_days"] and len(scores) >= LP["improve_min_swings"] and _quantile(scores, 0.3) >= ref:
        events.append((parse(eligible[-1]["at"]), "consistent_week", LP["consistent_week"]))
    breakdown = {k: 0 for k in ("practice_day", "swing", "improvement", "fix", "consistent_week")}
    for _, kind, lp in events:
        breakdown[kind] += lp
    total = sum(breakdown.values())
    reached = max((at for at, _, _ in events), default=None)
    # Weekly bonuses are judged on the whole week, so until the reset they are "pending": the live table ranks
    # on confirmed points (practice days, counted swings, fixes), which never fall as the week goes on. The
    # weekly result (``lp``) is unchanged: confirmed + pending, added at the reset.
    pending = sum(breakdown[k] for k in PENDING)
    confirmed_reached = max((at for at, kind, _ in events if kind not in PENDING), default=None)
    return {"lp": total, "confirmed": total - pending, "pending": pending,
            "confirmed_reached_at": confirmed_reached.isoformat() if confirmed_reached else None,
            "breakdown": breakdown, "reached_at": reached.isoformat() if reached else None,
            "practice_days": days, "counted_swings": len(awarded), "eligible_swings": len(eligible),
            "week_median": round(week_median, 2) if week_median is not None else None,
            "baseline": ref, "fixed_checks": fixed}


def rank_members(rows: Sequence[Mapping]) -> list[Mapping]:
    """Final order: most LP first; ties go to whoever reached the total first (then a stable key)."""

    far = "9999-12-31T00:00:00+00:00"
    return sorted(rows, key=lambda r: (-r["lp"], parse(r.get("reached_at") or far), r["key"]))


def outcomes(division: str, ranked_lp: Sequence[int]) -> list[str]:
    """Outcome per final position (``ranked_lp`` in final order): promoted / stayed / relegated / medal."""

    _, _, _, promote, relegate = DIVISION_RULES[division]
    top = DIVISIONS.index(division) == len(DIVISIONS) - 1
    bottom = DIVISIONS.index(division) == 0
    n = len(ranked_lp)
    out = []
    for pos, lp in enumerate(ranked_lp, 1):
        if pos <= promote and lp > 0:
            out.append("medal" if top else "promoted")
        elif not bottom and (pos > n - relegate or lp == 0):
            out.append("relegated")
        else:
            out.append("stayed")
    return out


def zone(division: str, position: int, n: int) -> str:
    _, _, _, promote, relegate = DIVISION_RULES[division]
    if position <= promote:
        return "medal" if division == DIVISIONS[-1] else "promote"
    if relegate and position > n - relegate:
        return "relegate"
    return "safe"


def next_division(division: str, outcome: str) -> str:
    i = DIVISIONS.index(division)
    if outcome == "promoted":
        return DIVISIONS[min(i + 1, len(DIVISIONS) - 1)]
    if outcome == "relegated":
        return DIVISIONS[max(i - 1, 0)]
    return division


def public_rules() -> dict:
    return {"lp": dict(LP), "group_size": GROUP_SIZE, "grace_minutes": int(GRACE.total_seconds() // 60),
            "divisions": [{"name": d, "day_swings": r[0], "baseline_quantile": r[1], "promote": r[3],
                           "relegate": r[4]} for d, r in DIVISION_RULES.items()]}


__all__ = ["DIVISIONS", "DIVISION_RULES", "GRACE", "GROUP_SIZE", "LP", "baseline", "counted", "league_week",
           "next_division", "outcomes", "public_rules", "rank_members", "week_lp", "week_start", "zone"]
