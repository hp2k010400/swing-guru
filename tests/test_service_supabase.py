"""The Supabase adapters send the right requests (mock transport; no real project needed)."""

from __future__ import annotations

import json

import httpx
import pytest

from swingguru.service.config import Settings
from swingguru.service.repo import PostgrestRepo
from swingguru.service.storage import StorageError, SupabaseStorage

SETTINGS = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key", bucket="swings")
KEY = "0c6f5d8e-1111-2222-3333-444455556666/9b2e1f00-aaaa-bbbb-cccc-000011112222/source.mov"


def _client(handler, calls):
    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(record), headers={"apikey": "service-key"})


def test_owned_queries_always_filter_on_the_user() -> None:
    calls: list[httpx.Request] = []
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("billing_delete"):
            return httpx.Response(200, json={"status": "deleted", "forfeited": 0, "swing": {"id": "s1"}})
        return httpx.Response(200, json=[])

    repo = PostgrestRepo(SETTINGS, _client(handler, calls))
    repo.get_owned("s1", "u1")
    repo.update_owned("s1", "u1", club="Driver")
    repo.delete_owned("s1", "u1")
    repo.list_owned("u1", ("complete",))
    reads_and_writes = [calls[0], calls[1], calls[3]]
    assert [request.url.path for request in reads_and_writes] == ["/rest/v1/swings"] * 3
    for request in reads_and_writes:
        assert request.url.params.get("user_id") == "eq.u1", request
    assert calls[1].method == "PATCH" and json.loads(calls[1].content)["club"] == "Driver"
    assert calls[3].url.params["status"] == "in.(complete)"
    # Deletion moved into the ownership-checked, forfeiting RPC: the user id is in the body, and the
    # handler checked it inside the same transaction as the delete.
    assert calls[2].url.path == "/rest/v1/rpc/billing_delete"
    assert json.loads(calls[2].content) == {"p_user_id": "u1", "p_swing_id": "s1"}


def test_jobs_are_claimed_through_the_atomic_sql_function() -> None:
    calls: list[httpx.Request] = []
    repo = PostgrestRepo(SETTINGS, _client(lambda r: httpx.Response(200, json=[{"id": "s1", "status": "processing"}]), calls))
    assert repo.claim_next("2026-09-30T00:00:00+00:00", 2)["id"] == "s1"
    assert calls[0].url.path == "/rest/v1/rpc/claim_swing_job"
    assert json.loads(calls[0].content) == {"stale_before": "2026-09-30T00:00:00+00:00", "max_attempts": 2}


def test_repo_refuses_unknown_columns() -> None:
    from swingguru.service.repo import RepoError

    calls: list[httpx.Request] = []
    repo = PostgrestRepo(SETTINGS, _client(lambda r: httpx.Response(200, json=[]), calls))
    with pytest.raises(RepoError):
        repo.update("s1", role="service_role")
    assert calls == []  # nothing was sent


def test_storage_routes_match_the_supabase_client() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(200, json={"url": f"/object/upload/sign/swings/{KEY}?token=abc"})
        if path.startswith("/storage/v1/object/sign/"):
            return httpx.Response(200, json={"signedURL": f"/object/sign/swings/{KEY}?token=xyz"})
        if request.method == "HEAD":
            return httpx.Response(200)  # no content-length reported
        if path.startswith("/storage/v1/object/info/"):
            return httpx.Response(200, json={"size": 1234})
        return httpx.Response(200, json={})

    storage = SupabaseStorage(SETTINGS, _client(handler, calls))
    upload = storage.upload_instructions(KEY, "video/quicktime")
    assert upload["method"] == "PUT" and upload["url"] == f"https://proj.supabase.co/storage/v1/object/upload/sign/swings/{KEY}?token=abc"
    assert storage.signed_url(KEY, 60) == f"https://proj.supabase.co/storage/v1/object/sign/swings/{KEY}?token=xyz"
    assert json.loads(calls[-1].content) == {"expiresIn": 60}
    assert storage.size(KEY) == 1234  # falls back to object info when HEAD has no length
    storage.delete([KEY])
    assert calls[-1].method == "DELETE" and calls[-1].url.path == "/storage/v1/object/swings"
    assert json.loads(calls[-1].content) == {"prefixes": [KEY]}
    with pytest.raises(StorageError):
        storage.signed_url("../../etc/passwd")


def test_batch_signing_is_one_request_and_fails_closed() -> None:
    calls: list[httpx.Request] = []
    other = KEY.replace("source.mov", "replay.mp4")

    def handler(request: httpx.Request) -> httpx.Response:
        paths = json.loads(request.content)["paths"]
        return httpx.Response(200, json=[{"path": p, "signedURL": f"/object/sign/swings/{p}?token=t", "error": None} for p in paths])

    storage = SupabaseStorage(SETTINGS, _client(handler, calls))
    signed = storage.signed_urls([KEY, other], 60)
    assert len(calls) == 1 and calls[0].url.path == "/storage/v1/object/sign/swings"
    assert json.loads(calls[0].content) == {"expiresIn": 60, "paths": [KEY, other]}
    assert signed[other] == f"https://proj.supabase.co/storage/v1/object/sign/swings/{other}?token=t"
    assert storage.signed_urls([]) == {} and len(calls) == 1

    missing = SupabaseStorage(SETTINGS, _client(lambda r: httpx.Response(200, json=[{"path": KEY, "signedURL": None, "error": "Not found"}]), []))
    with pytest.raises(StorageError):
        missing.signed_urls([KEY])


def test_missing_objects_and_storage_errors() -> None:
    storage = SupabaseStorage(SETTINGS, _client(lambda r: httpx.Response(404 if r.method == "HEAD" else 500), []))
    assert storage.size(KEY) is None
    with pytest.raises(StorageError):
        storage.signed_url(KEY)
