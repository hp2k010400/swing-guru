"""Hosted analysis service: job lifecycle, storage, retention, and cross-user isolation.

Runs the real API + worker against SQLite and local signed storage, with the
engine replaced by a fake runner that writes a small but real bundle.
"""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import cv2
import jwt
import numpy as np
import pytest
from fastapi.testclient import TestClient

from swingguru.analysis_runner import RunResult
from swingguru.service import auth
from swingguru.service.api import create_app
from swingguru.service.config import ConfigError, Settings, validate
from swingguru.service.repo import SqliteRepo, now
from swingguru.service.storage import LocalStorage, StorageError, check_key

A = {"Authorization": "Bearer dev:user-aaaaaaaa"}
B = {"Authorization": "Bearer dev:user-bbbbbbbb"}


def _analysis() -> dict:
    verdict = {"status": "attention", "label": "Slightly upright", "support": "Tilt a little more.", "verdict_id": "x",
               "mapping": {"id": "address.hip_hinge", "validation": "provisional_ui_mapping"}}
    return {"video": "swing.mp4", "duration_s": 1.0, "phases": [
        {"phase": "address", "label": "Address", "frame_index": 0, "time_s": 0.1, "checks": [
            {"kind": "torso_angle", "guided": True, "checkpoint": "Hip hinge", "value": "24°", "time_s": 0.1,
             "verdict": verdict}]},
        {"phase": "impact", "label": "Impact", "frame_index": 20, "time_s": 0.6, "checks": []}],
        "hidden": [{"phase": "impact", "kind": "head", "reason": "the camera moved during the swing"}],
        "interpretations": {"focus": None, "results": []}, "camera_check": {"signals": {}}}


class FakeRunner:
    def __init__(self, error: str | None = None, crash: bool = False) -> None:
        self.error, self.crash, self.calls = error, crash, 0
        self.sizes: list[int] = []

    def __call__(self, upload: Path, work: Path, *, on_prepared=None, timeout_s=600.0) -> RunResult:
        import json

        self.calls += 1
        assert upload.is_file() and upload.stat().st_size > 0
        self.sizes.append(upload.stat().st_size)
        if self.crash:
            raise RuntimeError("secret internal failure at C:/models/rtmw.onnx")
        if on_prepared:
            on_prepared("We found 2 swings in this video and analysed the clearest one.")
        if self.error:
            return RunResult(False, self.error, detail="internal detail")
        bundle = work / "bundle"
        bundle.mkdir(parents=True)
        (bundle / "analysis.json").write_text(json.dumps(_analysis()), encoding="utf-8")
        writer = cv2.VideoWriter(str(bundle / "swing.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 30, (64, 96))
        for _ in range(30):
            writer.write(np.full((96, 64, 3), 120, np.uint8))
        writer.release()
        return RunResult(True, bundle=bundle, notice="We found 2 swings in this video and analysed the clearest one.")


@pytest.fixture
def service(tmp_path):
    def make(runner=None, **overrides):
        settings = replace(Settings(data_dir=tmp_path / "svc", public_url="http://testserver"), **overrides)
        repo, storage = SqliteRepo(tmp_path / "svc" / "db.sqlite3"), LocalStorage(settings)
        runner = runner or FakeRunner()
        app = create_app(settings, repo=repo, storage=storage, start_worker=False, runner=runner)
        return TestClient(app), app, runner
    return make


def _upload(client, headers=A, body=b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000, content_type="video/mp4") -> dict:
    created = client.post("/v1/analysis/jobs", headers=headers, json={
        "file_name": "IMG_0001.MOV", "content_type": content_type, "size_bytes": len(body)})
    assert created.status_code == 201, created.text
    data = created.json()
    url = urlparse(data["upload"]["url"])
    put = client.put(f"{url.path}?{url.query}", content=body, headers=data["upload"]["headers"])
    assert put.status_code == 200, put.text
    return data


def _get(client, url: str, **kw):
    parsed = urlparse(url)
    return client.get(f"{parsed.path}?{parsed.query}", **kw)


def test_health_and_auth_required(service) -> None:
    client, _, _ = service()
    assert client.get("/health").json() == {"status": "ok"}
    assert set(client.get("/ready").json()["checks"]) == {"database", "models"}
    for method, path in (("get", "/v1/swings"), ("post", "/v1/analysis/jobs"), ("delete", "/v1/account")):
        response = getattr(client, method)(path)
        assert response.status_code == 401 and response.json()["error"]["code"] == "unauthorized"
    assert client.get("/v1/swings", headers={"Authorization": "Bearer dev:../../etc"}).status_code == 401


def test_full_job_lifecycle(service) -> None:
    client, app, runner = service()
    data = _upload(client)
    job_id = data["job"]["id"]
    assert data["job"]["status"] == "awaiting_upload" and data["api_version"] == "v1"
    started = client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).json()
    assert started["status"] == "queued"
    assert app.state.worker.run_once() is True and runner.calls == 1
    job = client.get(f"/v1/analysis/jobs/{job_id}", headers=A).json()
    assert job["status"] == "complete" and job["swing_id"] == job_id
    assert all(s["state"] == "done" for s in job["stages"]) and "2 swings" in job["notice"]
    listing = client.get("/v1/swings", headers=A).json()["swings"]
    assert [s["id"] for s in listing] == [job_id] and listing[0]["counts"]["attention"] == 1
    assert _get(client, listing[0]["thumb_url"]).headers["content-type"] == "image/jpeg"
    detail = client.get(f"/v1/swings/{job_id}", headers=A).json()
    assert detail["score"]["version"] == "swing_score_v3"  # the versioned model, stored when the job finished
    assert listing[0]["score"]["version"] == "swing_score_v3"
    assert _get(client, detail["replay"]["analysis_url"]).json()["swing_score"]["version"] == "swing_score_v3"
    assert detail["focus"]["source"] == "none"     # no Start here without an eligible rule
    assert detail["withheld"][0]["name"] == "Head" and detail["summary_text"]
    assert _get(client, detail["replay"]["analysis_url"]).json()["video"] == "swing.mp4"
    assert _get(client, detail["replay"]["video_url"]).status_code == 200
    assert client.get("/v1/viewer/index.html").status_code == 200
    assert client.get("/v1/viewer/../api.py").status_code == 404
    # The original upload is deleted as soon as the analysis ends (retention 0).
    row = app.state.repo.get(job_id)
    assert row["source_deleted_at"] and app.state.storage.size(row["source_key"]) is None
    assert client.patch(f"/v1/swings/{job_id}", headers=A, json={"club": "Driver"}).json()["club"] == "Driver"
    assert client.patch(f"/v1/swings/{job_id}", headers=A, json={"club": "Putter"}).status_code == 422


def test_users_cannot_see_or_change_each_others_swings(service) -> None:
    client, app, _ = service()
    job_id = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    app.state.worker.run_once()
    for method, path, kw in (("get", f"/v1/analysis/jobs/{job_id}", {}), ("post", f"/v1/analysis/jobs/{job_id}/start", {}),
                             ("get", f"/v1/swings/{job_id}", {}), ("patch", f"/v1/swings/{job_id}", {"json": {"club": "Driver"}}),
                             ("delete", f"/v1/swings/{job_id}", {})):
        response = getattr(client, method)(path, headers=B, **kw)
        assert response.status_code == 404, (method, path, response.text)
    assert client.get("/v1/swings", headers=B).json()["swings"] == []
    client.delete("/v1/account", headers=B)  # deleting B's account never touches A
    assert client.get(f"/v1/swings/{job_id}", headers=A).status_code == 200
    assert app.state.repo.get(job_id)["club"] is None


def test_signed_urls_are_tamper_proof_and_expire(service) -> None:
    client, app, _ = service()
    data = _upload(client)
    url = urlparse(data["upload"]["url"])
    assert client.put(f"{url.path}?{url.query.replace('sig=', 'sig=0')}", content=b"x").status_code == 403
    storage: LocalStorage = app.state.storage
    key = data["job"]["id"]
    expired = int(time.time()) - 10
    path = f"/v1/files/user-aaaaaaaa/{key}/source.mp4?exp={expired}&sig={storage.sign(f'user-aaaaaaaa/{key}/source.mp4', 'GET', expired)}"
    assert client.get(path).status_code == 403
    for bad in ("../x/y", "user-aaaaaaaa/../../secret", "a/b/c", "user-aaaaaaaa/swing-xxxxxxxx/../../x"):
        with pytest.raises(StorageError):
            check_key(bad)


def test_upload_limits_and_rate_limits(service) -> None:
    client, _, _ = service(max_active_jobs=2, jobs_per_hour=3)
    base = {"file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10}
    assert client.post("/v1/analysis/jobs", headers=A, json={**base, "content_type": "text/html"}).status_code == 415
    assert client.post("/v1/analysis/jobs", headers=A, json={**base, "size_bytes": 10**10}).status_code == 413
    assert client.post("/v1/analysis/jobs", headers=A, json={**base, "extra": 1}).status_code == 422
    assert client.post("/v1/analysis/jobs", headers=A, json=base).status_code == 201
    assert client.post("/v1/analysis/jobs", headers=A, json=base).status_code == 201
    busy = client.post("/v1/analysis/jobs", headers=A, json=base)
    assert busy.status_code == 429 and busy.json()["error"]["code"] == "analysis_in_progress"
    assert client.post("/v1/analysis/jobs", headers=B, json=base).status_code == 201  # per user


def test_missing_upload_and_golfer_facing_failures(service) -> None:
    client, app, _ = service(runner=FakeRunner(error="no_clear_swing"))
    created = client.post("/v1/analysis/jobs", headers=A, json={"file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10})
    missing = client.post(f"/v1/analysis/jobs/{created.json()['job']['id']}/start", headers=A)
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "upload_missing"
    job_id = _upload(client, headers=B)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=B)
    app.state.worker.run_once()
    job = client.get(f"/v1/analysis/jobs/{job_id}", headers=B).json()
    assert job["status"] == "failed" and job["error"]["code"] == "no_clear_swing"
    assert "still set-up" in job["error"]["advice"] and "internal detail" not in str(job)
    assert client.get("/v1/swings", headers=B).json()["swings"] == []  # failures never reach the Library


def test_engine_crash_never_leaks_internals(service) -> None:
    client, app, _ = service(runner=FakeRunner(crash=True))
    job_id = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    app.state.worker.run_once()
    body = client.get(f"/v1/analysis/jobs/{job_id}", headers=A).text
    assert '"failed"' in body and "rtmw" not in body and "C:/" not in body and "RuntimeError" not in body


def test_delete_swing_and_account_remove_everything(service) -> None:
    client, app, _ = service()
    ids = []
    for _ in range(2):
        job_id = _upload(client)["job"]["id"]
        client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
        app.state.worker.run_once()
        ids.append(job_id)
    objects = app.state.storage.root
    assert any(objects.rglob("*replay.mp4"))
    assert client.delete(f"/v1/swings/{ids[0]}", headers=A).status_code == 204
    assert client.get(f"/v1/swings/{ids[0]}", headers=A).status_code == 404
    deleted = client.delete("/v1/account", headers=A)
    assert deleted.status_code == 200 and deleted.json()["apple_revocation"] == "not_required"
    # A deleted user's JWT remains cryptographically valid until it expires (Supabase documents
    # this), so access is denied by the durable tombstone, not by the signature.
    denied = client.get("/v1/swings", headers=A)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "account_deleted"
    assert app.state.repo.account_status("user-aaaaaaaa") == "deleted"
    assert not any(p.is_file() for p in objects.rglob("*"))


def test_stale_jobs_are_reclaimed_and_attempts_capped(tmp_path) -> None:
    repo = SqliteRepo(tmp_path / "db.sqlite3")
    repo.insert({"id": "job-00000001", "user_id": "user-aaaaaaaa", "status": "queued"})
    claimed = repo.claim_next(now(), 2)
    assert claimed["status"] == "processing" and claimed["attempts"] == 1
    assert repo.claim_next((datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(), 2) is None  # fresh heartbeat
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    assert repo.claim_next(future, 2)["attempts"] == 2  # a dead worker's job is taken over
    assert repo.claim_next(future, 2) is None           # ...but not forever


def test_production_refuses_unsafe_configuration() -> None:
    with pytest.raises(ConfigError):
        validate(Settings(env="production"))  # dev auth, local storage, http
    with pytest.raises(ConfigError):
        validate(Settings(env="production", auth_mode="supabase", storage_mode="supabase", db_mode="supabase",
                          public_url="https://api.example.com", supabase_url="https://x.supabase.co"))  # no service key
    validate(Settings(env="production", auth_mode="supabase", storage_mode="supabase", db_mode="supabase",
                      public_url="https://api.example.com", supabase_url="https://x.supabase.co", supabase_service_key="k"))
    with pytest.raises(auth.AuthError):
        auth.verify("Bearer dev:user-aaaaaaaa", Settings(env="production", auth_mode="dev"))


def test_supabase_jwt_verification() -> None:
    settings = Settings(auth_mode="supabase", supabase_url="https://x.supabase.co", supabase_jwt_secret="s" * 40)
    good = {"sub": "0c6f5d8e-1111-2222-3333-444455556666", "aud": "authenticated", "role": "authenticated",
            "is_anonymous": True, "exp": int(time.time()) + 600}
    user = auth.verify("Bearer " + jwt.encode(good, "s" * 40, algorithm="HS256"), settings)
    assert user.id == good["sub"] and user.anonymous
    for claims, key in (({**good, "exp": int(time.time()) - 10}, "s" * 40), ({**good, "aud": "other"}, "s" * 40),
                        (good, "t" * 40), ({**good, "role": "service_role"}, "s" * 40)):
        with pytest.raises(auth.AuthError):
            auth.verify("Bearer " + jwt.encode(claims, key, algorithm="HS256"), settings)


def test_example_analysis_is_shared_read_only_and_free(service) -> None:
    """GET /v1/example: user A's chosen swing, readable by B, with no job, no billing and no edit rights."""

    client, app, _ = service()
    assert client.get("/v1/example", headers=B).status_code == 404          # nothing configured
    data = _upload(client)
    job_id = data["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert app.state.worker.run_once() is True
    client, app, runner = service(example_swing_id=job_id)
    assert client.get("/v1/example").status_code == 401                      # still needs an account
    swings_before = client.get("/v1/swings", headers=B).json()["swings"]
    example = client.get("/v1/example", headers=B).json()
    assert example["id"] == job_id and example["score"]["version"] == "swing_score_v3"
    assert example["notice"] is None                                         # owner's notices are not shared
    assert _get(client, example["replay"]["video_url"]).status_code == 200
    # Read-only for everyone but the owner, and it never joins B's library or starts any work.
    assert client.patch(f"/v1/swings/{job_id}", headers=B, json={"club": "Driver"}).status_code == 404
    assert client.delete(f"/v1/swings/{job_id}", headers=B).status_code == 404
    assert client.get(f"/v1/swings/{job_id}", headers=B).status_code == 404
    assert client.get("/v1/swings", headers=B).json()["swings"] == swings_before
    assert runner.calls == 0


def test_example_must_be_a_completed_swing(service) -> None:
    client, _, _ = service()
    data = _upload(client)                                                   # awaiting upload, never analysed
    client, _, _ = service(example_swing_id=data["job"]["id"])
    assert client.get("/v1/example", headers=B).status_code == 404
    client, _, _ = service(example_swing_id="not-a-uuid")
    assert client.get("/v1/example", headers=B).status_code == 404
