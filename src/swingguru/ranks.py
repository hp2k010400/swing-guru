"""Swing Guru Ranks, rules v2 (Bronze III to Diamond I): consistent quality over time, not upload volume.

Computed from a golfer's own swing records (``swing_history.record``), oldest first, every time it is
asked for. Numbers live in ``DEFAULT_CONFIG`` and can be overridden while testing with the
``SG_RANK_CONFIG`` environment variable (a JSON object merged over the defaults).

**Eligibility** (measurement quality, never identity):
* a swing with no Swing Score, or a re-upload of an earlier swing (``swing_history.near_duplicate``), counts
  for nothing;
* a swing with too few checks measured (under ``min_measured`` of 8) or a video below ``min_long_side``
  pixels on its long side (CAMERA_STABILITY.md: precision drops there) is **practice only**: it earns
  practice points but does not count towards the typical score. Cropped, trimmed and exported videos
  are not penalised. A moving camera needs no rule of its own: the engine already withholds the checks
  it affects, which lowers the measured count;
* everything else is **eligible**.

**Typical score** = the ``rating_percentile`` (30th) percentile of the last ``rating_window`` (20)
eligible swings, at least ``rating_min_swings`` (10); only the first ``rating_daily_cap`` eligible swings
of a UTC day enter it, so one long session cannot dominate.

**Promotion** happens at the weekly evaluation (Monday 00:00 UTC): a tier opens only when its typical-score
gate was met at ``gate_weeks`` (2) consecutive evaluations, and Platinum / Diamond also need tenure
(weeks since the first counted swing and eligible swings). Within an open tier, divisions follow points.
Ranks never go down.

**Points (RP)** reward practice, not volume: the first three counted swings of a day earn 10 / 5 / 5 and
the rest nothing; a personal best +25 (once a week); a check back in range +15 once the next eligible
swing confirms it (up to three a week); a practice week (3+ practice days) +30.

A rank describes progress with Swing Guru's analysis. It is not a handicap or a verified measure of
playing ability; the Swing Score itself is not yet validated against coach ratings.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from .swing_history import PLANE_CHECKS, club_group, near_duplicate

TIERS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond")
DIVISIONS = ("III", "II", "I")
RULES_VERSION = "ranks/2"

DEFAULT_CONFIG: dict = {
    "points": {"day_swings": [10, 5, 5], "personal_best": 25, "back_in_range": 15, "practice_week": 30},
    "practice_week_days": 3,
    "back_in_range_weekly_cap": 3,
    "personal_best_weekly_cap": 1,
    "pb_min_gain": 0.1,
    # Eligibility (measurement quality).
    "min_measured": 6,
    "min_long_side": 960,
    # Typical score.
    "rating_percentile": 0.30,
    "rating_window": 20,
    "rating_min_swings": 10,
    "rating_daily_cap": 3,
    # Promotion.
    "gate_weeks": 2,
    # Typical score needed to open each tier (on the 30th percentile; provisional, not raised above v1's).
    "tier_rating": {"Bronze": 0.0, "Silver": 7.0, "Gold": 7.8, "Platinum": 8.4, "Diamond": 8.9},
    # Weeks since the first counted swing, and eligible swings, needed to open a tier.
    "tenure": {"Platinum": {"weeks": 6, "swings": 40}, "Diamond": {"weeks": 10, "swings": 80}},
    # RP for each of the 15 divisions, Bronze III -> Diamond I.
    "division_rp": [0, 60, 140, 250, 380, 520, 700, 900, 1120, 1400, 1700, 2050, 2450, 2900, 3400],
    "provisional_swings": 5,
}

DISCLAIMER = "Based on your analysed swings. Not a handicap or an official measure of playing ability."


def load_config() -> dict:
    raw = os.environ.get("SG_RANK_CONFIG", "").strip()
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if raw:
        for key, value in json.loads(raw).items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key].update(value)
            else:
                config[key] = value
    if len(config["division_rp"]) != len(TIERS) * len(DIVISIONS):
        raise ValueError("division_rp must list 15 thresholds")
    return config


def rank_name(index: int) -> str:
    return f"{TIERS[index // 3]} {DIVISIONS[index % 3]}"


def _tier(index: int) -> str:
    return TIERS[index // 3]


def eligibility(rec: Mapping, config: Mapping) -> str:
    """'eligible', 'practice_only:<reason>' or 'no_score' (duplicates are decided against history)."""

    score = rec.get("score") or {}
    if score.get("total") is None:
        return "no_score"
    measured = score.get("measured")
    if measured is not None and measured < config["min_measured"]:
        return "practice_only:insufficient_measurements"
    long_side = (rec.get("video") or {}).get("long_side")
    if long_side is not None and long_side < config["min_long_side"]:
        return "practice_only:low_resolution"
    return "eligible"


def _percentile(values: Sequence[float], q: float) -> float:
    v = sorted(values)
    return v[int(q * (len(v) - 1))]


def _rating(scores: Sequence[float], config: Mapping) -> float | None:
    recent = list(scores)[-config["rating_window"]:]
    if len(recent) < config["rating_min_swings"]:
        return None
    return round(_percentile(recent, config["rating_percentile"]), 2)


def _week_start(when: datetime) -> datetime:
    day = when.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _parse(stamp) -> datetime:
    when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


class _State:
    """Everything the weekly evaluation needs, built up swing by swing."""

    def __init__(self, config: Mapping) -> None:
        self.config = config
        self.rp = 0
        self.rank = 0
        self.allowed_tier = 0                       # highest tier opened by an evaluation (never goes down)
        self.streak = {t: 0 for t in TIERS}         # consecutive evaluations meeting each tier's gate
        self.rating_scores: list[float] = []
        self.eligible = 0
        self.first: datetime | None = None
        self.evaluations: list[dict] = []
        self.events: list[dict] = []

    def tenure_ok(self, tier: str, at: datetime) -> bool:
        need = self.config["tenure"].get(tier)
        if not need or self.first is None:
            return need is None
        weeks = (at - _week_start(self.first)).days // 7
        return weeks >= need["weeks"] and self.eligible >= need["swings"]

    def evaluate(self, week_end: datetime) -> None:
        rating = _rating(self.rating_scores, self.config)
        met = []
        for tier in TIERS:
            gate = self.config["tier_rating"][tier]
            ok = gate <= 0 or (rating is not None and rating >= gate)
            self.streak[tier] = self.streak[tier] + 1 if ok else 0
            if ok:
                met.append(tier)
        for index, tier in enumerate(TIERS):
            gate_ok = self.config["tier_rating"][tier] <= 0 or self.streak[tier] >= self.config["gate_weeks"]
            if gate_ok and self.tenure_ok(tier, week_end) and index > self.allowed_tier:
                self.allowed_tier = index
        self.evaluations.append({"week_end": week_end.isoformat(), "rating": rating, "gates_met": met,
                                 "allowed_tier": TIERS[self.allowed_tier]})
        self.update_rank(week_end)

    def update_rank(self, at: datetime) -> None:
        by_rp = max(i for i in range(15) if self.rp >= self.config["division_rp"][i])
        reached = min(by_rp, self.allowed_tier * 3 + 2)
        if reached > self.rank:                     # ranks never go down
            self.events.append({"date": at.isoformat(), "kind": "rank_up", "from": rank_name(self.rank),
                                "to": rank_name(reached)})
            self.rank = reached


def compute(swings: Sequence[Mapping], config: Mapping | None = None, *, now: datetime | None = None) -> dict:
    """``swings``: oldest first, each ``{"id", "created_at", "club", "record"}``. ``now`` closes the weeks."""

    config = config or load_config()
    pts = config["points"]
    now = now or datetime.now(timezone.utc)
    st = _State(config)
    counted: list[Mapping] = []                     # non-duplicate scored swings
    best: float | None = None
    per_day: dict[str, int] = {}
    rating_per_day: dict[str, int] = {}
    practice_days: dict[datetime, set] = {}
    week_awards: dict[tuple, int] = {}
    last_seen: dict[str, tuple[str, str | None]] = {}
    pending_fix: dict[str, str] = {}               # check id -> club group it came back in range with
    timeline, achievements = [], {}
    eligibility_counts = {"eligible": 0, "practice_only": 0, "duplicate": 0, "no_score": 0}
    next_eval: datetime | None = None

    def close_weeks_until(at: datetime) -> None:
        nonlocal next_eval
        while next_eval is not None and next_eval <= at:
            st.evaluate(next_eval)
            next_eval += timedelta(days=7)

    def award(week: datetime, kind: str, cap: int) -> bool:
        key = (week, kind)
        if week_awards.get(key, 0) >= cap:
            return False
        week_awards[key] = week_awards.get(key, 0) + 1
        return True

    for swing in swings:
        rec = swing.get("record") or {}
        when = _parse(swing["created_at"])
        close_weeks_until(when)
        week = _week_start(when)
        day = when.astimezone(timezone.utc).date().isoformat()
        status = eligibility(rec, config)
        if status == "no_score":
            eligibility_counts["no_score"] += 1
            st.events.append({"swing_id": swing["id"], "date": when.isoformat(), "kind": "not_counted", "reason": "no_score"})
            continue
        if any(near_duplicate(rec, c["record"]) for c in counted):
            eligibility_counts["duplicate"] += 1
            st.events.append({"swing_id": swing["id"], "date": when.isoformat(), "kind": "not_counted", "reason": "duplicate"})
            continue
        counted.append(swing)
        if st.first is None:
            st.first = when
            next_eval = _week_start(when) + timedelta(days=7)
            achievements.setdefault("first_swing", when.isoformat())
        eligible = status == "eligible"
        eligibility_counts["eligible" if eligible else "practice_only"] += 1
        gained = []
        # Practice points: the first counted swings of the day only.
        n_today = per_day.get(day, 0)
        per_day[day] = n_today + 1
        if n_today < len(pts["day_swings"]):
            gained.append(("practice_swing", pts["day_swings"][n_today]))
        days = practice_days.setdefault(week, set())
        if day not in days:
            days.add(day)
            if len(days) == config["practice_week_days"]:
                gained.append(("practice_week", pts["practice_week"]))
        total = (rec.get("score") or {}).get("total")
        group = club_group(swing.get("club"))
        if eligible:
            st.eligible += 1
            if rating_per_day.get(day, 0) < config["rating_daily_cap"]:
                rating_per_day[day] = rating_per_day.get(day, 0) + 1
                st.rating_scores.append(total)
            if best is not None and total >= best + config["pb_min_gain"] and award(week, "pb", config["personal_best_weekly_cap"]):
                gained.append(("personal_best", pts["personal_best"]))
                achievements.setdefault("personal_best", when.isoformat())
            best = total if best is None else max(best, total)
            for c in rec.get("checks", []):
                cid, status_now = c["id"], c["status"]
                comparable = lambda g: not (cid in PLANE_CHECKS and g and group and g != group)
                if cid in pending_fix:
                    # The swing after a check came back in range confirms (or cancels) the fix.
                    if status_now == "good" and comparable(pending_fix[cid]) and award(week, "fix", config["back_in_range_weekly_cap"]):
                        gained.append(("back_in_range", pts["back_in_range"]))
                        achievements.setdefault(f"fixed:{cid}", when.isoformat())
                    pending_fix.pop(cid, None)
                else:
                    before = last_seen.get(cid)
                    if before and before[0] in ("attention", "problem") and status_now == "good" and comparable(before[1]):
                        pending_fix[cid] = group
                last_seen[cid] = (status_now, group)
            if rec.get("checks") and len(rec["checks"]) >= 5 and all(c["status"] == "good" for c in rec["checks"]):
                achievements.setdefault("all_in_range", when.isoformat())
        st.rp += sum(v for _, v in gained)
        st.update_rank(when)
        st.events.append({"swing_id": swing["id"], "date": when.isoformat(), "kind": "counted",
                          "rp": sum(v for _, v in gained), "reasons": [k for k, _ in gained],
                          "eligibility": status})
        timeline.append({"date": when.isoformat(), "rp": st.rp, "rating": _rating(st.rating_scores, config),
                         "rank": rank_name(st.rank)})
    close_weeks_until(now)
    rating = _rating(st.rating_scores, config)
    return {"rules": RULES_VERSION,
            "rank": {"index": st.rank, "name": rank_name(st.rank), "tier": _tier(st.rank), "division": DIVISIONS[st.rank % 3]},
            "rp": st.rp, "rating": rating, "next": _next(st, rating, config, next_eval, now),
            "personal_best": best, "eligible_swings": len(counted),
            "provisional": len(counted) < config["provisional_swings"],
            "provisional_remaining": max(0, config["provisional_swings"] - len(counted)),
            "eligibility": eligibility_counts, "rating_swings": len(st.rating_scores),
            "evaluations": st.evaluations[-12:], "next_evaluation": next_eval.isoformat() if next_eval else None,
            "events": st.events[-60:], "timeline": timeline, "achievements": achievements, "disclaimer": DISCLAIMER,
            "config": {"points": {"swing": pts["day_swings"][0], "personal_best": pts["personal_best"],
                                  "back_in_range": pts["back_in_range"], "practice_week": pts["practice_week"]},
                       "day_swings": pts["day_swings"], "daily_cap": len(pts["day_swings"]),
                       "tier_rating": config["tier_rating"], "division_rp": config["division_rp"],
                       "practice_week_swings": config["practice_week_days"], "provisional_swings": config["provisional_swings"],
                       "rating_window": config["rating_window"], "rating_percentile": config["rating_percentile"],
                       "rating_min_swings": config["rating_min_swings"], "gate_weeks": config["gate_weeks"],
                       "tenure": config["tenure"], "min_measured": config["min_measured"],
                       "min_long_side": config["min_long_side"]}}


def _next(st: _State, rating: float | None, config: Mapping, next_eval: datetime | None, now: datetime) -> dict | None:
    rank = st.rank
    if rank >= 14:
        return None
    target = rank + 1
    tier = _tier(target)
    need_rp = config["division_rp"][target]
    floor = config["division_rp"][rank]
    rp_progress = round(min(1.0, max(0.0, (st.rp - floor) / max(1, need_rp - floor))), 3)
    points_met = st.rp >= need_rp
    gate = config["tier_rating"][tier]
    tier_open = TIERS.index(tier) <= st.allowed_tier
    rating_met_now = gate <= 0 or (rating is not None and rating >= gate)
    current_gate = config["tier_rating"][_tier(rank)]
    if tier_open or rating_met_now:
        rating_progress = 1.0
    elif rating is None:
        rating_progress = 0.0
    else:
        rating_progress = round(min(1.0, max(0.0, (rating - current_gate) / max(0.01, gate - current_gate))), 3)
    need = config["tenure"].get(tier)
    weeks_done = 0 if st.first is None else (now - _week_start(st.first)).days // 7
    tenure = None if not need else {"weeks_required": need["weeks"], "weeks_done": weeks_done,
                                    "swings_required": need["swings"], "swings_done": st.eligible,
                                    "met": weeks_done >= need["weeks"] and st.eligible >= need["swings"]}
    weeks_met = config["gate_weeks"] if tier_open else min(st.streak[tier], config["gate_weeks"])
    score_ready = tier_open or (weeks_met >= config["gate_weeks"] and (tenure is None or tenure["met"]))
    progress = min(rp_progress, 1.0 if tier_open else min(rating_progress, weeks_met / config["gate_weeks"] if gate > 0 else 1.0))
    return {"name": rank_name(target), "rp_needed": max(0, need_rp - st.rp), "rp_progress": rp_progress,
            "rating_needed": None if tier_open or gate <= 0 else gate,
            "rp_required": need_rp, "rp_current": st.rp, "points_met": points_met,
            "rating_required": gate if gate > 0 else None, "rating_current": rating,
            "rating_met": tier_open or rating_met_now, "rating_progress": rating_progress,
            "rating_gap": None if tier_open or rating_met_now or rating is None else round(gate - rating, 2),
            "tier_open": tier_open, "weeks_required": config["gate_weeks"], "weeks_met": weeks_met,
            "tenure": tenure, "next_evaluation": next_eval.isoformat() if next_eval else None,
            "progress": round(progress, 3), "ready": points_met and score_ready}


__all__ = ["DEFAULT_CONFIG", "DISCLAIMER", "DIVISIONS", "RULES_VERSION", "TIERS", "compute", "eligibility", "load_config",
           "rank_name"]
