"""Manifest intake, score-snapshot validation and the blind submission lifecycle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swingguru.coach_review import fixtures, store


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "review.sqlite3"
    conn = store.connect(path)
    store.init_db(conn)
    conn.close()
    return path


def _import(tmp_path: Path, specs: list[dict], *, batch: str = "pilot", **kwargs) -> tuple[Path, dict]:
    db = _db(tmp_path) if not (tmp_path / "review.sqlite3").exists() else tmp_path / "review.sqlite3"
    entries = fixtures.write_batch(tmp_path / f"{batch}-media", specs)
    prepared = store.prepare_entries(entries)
    conn = store.connect(db)
    try:
        summary = store.import_batch(conn, batch, prepared, **kwargs)
    finally:
        conn.close()
    return db, summary


def test_import_snapshots_the_stored_score_and_never_recomputes(tmp_path: Path) -> None:
    score = fixtures.synthetic_score(total=3.3, version="swing_score_v1")
    db, summary = _import(tmp_path, [{"swing_id": "s1", "score": score, "club": "7 Iron"}])
    conn = store.connect(db)
    try:
        row = conn.execute("SELECT * FROM swing_item").fetchone()
    finally:
        conn.close()
    assert summary["items"] == 1 and summary["scores_stored"] == 1 and summary["scores_available"] == 1
    assert summary["scores_unavailable"] == 0 and summary["replaced"] is False
    assert row["score_stored"] == 1 and row["score_available"] == 1
    assert row["score_total"] == 3.3  # the stored value, not a recomputation
    assert json.loads(row["score_json"])["total"] == 3.3
    assert row["model_version"] == "swing_score_v1"
    assert row["club"] == "7 Iron"
    assert len(row["public_id"]) == 16
    assert row["video_mtime_ns"] > 0


def test_missing_snapshot_and_unavailable_snapshot_are_distinguished(tmp_path: Path) -> None:
    unavailable = fixtures.synthetic_score(available=False, total=None, excluded_ids=["impact.head"])
    db, summary = _import(tmp_path, [{"swing_id": "none", "score": None},
                                     {"swing_id": "unavailable", "score": unavailable}])
    conn = store.connect(db)
    try:
        rows = {row["swing_id"]: row for row in conn.execute("SELECT * FROM swing_item")}
    finally:
        conn.close()
    assert summary["scores_stored"] == 1 and summary["scores_available"] == 0 and summary["scores_unavailable"] == 2
    assert rows["none"]["score_stored"] == 0 and rows["none"]["score_json"] is None
    assert rows["unavailable"]["score_stored"] == 1 and rows["unavailable"]["score_available"] == 0
    assert json.loads(rows["unavailable"]["score_json"])["available"] is False


@pytest.mark.parametrize("mutate, expected", [
    (lambda score: score.update({"total": float("nan")}), "total"),
    (lambda score: score.update({"total": float("inf")}), "total"),
    (lambda score: score.update({"total": 99.0}), "total"),
    (lambda score: score.update({"total": "7.0"}), "total"),
    (lambda score: score["checks"][0].update({"weight": "1"}), "weight"),
    (lambda score: score["checks"][0].update({"weight": 0}), "weight"),
    (lambda score: score["checks"][0].update({"points": 42.0}), "points"),
    (lambda score: score["checks"][0].update({"confidence": "LOW"}), "confidence"),
    (lambda score: score.update({"measured": 99}), "measured"),
    (lambda score: score.update({"coverage": 0.1}), "coverage"),
    (lambda score: score.update({"version": ""}), "version"),
    (lambda score: score.update({"confidence": {"high": 1, "medium": 1, "label": "SURE"}}), "label"),
    (lambda score: score.update({"available": "yes"}), "boolean"),
])
def test_malformed_available_snapshots_are_rejected(tmp_path: Path, mutate, expected: str) -> None:
    entries = fixtures.write_batch(tmp_path / "media", [{"swing_id": "s1", "score": None}])
    payload = json.loads(Path(entries[0]["analysis"]).read_text(encoding="utf-8"))
    score = fixtures.synthetic_score(total=7.0)
    mutate(score)
    payload["swing_score"] = score
    Path(entries[0]["analysis"]).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(store.ManifestError) as error:
        store.prepare_entries(entries)
    assert expected in " ".join(error.value.problems)


def test_unavailable_snapshot_with_a_total_is_rejected(tmp_path: Path) -> None:
    entries = fixtures.write_batch(tmp_path / "media", [{"swing_id": "s1", "score": None}])
    payload = json.loads(Path(entries[0]["analysis"]).read_text(encoding="utf-8"))
    score = fixtures.synthetic_score(available=False)
    score["total"] = 7.0  # a dishonest snapshot: marked unavailable but carrying a total
    payload["swing_score"] = score
    Path(entries[0]["analysis"]).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(store.ManifestError) as error:
        store.prepare_entries(entries)
    assert "unavailable but carries a total" in " ".join(error.value.problems)


def test_duplicate_check_ids_and_excluded_overlap_are_rejected(tmp_path: Path) -> None:
    entries = fixtures.write_batch(tmp_path / "media", [{"swing_id": "s1", "score": None}])
    payload = json.loads(Path(entries[0]["analysis"]).read_text(encoding="utf-8"))
    score = fixtures.synthetic_score(total=7.0, excluded_ids=["impact.head"])
    score["checks"][1]["id"] = score["checks"][0]["id"]
    payload["swing_score"] = score
    Path(entries[0]["analysis"]).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(store.ManifestError) as error:
        store.prepare_entries(entries)
    joined = " ".join(error.value.problems)
    assert "repeats check id" in joined


def test_duplicate_ids_and_identical_clips_are_rejected_together(tmp_path: Path) -> None:
    entries = fixtures.write_batch(tmp_path / "media", [{"swing_id": "s1", "score": None},
                                                        {"swing_id": "s2", "score": None}])
    entries[1]["swing_id"] = "s1"
    entries.append({"swing_id": "s3", "video": entries[0]["video"], "analysis": entries[0]["analysis"]})
    entries.append({"swing_id": "s4", "video": str(tmp_path / "missing.mp4"), "analysis": entries[0]["analysis"]})
    with pytest.raises(store.ManifestError) as error:
        store.prepare_entries(entries)
    joined = " | ".join(error.value.problems)
    assert "duplicate swing_id" in joined
    assert "video not readable" in joined

    twin = fixtures.write_batch(tmp_path / "twins", [{"swing_id": "a", "score": None}])
    second = fixtures.write_swing(tmp_path / "twins" / "b", swing_id="b", score=None,
                                  video_bytes=Path(twin[0]["video"]).read_bytes())
    with pytest.raises(store.ManifestError) as error:
        store.prepare_entries([twin[0], second])
    assert "byte-identical" in " ".join(error.value.problems)


def test_import_is_all_or_nothing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    conn = store.connect(db)
    try:
        with pytest.raises(store.StoreError):
            store.import_batch(conn, "x" * 200, [{"swing_id": "s1", "video_path": "v", "analysis_path": "a",
                                                  "video_sha256": "0", "video_size": 1, "analysis_sha256": "0"}])
        assert conn.execute("SELECT COUNT(*) AS n FROM batch").fetchone()["n"] == 0
    finally:
        conn.close()


def test_replace_is_refused_once_there_is_evidence(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    same = store.prepare_entries(fixtures.write_batch(tmp_path / "again", [{"swing_id": "s1", "score": None}]))
    conn = store.connect(db)
    try:
        assert store.import_batch(conn, "pilot", same, replace=True)["replaced"] is True  # no evidence yet
        item_id = store.session_view(conn, "pilot", "coachA", "Coach A")["items"][0]["item_id"]
        store.submit_review(conn, "pilot", "coachA", item_id,
                            ratings={"setup": 5, "backswing": 5, "top": 5, "downswing": 5, "overall": 5},
                            confidence="HIGH")
        with pytest.raises(store.StoreError) as error:
            store.import_batch(conn, "pilot", same, replace=True)
        assert "new batch name" in str(error.value)
        assert conn.execute("SELECT COUNT(*) AS n FROM review").fetchone()["n"] == 1
    finally:
        conn.close()


def test_cross_batch_duplicate_swing_ids_and_clips_are_refused(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": None}])
    same_id = store.prepare_entries(fixtures.write_batch(tmp_path / "second", [{"swing_id": "s1", "score": None}]))
    conn = store.connect(db)
    try:
        with pytest.raises(store.StoreError) as error:
            store.import_batch(conn, "pilot-2", same_id)
        assert "already imported" in str(error.value)
        assert conn.execute("SELECT COUNT(*) AS n FROM batch").fetchone()["n"] == 1
    finally:
        conn.close()

    # A different id with byte-identical video is the same swing in disguise.
    original_video = _first_video(tmp_path / "pilot-media")
    twin = fixtures.write_swing(tmp_path / "third" / "twin", swing_id="twin",
                                score=None, video_bytes=original_video.read_bytes())
    prepared = store.prepare_entries([twin])
    conn = store.connect(db)
    try:
        with pytest.raises(store.StoreError) as error:
            store.import_batch(conn, "pilot-3", prepared)
        assert "video file" in str(error.value)
    finally:
        conn.close()


def _first_video(root: Path) -> Path:
    return next(root.glob("*/bundle/swing.mp4"))


def test_video_integrity_detects_drift_at_startup(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    conn = store.connect(db)
    try:
        assert store.video_integrity_problems(conn, "pilot") == []
    finally:
        conn.close()
    video = _first_video(tmp_path / "pilot-media")
    video.write_bytes(video.read_bytes() + b"changed")
    conn = store.connect(db)
    try:
        problems = store.video_integrity_problems(conn, "pilot")
    finally:
        conn.close()
    assert len(problems) == 1 and "size changed" in problems[0]


def test_reviewer_display_name_is_persisted(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": None}])
    conn = store.connect(db)
    try:
        store.remember_reviewer(conn, "pilot", "coachA", "Coach Ada")
        store.remember_reviewer(conn, "pilot", "coachA", "Coach Ada")
        rows = [dict(row) for row in conn.execute("SELECT * FROM reviewer")]
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0]["display_name"] == "Coach Ada"


def test_reviewer_order_is_stable_and_independent(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": f"s{i}", "score": None} for i in range(12)])
    conn = store.connect(db)
    try:
        first = store.session_view(conn, "pilot", "coachA", "Coach A")
        again = store.session_view(conn, "pilot", "coachA", "Coach A")
        other = store.session_view(conn, "pilot", "coachB", "Coach B")
    finally:
        conn.close()
    assert [i["item_id"] for i in first["items"]] == [i["item_id"] for i in again["items"]]
    assert [i["ordinal"] for i in first["items"]] == list(range(1, 13))
    assert [i["item_id"] for i in first["items"]] != [i["item_id"] for i in other["items"]]


def test_blind_submission_locks_and_edits_append_a_revision(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    conn = store.connect(db)
    try:
        item_id = store.session_view(conn, "pilot", "coachA", "Coach A")["items"][0]["item_id"]
        first = store.submit_review(conn, "pilot", "coachA", item_id, ratings={"setup": 1, "backswing": 2, "top": 3,
                                                                               "downswing": 4, "overall": 5},
                                    confidence="LOW", notes="blind")
        assert first["revision"] == 1 and first["blind"] is True
        with pytest.raises(store.StoreError):
            store.submit_review(conn, "pilot", "coachA", item_id,
                                ratings={"setup": 5, "backswing": 5, "top": 5, "downswing": 5, "overall": 5},
                                confidence="HIGH")
        second = store.submit_review(conn, "pilot", "coachA", item_id,
                                     ratings={"setup": 6, "backswing": 6, "top": 6, "downswing": 6, "overall": 6},
                                     confidence="HIGH", notes="after seeing the score", allow_edit=True)
        assert second["revision"] == 2 and second["blind"] is False
        assert second["first_submitted_at"] == first["first_submitted_at"]
        revisions = [dict(row) for row in conn.execute("SELECT * FROM review_revision ORDER BY revision")]
    finally:
        conn.close()
    assert [r["revision"] for r in revisions] == [1, 2]
    assert json.loads(revisions[0]["ratings_json"])["overall"] == 5
    assert revisions[0]["blind"] == 1 and revisions[1]["blind"] == 0


def test_skip_does_not_unlock_and_submitting_clears_it(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    conn = store.connect(db)
    try:
        item_id = store.session_view(conn, "pilot", "coachA", "Coach A")["items"][0]["item_id"]
        store.skip_item(conn, "pilot", "coachA", item_id)
        assert store.item_view(conn, "pilot", "coachA", item_id)["state"] == "skipped"
        with pytest.raises(store.StoreError):
            store.score_view(conn, "pilot", "coachA", item_id)
        store.unskip_item(conn, "pilot", "coachA", item_id)
        assert store.item_view(conn, "pilot", "coachA", item_id)["state"] == "new"
        store.skip_item(conn, "pilot", "coachA", item_id)
        store.submit_review(conn, "pilot", "coachA", item_id,
                            ratings={"setup": 5, "backswing": 5, "top": 5, "downswing": 5, "overall": 5},
                            confidence="MEDIUM")
        assert store.item_view(conn, "pilot", "coachA", item_id)["state"] == "submitted"
        assert store.score_view(conn, "pilot", "coachA", item_id)["stored"] is True
        assert conn.execute("SELECT COUNT(*) AS n FROM skip").fetchone()["n"] == 0
    finally:
        conn.close()


def test_reviewer_isolation_through_the_store(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    conn = store.connect(db)
    try:
        item_id = store.session_view(conn, "pilot", "coachA", "Coach A")["items"][0]["item_id"]
        store.submit_review(conn, "pilot", "coachA", item_id,
                            ratings={"setup": 5, "backswing": 5, "top": 5, "downswing": 5, "overall": 5},
                            confidence="HIGH")
        assert store.score_view(conn, "pilot", "coachA", item_id)["score"] is not None
        with pytest.raises(store.StoreError):
            store.score_view(conn, "pilot", "coachB", item_id)
        assert store.item_view(conn, "pilot", "coachB", item_id)["state"] == "new"
        assert store.item_view(conn, "pilot", "coachB", item_id)["review"] is None
    finally:
        conn.close()


def test_report_dataset_filters_batches_and_rejects_unknown(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}], batch="first")
    entries = fixtures.write_batch(tmp_path / "second-media", [{"swing_id": "s2", "score": None}])
    conn = store.connect(db)
    try:
        store.import_batch(conn, "second", store.prepare_entries(entries))
        filtered = store.report_dataset(conn, "first")
        assert [batch["name"] for batch in filtered["batches"]] == ["first"]
        assert [item["swing_id"] for item in filtered["items"]] == ["s1"]
        assert len(store.report_dataset(conn)["batches"]) == 2
        with pytest.raises(store.StoreError):
            store.report_dataset(conn, "nope")
    finally:
        conn.close()


@pytest.mark.parametrize("payload", [
    {"setup": 7.5, "backswing": 7, "top": 7, "downswing": 7, "overall": 7},
    {"setup": True, "backswing": 7, "top": 7, "downswing": 7, "overall": 7},
    {"setup": 0, "backswing": 7, "top": 7, "downswing": 7, "overall": 7},
    {"setup": 11, "backswing": 7, "top": 7, "downswing": 7, "overall": 7},
    {"setup": "7", "backswing": 7, "top": 7, "downswing": 7, "overall": 7},
    {"setup": 7, "backswing": 7, "top": 7, "downswing": 7},
    {"setup": 7, "backswing": 7, "top": 7, "downswing": 7, "overall": 7, "extra": 1},
])
def test_ratings_must_be_strict_whole_numbers(payload: dict) -> None:
    with pytest.raises(store.StoreError):
        store.validate_ratings(payload)


def test_notes_and_confidence_are_bounded(tmp_path: Path) -> None:
    db, _ = _import(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score()}])
    conn = store.connect(db)
    try:
        item_id = store.session_view(conn, "pilot", "coachA", "Coach A")["items"][0]["item_id"]
        ratings = {"setup": 7, "backswing": 7, "top": 7, "downswing": 7, "overall": 7}
        with pytest.raises(store.StoreError):
            store.submit_review(conn, "pilot", "coachA", item_id, ratings=ratings, confidence="SURE")
        with pytest.raises(store.StoreError):
            store.submit_review(conn, "pilot", "coachA", item_id, ratings=ratings, confidence="HIGH",
                                notes="x" * 3000)
    finally:
        conn.close()
