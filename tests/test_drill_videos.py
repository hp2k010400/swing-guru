"""Drill video catalogue: validation, API exposure, older-app compatibility and the offline import workflow."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swingguru import app_drills, drill_videos
from swingguru.app_drills import DRILLS
from swingguru.drill_videos_admin import ProbeResult, check_all, import_rows, read_rows, save, set_status, video_id

IDS = [d.id for d in DRILLS]
OK = ProbeResult(True, True, "A title", "A coach", "UC" + "x" * 22, 120)


def _catalogue() -> dict:
    return json.loads(drill_videos.DEFAULT_PATH.read_text(encoding="utf-8"))


def test_shipped_catalogue_is_valid_and_covers_every_drill() -> None:
    data = _catalogue()
    assert drill_videos.validate(data, IDS) == []
    assert {v["drill_id"] for v in data["videos"]} == set(IDS)
    active = [v for v in data["videos"] if v["status"] == "active"]
    assert len(active) == 7 and all(v["embeddable"] and v["suitability"] == "transcript_verified" for v in active)
    assert all(v["rights"] == "youtube_embed" and v["provider"] == "youtube" for v in data["videos"])
    quiet = next(v for v in data["videos"] if v["drill_id"] == "quiet-head")
    assert quiet["status"] == "disabled" and "conflicts" in quiet["disabled_reason"]   # Harry's decision, kept for the record


def test_drills_api_adds_demo_video_but_leaves_legacy_video_null() -> None:
    drills = {d["id"]: d for g in app_drills.catalogue()["groups"] for d in g["drills"]}
    assert all(d["video"] is None for d in drills.values())      # builds <= 20 play `video` natively as an MP4
    pump = drills["pump-drill"]["demo_video"]
    assert pump["video_id"] == "xFOXu1djU-A" and pump["embed_url"].startswith("https://www.youtube-nocookie.com/embed/")
    assert pump["creator"] == "Jim Estes" and pump["title"]
    assert drills["quiet-head"]["demo_video"] is None             # disabled: written steps only
    assert pump["duration_s"] == 102 and pump["aspect"] == "16:9"
    assert drills["butt-to-wall"]["demo_video"]["aspect"] == "9:16"           # a vertical Short
    posture = drills["posture-hold"]["demo_video"]
    assert posture["start_s"] == 49 and "start=49" in posture["embed_url"] and posture["watch_url"].endswith("&t=49s")
    # Steps and coaching are unchanged by videos.
    assert list(drills["pump-drill"]["steps"]) == list(next(d for d in DRILLS if d.id == "pump-drill").steps)


def test_analysis_summaries_do_not_carry_videos() -> None:
    assert "demo_video" not in app_drills.drills_for("impact.early_extension")[0]


def test_validation_rejects_bad_records() -> None:
    base = {"drill_id": "pump-drill", "provider": "youtube", "video_id": "xFOXu1djU-A", "title": "t", "creator": "c",
            "status": "active", "rights": "youtube_embed", "suitability": "transcript_verified", "checked_at": "2026-11-09",
            "embeddable": True, "start_s": 0, "duration_s": 100}
    assert drill_videos.validate({"videos": [base]}, IDS) == []
    bad = {
        "unknown drill": {**base, "drill_id": "nope"},
        "bad id": {**base, "video_id": "short"},
        "not embeddable": {**base, "embeddable": False},
        "unverified active": {**base, "suitability": "partial_match"},
        "start past end": {**base, "start_s": 100},
        "rights": {**base, "rights": "downloaded"},
    }
    for name, record in bad.items():
        assert drill_videos.validate({"videos": [record]}, IDS), name
    assert drill_videos.validate({"videos": [base, base]}, IDS)                       # duplicate
    assert drill_videos.validate({"videos": [base, {**base, "video_id": "aaaaaaaaaaa"}]}, IDS)   # two active


def test_broken_catalogue_never_breaks_drills(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "videos.json"
    broken.write_text('{"videos": [{"drill_id": "nope"}]}', encoding="utf-8")
    monkeypatch.setenv("SG_DRILL_VIDEOS_PATH", str(broken))
    drills = [d for g in app_drills.catalogue()["groups"] for d in g["drills"]]
    assert len(drills) == len(DRILLS) and all(d["demo_video"] is None for d in drills)


def test_override_path_swaps_videos_without_a_deploy(tmp_path, monkeypatch) -> None:
    data = _catalogue()
    for v in data["videos"]:
        if v["drill_id"] == "pump-drill":
            v["status"] = "disabled"
    path = tmp_path / "videos.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("SG_DRILL_VIDEOS_PATH", str(path))
    assert "pump-drill" not in drill_videos.active_by_drill() and "butt-to-wall" in drill_videos.active_by_drill()


def test_video_id_from_urls() -> None:
    for url in ("xFOXu1djU-A", "https://www.youtube.com/watch?v=xFOXu1djU-A&t=3s", "https://youtu.be/xFOXu1djU-A",
                "https://www.youtube.com/shorts/xFOXu1djU-A", "https://www.youtube.com/embed/xFOXu1djU-A?start=4"):
        assert video_id(url) == "xFOXu1djU-A"
    assert video_id("https://example.com/video") is None


def test_import_adds_updates_replaces_and_never_duplicates() -> None:
    data = _catalogue()
    rows = [{"drill_id": "pump-drill", "url": "https://youtu.be/CAef1L7rh0s", "status": "active", "start_s": "29",
             "suitability": "transcript_verified", "match": "Pump drill from the ground up"},
            {"drill_id": "made-up", "video_id": "CAef1L7rh0s"},
            {"drill_id": "pump-drill", "video_id": "not a video"}]
    new, report = import_rows(data, rows, lambda vid: OK, today="2026-11-10")
    pumps = [v for v in new["videos"] if v["drill_id"] == "pump-drill"]
    assert len(pumps) == 2
    assert [v["video_id"] for v in pumps if v["status"] == "active"] == ["CAef1L7rh0s"]
    old = next(v for v in pumps if v["video_id"] == "xFOXu1djU-A")
    assert old["status"] == "disabled" and "replaced by CAef1L7rh0s" in old["disabled_reason"]
    assert any("unknown drill id" in r for r in report) and any("no valid YouTube" in r for r in report)
    again, _ = import_rows(new, rows[:1], lambda vid: OK, today="2026-11-11")
    assert len([v for v in again["videos"] if v["drill_id"] == "pump-drill"]) == 2          # updated, not duplicated
    assert drill_videos.validate(again, IDS) == []


def test_import_of_an_unembeddable_video_is_disabled() -> None:
    blocked = ProbeResult(False, False, reason="embedding disabled by the owner")
    new, report = import_rows(_catalogue(), [{"drill_id": "quiet-head", "video_id": "t_OYJIiNcgs", "status": "active",
                                              "suitability": "transcript_verified"}], lambda vid: blocked)
    rec = next(v for v in new["videos"] if v["video_id"] == "t_OYJIiNcgs")
    assert rec["status"] == "disabled" and rec["disabled_reason"] == "embedding disabled by the owner"
    assert drill_videos.validate(new, IDS) == []


def test_check_disables_removed_videos_but_keeps_the_record() -> None:
    def probe(vid):
        return ProbeResult(False, False, reason="removed or private") if vid == "fOpifmb8KZo" else OK

    new, report = check_all(_catalogue(), probe, today="2026-11-10")
    wall = next(v for v in new["videos"] if v["drill_id"] == "butt-to-wall")
    assert wall["status"] == "disabled" and wall["disabled_reason"] == "removed or private"
    assert len(new["videos"]) == len(_catalogue()["videos"])


def test_enable_disable_and_save(tmp_path) -> None:
    data, _ = set_status(_catalogue(), "pump-drill", "disabled", reason="testing")
    assert next(v for v in data["videos"] if v["drill_id"] == "pump-drill")["status"] == "disabled"
    data, _ = set_status(data, "pump-drill", "active", video="xFOXu1djU-A")
    assert next(v for v in data["videos"] if v["drill_id"] == "pump-drill")["status"] == "active"
    path = tmp_path / "out.json"
    assert save(data, path) == [] and drill_videos.validate(json.loads(path.read_text(encoding="utf-8")), IDS) == []
    # Enabling a partial match is refused at save time (content must be verified first).
    risky, _ = set_status(data, "quiet-head", "active", video="hfsPeSP87EM")
    assert save(risky, tmp_path / "nope.json") and not (tmp_path / "nope.json").exists()


def test_read_rows_csv(tmp_path) -> None:
    f = tmp_path / "rows.csv"
    f.write_text("drill_id,url,start_s,status\npump-drill,https://youtu.be/xFOXu1djU-A,5,active\n", encoding="utf-8")
    assert read_rows(f) == [{"drill_id": "pump-drill", "url": "https://youtu.be/xFOXu1djU-A", "start_s": "5", "status": "active"}]


@pytest.fixture(autouse=True)
def _clear_cache():
    drill_videos._load.cache_clear()
    yield
    drill_videos._load.cache_clear()
