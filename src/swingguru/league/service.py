"""The weekly league: placement, live standings, and the Monday reset (close the week, promote / relegate, form
the new groups). Everything is idempotent: the reset can run any number of times, from any request, and
produces the same result once (the week row's status, changed inside one write transaction, is the guard).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from . import names as names_mod
from . import rivals as rivals_mod
from . import rules
from .store import LeagueStore

JOIN_MIN_SWINGS = 5              # the provisional rank is confirmed after 5 counted swings
ACTIVE_DAYS = 14                 # players with no swing for 2 weeks are not placed until they return
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,18}[A-Za-z0-9]$")
RESERVED = ("rival", "bot", "swing guru", "swingguru", "admin", "official", "support", "staff", "moderator")


class LeagueError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _iso(day: datetime) -> str:
    return day.date().isoformat()


def _day(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def _order(week: str, key: str) -> str:
    return hashlib.sha256(f"{week}:{key}".encode()).hexdigest()


def _dumps(value) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def validate_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not NAME_RE.match(name):
        raise LeagueError(422, "invalid_name", "Use 3 to 20 letters or numbers (spaces, dots, dashes and underscores "
                                               "are fine inside the name).")
    low = name.lower()
    if any(word in low for word in RESERVED):
        raise LeagueError(422, "invalid_name", "Please choose a different name.")
    return name


class League:
    def __init__(self, store: LeagueStore, history_for: Callable[[str], Sequence[dict]], *,
                 custom_names: bool = False, on_week_closed: Callable[[datetime], object] | None = None) -> None:
        """``history_for(user_id)`` returns the user's complete swings ``{id, created_at, record}``.
        ``on_week_closed(now)`` runs after a week is closed (the API takes a backup there)."""

        self.store = store
        self.history_for = history_for
        self.custom_names = custom_names
        self.on_week_closed = on_week_closed
        self.upgrade_rivals()

    def upgrade_rivals(self) -> int:
        """One-time, idempotent: give rivals created before names v2 an ordinary name and a v2 persona.

        Ids, divisions, memberships and closed-week results are untouched, so league history is preserved.
        Returns how many rivals were upgraded.
        """

        changed = 0
        for r in self.store.rivals():
            if r["persona"].get("version", 1) >= rivals_mod.PERSONA_VERSION and not r["display_name"].startswith("Rival · "):
                continue
            name = self._free_name(r["created_index"], exclude_rival=r["id"])
            p = rivals_mod.persona(r["created_index"], r["created_division"], r["persona"].get("created_week")
                                   or _iso(rules.week_start(rules.parse(r["created_at"]))), seed=r["persona"].get("seed"))
            with self.store.tx() as db:
                db.execute("update league_rivals set display_name = ?, persona = ? where id = ?", (name, _dumps(p), r["id"]))
            changed += 1
        # A rival placed in the open week has its baseline frozen from its persona's simulated history. If that was
        # an older persona, re-freeze it from the current one, so an upgrade can't hand it a spurious improvement or
        # fix bonus. Rivals only: a real golfer's frozen baseline is never touched; closed weeks stay as they were.
        week = self.store.latest_week()
        if week and week["status"] == "open":
            start = _day(week["week_start"])
            for m in self.store.week_members(week["week_start"]):
                if m["kind"] != "rival" or m["base"].get("persona_version") == rivals_mod.PERSONA_VERSION:
                    continue
                r = self.store.rival(m["member_key"])
                if not r:
                    continue
                base = self._rival_base(r, m["division"], start)
                with self.store.tx() as db:
                    db.execute("update league_members set base = ? where week_start = ? and member_key = ? and kind = 'rival'",
                               (_dumps(base), week["week_start"], m["member_key"]))
                changed += 1
        return changed

    def _free_name(self, index: int, *, exclude_rival: str | None = None, taken: set[str] | None = None) -> str:
        """The rival's deterministic name, or the next deterministic alternative if a player or rival has it."""

        used = {n.lower() for n in (taken or set())}
        used |= {p["display_name"].lower() for p in self.store.profiles()}
        used |= {r["display_name"].lower() for r in self.store.rivals() if r["id"] != exclude_rival}
        for k in range(50):
            candidate = names_mod.name_for(index + 7919 * k)
            if candidate.lower() not in used:
                return candidate
        raise LeagueError(503, "temporarily_unavailable", "Please try again in a moment.")

    # -- the weekly reset -----------------------------------------------------------

    def ensure_week(self, now: datetime) -> str:
        current = _iso(rules.league_week(now))
        latest = self.store.latest_week()
        if latest and latest["week_start"] >= current:
            return latest["week_start"]
        if latest and latest["status"] == "open":
            self.close_week(latest["week_start"], now)
        self.form_week(current, now)
        return current

    def close_week(self, week: str, now: datetime) -> None:
        """Final standings, outcomes, new divisions. Idempotent (a closed week is never closed again)."""

        state = self.store.week(week)
        if not state or state["status"] != "open":
            return
        start = _day(week)
        end = start + timedelta(days=7)
        finals: dict[str, list[dict]] = {}
        for m in self.store.week_members(week):
            finals.setdefault(m["group_id"], []).append({**self._standing(m, start, end), "member": m,
                                                         "key": m["member_key"]})
        updates = []
        for rows in finals.values():
            ranked = rules.rank_members(rows)
            division = ranked[0]["member"]["division"]
            for pos, (row, outcome) in enumerate(zip(ranked, rules.outcomes(division, [r["lp"] for r in ranked])), 1):
                updates.append((row, pos, outcome))
        with self.store.tx() as db:
            again = db.execute("select status from league_weeks where week_start = ?", (week,)).fetchone()
            if not again or again["status"] != "open":
                return
            for row, pos, outcome in updates:
                m = row["member"]
                db.execute("update league_members set final_lp = ?, final_position = ?, outcome = ?, reached_at = ?, "
                           "breakdown = ? where week_start = ? and member_key = ?",
                           (row["lp"], pos, outcome, row["reached_at"], _dumps(row["breakdown"]), week, m["member_key"]))
                new = rules.next_division(m["division"], outcome)
                table, col = ("league_profiles", "user_id") if m["kind"] == "user" else ("league_rivals", "id")
                db.execute(f"update {table} set division = ? where {col} = ?", (new, m["member_key"]))
            db.execute("delete from league_entries where week_start = ?", (week,))
            db.execute("update league_weeks set status = 'closed', closed_at = ? where week_start = ?",
                       (now.isoformat(timespec="seconds"), week))
        if self.on_week_closed:                 # only reached by the request that actually closed the week
            try:
                self.on_week_closed(now)
            except Exception:
                pass

    def form_week(self, week: str, now: datetime) -> None:
        if self.store.week(week):
            return
        start = _day(week)
        placed: dict[str, list[tuple[str, dict]]] = {}
        for p in self.store.profiles():
            history = self.history_for(p["user_id"])
            if not self._active(p, history, start):
                continue
            base = rules.baseline(history, p["division"], start)
            placed.setdefault(p["division"], []).append((p["user_id"], base))
        new_rivals: list[dict] = []
        groups: list[tuple[str, str, int]] = []
        members: list[tuple] = []
        next_index = self.store.rival_count()
        stamp = now.isoformat(timespec="seconds")
        for division in rules.DIVISIONS:
            users = sorted(placed.get(division, []), key=lambda u: _order(week, u[0]))
            if not users:
                continue
            k = math.ceil(len(users) / rules.GROUP_SIZE)
            roster = sorted(self.store.rivals(division), key=lambda r: _order(week, r["id"]))
            for seq in range(k):
                chunk = users[seq::k]
                gid = f"{week}:{division}:{seq + 1}"
                groups.append((gid, division, seq + 1))
                for uid, base in chunk:
                    members.append((week, uid, "user", gid, division, _dumps(base), stamp))
                for _ in range(rules.GROUP_SIZE - len(chunk)):
                    if roster:
                        r = roster.pop()
                    else:
                        r = self._new_rival(next_index, division, start, new_rivals)
                        next_index += 1
                        new_rivals.append(r)
                    members.append((week, r["id"], "rival", gid, division, _dumps(self._rival_base(r, division, start)),
                                    stamp))
        with self.store.tx() as db:
            if db.execute("insert or ignore into league_weeks (week_start, status, formed_at) values (?, 'open', ?)",
                          (week, stamp)).rowcount == 0:
                return          # another request formed this week first
            for r in new_rivals:
                db.execute("insert into league_rivals (id, display_name, persona, division, created_division, "
                           "created_index, created_at) values (?, ?, ?, ?, ?, ?, ?)",
                           (r["id"], r["display_name"], _dumps(r["persona"]), r["division"], r["division"],
                            r["created_index"], stamp))
            for gid, division, seq in groups:
                db.execute("insert into league_groups (id, week_start, division, seq) values (?, ?, ?, ?)",
                           (gid, week, division, seq))
            db.executemany("insert into league_members (week_start, member_key, kind, group_id, division, base, joined_at) "
                           "values (?, ?, ?, ?, ?, ?, ?)", members)

    # -- placement -------------------------------------------------------------

    def place(self, user_id: str, now: datetime) -> dict:
        """Put a joined player into this week's league (mid-week joiners and returning players).

        Real golfers come first, and nobody's standings jump mid-week: the joiner takes the place of a practice
        rival that has not scored yet this week (0 LP, so the table above it is unchanged), in the group of their
        division with the most real golfers. If no group has such a place, a new group is started and filled
        with rivals. Rivals therefore thin out naturally as real golfers join; at the next weekly reset groups
        are re-formed from real golfers first (``form_week``).
        """

        week = self.ensure_week(now)
        existing = self.store.member(week, user_id)
        if existing:
            return existing
        profile = self.store.profile(user_id)
        if not profile:
            raise LeagueError(404, "not_joined", "Join the league first.")
        division = profile["division"]
        start = _day(week)
        base = rules.baseline(self.history_for(user_id), division, start)
        swap = None
        group_id = None
        candidates = []
        for g in self.store.groups(week):
            if g["division"] != division:
                continue
            members = self.store.group_members(g["id"])
            candidates.append((-sum(m["kind"] == "user" for m in members), g["seq"], g["id"], members))
        for _, _, gid, members in sorted(candidates):
            idle = [m for m in members if m["kind"] == "rival" and self._standing(m, start, now)["lp"] == 0]
            if idle:
                swap, group_id = min(idle, key=lambda m: m["member_key"]), gid
                break
        stamp = now.isoformat(timespec="seconds")
        new_rivals: list[dict] = []
        rows: list[tuple] = []
        new_group = None
        if group_id is None:
            seq = 1 + max([g["seq"] for g in self.store.groups(week) if g["division"] == division], default=0)
            group_id = f"{week}:{division}:{seq}"
            new_group = (group_id, division, seq)
            used = {m["member_key"] for m in self.store.week_members(week)}
            roster = [r for r in self.store.rivals(division) if r["id"] not in used]
            next_index = self.store.rival_count()
            for _ in range(rules.GROUP_SIZE - 1):
                if roster:
                    r = roster.pop(0)
                else:
                    r = self._new_rival(next_index, division, start, new_rivals)
                    next_index += 1
                    new_rivals.append(r)
                rows.append((week, r["id"], "rival", group_id, division, _dumps(self._rival_base(r, division, start)),
                             stamp))
        with self.store.tx() as db:
            if db.execute("select 1 from league_members where week_start = ? and member_key = ?",
                          (week, user_id)).fetchone():
                return self.store.member(week, user_id)
            if swap is not None:
                gone = db.execute("delete from league_members where week_start = ? and member_key = ? and kind = 'rival'",
                                  (week, swap["member_key"])).rowcount
                if gone == 0:            # a concurrent join took that place; try again
                    raise _Retry()
            for r in new_rivals:
                db.execute("insert into league_rivals (id, display_name, persona, division, created_division, "
                           "created_index, created_at) values (?, ?, ?, ?, ?, ?, ?)",
                           (r["id"], r["display_name"], _dumps(r["persona"]), division, division, r["created_index"], stamp))
            if new_group:
                db.execute("insert into league_groups (id, week_start, division, seq) values (?, ?, ?, ?)",
                           (*new_group[:1], week, *new_group[1:]))
            db.executemany("insert into league_members (week_start, member_key, kind, group_id, division, base, joined_at) "
                           "values (?, ?, ?, ?, ?, ?, ?)", rows + [(week, user_id, "user", group_id, division,
                                                                    _dumps(base), stamp)])
        return self.store.member(week, user_id)

    def _place(self, user_id: str, now: datetime) -> dict:
        for _ in range(5):
            try:
                return self.place(user_id, now)
            except _Retry:
                continue
        raise LeagueError(503, "temporarily_unavailable", "Please try again in a moment.")

    # -- API operations -------------------------------------------------------------

    def _chosen_name(self, display_name: str) -> str:
        """League names are chosen from generated suggestions (no free text, so no user-generated content to
        moderate: App Store Guideline 1.2). ``custom_names`` (tests and tools only) allows validated free text."""

        if self.custom_names:
            return validate_name(display_name)
        name = " ".join((display_name or "").split())
        if not names_mod.is_generated(name):
            raise LeagueError(422, "invalid_name", "Please choose one of the suggested names.")
        return next(n for n in names_mod._sequence() if n.lower() == name.lower())

    def name_suggestions(self, user_id: str, page: int = 0) -> list[str]:
        taken = {p["display_name"] for p in self.store.profiles()} | {r["display_name"] for r in self.store.rivals()}
        return names_mod.suggestions(user_id, max(0, page), taken)

    def join(self, user_id: str, display_name: str, now: datetime) -> dict:
        name = self._chosen_name(display_name)
        if self.store.profile(user_id):
            return self.view(user_id, now)
        counted = len(rules.counted(self.history_for(user_id)))
        if counted < JOIN_MIN_SWINGS:
            raise LeagueError(409, "not_enough_swings",
                              f"Analyse {JOIN_MIN_SWINGS - counted} more swing{'s' if JOIN_MIN_SWINGS - counted != 1 else ''} "
                              "to join the league.")
        if self.store.name_taken(name, user_id):
            raise LeagueError(409, "name_taken", "That name is already taken. Please choose another.")
        with self.store.tx() as db:
            db.execute("insert or ignore into league_profiles (user_id, display_name, division, joined_at) "
                       "values (?, ?, 'Bronze', ?)", (user_id, name, now.isoformat(timespec="seconds")))
        self._place(user_id, now)
        return self.view(user_id, now)

    def rename(self, user_id: str, display_name: str, now: datetime) -> dict:
        name = self._chosen_name(display_name)
        if not self.store.profile(user_id):
            raise LeagueError(404, "not_joined", "Join the league first.")
        if self.store.name_taken(name, user_id):
            raise LeagueError(409, "name_taken", "That name is already taken. Please choose another.")
        with self.store.tx() as db:
            db.execute("update league_profiles set display_name = ? where user_id = ?", (name, user_id))
        return self.view(user_id, now)

    def leave(self, user_id: str) -> None:
        self.store.purge_user(user_id)

    def swing_deleted(self, user_id: str, swing: dict, now: datetime) -> None:
        """Before a swing is deleted: keep it as a withdrawn entry if it belongs to the open week."""

        if not self.store.profile(user_id) or not swing.get("record"):
            return
        week = self.store.latest_week()
        if not week or week["status"] != "open":
            return
        start = _day(week["week_start"])
        at = rules.parse(swing["created_at"])
        if start <= at < start + timedelta(days=7):
            self.store.capture(user_id, week["week_start"], [{"id": swing["id"], "at": at.isoformat(),
                                                              "record": swing["record"]}], withdrawn=True)
            self.store.withdraw(user_id, swing["id"])

    def view(self, user_id: str, now: datetime) -> dict:
        week = self.ensure_week(now)
        start = _day(week)
        end = start + timedelta(days=7)
        out: dict = {"week": {"start": start.isoformat(), "end": end.isoformat(),
                              "closes_at": (end + rules.GRACE).isoformat()},
                     "rules": rules.public_rules(), "rival_label": rivals_mod.LABEL,
                     "rival_profile_line": rivals_mod.PROFILE_LINE}
        profile = self.store.profile(user_id)
        if not profile:
            counted = len(rules.counted(self.history_for(user_id)))
            out.update(joined=False, counted_swings=counted, join_min_swings=JOIN_MIN_SWINGS,
                       can_join=counted >= JOIN_MIN_SWINGS)
            return out
        me = self._place(user_id, now)
        profile = self.store.profile(user_id)
        rows = []
        names = {p["user_id"]: p["display_name"] for p in self.store.profiles()}
        for m in self.store.group_members(me["group_id"]):
            st = self._standing(m, start, min(now, end))
            # The live table ranks on confirmed points; weekly bonuses stay pending until the reset (close_week
            # ranks on the full weekly total, so final results are unchanged).
            rows.append({**st, "lp": st["confirmed"], "reached_at": st["confirmed_reached_at"], "week_lp": st["lp"],
                         "member": m, "key": m["member_key"]})
        ranked = rules.rank_members(rows)
        division = me["division"]
        members, you = [], None
        for pos, row in enumerate(ranked, 1):
            m = row["member"]
            is_rival = m["kind"] == "rival"
            is_you = m["member_key"] == user_id
            name = (self._rival_name(m["member_key"]) if is_rival else names.get(m["member_key"], "Golfer"))
            members.append({"position": pos, "name": name, "lp": row["lp"], "is_rival": is_rival, "is_you": is_you,
                            "zone": rules.zone(division, pos, len(ranked))})
            if is_you:
                you = {"position": pos, "lp": row["lp"], "pending": row["pending"], "breakdown": row["breakdown"],
                       "practice_days": row["practice_days"], "counted_swings": row["counted_swings"],
                       "eligible_swings": row["eligible_swings"], "week_median": row["week_median"],
                       "baseline": row["baseline"], "baseline_default": bool(me["base"].get("default")),
                       "fixed_checks": row["fixed_checks"], "zone": rules.zone(division, pos, len(ranked))}
        last = self.store.last_result(user_id, week)
        out.update(joined=True, profile={"display_name": profile["display_name"], "division": profile["division"],
                                         "joined_at": profile["joined_at"]},
                   group={"division": division, "size": len(ranked), "rival_count": sum(m["is_rival"] for m in members),
                          "promote": rules.DIVISION_RULES[division][3], "relegate": rules.DIVISION_RULES[division][4],
                          "members": members},
                   you=you,
                   last_result=None if not last else {
                       "week_start": _day(last["week_start"]).isoformat(), "division": last["division"],
                       "position": last["final_position"], "lp": last["final_lp"], "outcome": last["outcome"],
                       "new_division": rules.next_division(last["division"], last["outcome"])},
                   history=[{"week_start": _day(r["week_start"]).isoformat(), "division": r["division"],
                             "position": r["final_position"], "lp": r["final_lp"], "outcome": r["outcome"]}
                            for r in self.store.results(user_id)])
        return out

    # -- internals --------------------------------------------------------------

    def _active(self, profile: dict, history: Sequence[dict], start: datetime) -> bool:
        since = start - timedelta(days=ACTIVE_DAYS)
        if rules.parse(profile["joined_at"]) >= since:
            return True
        return any(rules.parse(h["created_at"]) >= since for h in history)

    def _standing(self, member: dict, start: datetime, until: datetime) -> dict:
        division = member["division"]
        if member["kind"] == "rival":
            r = self.store.rival(member["member_key"])
            entries = rivals_mod.week_swings(r["persona"], start) if r else []
            return rules.week_lp(entries, division, member["base"], now=until)
        uid = member["member_key"]
        week = _iso(start)
        end = start + timedelta(days=7)
        fresh = [{"id": h["id"], "at": rules.parse(h["created_at"]).isoformat(), "record": h["record"]}
                 for h in self.history_for(uid) if start <= rules.parse(h["created_at"]) < end]
        self.store.capture(uid, week, fresh)
        return rules.week_lp(self.store.entries(uid, week), division, member["base"], now=until)

    def _rival_base(self, rival: dict, division: str, start: datetime) -> dict:
        base = rules.baseline(rivals_mod.history_before(rival["persona"], start), division, start)
        return {**base, "persona_version": rival["persona"].get("version", 1)}

    def _new_rival(self, index: int, division: str, start: datetime, batch: Sequence[dict] = ()) -> dict:
        rid, _ = rivals_mod.identity(index)
        name = self._free_name(index, taken={r["display_name"] for r in batch})
        return {"id": rid, "display_name": name, "division": division, "created_index": index,
                "persona": rivals_mod.persona(index, division, start)}

    def _rival_name(self, rival_id: str) -> str:
        r = self.store.rival(rival_id)
        return r["display_name"] if r else "Rival"


class _Retry(Exception):
    pass


__all__ = ["JOIN_MIN_SWINGS", "League", "LeagueError", "validate_name"]
