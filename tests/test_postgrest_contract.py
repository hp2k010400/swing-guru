"""Production-like database regression: the real API and worker, through the real ``PostgrestRepo``,
against the real Supabase migrations (PGlite behind a small PostgREST stand-in).

Why: the local suite used ``SqliteRepo``, so two production bugs passed it on 2026-10-05: ``swing_create``
nulling ``created_at`` (``jsonb_populate_record`` ignores column defaults) and completed swings losing
their results (``swing_finish`` only takes status columns). Both live in the PostgREST payload / SQL
contract, which this exercises end to end: create, defaults, upload, queue, claim, progress, results,
score, replay, thumbnail, library, club edit, failure, deletion and account deletion.

Skipped when Node or PGlite is not available (``var/qa/billing-postgres/node_modules``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from swingguru.service.api import create_app
from swingguru.service.config import Settings
from swingguru.service.repo import PostgrestRepo, RepoError
from swingguru.service.storage import LocalStorage

from test_service import FakeRunner, _get

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "var" / "qa" / "billing-postgres"
EMULATOR = HARNESS / "postgrest-emulator.mjs"


@pytest.fixture(scope="module")
def emulator():
    node = shutil.which("node")
    if not node or not EMULATOR.is_file() or not (HARNESS / "node_modules" / "@electric-sql" / "pglite").is_dir():
        pytest.skip("PGlite PostgREST emulator not available")
    proc = subprocess.Popen([node, str(EMULATOR)], cwd=HARNESS, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    line = proc.stdout.readline().strip()
    if not line.startswith("PORT "):
        proc.kill()
        pytest.fail(f"emulator did not start: {line} {proc.stderr.read()[-800:]}")
    base = f"http://127.0.0.1:{line.split()[1]}"
    yield base
    proc.kill()


def _sql(base: str, sql: str, params: list | None = None) -> list[dict]:
    response = httpx.post(f"{base}/__sql", json={"sql": sql, "params": params or []}, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()


def _user(base: str) -> tuple[str, dict]:
    user_id = str(uuid.uuid4())
    _sql(base, "insert into auth.users(id) values ($1)", [user_id])
    return user_id, {"Authorization": f"Bearer dev:{user_id}"}


@pytest.fixture
def service(emulator, tmp_path):
    def make(runner=None, **overrides):
        settings = replace(Settings(data_dir=tmp_path / "svc", public_url="http://testserver"), **overrides)
        repo = PostgrestRepo(SimpleNamespace(supabase_url=emulator, supabase_service_key="contract-test"),
                             client=httpx.Client(timeout=30.0, headers={"Content-Type": "application/json"}))
        runner = runner or FakeRunner()
        app = create_app(settings, repo=repo, storage=LocalStorage(settings), start_worker=False, runner=runner)
        return TestClient(app), app, runner
    return make


def _upload(client, headers, club: str | None = "7 Iron") -> str:
    body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000
    created = client.post("/v1/analysis/jobs", headers=headers, json={
        "file_name": "IMG_0001.MOV", "content_type": "video/mp4", "size_bytes": len(body), "club": club})
    assert created.status_code == 201, created.text
    data = created.json()
    url = urlparse(data["upload"]["url"])
    assert client.put(f"{url.path}?{url.query}", content=body, headers=data["upload"]["headers"]).status_code == 200
    return data["job"]["id"]


def test_create_fills_every_not_null_default_through_swing_create(service, emulator) -> None:
    client, app, _ = service()
    user_id, headers = _user(emulator)
    job_id = _upload(client, headers)
    row = _sql(emulator, "select created_at, updated_at, attempts, status, club, user_id::text as u from public.swings where id = $1",
               [job_id])[0]
    assert row["created_at"] and row["updated_at"] and row["attempts"] == 0
    assert row["status"] == "awaiting_upload" and row["club"] == "7 Iron" and row["u"] == user_id


def test_full_lifecycle_keeps_results_score_replay_and_thumbnail(service, emulator) -> None:
    client, app, runner = service()
    user_id, headers = _user(emulator)
    job_id = _upload(client, headers)
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers).json()["status"] == "queued"
    # Starting twice must not queue twice (the status-guarded RPC).
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers).status_code in (200, 409)
    assert app.state.worker.run_once() is True and runner.calls == 1

    stored = _sql(emulator, "select status, attempts, result_key, video_key, thumb_key, summary, analysis_version, "
                            "finished_at, progress from public.swings where id = $1", [job_id])[0]
    # The lost-results bug: "complete" without its keys and summary.
    assert stored["status"] == "complete" and stored["attempts"] == 1
    assert stored["result_key"] and stored["video_key"] and stored["thumb_key"] and stored["analysis_version"]
    assert stored["summary"] and stored["summary"]["score"]["version"] == "swing_score_v3"
    assert stored["finished_at"] and stored["progress"]

    job = client.get(f"/v1/analysis/jobs/{job_id}", headers=headers).json()
    assert job["status"] == "complete" and all(s["state"] == "done" for s in job["stages"])
    listing = client.get("/v1/swings", headers=headers).json()["swings"]
    assert [s["id"] for s in listing] == [job_id] and listing[0]["score"]["version"] == "swing_score_v3"
    assert _get(client, listing[0]["thumb_url"]).headers["content-type"] == "image/jpeg"
    detail = client.get(f"/v1/swings/{job_id}", headers=headers).json()
    assert _get(client, detail["replay"]["analysis_url"]).json()["swing_score"]["version"] == "swing_score_v3"
    assert _get(client, detail["replay"]["video_url"]).status_code == 200
    assert client.patch(f"/v1/swings/{job_id}", headers=headers, json={"club": "Driver"}).json()["club"] == "Driver"

    # Another user never sees it.
    _, other = _user(emulator)
    assert client.get(f"/v1/swings/{job_id}", headers=other).status_code == 404
    assert client.get("/v1/swings", headers=other).json()["swings"] == []

    assert client.delete(f"/v1/swings/{job_id}", headers=headers).status_code == 204
    assert client.get("/v1/swings", headers=headers).json()["swings"] == []
    assert _sql(emulator, "select count(*)::int n from public.swings where id = $1", [job_id])[0]["n"] == 0


def test_failed_analysis_is_recorded_with_its_error_code(service, emulator) -> None:
    client, app, _ = service(runner=FakeRunner(error="no_swing"))
    _, headers = _user(emulator)
    job_id = _upload(client, headers)
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers)
    assert app.state.worker.run_once() is True
    row = _sql(emulator, "select status, error_code, result_key from public.swings where id = $1", [job_id])[0]
    assert row["status"] == "failed" and row["error_code"] == "no_swing" and row["result_key"] is None
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=headers).json()["status"] == "failed"


def test_a_stale_attempt_cannot_overwrite_the_owner(service, emulator) -> None:
    client, app, _ = service()
    _, headers = _user(emulator)
    job_id = _upload(client, headers)
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers)
    repo = app.state.repo
    claimed = repo.claim_next("1970-01-01T00:00:00+00:00", 3)
    assert claimed and claimed["id"] == job_id and claimed["status"] == "processing"
    assert repo.update_progress(job_id, claimed["attempts"], progress=[{"stage": "pose", "state": "running"}])
    # Attempt numbers that do not own the job: no progress, no results, no status flip.
    assert repo.update_progress(job_id, claimed["attempts"] + 1) is False
    assert repo.finish_job(job_id, status="complete", attempts=claimed["attempts"] + 1,
                           result_key="x/analysis.json", summary={"score": None}) is None
    row = repo.get(job_id)
    assert row["status"] == "processing" and row["result_key"] is None


def test_account_deletion_removes_rows_and_blocks_new_writes(service, emulator) -> None:
    client, app, _ = service()
    user_id, headers = _user(emulator)
    _upload(client, headers)
    deleted = client.delete("/v1/account", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"status": "deleted", "apple_revocation": "not_required"}
    assert _sql(emulator, "select count(*)::int n from public.swings where user_id = $1", [user_id])[0]["n"] == 0
    # The tombstone refuses a late create for the same account.
    again = client.post("/v1/analysis/jobs", headers=headers, json={
        "file_name": "a.mov", "content_type": "video/mp4", "size_bytes": 5000})
    assert again.status_code in (401, 403)


def test_unknown_columns_are_refused_before_reaching_the_database(service, emulator) -> None:
    _, app, _ = service()
    with pytest.raises(RepoError):
        app.state.repo.update(str(uuid.uuid4()), not_a_column=1)


def test_timestamps_round_trip_with_microseconds_and_zone(service, emulator) -> None:
    client, app, _ = service()
    _, headers = _user(emulator)
    job_id = _upload(client, headers)
    row = app.state.repo.get(job_id)
    assert json.dumps(row)  # serialisable as PostgREST returns it
    assert "+00:00" in row["created_at"] or row["created_at"].endswith("Z")


def test_feedback_is_stored_once_per_swing_owned_and_removed_with_the_account(service, emulator) -> None:
    client, app, _ = service()
    user_id, headers = _user(emulator)
    job_id = _upload(client, headers)
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers)
    assert app.state.worker.run_once() is True

    down = client.post("/v1/feedback", headers=headers, json={
        "kind": "analysis", "swing_id": job_id, "rating": "down", "message": "Head looked fine to me",
        "measured_checks": ["impact.head", "not.a.check"], "app_version": "0.1.0 (9)"})
    assert down.status_code == 201, down.text
    # A second tap updates the same row instead of adding another.
    assert client.post("/v1/feedback", headers=headers, json={"kind": "analysis", "swing_id": job_id,
                                                               "rating": "up"}).status_code == 201
    rows = _sql(emulator, "select kind, rating, message, context, app_version from public.feedback where user_id = $1",
                [user_id])
    assert len(rows) == 1 and rows[0]["rating"] == "up" and rows[0]["message"] is None
    assert rows[0]["context"]["score"]["version"] == "swing_score_v3"   # read from the stored swing

    general = client.post("/v1/feedback", headers=headers, json={"kind": "general", "category": "bug",
                                                                 "message": "The X was hard to tap"})
    assert general.status_code == 201
    # Someone else's swing is invisible; missing fields are refused.
    _, other = _user(emulator)
    assert client.post("/v1/feedback", headers=other, json={"kind": "analysis", "swing_id": job_id,
                                                            "rating": "up"}).status_code == 404
    assert client.post("/v1/feedback", headers=headers, json={"kind": "general", "category": "bug"}).status_code == 422
    assert client.post("/v1/feedback", headers=headers, json={"kind": "general", "message": "x" * 2001,
                                                              "category": "bug"}).status_code == 422

    # Deleting the swing keeps the feedback (swing_id -> null); deleting the auth user removes it.
    assert client.delete(f"/v1/swings/{job_id}", headers=headers).status_code == 204
    assert _sql(emulator, "select count(*)::int n from public.feedback where user_id = $1 and kind = 'analysis' and swing_id is null",
                [user_id])[0]["n"] == 1
    _sql(emulator, "delete from auth.users where id = $1", [user_id])
    assert _sql(emulator, "select count(*)::int n from public.feedback where user_id = $1", [user_id])[0]["n"] == 0


def test_feedback_is_refused_once_the_account_is_being_deleted(service, emulator) -> None:
    client, app, _ = service()
    user_id, headers = _user(emulator)
    assert client.delete("/v1/account", headers=headers).status_code == 200
    late = client.post("/v1/feedback", headers=headers, json={"kind": "general", "category": "other", "message": "hi"})
    assert late.status_code in (401, 403)
    assert _sql(emulator, "select count(*)::int n from public.feedback where user_id = $1", [user_id])[0]["n"] == 0
