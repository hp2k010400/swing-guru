"""League polish (2026-11-09): realistic rival names, persistent behaviour styles, natural activity, upgrade of
existing rivals without losing history, and real-golfer-first mid-week placement."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

from swingguru.league import League, LeagueError, LeagueStore
from swingguru.league import names, rivals, rules
from test_league import MON, World, rec

PUNS = {"fairway", "divot", "tee", "bunker", "birdie", "eagle", "rival", "fox", "badger", "drake", "tiger", "hawk",
        "lark", "wren", "owl", "puffin", "heron", "falcon", "lynx", "putt", "chip", "wedge", "iron", "par", "bot"}


def test_names_are_ordinary_unique_and_plentiful() -> None:
    first_1000 = [names.name_for(i) for i in range(1000)]
    assert len(set(first_1000)) == 1000                                     # never the same full name twice
    assert len(set(names.name_for(i) for i in range(5000))) == 5000         # far more than 500 available
    assert all(not ({w.lower() for w in n.split()} & PUNS) for n in first_1000)
    assert not any(n.lower() in names.EXCLUDED for n in first_1000)          # no real professional golfers
    female = sum(names.is_female(n) for n in first_1000) / 1000
    assert 0.11 <= female <= 0.19                                            # about 15%
    firsts = [n.split()[0] for n in first_1000[:60]]
    lasts = [n.split()[-1] for n in first_1000[:60]]
    assert firsts != sorted(firsts) and lasts != sorted(lasts)                # no alphabetical ordering
    assert len(set(firsts)) > 35 and len(set(lasts)) > 40                     # a natural mix, not one family
    assert names.name_for(123) == names.name_for(123)                        # deterministic
    assert names.initials("Sophie Anderson") == "SA" and names.initials("Henrik de Vries") == "HV"


def _legacy_store(tmp_path) -> LeagueStore:
    """A store as Build 20-22 left it: rivals named 'Rival · …' with v1 personas, plus a closed week."""

    store = LeagueStore(tmp_path / "league.sqlite3")
    legacy = [("rival:fairway-fox", "Rival · Fairway Fox"), ("rival:tee-tiger", "Rival · Tee Tiger"),
              ("rival:divot-drake", "Rival · Divot Drake")]
    with store.tx() as db:
        db.execute("insert into league_weeks (week_start, status, formed_at, closed_at) values ('2026-11-09', 'closed', 'x', 'y')")
        db.execute("insert into league_groups (id, week_start, division, seq) values ('2026-11-09:Bronze:1', '2026-11-09', 'Bronze', 1)")
        for i, (rid, name) in enumerate(legacy):
            persona = {"seed": f"rival:{i}", "skill": 7.0, "spread": 0.7, "days_per_week": 2.5, "swings_per_session": 3,
                       "trend_per_week": 0.0, "holiday": 0.1, "session_style": "evening", "weekend_bias": 0.3,
                       "weak_checks": [], "created_week": "2026-11-09", "created_division": "Bronze"}
            db.execute("insert into league_rivals (id, display_name, persona, division, created_division, created_index, "
                       "created_at) values (?, ?, ?, 'Silver', 'Bronze', ?, '2026-11-09T00:00:00+00:00')",
                       (rid, name, json.dumps(persona), i))
            db.execute("insert into league_members (week_start, member_key, kind, group_id, division, base, joined_at, "
                       "final_lp, final_position, outcome) values ('2026-11-09', ?, 'rival', '2026-11-09:Bronze:1', 'Bronze', "
                       "'{}', 'x', ?, ?, 'promoted')", (rid, 50 - i, i + 1))
    return store


def test_existing_rivals_are_renamed_without_losing_ids_or_history(tmp_path) -> None:
    store = _legacy_store(tmp_path)
    league = League(store, lambda uid: [])
    rivals_now = {r["id"]: r for r in store.rivals()}
    assert set(rivals_now) == {"rival:fairway-fox", "rival:tee-tiger", "rival:divot-drake"}       # same ids
    assert all(not r["display_name"].startswith("Rival") for r in rivals_now.values())
    assert rivals_now["rival:fairway-fox"]["display_name"] == names.name_for(0)
    assert all(r["persona"]["version"] == 2 and r["division"] == "Silver" for r in rivals_now.values())
    history = store.read("select member_key, final_lp, final_position, outcome from league_members order by member_key")
    assert [h["final_lp"] for h in history] == [48, 50, 49]                                         # untouched
    assert league.upgrade_rivals() == 0                                                             # idempotent
    assert League(store, lambda uid: []).store.rival("rival:tee-tiger")["display_name"] == rivals_now["rival:tee-tiger"]["display_name"]


def test_open_week_rival_baselines_are_refrozen_but_players_are_not(tmp_path) -> None:
    """Upgrading mid-week must not give rivals a spurious improvement/fix bonus from their old baseline."""

    store = _legacy_store(tmp_path)
    old_base = {"baseline": 5.0, "statuses": {c: "problem" for c in ("address.torso_angle", "top.posture")}, "swings": 10}
    user_base = {"baseline": 7.3, "statuses": {}, "swings": 12, "default": False}
    with store.tx() as db:
        db.execute("insert into league_weeks (week_start, status, formed_at) values ('2026-11-16', 'open', 'x')")
        db.execute("insert into league_groups (id, week_start, division, seq) values ('2026-11-16:Silver:1', '2026-11-16', 'Silver', 1)")
        db.execute("insert into league_members (week_start, member_key, kind, group_id, division, base, joined_at) values "
                   "('2026-11-16', 'rival:tee-tiger', 'rival', '2026-11-16:Silver:1', 'Silver', ?, 'x')", (json.dumps(old_base),))
        db.execute("insert into league_members (week_start, member_key, kind, group_id, division, base, joined_at) values "
                   "('2026-11-16', 'user-1', 'user', '2026-11-16:Silver:1', 'Silver', ?, 'x')", (json.dumps(user_base),))
    league = League(store, lambda uid: [])
    rival = store.member("2026-11-16", "rival:tee-tiger")
    assert rival["base"]["persona_version"] == 2 and rival["base"]["baseline"] != 5.0
    assert store.member("2026-11-16", "user-1")["base"] == user_base                  # a player's baseline is untouched
    assert league.upgrade_rivals() == 0                                                 # idempotent


def test_names_never_collide_with_players(tmp_path) -> None:
    store = LeagueStore(tmp_path / "league.sqlite3")
    with store.tx() as db:      # a real golfer already uses the name rival #0 would get
        db.execute("insert into league_profiles (user_id, display_name, division, joined_at) values ('u1', ?, 'Bronze', ?)",
                   (names.name_for(0), MON.isoformat()))
    league = League(store, lambda uid: [])
    r = league._new_rival(0, "Bronze", MON)
    assert r["display_name"] != names.name_for(0) and r["display_name"] == league._new_rival(0, "Bronze", MON)["display_name"]
    with store.tx() as db:
        db.execute("insert into league_rivals (id, display_name, persona, division, created_division, created_index, created_at) "
                   "values ('rival:00001', ?, ?, 'Bronze', 'Bronze', 1, 'x')",
                   (names.name_for(1), json.dumps(rivals.persona(1, "Bronze", MON))))
    assert store.name_taken(names.name_for(1), "someone")                     # players can't take a rival's name


def test_styles_are_varied_and_persistent() -> None:
    styles = Counter(rivals.persona(i, "Gold", MON)["style"] for i in range(1000))
    assert set(styles) == set(rivals.STYLES)
    for style, (weight, *_rest) in rivals.STYLES.items():
        assert abs(styles[style] / 1000 - weight) < 0.05
    assert rivals.persona(42, "Gold", MON) == rivals.persona(42, "Gold", MON)


def test_activity_is_natural_through_the_week_and_within_caps() -> None:
    """300 rivals over 8 weeks: points arrive through the week, not all at once; a few stay at zero;
    totals are varied; caps are never exceeded."""

    share_by_day = {d: [] for d in range(7)}
    sunday_zero, distinct_ratio, caps_ok = [], [], True
    for w in range(8):
        week = MON + timedelta(days=7 * w)
        totals = []
        for i in range(300):
            division = rules.DIVISIONS[i % 5]
            p = rivals.persona(i, division, MON - timedelta(days=35))
            base = rules.baseline(rivals.history_before(p, week), division, week)
            entries = rivals.week_swings(p, week)
            for d in range(7):
                lp = rules.week_lp(entries, division, base, now=week + timedelta(days=d, hours=23, minutes=59))["lp"]
                share_by_day[d].append(lp > 0)
            final = rules.week_lp(entries, division, base)
            b = final["breakdown"]
            caps_ok &= (b["practice_day"] <= 18 and b["swing"] <= 18 and b["improvement"] <= 60 and b["fix"] <= 45
                        and b["consistent_week"] <= 20)
            totals.append(final["lp"])
        sunday_zero.append(sum(t == 0 for t in totals) / len(totals))
        for g in range(0, 300, 20):                      # groups of 20
            group = totals[g:g + 20]
            distinct_ratio.append(len(set(group)) / len(group))
    share = {d: sum(v) / len(v) for d, v in share_by_day.items()}
    assert caps_ok
    assert share[0] < 0.45 and share[0] < share[2] < share[4] < share[6]    # builds through the week
    assert 0.05 <= statistics.mean(sunday_zero) <= 0.30                       # some zero weeks, not most
    assert statistics.mean(distinct_ratio) >= 0.6                             # few identical totals


def test_rival_points_do_not_depend_on_real_players(tmp_path) -> None:
    """Fairness: a rival's points are the same whoever else is in the group."""

    p = rivals.persona(7, "Bronze", MON)
    base = rules.baseline(rivals.history_before(p, MON), "Bronze", MON)
    a = rules.week_lp(rivals.week_swings(p, MON), "Bronze", base)
    b = rivals.standing(p, "Bronze", MON)
    assert a == b


def _joined(tmp_path, n_users: int):
    world = World(n_users)
    league = League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True)
    for p in world.people:
        for i in range(6):
            world.add(p["id"], MON - timedelta(days=3, minutes=i), 7.0)
    return world, league


def test_midweek_joiner_takes_an_idle_rivals_place_without_moving_anyone(tmp_path) -> None:
    world, league = _joined(tmp_path, 2)
    a, b = world.people[0]["id"], world.people[1]["id"]
    when = MON + timedelta(days=3, hours=12)                   # Thursday: some rivals have scored
    first = league.join(a, "Golfer A", when)
    before = {m["name"]: m["lp"] for m in first["group"]["members"] if m["is_rival"] and m["lp"] > 0}
    second = league.join(b, "Golfer B", when)
    after = {m["name"]: m["lp"] for m in second["group"]["members"]}
    assert sum(not m["is_rival"] for m in second["group"]["members"]) == 2       # real golfers together
    assert all(after.get(name) == lp for name, lp in before.items())             # nobody who scored disappeared


def test_rivals_thin_out_as_real_golfers_join(tmp_path) -> None:
    world, league = _joined(tmp_path, 45)
    for p in world.people:
        league.join(p["id"], p["id"][5:].replace("-", " ")[:20], MON + timedelta(hours=1))
    week = MON.date().isoformat()
    members = league.store.week_members(week)
    groups = {m["group_id"] for m in members}
    real = sum(m["kind"] == "user" for m in members)
    assert real == 45
    # Next week the groups are re-formed from real golfers first: 45 golfers -> 3 groups, 15 rivals in total.
    league.ensure_week(MON + timedelta(days=7, minutes=31))
    nxt = league.store.week_members((MON + timedelta(days=7)).date().isoformat())
    assert sum(m["kind"] == "user" for m in nxt) == 45
    assert sum(m["kind"] == "rival" for m in nxt) == 3 * 20 - 45
    assert len(groups) >= 3


def test_views_are_stable_across_refreshes_and_restarts(tmp_path) -> None:
    world, league = _joined(tmp_path, 1)
    uid = world.people[0]["id"]
    league.join(uid, "Golfer", MON + timedelta(hours=1))
    when = MON + timedelta(days=4, hours=19)
    first = league.view(uid, when)
    again = league.view(uid, when)
    restarted = League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True).view(uid, when)
    assert first["group"]["members"] == again["group"]["members"] == restarted["group"]["members"]
