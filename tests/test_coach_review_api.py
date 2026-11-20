"""Real-HTTP blindness, validation and isolation checks for the review server."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swingguru.coach_review import fixtures, store
from swingguru.coach_review.api import RangeError, create_app, parse_range, same_origin

BASE = "http://127.0.0.1:8791"
RATINGS = {"setup": 7, "backswing": 6, "top": 8, "downswing": 5, "overall": 7}


def _client(tmp_path: Path, *, batch: str = "pilot", reviewer: str = "coachA", edit_mode: bool = False,
            token: str = "token-a") -> TestClient:
    app = create_app(db_path=tmp_path / "review.sqlite3", batch=batch, reviewer_id=reviewer,
                     reviewer_name=reviewer.title(), token=token, edit_mode=edit_mode)
    return TestClient(app, base_url=BASE)


@pytest.fixture()
def batch(tmp_path: Path) -> dict:
    specs = [{"swing_id": "s1", "score": fixtures.synthetic_score(total=8.6), "club": "Driver"},
             {"swing_id": "s2", "score": None, "club": "7 Iron"},
             {"swing_id": "s3", "score": fixtures.synthetic_score(total=4.2, excluded_ids=["impact.head"])}]
    summary = fixtures.build_db(tmp_path / "review.sqlite3", "pilot", specs, root=tmp_path / "media")
    return {"tmp_path": tmp_path, "summary": summary}


def _item_ids(client: TestClient) -> list[str]:
    return [item["item_id"] for item in client.get("/api/session").json()["items"]]


def _item_with_club(client: TestClient, club: str) -> str:
    for item_id in _item_ids(client):
        if client.get(f"/api/items/{item_id}").json()["club"] == club:
            return item_id
    raise AssertionError(f"no item with club {club!r}")


def test_session_payload_is_blind_and_minimal(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    response = client.get("/api/session")
    assert response.status_code == 200
    body = response.json()
    assert body["batch"]["name"] == "pilot" and body["batch"]["items"] == 3
    assert body["reviewer"] == {"id": "coachA", "name": "Coacha"}
    text = json.dumps(body).lower()
    for forbidden in ("swing_score", "score", "analysis", "measurement", "coverage", "total", "confidence\"", "var\\",
                      ".mp4", "sha256"):
        assert forbidden not in text, f"{forbidden} leaked into the session payload"
    for item in body["items"]:
        assert set(item) == {"item_id", "ordinal", "club", "state", "revision", "locked", "video"}
        assert item["state"] == "new" and item["revision"] is None and item["locked"] is False
        assert item["video"] == f"/media/{item['item_id']}"
    assert [item["ordinal"] for item in body["items"]] == [1, 2, 3]


def test_item_endpoint_never_returns_a_score(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    response = client.get(f"/api/items/{item_id}")
    assert response.status_code == 200
    assert set(response.json()) == {"item_id", "ordinal", "club", "state", "locked", "video", "review"}
    assert response.json()["review"] is None
    assert "score" not in json.dumps(response.json()).lower()
    assert client.get("/api/items/does-not-exist").status_code == 404


def test_score_stays_locked_until_this_reviewer_submits(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_with_club(client, "Driver")
    locked = client.get(f"/api/items/{item_id}/score")
    assert locked.status_code == 403
    assert "hidden" in locked.json()["error"]
    ok = client.post(f"/api/items/{item_id}/review",
                     json={"ratings": RATINGS, "confidence": "HIGH", "notes": "solid setup"},
                     headers={"X-Coach-Review-Token": "token-a"})
    assert ok.status_code == 200
    assert ok.json()["revision"] == 1 and ok.json()["blind"] is True and ok.json()["locked"] is True
    assert ok.json()["review"]["overall"] == 7
    revealed = client.get(f"/api/items/{item_id}/score")
    assert revealed.status_code == 200
    payload = revealed.json()
    assert payload["stored"] is True and payload["score"]["total"] == 8.6
    assert payload["model_version"] == "swing_score_v3"
    state = client.get(f"/api/items/{item_id}").json()
    assert state["state"] == "submitted" and state["locked"] is True


def test_score_endpoint_explains_a_missing_snapshot(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    target = _item_with_club(client, "7 Iron")
    client.post(f"/api/items/{target}/review", json={"ratings": RATINGS, "confidence": "LOW"},
                headers={"X-Coach-Review-Token": "token-a"})
    payload = client.get(f"/api/items/{target}/score").json()
    assert payload["stored"] is False and payload["score"] is None


def test_resubmission_is_locked_without_edit_mode(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    headers = {"X-Coach-Review-Token": "token-a"}
    client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH"}, headers=headers)
    again = client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH"}, headers=headers)
    assert again.status_code == 409
    assert client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH", "edit": True},
                       headers=headers).status_code == 409


def test_edit_mode_appends_a_post_reveal_revision(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path, edit_mode=True)
    item_id = _item_ids(client)[0]
    headers = {"X-Coach-Review-Token": "token-a"}
    first = client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH"}, headers=headers)
    assert first.status_code == 200 and first.json()["locked"] is False
    second = client.post(f"/api/items/{item_id}/review",
                         json={"ratings": {**RATINGS, "overall": 3}, "confidence": "LOW", "edit": True}, headers=headers)
    assert second.status_code == 200
    assert second.json()["revision"] == 2 and second.json()["blind"] is False
    assert second.json()["first_submitted_at"] == first.json()["first_submitted_at"]


def test_mutations_need_the_token_and_same_origin(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    body = {"ratings": RATINGS, "confidence": "HIGH"}
    assert client.post(f"/api/items/{item_id}/review", json=body).status_code == 403
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "wrong"}).status_code == 403
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "token-a", "Origin": "http://evil.example"}).status_code == 403
    # Another local port, or the same port on a different loopback name, is still cross-origin.
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "token-a",
                                "Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "token-a",
                                "Origin": "http://localhost:8791"}).status_code == 403
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "token-a",
                                "Origin": "https://127.0.0.1:8791"}).status_code == 403
    assert client.post(f"/api/items/{item_id}/review", json=body,
                       headers={"X-Coach-Review-Token": "token-a", "Origin": BASE}).status_code == 200


def test_same_origin_compares_scheme_host_and_port() -> None:
    assert same_origin(None, "127.0.0.1:8791") is True
    assert same_origin(BASE, "127.0.0.1:8791") is True
    assert same_origin(BASE + "/", "127.0.0.1:8791") is True
    assert same_origin("http://127.0.0.1:8792", "127.0.0.1:8791") is False
    assert same_origin("https://127.0.0.1:8791", "127.0.0.1:8791") is False
    assert same_origin("http://localhost:8791", "127.0.0.1:8791") is False


def test_foreign_host_is_refused(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    response = client.get("/api/session", headers={"host": "evil.example"})
    assert response.status_code == 403
    assert "loopback" in response.json()["error"]


@pytest.mark.parametrize("ratings", [
    {**RATINGS, "setup": 7.5},
    {**RATINGS, "setup": True},
    {**RATINGS, "setup": 0},
    {**RATINGS, "setup": 11},
    {k: v for k, v in RATINGS.items() if k != "overall"},
])
def test_invalid_ratings_are_refused(tmp_path: Path, batch: dict, ratings: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    response = client.post(f"/api/items/{item_id}/review", json={"ratings": ratings, "confidence": "HIGH"},
                           headers={"X-Coach-Review-Token": "token-a"})
    assert response.status_code == 400
    assert client.get(f"/api/items/{item_id}").json()["state"] == "new"


def test_confidence_enum_and_body_limits(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    headers = {"X-Coach-Review-Token": "token-a"}
    assert client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "VERY"},
                       headers=headers).status_code == 400
    assert client.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH",
                                                             "notes": "x" * 20000}, headers=headers).status_code == 413
    assert client.post(f"/api/items/{item_id}/review", content=b"not json",
                       headers={**headers, "Content-Type": "application/json"}).status_code == 400


def test_body_limit_applies_to_chunked_uploads(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]

    def chunks():  # no Content-Length: the server must stop while the bytes arrive
        for _ in range(60):
            yield b"x" * 1024

    response = client.post(f"/api/items/{item_id}/review", content=chunks(),
                           headers={"Content-Type": "application/json", "X-Coach-Review-Token": "token-a"})
    assert response.status_code == 413
    assert client.get(f"/api/items/{item_id}").json()["state"] == "new"


def test_reviewer_isolation_over_http(tmp_path: Path, batch: dict) -> None:
    client_a = _client(tmp_path, reviewer="coachA", token="token-a")
    client_b = _client(tmp_path, reviewer="coachB", token="token-b")
    item_id = _item_ids(client_a)[0]
    client_a.post(f"/api/items/{item_id}/review", json={"ratings": RATINGS, "confidence": "HIGH"},
                  headers={"X-Coach-Review-Token": "token-a"})
    assert client_b.get(f"/api/items/{item_id}/score").status_code == 403
    assert client_b.get(f"/api/items/{item_id}").json()["state"] == "new"
    assert client_b.get(f"/api/items/{item_id}").json()["review"] is None
    assert client_b.get("/api/session").json()["batch"]["reviewed"] == 0
    assert client_a.get("/api/session").json()["batch"]["reviewed"] == 1


def test_skip_and_unskip_over_http(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    headers = {"X-Coach-Review-Token": "token-a"}
    assert client.post(f"/api/items/{item_id}/skip", headers=headers).json()["state"] == "skipped"
    assert client.get(f"/api/items/{item_id}").json()["state"] == "skipped"
    assert client.get(f"/api/items/{item_id}/score").status_code == 403
    assert client.post(f"/api/items/{item_id}/unskip", headers=headers).json()["state"] == "new"
    assert client.get(f"/api/items/{item_id}").json()["state"] == "new"


def test_video_streams_with_ranges_and_no_filename(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    full = client.get(f"/media/{item_id}")
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("video/mp4")
    assert full.headers["accept-ranges"] == "bytes"
    assert "content-disposition" not in {k.lower() for k in full.headers}
    assert "swing.mp4" not in str(full.headers)
    partial = client.get(f"/media/{item_id}", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206
    assert partial.headers["content-range"].startswith("bytes 0-3/")
    assert len(partial.content) == 4
    assert client.get(f"/media/{item_id}", headers={"Range": "bytes=999999-"}).status_code == 416
    assert client.get("/media/not-a-swing").status_code == 404


def test_video_head_and_range_edge_cases(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    item_id = _item_ids(client)[0]
    head = client.head(f"/media/{item_id}")
    assert head.status_code == 200 and head.content == b""
    assert head.headers["accept-ranges"] == "bytes" and int(head.headers["content-length"]) > 0
    suffix = client.get(f"/media/{item_id}", headers={"Range": "bytes=-4"})
    assert suffix.status_code == 206 and len(suffix.content) == 4
    ranged_head = client.head(f"/media/{item_id}", headers={"Range": "bytes=0-9"})
    assert ranged_head.status_code == 206 and ranged_head.headers["content-length"] == "10"
    for bad in ("bytes=0-1,3-4", "bytes=abc", "bytes=5-2", "items=0-1", "bytes=", "bytes=1", "bytes=0-1,"):
        response = client.get(f"/media/{item_id}", headers={"Range": bad})
        assert response.status_code == 416, bad
        assert response.headers["content-length"] == "0"
        assert response.content == b""


def test_parse_range_is_explicit() -> None:
    assert parse_range(None, 100) is None
    assert parse_range("bytes=0-9", 100) == (0, 9)
    assert parse_range("bytes=10-", 100) == (10, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=0-1000", 100) == (0, 99)
    for bad in ("bytes=0-1,3-4", "bytes=abc", "bytes=5-2", "bytes=-0", "chunks=0-1", "bytes="):
        with pytest.raises(RangeError):
            parse_range(bad, 100)
    with pytest.raises(RangeError):
        parse_range("bytes=0-9", 0)


def test_media_type_matches_the_file_extension(tmp_path: Path) -> None:
    root = tmp_path / "mov"
    (root / "bundle").mkdir(parents=True)
    video = root / "bundle" / "swing.mov"
    video.write_bytes(b"\x00\x00\x00\x18ftypqt  " + b"m" * 64)
    analysis = root / "bundle" / "analysis.json"
    analysis.write_text(json.dumps(fixtures.synthetic_analysis(fixtures.synthetic_score())), encoding="utf-8")
    db = tmp_path / "review.sqlite3"
    conn = store.connect(db)
    try:
        store.init_db(conn)
        store.import_batch(conn, "pilot", store.prepare_entries(
            [{"swing_id": "mov-1", "video": str(video), "analysis": str(analysis)}]))
    finally:
        conn.close()
    client = _client(tmp_path, batch="pilot", reviewer="coachA")
    item_id = _item_ids(client)[0]
    response = client.get(f"/media/{item_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("video/quicktime")


def test_large_media_streams_in_bounded_chunks(tmp_path: Path, batch: dict) -> None:
    big = tmp_path / "big"
    big.mkdir()
    video = big / "large.mp4"
    payload = bytes(range(256)) * 32768  # 8 MiB
    video.write_bytes(payload)
    analysis = big / "analysis.json"
    analysis.write_text(json.dumps(fixtures.synthetic_analysis(fixtures.synthetic_score())), encoding="utf-8")
    db = tmp_path / "review.sqlite3"
    conn = store.connect(db)
    try:
        store.import_batch(conn, "big-batch", store.prepare_entries(
            [{"swing_id": "big-1", "video": str(video), "analysis": str(analysis)}]))
    finally:
        conn.close()
    client = _client(tmp_path, batch="big-batch", reviewer="coachA")
    item_id = [item["item_id"] for item in client.get("/api/session").json()["items"]][0]
    window = client.get(f"/media/{item_id}", headers={"Range": "bytes=1048576-1050623"})
    assert window.status_code == 206 and len(window.content) == 2048
    assert window.content == payload[1048576:1050624]
    assert window.headers["content-length"] == "2048"


@pytest.mark.parametrize("path", ["/var/qa/test_swings", "/bundle/analysis.json", "/../var",
                                  "/api/items/%2e%2e%2f%2e%2e%2fvar", "/media/%2e%2e%2f%2e%2e%2fcoach_review.sqlite3",
                                  "/qa/human_review/api/reviews", "/app.js.map"])
def test_only_the_allowlisted_assets_exist(tmp_path: Path, batch: dict, path: str) -> None:
    client = _client(tmp_path)
    assert client.get(path).status_code == 404


def test_page_and_assets_are_served_with_a_strict_policy(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    page = client.get("/")
    assert page.status_code == 200
    assert "token-a" in page.text
    assert "data-edit-mode=\"0\"" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert client.get("/app.css").headers["content-type"].startswith("text/css")
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/healthz").json() == {"status": "ok"}


def test_session_order_is_stable_across_requests(tmp_path: Path, batch: dict) -> None:
    client = _client(tmp_path)
    assert _item_ids(client) == _item_ids(client)
