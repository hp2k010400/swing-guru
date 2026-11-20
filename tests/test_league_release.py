"""Release safeguards for leagues in production: pending weekly bonuses (live totals never fall), backups off the
volume and a verified one-shot restore, and a concurrent weekly reset that still happens exactly once."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from swingguru.league import League, LeagueStore, rivals, rules
from swingguru.league.backup import KEEP, LeagueBackups
from swingguru.service.config import Settings
from swingguru.service.storage import LocalStorage
from test_league import MON, World, _client, _join, _seed
from test_service import A


# -- pending bonuses ------------------------------------------------------------------------

def test_confirmed_points_never_fall_during_the_week_but_the_weekly_total_is_unchanged() -> None:
    saw_total_fall = False
    for i in range(200):
        division = rules.DIVISIONS[i % 5]
        p = rivals.persona(i, division, MON - timedelta(days=35))
        base = rules.baseline(rivals.history_before(p, MON), division, MON)
        entries = rivals.week_swings(p, MON)
        hourly = [rules.week_lp(entries, division, base, now=MON + timedelta(hours=h)) for h in range(0, 7 * 24 + 1, 3)]
        confirmed = [s["confirmed"] for s in hourly]
        assert confirmed == sorted(confirmed)                                   # never falls
        totals = [s["lp"] for s in hourly]
        saw_total_fall |= totals != sorted(totals)
        final = rules.week_lp(entries, division, base)
        assert final["lp"] == final["confirmed"] + final["pending"]             # weekly result unchanged
        assert final["pending"] == final["breakdown"]["improvement"] + final["breakdown"]["consistent_week"]
    assert saw_total_fall           # the old live total did fall sometimes: this is what pending fixes


def test_live_table_shows_confirmed_points_and_the_reset_uses_the_full_week(tmp_path) -> None:
    world = World(1)
    league = League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True)
    uid = world.people[0]["id"]
    for i in range(6):
        world.add(uid, MON - timedelta(days=3, minutes=i), 6.8)
    league.join(uid, "Golfer", MON + timedelta(hours=1))
    for d in range(4):                                           # an improving week: weekly bonuses are earned
        for k in range(3):
            world.add(uid, MON + timedelta(days=d, hours=10, minutes=k), 7.9)
    view = league.view(uid, MON + timedelta(days=5))
    you = view["you"]
    assert you["pending"] > 0
    assert you["lp"] == you["breakdown"]["practice_day"] + you["breakdown"]["swing"] + you["breakdown"]["fix"]
    mine = next(m for m in view["group"]["members"] if m["is_you"])
    assert mine["lp"] == you["lp"]                                # the table shows confirmed points only
    league.ensure_week(MON + timedelta(days=7, minutes=31))      # reset
    final = league.store.member(MON.date().isoformat(), uid)
    assert final["final_lp"] == you["lp"] + you["pending"]       # bonuses added at the reset


# -- backups and restore -------------------------------------------------------------------

def _backups(tmp_path):
    settings = replace(Settings(), data_dir=tmp_path / "svc", public_url="http://testserver")
    store = LeagueStore(tmp_path / "volume" / "league.sqlite3")
    return store, LeagueBackups(store, LocalStorage(settings)), LocalStorage(settings)


def test_backup_is_a_verified_copy_off_the_volume(tmp_path) -> None:
    store, backups, storage = _backups(tmp_path)
    store.set_meta("probe", "before")
    key = backups.backup(MON, "test")
    assert key and key.startswith("league-backups/") and storage.size(key) > 0
    copy = tmp_path / "copy.sqlite3"
    storage.download_to(key, copy)
    assert LeagueStore.verify_file(copy) is None
    assert sqlite3.connect(copy).execute("select value from league_meta where key = 'probe'").fetchone()[0] == "before"


def test_backups_in_the_same_second_never_overwrite_each_other(tmp_path) -> None:
    """Found on Railway staging: the pre-restore and startup backups shared a key and the first was lost."""

    store, backups, storage = _backups(tmp_path)
    store.set_meta("probe", "first")
    a = backups.backup(MON, "pre-restore")
    store.set_meta("probe", "second")
    b = backups.backup(MON, "startup")
    c = backups.backup(MON, "startup")
    assert len({a, b, c}) == 3
    first = tmp_path / "a.sqlite3"
    storage.download_to(a, first)
    assert sqlite3.connect(first).execute("select value from league_meta where key = 'probe'").fetchone()[0] == "first"


def test_daily_backups_and_pruning(tmp_path) -> None:
    store, backups, storage = _backups(tmp_path)
    assert backups.maybe_backup(MON)                                       # first one
    assert backups.maybe_backup(MON + timedelta(hours=23)) is None         # not yet a day
    keys = [backups.maybe_backup(MON + timedelta(days=d)) for d in range(1, KEEP + 5)]
    assert all(keys) and len(backups.keys()) == KEEP
    assert storage.size(backups.keys()[0]) and storage.size(keys[0]) is None    # oldest pruned


def test_restore_is_verified_one_shot_and_saves_the_current_state_first(tmp_path) -> None:
    store, backups, storage = _backups(tmp_path)
    store.set_meta("probe", "good")
    key = backups.backup(MON, "test")
    store.set_meta("probe", "damaged")
    assert backups.restore(key, MON + timedelta(hours=1)) is True
    assert store.meta("probe") == "good"
    assert key in backups.keys() and len(backups.keys()) == 2              # the pre-restore backup is kept
    store.set_meta("probe", "after")
    assert backups.restore(key, MON + timedelta(hours=2)) is False          # restarts don't restore again
    assert store.meta("probe") == "after"


def test_a_bad_backup_is_refused_and_the_database_is_untouched(tmp_path) -> None:
    store, backups, storage = _backups(tmp_path)
    store.set_meta("probe", "live")
    junk = tmp_path / "junk.sqlite3"
    junk.write_bytes(b"not a database at all")
    storage.put_file("league-backups/snap-junk-0001/league.sqlite3", junk, "application/vnd.sqlite3")
    with pytest.raises(RuntimeError, match="refused"):
        backups.restore("league-backups/snap-junk-0001/league.sqlite3", MON)
    assert store.meta("probe") == "live"


def test_server_backs_up_at_startup_and_restores_from_the_variable(tmp_path) -> None:
    db = tmp_path / "volume" / "league.sqlite3"
    client = _client(tmp_path, league_db=str(db))
    _seed(client, "user-aaaaaaaa", 6, MON - timedelta(days=3))
    name = _join(client, A)
    backups = client.app.state.league_backups
    key = backups.backup(MON + timedelta(days=1), "test")
    client.app.state.league.leave("user-aaaaaaaa")                          # "lose" the data
    assert client.app.state.league.store.profile("user-aaaaaaaa") is None
    restored = _client(tmp_path, league_db=str(db), league_restore_key=key)   # redeploy with the variable set
    assert restored.app.state.league.store.profile("user-aaaaaaaa")["display_name"] == name
    again = _client(tmp_path, league_db=str(db), league_restore_key=key)      # a restart doesn't restore twice
    assert len(again.app.state.league_backups.keys()) >= 3                     # startup backups each time


# -- concurrency: the weekly reset happens exactly once ---------------------------------------

def test_concurrent_resets_close_and_form_the_week_once(tmp_path) -> None:
    world = World(25)
    league = League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True)
    for w in range(2):
        world.practise_week(MON - timedelta(days=7 * (2 - w)))
    for p in world.people:
        if len(rules.counted(world.history(p["id"]))) >= 5:
            league.join(p["id"], p["id"][5:].replace("-", " ")[:20], MON + timedelta(hours=1))
    world.practise_week(MON)
    reset = MON + timedelta(days=7, minutes=31)
    errors: list[Exception] = []

    def go():
        try:
            League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True).ensure_week(reset)
        except Exception as e:      # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    store = LeagueStore(tmp_path / "league.sqlite3")
    weeks = store.read("select week_start, status from league_weeks order by week_start")
    assert [w["status"] for w in weeks] == ["closed", "open"]
    closed = store.week_members(MON.date().isoformat())
    assert all(m["outcome"] for m in closed)
    nxt = store.week_members((MON + timedelta(days=7)).date().isoformat())
    assert len({m["member_key"] for m in nxt}) == len(nxt)                   # nobody placed twice
    assert len(nxt) % 20 == 0
