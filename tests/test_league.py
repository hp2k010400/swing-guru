"""Weekly leagues: LP rules, idempotency, rivals, the weekly reset and simulated seasons (time travel)."""

from __future__ import annotations

import random
import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swingguru.league import League, LeagueError, LeagueStore
from swingguru.league import names, rivals, rules
from swingguru.scoring.config import CHECKS
from swingguru.service.api import create_app
from swingguru.service.config import ConfigError, Settings, validate
from swingguru.service.repo import SqliteRepo
from swingguru.service.storage import LocalStorage
from swingguru.swing_history import fingerprint
from test_service import A, B, FakeRunner

MON = datetime(2026, 11, 16, tzinfo=timezone.utc)          # a Monday
IDS = [c.id for c in CHECKS]


def rec(score: float, *, salt: int = 0, status: dict | None = None, measured: int = 8, long_side: int = 1920) -> dict:
    """A history record; ``salt`` makes a distinct swing, the same salt and score is the same video again."""

    rnd = random.Random(salt)
    checks = [{"id": i, "status": (status or {}).get(i, "good"), "value": round(rnd.uniform(0, 60), 2), "unit": "deg"}
              for i in IDS]
    return {"version": 2, "score": {"total": score, "measured": measured}, "video": {"long_side": long_side},
            "checks": checks, "fingerprint": fingerprint(score, checks)}


def entry(day: int, score: float, *, salt: int | None = None, hour: int = 9, minute: int = 0, **kw) -> dict:
    at = MON + timedelta(days=day, hours=hour, minutes=minute)
    salt = salt if salt is not None else day * 1000 + hour * 60 + minute
    return {"id": f"s{salt}", "at": at.isoformat(), "record": rec(score, salt=salt, **kw)}


BASE7 = {"baseline": 7.0, "statuses": {}, "default": True}


# -- the LP function ------------------------------------------------------------------

def test_practice_days_are_capped_at_three() -> None:
    out = rules.week_lp([entry(d, 6.0) for d in range(5)], "Bronze", BASE7)
    assert out["breakdown"]["practice_day"] == 3 * 6 and out["practice_days"] == 5
    assert out["breakdown"]["swing"] == 5 * 2


def test_counted_swings_per_day_and_per_week() -> None:
    six_today = [entry(0, 6.0, minute=m) for m in range(6)]
    assert rules.week_lp(six_today, "Bronze", BASE7)["breakdown"]["swing"] == 3 * 2
    assert rules.week_lp(six_today, "Platinum", {**BASE7, "baseline": 8.2})["breakdown"]["swing"] == 2 * 2
    heavy = [entry(d, 6.0, minute=m) for d in range(7) for m in range(10)]
    out = rules.week_lp(heavy, "Bronze", BASE7)
    assert out["breakdown"]["swing"] == 9 * 2 and out["breakdown"]["practice_day"] == 18


def test_improvement_is_judged_once_a_week() -> None:
    three = [entry(d, 7.5) for d in range(3)]
    assert rules.week_lp(three, "Bronze", BASE7)["breakdown"]["improvement"] == 40
    thirty = [entry(d % 7, 7.5, minute=d) for d in range(30)]
    assert rules.week_lp(thirty, "Bronze", BASE7)["breakdown"]["improvement"] == 40      # more swings, no more chances
    two = [entry(d, 9.0) for d in range(2)]
    assert rules.week_lp(two, "Bronze", BASE7)["breakdown"]["improvement"] == 0         # needs 3 eligible swings
    big = [entry(d, 9.5) for d in range(3)]
    assert rules.week_lp(big, "Bronze", BASE7)["breakdown"]["improvement"] == 60        # capped


def test_practice_only_swings_earn_practice_but_not_improvement() -> None:
    low_res = [entry(d, 9.0, long_side=720) for d in range(4)]
    out = rules.week_lp(low_res, "Bronze", BASE7)
    assert out["breakdown"]["improvement"] == 0 and out["breakdown"]["consistent_week"] == 0
    assert out["breakdown"]["practice_day"] == 18 and out["eligible_swings"] == 0


def test_check_back_in_range_needs_two_in_a_row_and_is_capped() -> None:
    bad = {i: "attention" for i in IDS}
    base = {**BASE7, "statuses": bad}
    one = rules.week_lp([entry(0, 6.0)], "Bronze", base)
    assert one["breakdown"]["fix"] == 0
    two = rules.week_lp([entry(0, 6.0), entry(1, 6.0)], "Bronze", base)
    assert two["breakdown"]["fix"] == 3 * 15                         # 8 checks fixed, at most 3 awarded
    broken = rules.week_lp([entry(0, 6.0), entry(1, 6.0, status=bad), entry(2, 6.0)], "Bronze", base)
    assert broken["breakdown"]["fix"] == 0


def test_consistent_week() -> None:
    steady = [entry(d, 7.2) for d in range(3)]
    assert rules.week_lp(steady, "Bronze", BASE7)["breakdown"]["consistent_week"] == 20
    assert rules.week_lp(steady[:2] + [entry(1, 7.2, minute=5)], "Bronze", BASE7)["breakdown"]["consistent_week"] == 0


def test_reuploads_retries_and_duplicates_earn_nothing_extra() -> None:
    once = [entry(0, 7.5, salt=1)]
    lp_once = rules.week_lp(once, "Bronze", BASE7)["lp"]
    # The same video uploaded again (new swing ids) and the same swing listed twice (a retry).
    again = once + [{**entry(0, 7.5, salt=1, minute=m), "id": f"re{m}"} for m in (5, 10)] + once
    assert rules.week_lp(again, "Bronze", BASE7)["lp"] == lp_once
    # Re-uploads on later days don't create practice days either.
    later = once + [{**entry(d, 7.5, salt=1), "id": f"d{d}"} for d in (1, 2, 3)]
    assert rules.week_lp(later, "Bronze", BASE7)["lp"] == lp_once


def test_deleting_swings_never_raises_lp() -> None:
    week = [entry(0, 6.0), entry(1, 6.0), entry(2, 8.0), entry(3, 8.0), entry(4, 8.0)]
    before = rules.week_lp(week, "Bronze", BASE7)
    after = rules.week_lp([{**e, "withdrawn": e["record"]["score"]["total"] < 7} for e in week], "Bronze", BASE7)
    assert after["breakdown"]["improvement"] == before["breakdown"]["improvement"]
    assert after["lp"] < before["lp"]


def test_delete_and_reupload_counts_once() -> None:
    first = entry(0, 7.0, salt=5)
    gone = {**first, "withdrawn": True}
    back = {**entry(0, 7.0, salt=5, minute=30), "id": "again"}
    assert rules.week_lp([gone, back], "Bronze", BASE7)["lp"] == rules.week_lp([first], "Bronze", BASE7)["lp"]


def test_baseline_uses_division_quantile_and_default() -> None:
    history = [{"id": f"h{i}", "created_at": (MON - timedelta(days=1, minutes=i)).isoformat(),
                "record": rec(6.0 + i * 0.1, salt=100 + i)} for i in range(20)]
    bronze, diamond = rules.baseline(history, "Bronze", MON), rules.baseline(history, "Diamond", MON)
    assert bronze["baseline"] < diamond["baseline"] and not bronze["default"]
    assert rules.baseline(history[:4], "Gold", MON) == {"baseline": 7.8, "statuses": rules.baseline(history[:4], "Gold", MON)["statuses"],
                                                        "swings": 4, "default": True}
    # Swings this week or later never enter the baseline.
    assert rules.baseline(history, "Bronze", MON - timedelta(days=7))["swings"] == 0


def test_outcomes_per_division() -> None:
    lp = list(range(100, 80, -1))
    assert rules.outcomes("Bronze", lp).count("promoted") == 7 and "relegated" not in rules.outcomes("Bronze", lp)
    silver = rules.outcomes("Silver", lp[:16] + [0, 0, 0, 0])
    assert silver.count("promoted") == 6 and silver.count("relegated") == 4      # bottom 3, plus every 0 LP
    diamond = rules.outcomes("Diamond", lp)
    assert diamond.count("medal") == 3 and diamond.count("relegated") == 5 and "promoted" not in diamond
    assert rules.outcomes("Bronze", [10] * 5 + [0] * 15).count("promoted") == 5   # 0 LP never promotes


def test_ties_go_to_whoever_reached_the_total_first() -> None:
    rows = [{"key": "b", "lp": 50, "reached_at": "2026-11-17T10:00:00+00:00"},
            {"key": "a", "lp": 50, "reached_at": "2026-11-18T10:00:00+00:00"}, {"key": "c", "lp": 60, "reached_at": None}]
    assert [r["key"] for r in rules.rank_members(rows)] == ["c", "b", "a"]


# -- rivals -----------------------------------------------------------------------------

def test_rivals_are_persistent_deterministic_and_flagged_simulated() -> None:
    rid, name = rivals.identity(0)
    assert rid == "rival:00000" and name == rivals.identity(0)[1] and not name.startswith("Rival")
    p = rivals.persona(3, "Gold", MON)
    assert p == rivals.persona(3, "Gold", MON) and p["style"] in rivals.STYLES
    assert rivals.week_swings(p, MON) == rivals.week_swings(p, MON)
    assert all(s["record"]["simulated"] for s in rivals.week_swings(p, MON))


def test_rival_lp_appears_through_the_week_under_the_same_caps() -> None:
    for i in range(30):
        p = rivals.persona(i, "Diamond", MON)
        full = rivals.standing(p, "Diamond", MON)
        seen = [rivals.standing(p, "Diamond", MON, now=MON + timedelta(days=d))["lp"] for d in range(8)]
        assert seen[0] == 0 and seen[-1] == full["lp"]
        assert full["breakdown"]["practice_day"] <= 18 and full["breakdown"]["swing"] <= 18


def test_division_level_rivals_are_harder() -> None:
    lows = [rivals.persona(i, "Bronze", MON)["skill"] for i in range(200)]
    highs = [rivals.persona(i, "Diamond", MON)["skill"] for i in range(200)]
    assert statistics.mean(highs) - statistics.mean(lows) > 1.2 and max(lows) < min(highs) + 0.6


# -- the service: weekly reset, promotion / relegation, simulated seasons --------------

class World:
    """Fake swing histories for a set of golfers, with persona habits (time travel friendly)."""

    def __init__(self, n: int, seed: int = 7) -> None:
        self.rnd = random.Random(seed)
        self.swings: dict[str, list[dict]] = {}
        kinds = [("casual", 7.0, 1.5, 2, 0.0), ("regular", 7.6, 3.5, 3, 0.01), ("improver", 6.9, 4.0, 3, 0.07),
                 ("grinder", 7.4, 6.5, 10, 0.005)]
        self.people = []
        for i in range(n):
            kind, skill, days, per, trend = kinds[i % 4]
            self.people.append({"id": f"user-{kind}-{i:03d}", "kind": kind, "skill": self.rnd.gauss(skill, 0.2),
                                "days": days, "per": per, "trend": trend})
        self.salt = 0

    def history(self, uid: str) -> list[dict]:
        return list(self.swings.get(uid, []))

    def add(self, uid: str, at: datetime, score: float) -> None:
        self.salt += 1
        self.swings.setdefault(uid, []).append({"id": f"{uid}:{self.salt}", "created_at": at.isoformat(),
                                                "record": rec(round(score, 1), salt=self.salt)})

    def practise_week(self, week: datetime) -> None:
        for p in self.people:
            if self.rnd.random() < 0.08:
                continue
            days = min(7, max(0, round(self.rnd.gauss(p["days"], 0.8))))
            for d in sorted(self.rnd.sample(range(7), days)):
                for k in range(p["per"]):
                    at = week + timedelta(days=d, hours=18, minutes=3 * k)
                    self.add(p["id"], at, min(10.0, max(3.0, self.rnd.gauss(p["skill"], 0.7))))
            p["skill"] = min(9.6, p["skill"] + p["trend"])


def _league(tmp_path, world: World) -> League:
    return League(LeagueStore(tmp_path / "league.sqlite3"), world.history, custom_names=True)


def _snapshot(league: League) -> list[tuple]:
    rows = league.store.read("select week_start, member_key, group_id, division, final_lp, final_position, outcome "
                             "from league_members order by week_start, member_key")
    return [tuple(r.values()) for r in rows] + [tuple(r.values()) for r in league.store.read(
        "select user_id, division from league_profiles order by user_id")]


def _join_all(league: League, world: World, when: datetime) -> list[dict]:
    """Everyone with a confirmed provisional rank (5 counted swings) joins; the rest are refused."""

    joined = []
    for p in world.people:
        if len(rules.counted(world.history(p["id"]))) >= 5:
            league.join(p["id"], p["id"][5:].replace("-", " ")[:20], when)
            joined.append(p)
        else:
            with pytest.raises(LeagueError):
                league.join(p["id"], p["id"][5:].replace("-", " ")[:20], when)
    return joined


def test_join_needs_five_counted_swings_and_a_valid_name(tmp_path) -> None:
    world = World(1)
    league = _league(tmp_path, world)
    uid = world.people[0]["id"]
    for i in range(4):
        world.add(uid, MON - timedelta(days=3, minutes=i), 7.0)
    with pytest.raises(LeagueError) as e:
        league.join(uid, "Harry", MON)
    assert e.value.code == "not_enough_swings"
    world.add(uid, MON - timedelta(days=2), 7.0)
    for bad in ("x", "Rival Fox", "Swing Guru team", "a" * 30, "<script>"):
        with pytest.raises(LeagueError):
            league.join(uid, bad, MON)
    out = league.join(uid, "Harry P", MON)
    assert out["joined"] and out["profile"]["division"] == "Bronze" and out["group"]["size"] == 20
    assert out["group"]["rival_count"] == 19
    names = [m["name"] for m in out["group"]["members"]]
    assert len(set(names)) == 20 and not any(n.startswith("Rival") for n in names)
    assert [m["is_you"] for m in out["group"]["members"]].count(True) == 1


def test_simulated_season(tmp_path) -> None:
    world = World(60)
    league = _league(tmp_path, world)
    for w in range(4):                                       # some history before the league opens
        world.practise_week(MON - timedelta(days=7 * (4 - w)))
    joined = _join_all(league, world, MON + timedelta(hours=1))
    assert len(joined) >= 50
    assert {p["division"] for p in league.store.profiles()} == {"Bronze"}            # everyone starts in Bronze
    weeks = 12
    rival_counts = []
    for w in range(weeks):
        week = MON + timedelta(days=7 * w)
        world.practise_week(week)
        mid = week + timedelta(days=3, hours=12)
        view = league.view(joined[0]["id"], mid)
        assert view["group"]["size"] == 20
        # The reset: the first request after Sunday midnight + grace closes the week and forms the next.
        reset = week + timedelta(days=7, minutes=31)
        league.ensure_week(reset)
        snap = _snapshot(league)
        league.ensure_week(reset + timedelta(minutes=1))
        league.close_week(rules.week_start(week).date().isoformat(), reset)
        assert _snapshot(league) == snap                                               # idempotent
        closed = league.store.week_members(week.date().isoformat())
        for gid in {m["group_id"] for m in closed}:
            group = sorted((m for m in closed if m["group_id"] == gid), key=lambda m: m["final_position"])
            division = group[0]["division"]
            assert len(group) == 20
            promote, relegate = rules.DIVISION_RULES[division][3], rules.DIVISION_RULES[division][4]
            assert [m["final_position"] for m in group] == list(range(1, 21))
            assert [m["final_lp"] for m in group] == sorted((m["final_lp"] for m in group), reverse=True)
            if division != "Diamond":
                assert sum(m["outcome"] == "promoted" for m in group) == sum(m["final_lp"] > 0 for m in group[:promote])
            if division == "Bronze":
                assert not any(m["outcome"] == "relegated" for m in group)
            else:
                assert all(m["outcome"] == "relegated" for m in group[20 - relegate:])
                assert all(m["outcome"] == "relegated" for m in group if m["final_lp"] == 0)
        rival_counts.append(league.store.rival_count())
    final = {p["user_id"]: p["division"] for p in league.store.profiles()}
    level = {d: i for i, d in enumerate(rules.DIVISIONS)}
    by_kind = {k: statistics.mean(level[final[p["id"]]] for p in joined if p["kind"] == k)
               for k in ("casual", "regular", "improver", "grinder")}
    assert by_kind["improver"] > by_kind["casual"] and by_kind["regular"] > by_kind["casual"]
    assert any(d != "Bronze" for d in final.values())
    # Rivals are persistent: the roster grows only when divisions need more, never by a full new set each week.
    assert rival_counts[-1] < 19 * len(rules.DIVISIONS) * 3
    first = league.store.rival("rival:00000")
    assert first["display_name"] == names.name_for(0) and first["persona"]["version"] == 2


def test_reuploading_cannot_farm_a_league_week(tmp_path) -> None:
    world = World(2)
    league = _league(tmp_path, world)
    a, b = world.people[0]["id"], world.people[1]["id"]
    for uid in (a, b):
        for i in range(5):
            world.add(uid, MON - timedelta(days=2, minutes=i), 7.0)
    league.join(a, "Golfer A", MON + timedelta(hours=1))
    league.join(b, "Golfer B", MON + timedelta(hours=1))
    world.add(a, MON + timedelta(days=1, hours=9), 7.4)
    world.add(b, MON + timedelta(days=1, hours=9), 7.4)
    original = world.swings[b][-1]
    for k in range(10):                                         # b uploads the same video 10 more times
        world.swings[b].append({**original, "id": f"dup{k}", "created_at": (MON + timedelta(days=2 + k % 4)).isoformat()})
    now = MON + timedelta(days=6)
    for _ in range(3):                                          # repeated reads / retries change nothing
        view_a, view_b = league.view(a, now), league.view(b, now)
    assert view_a["you"]["lp"] == view_b["you"]["lp"]


def test_deleting_a_swing_mid_week_cannot_raise_lp(tmp_path) -> None:
    world = World(1)
    league = _league(tmp_path, world)
    uid = world.people[0]["id"]
    for i in range(5):
        world.add(uid, MON - timedelta(days=2, minutes=i), 7.0)
    league.join(uid, "Golfer", MON + timedelta(hours=1))
    for d, s in enumerate((6.0, 6.0, 8.0, 8.0)):
        world.add(uid, MON + timedelta(days=d, hours=10), s)
    now = MON + timedelta(days=5)
    before = league.view(uid, now)["you"]
    for swing in [s for s in world.swings[uid] if s["record"]["score"]["total"] == 6.0]:
        league.swing_deleted(uid, swing, now)
        world.swings[uid].remove(swing)
    after = league.view(uid, now)["you"]
    assert after["lp"] <= before["lp"] and after["breakdown"]["improvement"] == before["breakdown"]["improvement"]


def test_inactive_players_rejoin_when_they_return(tmp_path) -> None:
    world = World(1)
    league = _league(tmp_path, world)
    uid = world.people[0]["id"]
    for i in range(5):
        world.add(uid, MON - timedelta(days=2, minutes=i), 7.0)
    league.join(uid, "Golfer", MON + timedelta(hours=1))
    later = MON + timedelta(days=7 * 5, hours=2)                # five quiet weeks later
    league.ensure_week(later)
    assert league.store.member(rules.league_week(later).date().isoformat(), uid) is None   # not placed while away
    world.add(uid, later, 7.2)
    assert league.view(uid, later + timedelta(minutes=5))["you"]["counted_swings"] == 1


# -- the API -----------------------------------------------------------------------------

def _client(tmp_path, **overrides) -> TestClient:
    settings = replace(Settings(), data_dir=tmp_path / "svc", public_url="http://testserver",
                       **{"league_enabled": True, **overrides})
    repo, storage = SqliteRepo(tmp_path / "svc" / "db.sqlite3"), LocalStorage(settings)
    client = TestClient(create_app(settings, repo=repo, storage=storage, start_worker=False, runner=FakeRunner()))
    client.app.state.clock = lambda: MON + timedelta(days=2)
    return client


def _seed(client, user: str, n: int, start: datetime, salt: int = 0) -> list[str]:
    ids = []
    for i in range(n):
        sid = f"00000000-0000-4000-8000-{salt + i:012d}"
        client.app.state.repo.insert({"id": sid, "user_id": user, "status": "complete", "content_type": "video/mp4",
                                      "created_at": (start + timedelta(hours=i)).isoformat(),
                                      "summary": {"record": rec(7.0 + 0.1 * (i % 5), salt=salt + i)}})
        ids.append(sid)
    return ids


def _suggested(client, headers, page=0) -> list[str]:
    out = client.get(f"/v1/league/names?page={page}", headers=headers)
    assert out.status_code == 200
    return out.json()["names"]


def test_league_api_join_view_rename_and_privacy(tmp_path) -> None:
    client = _client(tmp_path)
    assert client.get("/v1/league", headers=A).json()["joined"] is False
    picks_a = _suggested(client, A)
    assert len(picks_a) == 4 and len(set(picks_a)) == 4 and all(names.is_generated(n) for n in picks_a)
    assert _suggested(client, A) == picks_a and _suggested(client, A, 1) != picks_a       # stable per page
    assert client.post("/v1/league/join", headers=A, json={"display_name": picks_a[0]}).status_code == 409   # < 5 swings
    _seed(client, "user-aaaaaaaa", 6, MON - timedelta(days=3))
    _seed(client, "user-bbbbbbbb", 6, MON - timedelta(days=3), salt=500)
    # Free text is refused: names are chosen from suggestions (no user-generated content, Guideline 1.2).
    for typed in ("Harry", "Rival X", "<b>hi</b>", "Some Rude Word"):
        assert client.post("/v1/league/join", headers=A, json={"display_name": typed}).json()["error"]["code"] == "invalid_name"
    out = client.post("/v1/league/join", headers=A, json={"display_name": picks_a[0].lower()}).json()
    assert out["joined"] and out["group"]["size"] == 20 and out["profile"]["display_name"] == picks_a[0]
    assert picks_a[0] not in _suggested(client, B) + _suggested(client, B, 1)              # taken names aren't offered
    assert client.post("/v1/league/join", headers=B, json={"display_name": picks_a[0]}).json()["error"]["code"] == "name_taken"
    pick_b = _suggested(client, B)[0]
    out_b = client.post("/v1/league/join", headers=B, json={"display_name": pick_b}).json()
    assert out_b["group"]["rival_count"] == 18                     # same Bronze group, a rival made room
    real = [m["name"] for m in out_b["group"]["members"] if not m["is_rival"]]
    assert sorted(real) == sorted([picks_a[0], pick_b])
    assert "user-aaaaaaaa" not in str(out_b)                       # other players' ids are never exposed
    assert client.patch("/v1/league/profile", headers=A, json={"display_name": "H P"}).status_code == 422
    new = _suggested(client, A, 2)[0]
    assert client.patch("/v1/league/profile", headers=A, json={"display_name": new}).json()["profile"]["display_name"] == new


def _join(client, headers) -> str:
    name = _suggested(client, headers)[0]
    assert client.post("/v1/league/join", headers=headers, json={"display_name": name}).status_code == 200
    return name


def test_this_weeks_swings_and_deletes(tmp_path) -> None:
    client = _client(tmp_path)
    _seed(client, "user-aaaaaaaa", 6, MON - timedelta(days=3))
    _join(client, A)
    ids = _seed(client, "user-aaaaaaaa", 2, MON + timedelta(hours=9), salt=900)
    you = client.get("/v1/league", headers=A).json()["you"]
    assert you["counted_swings"] == 2 and you["breakdown"]["swing"] == 4
    assert client.delete(f"/v1/swings/{ids[0]}", headers=A).status_code == 204
    after = client.get("/v1/league", headers=A).json()["you"]
    assert after["counted_swings"] == 1 and after["lp"] <= you["lp"]


def test_account_deletion_removes_league_data(tmp_path) -> None:
    client = _client(tmp_path)
    _seed(client, "user-aaaaaaaa", 6, MON - timedelta(days=3))
    _join(client, A)
    league = client.app.state.league
    assert league.store.profile("user-aaaaaaaa")
    client.delete("/v1/account", headers=A)
    assert league.store.profile("user-aaaaaaaa") is None
    assert not league.store.read("select 1 from league_members where member_key = ?", ("user-aaaaaaaa",))
    assert not league.store.read("select 1 from league_entries where user_id = ?", ("user-aaaaaaaa",))


PROD = dict(env="production", auth_mode="supabase", storage_mode="supabase", db_mode="supabase",
            supabase_url="https://x.supabase.co", supabase_service_key="s", public_url="https://x")


def test_league_is_off_unless_enabled_and_needs_a_volume_in_production(tmp_path) -> None:
    client = _client(tmp_path, league_enabled=False)
    assert client.get("/v1/league", headers=A).status_code == 404
    assert client.get("/v1/league/names", headers=A).status_code == 404
    with pytest.raises(ConfigError, match="SG_LEAGUE_DB"):          # no volume path: refused
        validate(Settings(**PROD, league_enabled=True))
    with pytest.raises(ConfigError, match="absolute"):
        validate(Settings(**PROD, league_enabled=True, league_db="league.sqlite3"))
    validate(Settings(**PROD, league_enabled=True, league_db="/data/league.sqlite3"))   # the approved setup
    validate(Settings(**PROD))                                                          # leagues off: unchanged


def test_league_store_uses_the_volume_path(tmp_path) -> None:
    db = tmp_path / "volume" / "league.sqlite3"
    client = _client(tmp_path, league_db=str(db))
    _seed(client, "user-aaaaaaaa", 6, MON - timedelta(days=3))
    _join(client, A)
    assert db.is_file() and not (tmp_path / "svc" / "league.sqlite3").exists()
