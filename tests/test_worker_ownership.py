"""A worker that lost its job (re-claimed by a newer attempt, finished or deleted meanwhile) must not
overwrite the job's status, progress or results - with billing off as well as on."""

from __future__ import annotations

import json

import httpx

from swingguru.service.repo import PostgrestRepo, SqliteRepo
from swingguru.service.worker import result_keys
from test_service import A, FakeRunner, _upload, service  # noqa: F401  (pytest fixture)


def _start(client) -> str:
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    return job_id


class ReclaimedDuringRun(FakeRunner):
    """While this (old) attempt runs, another worker re-claims the job (attempts goes up)."""

    def __init__(self, repo_ref: dict, error: str | None = None) -> None:
        super().__init__(error=error)
        self.repo_ref = repo_ref

    def __call__(self, upload, work, **kwargs):
        repo, job_id = self.repo_ref["repo"], self.repo_ref["id"]
        repo.update(job_id, attempts=int(repo.get(job_id)["attempts"]) + 1, progress=json.dumps([{"id": "newer"}]))
        return super().__call__(upload, work, **kwargs)


def _objects(app) -> list[str]:
    root = app.state.storage.root if hasattr(app.state.storage, "root") else None
    base = root or (app.state.settings.data_dir / "objects")
    return sorted(p.name for p in base.rglob("*") if p.is_file())


def test_a_stale_attempt_cannot_complete_a_reclaimed_job(service) -> None:
    ref: dict = {}
    client, app, _ = service(runner=ReclaimedDuringRun(ref))
    job_id = _start(client)
    ref.update(repo=app.state.repo, id=job_id)
    app.state.worker.run_once()
    row = app.state.repo.get(job_id)
    assert row["status"] == "processing" and row["attempts"] == 2      # the newer attempt still owns it
    assert row["result_key"] is None and row["finished_at"] is None
    assert "newer" in json.dumps(row["progress"])                       # its progress was not overwritten
    assert not [n for n in _objects(app) if n.endswith(("analysis.json", "replay.mp4", "thumb.jpg"))]


def test_a_stale_attempt_cannot_fail_a_reclaimed_job(service) -> None:
    ref: dict = {}
    client, app, _ = service(runner=ReclaimedDuringRun(ref, error="no_swing"))
    job_id = _start(client)
    ref.update(repo=app.state.repo, id=job_id)
    app.state.worker.run_once()
    row = app.state.repo.get(job_id)
    assert row["status"] == "processing" and row["error_code"] is None and row["attempts"] == 2


def test_ownership_lost_after_upload_removes_the_stale_objects(service) -> None:
    client, app, _ = service()
    job_id = _start(client)
    storage, repo = app.state.storage, app.state.repo
    original = storage.put_file
    state = {"bumped": False}

    def put_then_lose(key, path, content_type):
        original(key, path, content_type)
        if not state["bumped"]:  # another worker re-claims right after our first upload
            repo.update(job_id, attempts=int(repo.get(job_id)["attempts"]) + 1)
            state["bumped"] = True

    storage.put_file = put_then_lose
    app.state.worker.run_once()
    row = repo.get(job_id)
    assert row["status"] == "processing" and row["result_key"] is None
    assert not [n for n in _objects(app) if n.startswith("a1-")]  # nothing the old attempt wrote is left


def test_the_owning_attempt_completes_with_attempt_named_results(service) -> None:
    client, app, _ = service()
    job_id = _start(client)
    app.state.worker.run_once()
    row = app.state.repo.get(job_id)
    assert row["status"] == "complete"
    assert row["result_key"].endswith("/a1-analysis.json") and row["video_key"].endswith("/a1-replay.mp4")
    assert client.get(f"/v1/swings/{job_id}", headers=A).status_code == 200


def test_result_keys_keep_the_storage_key_shape() -> None:
    from swingguru.service.storage import check_key

    user, swing = "0c6f5d8e-1111-2222-3333-444455556666", "9b2e1f00-aaaa-bbbb-cccc-000011112222"
    for attempts in (None, 1, 2):
        for key in result_keys(user, swing, attempts).values():
            assert check_key(key) == key
    assert result_keys(user, swing, 2)["result_key"].endswith("/a2-analysis.json")


def test_sqlite_finish_job_is_guarded(tmp_path) -> None:
    repo = SqliteRepo(tmp_path / "db.sqlite3")
    repo.insert({"id": "job-1", "user_id": "u", "status": "processing", "attempts": 2, "source_key": "k",
                 "content_type": "video/mp4", "size_bytes": 1})
    assert repo.finish_job("job-1", status="complete", attempts=1) is None            # old attempt
    stamp = repo.get("job-1")["updated_at"]
    assert repo.finish_job("job-1", status="failed", attempts=2, expected_updated_at="1999") is None
    done = repo.finish_job("job-1", status="failed", attempts=2, expected_updated_at=stamp, error_code="failed")
    assert done["status"] == "failed" and done["error_code"] == "failed"
    assert repo.finish_job("job-1", status="complete", attempts=2) is None            # already terminal
    assert repo.finish_job("missing", status="failed", attempts=1) is None            # deleted


def test_postgrest_finish_job_sends_the_guard() -> None:
    from dataclasses import replace

    from swingguru.service.config import Settings

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    settings = replace(Settings(), supabase_url="https://proj.supabase.co", supabase_service_key="k")
    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert repo.finish_job("job-1", status="failed", attempts=3, older_than="2026-10-03T10:00:00+00:00",
                           error_code="failed") is None
    # The terminal write is a guarded SQL function now, so the account-lifecycle predicate is atomic
    # with the attempt/heartbeat guard (a PostgREST PATCH cannot express `not exists (...)`).
    assert seen[0].url.path == "/rest/v1/rpc/swing_finish"
    payload = json.loads(seen[0].content)
    assert payload == {"p_swing_id": "job-1", "p_status": "failed", "p_attempts": 3,
                       "p_expected_updated_at": None, "p_older_than": "2026-10-03T10:00:00+00:00",
                       "p_error_code": "failed", "p_progress": None}


def test_starting_a_job_wakes_the_worker_instead_of_waiting_for_the_poll(service) -> None:
    import threading
    import time
    from dataclasses import replace

    client, app, runner = service(worker_poll_s=30.0)
    worker = app.state.worker
    loop = threading.Thread(target=worker._loop, daemon=True)
    loop.start()
    try:
        time.sleep(0.3)  # the idle worker is now waiting on its (30 s) poll
        job_id = _start(client)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and app.state.repo.get(job_id)["status"] != "complete":
            time.sleep(0.05)
        assert app.state.repo.get(job_id)["status"] == "complete", "claimed long before the 30 s poll"
    finally:
        worker.stop()
        loop.join(timeout=5)


def test_a_second_start_never_requeues_a_claimed_job(service) -> None:
    """Upload completion and the processing screen can both call /start at the same moment."""

    client, app, _ = service()
    job_id = _start(client)
    repo = app.state.repo
    repo.update(job_id, status="processing", attempts=1)          # a worker claimed it
    again = client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert again.status_code == 200 and again.json()["status"] == "processing"
    assert repo.get(job_id)["status"] == "processing" and repo.get(job_id)["attempts"] == 1


def test_queue_if_awaiting_happens_once(tmp_path) -> None:
    repo = SqliteRepo(tmp_path / "db.sqlite3")
    repo.insert({"id": "job-1", "user_id": "u", "status": "awaiting_upload", "attempts": 0, "source_key": "k",
                 "content_type": "video/mp4", "size_bytes": 1})
    assert repo.queue_if_awaiting("job-1", "u", 10)["status"] == "queued"
    assert repo.queue_if_awaiting("job-1", "u", 10) is None
    assert repo.queue_if_awaiting("job-1", "someone-else", 10) is None


def test_postgrest_insert_sends_the_not_null_defaulted_columns() -> None:
    """swing_create uses jsonb_populate_record: omitted columns become NULL, not their default."""

    from dataclasses import replace as _replace

    from swingguru.service.config import Settings

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/rpc/swing_create"):
            return httpx.Response(200, json={"id": "s1"})
        return httpx.Response(200, json=[{"id": "s1", "status": "awaiting_upload"}])

    settings = _replace(Settings(), supabase_url="https://proj.supabase.co", supabase_service_key="k")
    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    repo.insert({"id": "s1", "user_id": "u1", "status": "awaiting_upload", "source_key": "u1/s1/source.mov"})
    fields = json.loads(seen[0].content)["p_fields"]
    for column in ("created_at", "updated_at", "attempts"):
        assert fields.get(column) is not None, column


def test_postgrest_completion_stores_results_before_flipping_status() -> None:
    """swing_finish has no parameters for result keys/summary: they must be written first."""

    from dataclasses import replace as _replace

    from swingguru.service.config import Settings

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/rpc/swing_finish"):
            return httpx.Response(200, json=[{"id": "s1", "status": "complete"}])
        return httpx.Response(200, json=[{"id": "s1"}])

    settings = _replace(Settings(), supabase_url="https://proj.supabase.co", supabase_service_key="k")
    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    done = repo.finish_job("s1", status="complete", attempts=1, result_key="u/s1/a1-analysis.json",
                           video_key="u/s1/a1-replay.mp4", summary={"score": {"total": 7.0}}, progress=[{"id": "build"}])
    assert done and [r.method for r in seen] == ["PATCH", "POST"]
    params = dict(seen[0].url.params)
    assert params["status"] == "eq.processing" and params["attempts"] == "eq.1"
    body = json.loads(seen[0].content)
    assert body["result_key"].endswith("a1-analysis.json") and body["summary"]["score"]["total"] == 7.0

    # Lost ownership before the results were written: nothing is flipped to complete.
    seen.clear()
    lost = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(
        lambda r: (seen.append(r), httpx.Response(200, json=[]))[1])))
    assert lost.finish_job("s1", status="complete", attempts=1, result_key="x/y/a1-analysis.json") is None
    assert [r.method for r in seen] == ["PATCH"]
