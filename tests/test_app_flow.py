"""Phase 4 product flow: summary copy, withheld checks, upload jobs, club tags."""

from __future__ import annotations

import io
import json

import pytest

from swingguru import app_api, app_jobs
from swingguru.app_model import friendly_reason, summary_text, withheld


def _c(phase, label, name, status):
    return {"phase": phase, "phase_label": label, "name": name, "status": status}


def test_summary_text_describes_verdicts_without_ranking_them() -> None:
    items = [_c("address", "Address", "Hip hinge", "good"), _c("address", "Address", "Knee bend", "good"),
             _c("mid_back", "Mid-Back", "Plane", "attention"), _c("impact", "Impact", "Extension", "problem"),
             _c("impact", "Impact", "Head", "attention")]
    text = summary_text(items)
    assert text.startswith("Your set-up was solid: all 2 set-up checks were in range.")
    assert "Impact extension was clearly out of range" in text
    assert "Mid-Back plane and Impact head movement were slightly out" in text
    for word in ("priority", "first", "focus", "should", "most important"):
        assert word not in text.lower()


def test_summary_text_all_good_and_withheld() -> None:
    items = [_c("address", "Address", "Hip hinge", "good"), _c("top", "Top", "Posture", "good")]
    assert "Every check we could measure was in range." in summary_text(items)
    assert "1 check couldn't be measured" in summary_text(items, [{"id": "x"}])
    assert summary_text([], [{"id": "x"}]).startswith("We couldn't measure enough")


def test_withheld_lists_hidden_checkpoints_with_plain_reasons() -> None:
    analysis = {"phases": [{"phase": "impact", "checks": []}],
                "hidden": [{"phase": "impact", "kind": "head", "reason": "the camera moved during the swing, so ..."},
                           {"phase": "impact", "kind": "head", "reason": "duplicate"},
                           {"phase": "takeaway", "kind": "head", "reason": "the camera moved"}]}  # not a checkpoint
    rows = withheld(analysis)
    assert [r["id"] for r in rows] == ["impact.head"]
    assert rows[0]["name"] == "Head" and "phone moved" in rows[0]["reason"]
    assert "club" in friendly_reason("club tracking not run")


def _analysis(signals=None, phases=6, guided=True):
    return {"camera_check": {"signals": {k: {"status": v} for k, v in (signals or {}).items()}},
            "phases": [{"frame_index": i if i < phases else None, "checks": [{"guided": guided}]} for i in range(6)]}


@pytest.mark.parametrize("signals,expected", [
    ({"orientation_width": "INVALID"}, "camera_angle"),
    ({"hands_in_front": "INVALID"}, "camera_angle"),
    ({"full_body": "INVALID"}, "framing"),
    ({"golfer_size": "INVALID"}, "too_far"),
    ({"camera_motion": "INVALID"}, None),  # a moving camera is corrected or withheld, not rejected
])
def test_rejections_follow_the_camera_check(signals, expected) -> None:
    assert app_jobs.reject_reason(_analysis(signals)) == expected


def test_rejects_when_the_swing_was_not_found() -> None:
    assert app_jobs.reject_reason(_analysis(phases=3)) == "no_swing"
    assert app_jobs.reject_reason(_analysis(guided=False)) == "no_swing"
    assert app_jobs.reject_reason(_analysis()) is None


def test_unreadable_and_empty_uploads_fail_cleanly(tmp_path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    assert app_jobs.probe(bad) is None
    assert app_jobs.start_job(io.BytesIO(b""), 0, file_name="x.mp4", modified_ms=None)["error"] == "unreadable"
    assert app_jobs.start_job(io.BytesIO(b""), app_jobs.MAX_BYTES + 1, file_name="x.mp4", modified_ms=None)["error"] == "too_large"
    for code in ("unreadable", "no_swing", "no_clear_swing", "camera_angle", "framing", "too_far", "failed"):
        title, advice = app_jobs.ERRORS[code]
        assert title and advice and "RTMW" not in advice and "OWL" not in advice


def test_the_same_file_is_not_analysed_twice(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_jobs, "JOBS", tmp_path / "jobs")
    monkeypatch.setattr(app_jobs, "SWINGS", tmp_path / "swings")
    monkeypatch.setattr(app_jobs, "META", tmp_path / "meta.json")
    payload = b"a swing video" * 50
    import hashlib

    (tmp_path / "swings" / "s1" / "bundle").mkdir(parents=True)
    (tmp_path / "swings" / "s1" / "bundle" / "analysis.json").write_text("{}")
    app_jobs.update_meta("s1", sha1=hashlib.sha1(payload).hexdigest())
    started = []
    monkeypatch.setattr(app_jobs.threading, "Thread", lambda *a, **k: started.append(1))
    job = app_jobs.start_job(io.BytesIO(payload), len(payload), file_name="again.mp4", modified_ms=None)
    assert job["state"] == "done" and job["swing_id"] == "s1" and job["duplicate"] and not started


def test_club_tags_are_validated(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app_jobs, "META", tmp_path / "meta.json")
    monkeypatch.setattr(app_api, "bundle_dirs", lambda: {"sw": tmp_path})
    assert app_api.set_club("sw", "Driver") == (200, {"id": "sw", "club": "Driver"})
    assert app_jobs.read_meta()["sw"]["club"] == "Driver"
    assert app_api.set_club("sw", "Putter")[0] == 400
    assert app_api.set_club("nope", "Driver")[0] == 404
    body = json.dumps({"club": None}).encode()
    status, _ = app_api.handle_post("/api/swings/sw/meta", io.BytesIO(body), {"Content-Length": str(len(body))})
    assert status == 200 and app_jobs.read_meta()["sw"]["club"] is None


@pytest.mark.skipif(not app_api.bundle_dirs(), reason="no analysed swings in var/")
def test_real_summary_has_breakdown_headline_and_no_takeaway_rows() -> None:
    swing_id = next(iter(app_api.bundle_dirs()))
    s = app_api.summary(swing_id, demo=False)
    assert s["replay"] == f"#/replay/{swing_id}"
    assert s["headline"]["total"] == s["counts"]["total"] and s["headline"]["phases_analysed"] >= 4
    phases = [g["phase"] for g in s["breakdown"]]
    assert "takeaway" not in phases and phases == sorted(phases, key=["address", "mid_back", "top", "downswing", "impact"].index)
    assert s["score"]["version"] == "swing_score_v3" and s["score"]["source"] == "model" and s["summary_text"]
    assert len(s["recommended"]) <= 2
