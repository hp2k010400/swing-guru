"""RevenueCat billing: verified admission, the atomic free ledger, and fail-closed behaviour.

Runs the real API + worker against SQLite and local signed storage with the
RevenueCat client replaced by a deterministic fake, so every entitlement state
(active, expired, refunded, wrong store, wrong environment, lifetime, malformed,
outage) is exercised without a project or a purchase.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

import httpx
from swingguru.service.api import create_app
from swingguru.service.billing import (
    BillingExhausted, BillingProducts, BillingService, Entitlement, RevenueCatClient, RevenueCatError,
)
from swingguru.service.config import ConfigError, Settings, validate
from swingguru.service.repo import PostgrestRepo, RepoError, SqliteRepo
from swingguru.service.storage import LocalStorage
from test_service import A, B, FakeRunner, _upload

UTC = timezone.utc


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat().replace("+00:00", "Z")


PRODUCTS = BillingProducts(
    entitlement_id="pro",
    monthly="com.swingguru.app.pro.monthly",
    yearly="com.swingguru.app.pro.yearly",
    lifetime="com.swingguru.app.pro.lifetime",
)


class FakeRevenueCat:
    """Stands in for :class:`RevenueCatClient`; records the verified user ids."""

    def __init__(self, entitlement: Entitlement | None = None) -> None:
        self.entitlement = entitlement or Entitlement.none()
        self.calls: list[str] = []
        self.error: Exception | None = None

    def fetch(self, app_user_id: str, **_: object) -> Entitlement:
        self.calls.append(app_user_id)
        if self.error is not None:
            raise self.error
        return self.entitlement


def billing_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "svc", public_url="http://testserver",
        billing_mode="on", revenuecat_secret_key="sk_test", billing_env="production",
        billing_paid_allowance="unlimited",
    )
    return replace(Settings(), **{**base, **overrides})


@pytest.fixture
def service(tmp_path):
    """The API with billing on and a fake RevenueCat client (inactive by default)."""

    def make(runner=None, client: FakeRevenueCat | None = None, **overrides):
        settings = billing_settings(tmp_path, **overrides)
        repo = SqliteRepo(tmp_path / "svc" / "db.sqlite3")
        storage = LocalStorage(settings)
        fake = client or FakeRevenueCat()
        billing = BillingService(settings, repo, client=fake)
        runner = runner or FakeRunner()
        app = create_app(settings, repo=repo, storage=storage, start_worker=False,
                         runner=runner, billing=billing)
        return TestClient(app), app, runner, fake

    return make


def _analyse(client, app, headers=A, body=None) -> str:
    data = _upload(client, headers=headers, **({"body": body} if body else {}))
    job_id = data["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers).status_code == 200
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=headers).json()["status"] == "complete"
    return job_id


# ---------------------------------------------------------------- rollout modes


def test_billing_off_is_fully_backwards_compatible(tmp_path) -> None:
    settings = replace(Settings(), data_dir=tmp_path / "svc", public_url="http://testserver")
    repo, storage = SqliteRepo(tmp_path / "svc" / "db.sqlite3"), LocalStorage(settings)
    app = create_app(settings, repo=repo, storage=storage, start_worker=False, runner=FakeRunner())
    client = TestClient(app)

    status = client.get("/v1/billing", headers=A).json()
    assert status["enabled"] is False and status["access"] == "disabled" and status["can_analyse"] is True
    for _ in range(4):  # more than the three free analyses: OFF never gates
        _analyse(client, app)
    assert app.state.repo.billing_status("user-aaaaaaaa", 3)["free_settled"] == 0


def test_billing_on_refuses_an_incomplete_configuration() -> None:
    complete = dict(billing_mode="on", revenuecat_secret_key="sk_live", billing_env="production",
                    billing_paid_allowance="unlimited")
    validate(Settings(**complete))  # the complete shape is accepted
    for missing in ("revenuecat_secret_key", "billing_env", "billing_paid_allowance"):
        broken = {**complete, missing: ""}
        with pytest.raises(ConfigError):
            validate(Settings(**broken))
    with pytest.raises(ConfigError):
        validate(Settings(**{**complete, "revenuecat_secret_key": "pk_live"}))
    with pytest.raises(ConfigError):
        validate(Settings(**{**complete, "billing_paid_allowance": "monthly_20"}))
    with pytest.raises(ConfigError):
        validate(Settings(**{**complete, "revenuecat_monthly_product_id": "com.swingguru.app.pro.monthly",
                              "revenuecat_yearly_product_id": "com.swingguru.app.pro.monthly"}))
    with pytest.raises(ConfigError):
        validate(Settings(billing_mode="sometimes"))


def test_sandbox_is_allowed_only_when_explicitly_selected() -> None:
    # Sandbox is a deployment decision (TestFlight), never request-selected, and is
    # refused if it is not stated explicitly.
    validate(Settings(billing_mode="on", revenuecat_secret_key="sk_test", billing_env="sandbox",
                      billing_paid_allowance="unlimited"))
    with pytest.raises(ConfigError):
        validate(Settings(billing_mode="on", revenuecat_secret_key="sk_test", billing_paid_allowance="unlimited"))


# ---------------------------------------------------------------- free allowance


def test_three_free_analyses_then_the_fourth_requires_a_plan(service) -> None:
    client, app, _, fake = service()
    for _ in range(3):
        _analyse(client, app)
    status = client.get("/v1/billing", headers=A).json()
    assert (status["access"], status["free_used"], status["free_remaining"], status["can_analyse"]) == \
        ("exhausted", 3, 0, False)

    blocked = client.post("/v1/analysis/jobs", headers=A, json={
        "file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10})
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "purchase_required"

    # The upload that a racy client already created is refused at /start too.
    created = client.post("/v1/analysis/jobs", headers=B, json={
        "file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10})
    assert created.status_code == 201
    job_id = created.json()["job"]["id"]
    url = urlparse(created.json()["upload"]["url"])
    assert client.put(f"{url.path}?{url.query}", content=b"x" * 100, headers=created.json()["upload"]["headers"]).status_code == 200
    # ...but B has their own allowance; A cannot spend it.
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=B).status_code == 200
    assert client.get("/v1/billing", headers=B).json()["free_used"] == 0
    assert client.get("/v1/billing", headers=B).json()["free_reserved"] == 1


def test_pending_reservations_count_and_start_is_idempotent(service) -> None:
    client, app, _, _ = service(billing_free_analyses=1)
    first = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{first}/start", headers=A).json()["status"] == "queued"
    again = client.post(f"/v1/analysis/jobs/{first}/start", headers=A)
    assert again.status_code == 200 and again.json()["status"] == "queued"  # no second charge
    assert client.get("/v1/billing", headers=A).json()["free_reserved"] == 1
    assert client.get("/v1/billing", headers=A).json()["free_remaining"] == 0
    # The only slot is held by a job that has not finished: a retryable wait, never a paywall.
    blocked = client.post("/v1/analysis/jobs", headers=A, json={
        "file_name": "b.mp4", "content_type": "video/mp4", "size_bytes": 10})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "free_analyses_in_progress"
    assert blocked.json()["error"]["code"] != "purchase_required"
    assert client.get("/v1/billing", headers=A).json()["access"] == "in_progress"
    rows = app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage").fetchone()[0]
    assert rows == 1  # the pending reservation is the only charge


def test_pre_ledger_completed_swings_are_grandfathered(service) -> None:
    """Swings analysed before billing existed are never silently billed to the new monthly account."""

    client, app, _, _ = service()
    repo = app.state.repo
    for index in range(2):
        repo.insert({"id": f"00000000-0000-0000-0000-00000000001{index}", "user_id": "user-aaaaaaaa",
                     "status": "complete"})
    status = client.get("/v1/billing", headers=A).json()
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (0, 0, 3)
    # The lifetime grandfather counter is no longer read or seeded.
    account = repo.db.execute("SELECT free_used_seed FROM billing_accounts WHERE user_id=?",
                              ("user-aaaaaaaa",)).fetchone()
    assert account["free_used_seed"] == 0
    # Only a free ledger entry actually charged this month consumes the monthly allowance.
    _analyse(client, app)
    assert client.get("/v1/billing", headers=A).json()["free_used"] == 1


# ---------------------------------------------------------------- settlement


def test_success_settles_the_reservation(service) -> None:
    client, app, _, _ = service()
    job_id = _analyse(client, app)
    status = client.get("/v1/billing", headers=A).json()
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (1, 0, 2)
    row = app.state.repo.get(job_id)
    usage = app.state.repo.db.execute("SELECT kind, state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()
    assert row["status"] == "complete" and (usage["kind"], usage["state"]) == ("free", "settled")


def test_terminal_failure_releases_the_reservation_once(service) -> None:
    client, app, _, _ = service(runner=FakeRunner(error="no_clear_swing"))
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=A).json()["status"] == "failed"
    released = app.state.repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()
    assert released["state"] == "released"
    assert client.get("/v1/billing", headers=A).json()["free_used"] == 0
    # The failed job's video is not an analysis; the golfer can try again.
    assert client.post("/v1/analysis/jobs", headers=A, json={
        "file_name": "b.mp4", "content_type": "video/mp4", "size_bytes": 10}).status_code == 201
    # ...and the released row is never re-settled into a second charge.
    settled_attempts = app.state.repo.get(job_id)["attempts"]
    app.state.billing.release(job_id, settled_attempts, "failed")
    assert app.state.repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "released"


def test_late_failure_of_a_deleted_swing_keeps_the_credit_charged(service) -> None:
    """Deleting a running swing must not return its analysis when the worker reports later."""

    client, app, _, _ = service(billing_free_analyses=1)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    attempts = app.state.repo.get(job_id)["attempts"]
    assert client.delete(f"/v1/swings/{job_id}", headers=A).status_code == 204
    assert app.state.billing.release(job_id, attempts, "failed") is None  # nothing transitioned
    usage = app.state.repo.db.execute("SELECT state, charge_month FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()
    assert usage["state"] == "forfeited"  # the deleted job's slot stays spent for the deletion month
    assert usage["charge_month"] is not None
    status = client.get("/v1/billing", headers=A).json()
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (1, 0, 0)
    assert client.post("/v1/analysis/jobs", headers=A, json={
        "file_name": "b.mp4", "content_type": "video/mp4", "size_bytes": 10}).status_code == 402


def test_a_stale_attempt_cannot_release_the_current_attempt_credit(service) -> None:
    client, app, _, _ = service(billing_free_analyses=1)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    # A second worker re-claims the job: attempts goes 1 -> 2 while it is claimed again.
    app.state.repo.update(job_id, status="processing", attempts=2)
    # The first attempt's late failure must not release the credit the second owns.
    assert app.state.billing.release(job_id, 1, "failed") is None
    assert app.state.repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "reserved"
    assert app.state.repo.get(job_id)["status"] == "processing"  # the owning attempt is untouched
    # The owning attempt can still settle or release it.
    assert app.state.repo.billing_finish(job_id, status="failed", attempts=2, error_code="failed") is not None
    assert app.state.repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "released"


def test_janitor_rechecks_a_refreshed_heartbeat_before_failing(service) -> None:
    client, app, _, _ = service()
    repo = app.state.repo
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    job_id = "00000000-0000-0000-0000-0000000000ab"
    repo.insert({"id": job_id, "user_id": "user-aaaaaaaa", "status": "processing", "attempts": 2, "updated_at": old})
    repo.db.execute("INSERT INTO billing_usage (swing_id,user_id,kind,state,created_at,updated_at)"
                    " VALUES (?,?,?,'reserved',?,?)", (job_id, "user-aaaaaaaa", "free", old, old))
    stale_row = repo.get(job_id)
    repo.update(job_id)  # a heartbeat lands after the janitor's query
    original_where = repo.where
    repo.where = lambda *args, **kwargs: [stale_row] if args[:1] == ("processing",) else original_where(*args, **kwargs)
    try:
        app.state.worker.janitor()
    finally:
        repo.where = original_where
    assert repo.get(job_id)["status"] == "processing"
    assert repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "reserved"


def test_a_heartbeat_at_the_terminal_boundary_does_not_release_the_credit(service) -> None:
    """The janitor pins the heartbeat it read; a heartbeat landing after that must win."""

    client, app, _, _ = service(billing_free_analyses=1)
    repo = app.state.repo
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    repo.update(job_id, status="processing", attempts=2)
    observed = repo.get(job_id)["updated_at"]
    repo.update(job_id)  # the worker heartbeats between the read and the terminal write
    stale_before = (datetime.now(UTC) - timedelta(minutes=15)).isoformat(timespec="seconds")
    assert app.state.billing.release(job_id, 2, "failed", expected_updated_at=observed,
                                     older_than=stale_before) is None
    assert repo.get(job_id)["status"] == "processing"
    assert repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "reserved"
    # Without the pinned heartbeat the owning attempt can still conclude the job.
    assert app.state.billing.release(job_id, 2, "failed") is not None
    assert repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()["state"] == "released"


def test_deleting_a_swing_never_refunds_the_allowance(service) -> None:
    client, app, _, _ = service(billing_free_analyses=1)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    assert client.delete(f"/v1/swings/{job_id}", headers=A).status_code == 204
    status = client.get("/v1/billing", headers=A).json()
    # The deleted, still-unqueued job forfeits its slot for the deletion month: the analysis is not
    # given back, and the slot does not sit reserved forever either.
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (1, 0, 0)
    assert client.post("/v1/analysis/jobs", headers=A, json={
        "file_name": "b.mp4", "content_type": "video/mp4", "size_bytes": 10}).status_code == 402
    usage = app.state.repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (job_id,)).fetchone()
    assert usage["state"] == "forfeited"


def test_janitor_fails_stale_exhausted_jobs_and_releases_them(service) -> None:
    client, app, _, _ = service()
    repo = app.state.repo
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    stuck = "00000000-0000-0000-0000-0000000000aa"
    repo.insert({"id": stuck, "user_id": "user-aaaaaaaa", "status": "processing", "attempts": 2, "updated_at": old})
    repo.db.execute("INSERT INTO billing_usage (swing_id,user_id,kind,state,created_at,updated_at)"
                    " VALUES (?,?,?,'reserved',?,?)", (stuck, "user-aaaaaaaa", "free", old, old))
    app.state.worker.janitor()
    assert repo.get(stuck)["status"] == "failed"
    assert repo.db.execute("SELECT state FROM billing_usage WHERE swing_id=?", (stuck,)).fetchone()["state"] == "released"
    assert client.get("/v1/billing", headers=A).json()["free_reserved"] == 0


def test_two_connections_cannot_overspend_one_free_credit(tmp_path) -> None:
    """The SQLite mirror is atomic across separate connections (Postgres uses the RPC)."""

    path = tmp_path / "db.sqlite3"
    first, second = SqliteRepo(path), SqliteRepo(path)
    for repo, swing in ((first, "00000000-0000-0000-0000-0000000000a1"), (second, "00000000-0000-0000-0000-0000000000a2")):
        repo.insert({"id": swing, "user_id": "user-aaaaaaaa", "status": "awaiting_upload"})
    results: list[tuple[str, dict]] = []
    barrier = threading.Barrier(2)

    def attempt(repo: SqliteRepo, swing: str) -> None:
        barrier.wait(timeout=10)
        results.append((swing, repo.billing_admit(user_id="user-aaaaaaaa", swing_id=swing, kind="free",
                                                  product_id=None, size_bytes=10, free_total=1)))

    threads = [threading.Thread(target=attempt, args=(first, "00000000-0000-0000-0000-0000000000a1")),
               threading.Thread(target=attempt, args=(second, "00000000-0000-0000-0000-0000000000a2"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(r["status"] for _, r in results) == ["admitted", "in_progress"]
    reserved = first.db.execute("SELECT COUNT(*) FROM billing_usage WHERE state='reserved'").fetchone()[0]
    assert reserved == 1
    # When the admitted job fails (its worker never finishes), the slot is released once and the
    # other job can be admitted: the reservation was a wait, not a permanent charge.
    winner = next(swing for swing, result in results if result["status"] == "admitted")
    loser = ("00000000-0000-0000-0000-0000000000a2" if winner == "00000000-0000-0000-0000-0000000000a1"
             else "00000000-0000-0000-0000-0000000000a1")
    first.update(winner, status="processing", attempts=1)
    assert first.billing_finish(winner, status="failed", attempts=1, error_code="failed") is not None
    assert second.billing_admit(user_id="user-aaaaaaaa", swing_id=loser, kind="free", product_id=None,
                                size_bytes=10, free_total=1)["status"] == "admitted"


def test_two_simultaneous_starts_for_the_same_last_credit_are_idempotent(service) -> None:
    """A double tap on /start at the last credit must not turn into a 402."""

    client, app, _, _ = service(billing_free_analyses=1)
    job_id = _upload(client)["job"]["id"]
    codes: list[int] = []
    barrier = threading.Barrier(2)

    def start() -> None:
        other = TestClient(app)
        barrier.wait(timeout=10)
        codes.append(other.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code)

    threads = [threading.Thread(target=start), threading.Thread(target=start)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert codes == [200, 200]
    rows = app.state.repo.db.execute("SELECT kind, state FROM billing_usage").fetchall()
    assert [(r["kind"], r["state"]) for r in rows] == [("free", "reserved")]
    assert app.state.repo.get(job_id)["status"] == "queued"


def test_two_different_jobs_racing_for_the_last_credit_admit_only_one(service) -> None:
    client, app, _, _ = service(billing_free_analyses=1)
    first = _upload(client)["job"]["id"]
    second = _upload(client)["job"]["id"]
    codes: list[int] = []
    barrier = threading.Barrier(2)

    def start(job_id: str) -> None:
        other = TestClient(app)
        barrier.wait(timeout=10)
        codes.append(other.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code)

    threads = [threading.Thread(target=start, args=(first,)), threading.Thread(target=start, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert sorted(codes) == [200, 409]
    loser = client.get("/v1/billing", headers=A).json()
    assert loser["access"] == "in_progress"
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE state='reserved'").fetchone()[0] == 1
    # Once the winner completes, the month's single free analysis is charged and the other job is a
    # genuine paywall answer.
    winner_id = app.state.repo.db.execute("SELECT swing_id FROM billing_usage WHERE state='reserved'").fetchone()[0]
    app.state.repo.update(winner_id, status="processing", attempts=1)
    assert app.state.billing.settle(winner_id, 1) is not None
    assert client.post(f"/v1/analysis/jobs/{first if winner_id != first else second}/start",
                       headers=A).status_code == 402


# ---------------------------------------------------------------- paid access


def test_active_entitlement_admits_as_paid_and_never_uses_free_credits(service) -> None:
    client, app, _, fake = service(client=FakeRevenueCat(Entitlement(
        active=True, product_id=PRODUCTS.lifetime, source="lifetime", environment="production")))
    for _ in range(4):
        _analyse(client, app)
    status = client.get("/v1/billing", headers=A).json()
    assert status["access"] == "pro" and status["entitlement_active"] is True
    assert (status["free_used"], status["free_remaining"]) == (0, 3)
    assert status["product_id"] == PRODUCTS.lifetime
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE kind='paid'").fetchone()[0] == 4


def test_expired_entitlement_falls_back_to_the_free_allowance(service) -> None:
    client, app, _, fake = service(client=FakeRevenueCat(Entitlement(
        active=False, product_id=PRODUCTS.monthly, expires_at=_iso(timedelta(days=-1)),
        environment="production")))
    _analyse(client, app)
    status = client.get("/v1/billing", headers=A).json()
    assert status["access"] == "free" and status["entitlement_active"] is False and status["free_used"] == 1


def test_sync_rejects_client_supplied_entitlement_state(service) -> None:
    client, _, _, fake = service()
    forged = client.post("/v1/billing/sync", headers=A, json={"entitlement_active": True})
    assert forged.status_code == 422
    forged_id = client.post("/v1/billing/sync", headers=A, json={"customer_id": "user-bbbbbbbb"})
    assert forged_id.status_code == 422
    ok = client.post("/v1/billing/sync", headers=A)
    assert ok.status_code == 200 and ok.json()["entitlement_active"] is False


def test_verification_always_uses_the_verified_auth_user(service) -> None:
    client, app, _, fake = service()
    job = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job}/start", headers=A)
    other = _upload(client, headers=B)["job"]["id"]
    # A request cannot pick the customer: the query/header are ignored, not trusted.
    client.post(f"/v1/analysis/jobs/{other}/start?user_id=user-aaaaaaaa",
                headers={**B, "X-RevenueCat-Customer-Id": "user-aaaaaaaa"})
    # Two verifications per analysis (preflight at create, admission at start),
    # always for the bearer token's user.
    assert fake.calls == ["user-aaaaaaaa", "user-aaaaaaaa", "user-bbbbbbbb", "user-bbbbbbbb"]


def test_provider_outage_and_malformed_responses_refuse_admission(service) -> None:
    for kind in ("unavailable", "malformed"):
        client, app, _, fake = service()
        fake.error = RevenueCatError(kind, "boom")
        blocked = client.post("/v1/analysis/jobs", headers=A, json={
            "file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10})
        assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "billing_unavailable"
        assert blocked.json()["error"]["code"] != "purchase_required"
        assert client.get("/v1/billing", headers=A).status_code == 503


# ---------------------------------------------------------------- client parsing


def _client(handler, environment: str = "production") -> tuple[RevenueCatClient, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return RevenueCatClient("sk_test", PRODUCTS, environment, client=http), calls


def _subscriber(**overrides) -> dict:
    entitlements = overrides.pop("entitlements", None)
    if entitlements is None:
        entitlements = {"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": _iso(timedelta(days=20)),
                                "grace_period_expires_date": None, "purchase_date": _iso(timedelta(days=1))}}
    subscriptions = {"com.swingguru.app.pro.monthly": {
        "store": "app_store", "is_sandbox": False, "expires_date": _iso(timedelta(days=20)),
        "grace_period_expires_date": None, "purchase_date": _iso(timedelta(days=1)),
        "refunded_at": None, "unsubscribe_detected_at": None, "period_type": "normal"}}
    subscriptions.update(overrides.pop("subscriptions", {}))
    body = {"request_date": _iso(timedelta()), "subscriber": {
        "entitlements": entitlements, "subscriptions": subscriptions, "non_subscriptions": {},
        "management_url": "https://apps.apple.com/account/subscriptions"}}
    for key, value in overrides.items():
        if key == "subscriber":
            body["subscriber"].update(value)
        else:
            body[key] = value
    return body


def test_active_subscription_is_verified_and_cancellation_keeps_access() -> None:
    live = _client(lambda _: httpx.Response(200, json=_subscriber(
        subscriptions={"com.swingguru.app.pro.monthly": {
            "store": "app_store", "is_sandbox": False, "expires_date": _iso(timedelta(days=5)),
            "purchase_date": _iso(timedelta(days=25)), "unsubscribe_detected_at": _iso(timedelta(days=1)),
            "refunded_at": None}})))
    entitlement = live[0].fetch("0c6f5d8e-1111-2222-3333-444455556666")
    assert entitlement.active and entitlement.source == "subscription" and entitlement.product_id == PRODUCTS.monthly
    assert live[1][0].url.path == "/v1/subscribers/0c6f5d8e-1111-2222-3333-444455556666"
    assert live[1][0].headers["authorization"] == "Bearer sk_test"
    assert "X-Is-Sandbox" not in live[1][0].headers


def test_get_or_create_201_is_a_success() -> None:
    """The official customers doc documents 200 and 201 for GET or Create Customer."""

    client, _ = _client(lambda _: httpx.Response(201, json=_subscriber()))
    entitlement = client.fetch("brand-new-user")
    assert entitlement.active is True


def test_anonymous_style_ids_are_url_encoded() -> None:
    client, calls = _client(lambda _: httpx.Response(200, json=_subscriber()))
    client.fetch("$RCAnonymousID:0c6f5d8e")
    assert "%24RCAnonymousID" in str(calls[0].url)


@pytest.mark.parametrize("scenario,entitlement_active", [
    ("expired", False),
    ("grace_period", True),
    ("refunded", False),
    ("wrong_store", False),
    ("wrong_environment", False),
    ("unpermitted_product", False),
    ("promotional_null_expiry", False),
    ("lifetime_without_record", False),
])
def test_untrusted_entitlement_shapes_never_grant_paid(scenario: str, entitlement_active: bool) -> None:
    expired = _iso(timedelta(days=-1))
    if scenario == "expired":
        body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": expired}},
                           subscriptions={"com.swingguru.app.pro.monthly": {"store": "app_store", "is_sandbox": False,
                                                                             "expires_date": expired}})
    elif scenario == "grace_period":
        body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": expired,
                                                 "grace_period_expires_date": _iso(timedelta(days=3))}},
                           subscriptions={"com.swingguru.app.pro.monthly": {
                               "store": "app_store", "is_sandbox": False, "expires_date": expired,
                               "grace_period_expires_date": _iso(timedelta(days=3)),
                               "purchase_date": _iso(timedelta(days=40))}})
    elif scenario == "refunded":
        body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": _iso(timedelta(days=20)),
                                                 "purchase_date": _iso(timedelta(days=-2))}},
                           subscriptions={"com.swingguru.app.pro.monthly": {
                               "store": "app_store", "is_sandbox": False, "expires_date": _iso(timedelta(days=20)),
                               "purchase_date": _iso(timedelta(days=-2)), "refunded_at": _iso(timedelta(days=-1))}})
    elif scenario == "wrong_store":
        body = _subscriber(subscriptions={"com.swingguru.app.pro.monthly": {
            "store": "play_store", "is_sandbox": False, "expires_date": _iso(timedelta(days=20))}})
    elif scenario == "wrong_environment":
        body = _subscriber(subscriptions={"com.swingguru.app.pro.monthly": {
            "store": "app_store", "is_sandbox": True, "expires_date": _iso(timedelta(days=20))}})
    elif scenario == "unpermitted_product":
        body = _subscriber(entitlements={"pro": {"product_identifier": "com.other.product", "expires_date": _iso(timedelta(days=20))}})
    elif scenario == "promotional_null_expiry":
        body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": None}})
    else:  # lifetime_without_record: a null expiry and no non-subscription purchase
        body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.lifetime, "expires_date": None}})
    client, _ = _client(lambda _: httpx.Response(200, json=body))
    assert client.fetch("user-aaaaaaaa").active is entitlement_active


def test_grace_period_is_reported_as_active_until_it_ends() -> None:
    expired = _iso(timedelta(days=-1))
    body = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": expired,
                                             "grace_period_expires_date": _iso(timedelta(days=3))}},
                       subscriptions={"com.swingguru.app.pro.monthly": {
                           "store": "app_store", "is_sandbox": False, "expires_date": expired,
                           "grace_period_expires_date": _iso(timedelta(days=3)),
                           "purchase_date": _iso(timedelta(days=40))}})
    client, _ = _client(lambda _: httpx.Response(200, json=body))
    entitlement = client.fetch("user-aaaaaaaa")
    assert entitlement.active and entitlement.in_grace
    # A grace period that has also expired is not access.
    ended = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": expired,
                                              "grace_period_expires_date": _iso(timedelta(days=-2))}},
                        subscriptions={"com.swingguru.app.pro.monthly": {
                            "store": "app_store", "is_sandbox": False, "expires_date": expired,
                            "grace_period_expires_date": _iso(timedelta(days=-2)),
                            "purchase_date": _iso(timedelta(days=40))}})
    ended_client, _ = _client(lambda _: httpx.Response(200, json=ended))
    assert ended_client.fetch("user-aaaaaaaa").active is False


def test_lifetime_requires_the_non_consumable_record() -> None:
    body = _subscriber(
        entitlements={"pro": {"product_identifier": PRODUCTS.lifetime, "expires_date": None,
                              "purchase_date": _iso(timedelta(days=10))}},
        subscriber={"non_subscriptions": {PRODUCTS.lifetime: [
            {"id": "abc", "store": "app_store", "is_sandbox": False, "purchase_date": _iso(timedelta(days=10))}]}})
    client, _ = _client(lambda _: httpx.Response(200, json=body))
    entitlement = client.fetch("user-aaaaaaaa")
    assert entitlement.active and entitlement.source == "lifetime" and entitlement.expires_at is None
    # A sandbox lifetime in a production deployment is rejected.
    sandboxed = _subscriber(
        entitlements={"pro": {"product_identifier": PRODUCTS.lifetime, "expires_date": None}},
        subscriber={"non_subscriptions": {PRODUCTS.lifetime: [
            {"id": "abc", "store": "app_store", "is_sandbox": True, "purchase_date": _iso(timedelta(days=10))}]}})
    client, _ = _client(lambda _: httpx.Response(200, json=sandboxed))
    assert client.fetch("user-aaaaaaaa").active is False


def test_lifetime_revocation_signals_are_not_active() -> None:
    """The documented non-subscription record has no refund field; if one ever appears, reject it."""

    for marker in ({"refunded_at": _iso(timedelta(days=-1))}, {"revoked_at": _iso(timedelta(days=-1))},
                   {"is_revoked": True}):
        body = _subscriber(
            entitlements={"pro": {"product_identifier": PRODUCTS.lifetime, "expires_date": None}},
            subscriber={"non_subscriptions": {PRODUCTS.lifetime: [
                {"id": "abc", "store": "app_store", "is_sandbox": False,
                 "purchase_date": _iso(timedelta(days=10)), **marker}]}})
        client, _ = _client(lambda _: httpx.Response(200, json=body))
        assert client.fetch("user-aaaaaaaa").active is False, marker
    # The entitlement disappearing entirely (the documented effect of a non-consumable refund)
    # is already the not-entitled path.
    body = _subscriber(entitlements={}, subscriber={"non_subscriptions": {}})
    client, _ = _client(lambda _: httpx.Response(200, json=body))
    assert client.fetch("user-aaaaaaaa").active is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_is_sandbox_must_be_a_real_boolean(value) -> None:
    body = _subscriber(subscriptions={"com.swingguru.app.pro.monthly": {
        "store": "app_store", "is_sandbox": value, "expires_date": _iso(timedelta(days=20))}})
    client, _ = _client(lambda _: httpx.Response(200, json=body))
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "malformed"


def test_missing_is_sandbox_and_missing_expires_date_are_malformed() -> None:
    no_sandbox = _subscriber(subscriptions={"com.swingguru.app.pro.monthly": {
        "store": "app_store", "expires_date": _iso(timedelta(days=20))}})
    client, _ = _client(lambda _: httpx.Response(200, json=no_sandbox))
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "malformed"

    # A missing expires_date key is not the same as an explicit null (lifetime).
    no_expiry = _subscriber(entitlements={"pro": {"product_identifier": PRODUCTS.monthly,
                                                  "purchase_date": _iso(timedelta(days=1))}})
    client, _ = _client(lambda _: httpx.Response(200, json=no_expiry))
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "malformed"

    no_product = _subscriber(entitlements={"pro": {"expires_date": None, "purchase_date": _iso(timedelta(days=1))}})
    client, _ = _client(lambda _: httpx.Response(200, json=no_product))
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "malformed"


def test_sandbox_deployment_accepts_sandbox_records_and_asks_for_them() -> None:
    body = _subscriber(subscriptions={"com.swingguru.app.pro.monthly": {
        "store": "app_store", "is_sandbox": True, "expires_date": _iso(timedelta(days=20))}},
        entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": _iso(timedelta(days=20))}})
    client, calls = _client(lambda _: httpx.Response(200, json=body), environment="sandbox")
    assert client.fetch("user-aaaaaaaa").active is True
    assert calls[0].headers["X-Is-Sandbox"] == "true"


@pytest.mark.parametrize("status", [500, 502, 429, 404, 401, 403])
def test_provider_failures_are_unavailable_never_unentitled(status: int) -> None:
    client, _ = _client(lambda _: httpx.Response(status, json={"error": "boom"}))
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "unavailable"


def test_malformed_bodies_are_rejected() -> None:
    for body in ([], {"subscriber": None}, {"subscriber": {"entitlements": []}},
                 {"subscriber": {"entitlements": {"pro": {"product_identifier": PRODUCTS.monthly,
                                                          "expires_date": "not-a-date"}}}},
                 {"subscriber": {"entitlements": {"pro": {"product_identifier": PRODUCTS.monthly,
                                                          "expires_date": _iso(timedelta(days=1))}},
                                 "subscriptions": {}}}):
        client, _ = _client(lambda _: httpx.Response(200, json=body))
        with pytest.raises(RevenueCatError) as error:
            client.fetch("user-aaaaaaaa")
        assert error.value.kind == "malformed"


def test_transport_errors_are_unavailable() -> None:
    class Broken:
        def get(self, *_args, **_kwargs):
            raise httpx.ConnectError("no route")

    client = RevenueCatClient("sk_test", PRODUCTS, "production", client=Broken())
    with pytest.raises(RevenueCatError) as error:
        client.fetch("user-aaaaaaaa")
    assert error.value.kind == "unavailable"


# ---------------------------------------------------------------- supabase adapter


def test_postgrest_billing_calls_the_right_rpcs() -> None:
    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("billing_finish"):
            return httpx.Response(200, json=[{"id": "s1", "status": "complete"}])
        return httpx.Response(200, json={"free_total": 3, "free_used_seed": 0, "free_settled": 1,
                                         "free_reserved": 0, "status": "admitted"})

    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert repo.billing_status("u1", 3)["free_settled"] == 1
    assert repo.billing_admit(user_id="u1", swing_id="s1", kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "admitted"
    assert repo.billing_finish("s1", status="complete", attempts=1, error_code=None)["id"] == "s1"
    assert [c.url.path for c in calls] == ["/rest/v1/rpc/billing_status", "/rest/v1/rpc/billing_admit",
                                           "/rest/v1/rpc/billing_finish"]
    assert json.loads(calls[0].content) == {"p_user_id": "u1", "p_free_total": 3}
    assert json.loads(calls[1].content) == {"p_user_id": "u1", "p_swing_id": "s1", "p_kind": "free",
                                            "p_product_id": None, "p_size_bytes": 10, "p_free_total": 3}
    assert json.loads(calls[2].content)["p_attempts"] == 1
    assert "p_expected_updated_at" in json.loads(calls[2].content)
    with pytest.raises(RepoError):
        repo.billing_finish("s1", status="complete", attempts=1, role="service_role")


def test_missing_billing_settlement_row_is_none() -> None:
    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    repo = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))))
    assert repo.billing_finish("gone", status="failed", attempts=1, error_code="failed") is None


def test_billing_migration_is_additive_and_locked_down() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = root / "supabase" / "migrations" / "20261002000000_billing.sql"
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    assert "create table if not exists public.billing_accounts" in sql
    assert "create table if not exists public.billing_usage" in sql
    assert "alter table public.billing_accounts enable row level security" in sql
    assert "alter table public.billing_usage enable row level security" in sql
    assert sql.count("set search_path = ''") == 3
    assert sql.count("security definer") == 3
    # The terminal write is guarded by the attempt that produced the result, and the
    # ledger only moves after that guarded transition succeeded.
    assert "p_attempts integer" in sql
    assert "and s.attempts = p_attempts" in sql
    assert "p_expected_updated_at timestamptz" in sql
    assert "p_older_than timestamptz" in sql
    assert "p_older_than is null or s.updated_at < p_older_than" in sql
    assert "p_expected_updated_at is null or s.updated_at = p_expected_updated_at" in sql
    assert sql.index("if v_row.id is null then") < sql.index("update public.billing_usage u")
    assert "references auth.users (id) on delete cascade" in sql
    # No FK to swings: deleting a swing must not return a consumed analysis.
    assert "references public.swings" not in sql
    # Nothing historical is altered or dropped by this migration.
    assert "alter table public.swings" not in sql
    assert "drop table" not in sql.lower()
    assert "20261002000000" > "20261001000000"  # strictly after the newest existing migration
    assert (root / "supabase" / "migrations" / "20261001000000_club_list.sql").exists()


def test_owned_product_without_the_entitlement_is_diagnosed_but_never_unlocks(caplog) -> None:
    """Build 8 TestFlight (2026-10-05): a real sandbox monthly trial reached RevenueCat with
    ``entitlements: {}`` because the products were not attached to ``pro`` in the dashboard. The
    server must keep refusing access, and say why in the log (product ids only)."""

    future = (datetime.now(timezone.utc) + timedelta(days=13)).isoformat().replace("+00:00", "Z")
    payload = {"subscriber": {"entitlements": {}, "management_url": None, "non_subscriptions": {},
                              "subscriptions": {PRODUCTS.monthly: {"store": "app_store", "is_sandbox": True,
                                                                   "expires_date": future, "period_type": "trial"}}}}
    client, _ = _client(lambda request: httpx.Response(200, json=payload), environment="sandbox")
    with caplog.at_level("WARNING", logger="swingguru.billing"):
        result = client.fetch("user-1")
    assert result.active is False
    assert any(PRODUCTS.monthly in r.message and "attach" in r.message for r in caplog.records)
    assert not any("user-1" in r.message for r in caplog.records)


def test_no_purchases_and_no_entitlement_logs_nothing(caplog) -> None:
    payload = {"subscriber": {"entitlements": {}, "subscriptions": {}, "non_subscriptions": {}}}
    client, _ = _client(lambda request: httpx.Response(200, json=payload), environment="sandbox")
    with caplog.at_level("WARNING", logger="swingguru.billing"):
        assert client.fetch("user-2").active is False
    assert not caplog.records


# ---------------------------------------------------------------- SG_BILLING_ENV=both (App Review + customers)


def _monthly(is_sandbox: bool, **extra) -> dict:
    record = {"store": "app_store", "is_sandbox": is_sandbox, "expires_date": _iso(timedelta(days=20)),
              "purchase_date": _iso(timedelta(days=-2)), "refunded_at": None, "unsubscribe_detected_at": None}
    record.update(extra)
    return _subscriber(subscriptions={PRODUCTS.monthly: record})


def _lifetime(is_sandbox: bool) -> dict:
    return _subscriber(
        entitlements={"pro": {"product_identifier": PRODUCTS.lifetime, "expires_date": None,
                              "grace_period_expires_date": None, "purchase_date": _iso(timedelta(days=3))}},
        subscriber={"non_subscriptions": {PRODUCTS.lifetime: [
            {"id": "x", "store": "app_store", "is_sandbox": is_sandbox, "purchase_date": _iso(timedelta(days=3))}]}})


def test_both_is_an_explicit_deployment_choice() -> None:
    validate(Settings(billing_mode="on", revenuecat_secret_key="sk_test", billing_env="both",
                      billing_paid_allowance="unlimited"))
    with pytest.raises(ConfigError):
        validate(Settings(billing_mode="on", revenuecat_secret_key="sk_test", billing_env="any",
                          billing_paid_allowance="unlimited"))


@pytest.mark.parametrize("is_sandbox,label", [(True, "sandbox"), (False, "production")])
def test_both_accepts_either_environment_and_labels_it_from_the_purchase_record(is_sandbox, label, caplog) -> None:
    for payload, source in ((_monthly(is_sandbox), "subscription"), (_lifetime(is_sandbox), "lifetime")):
        client, calls = _client(lambda _: httpx.Response(200, json=payload), environment="both")
        with caplog.at_level("INFO", logger="swingguru.billing"):
            entitlement = client.fetch("user-1")
        assert entitlement.active and entitlement.environment == label and entitlement.source == source
        assert "X-Is-Sandbox" not in calls[0].headers          # one production-style request
        assert any(f"environment={label}" in r.message for r in caplog.records)
        assert not any("user-1" in r.message for r in caplog.records)


def test_both_does_not_weaken_any_other_check() -> None:
    expired = _iso(timedelta(days=-1))
    refused = {
        "refunded sandbox": _monthly(True, refunded_at=_iso(timedelta(days=-1))),
        "expired sandbox": _subscriber(
            entitlements={"pro": {"product_identifier": PRODUCTS.monthly, "expires_date": expired,
                                  "grace_period_expires_date": None, "purchase_date": _iso(timedelta(days=30))}},
            subscriptions={PRODUCTS.monthly: {"store": "app_store", "is_sandbox": True, "expires_date": expired,
                                              "purchase_date": _iso(timedelta(days=30)), "refunded_at": None}}),
        "other store": _monthly(True, store="play_store"),
        "no pro entitlement": _subscriber(entitlements={}),
        "unaccepted product": _subscriber(entitlements={"pro": {"product_identifier": "com.other.product",
                                                                "expires_date": _iso(timedelta(days=5))}}),
    }
    for name, payload in refused.items():
        client, _ = _client(lambda _, p=payload: httpx.Response(200, json=p), environment="both")
        assert client.fetch("user-1").active is False, name
    # A string "true" is not a boolean: a malformed record is an error, never a guess.
    client, _ = _client(lambda _: httpx.Response(200, json=_monthly("true")), environment="both")  # type: ignore[arg-type]
    with pytest.raises(Exception):
        client.fetch("user-1")


@pytest.mark.parametrize("deployment,is_sandbox", [("production", True), ("sandbox", False)])
def test_single_environment_deployments_stay_strict(deployment, is_sandbox) -> None:
    client, _ = _client(lambda _: httpx.Response(200, json=_monthly(is_sandbox)), environment=deployment)
    assert client.fetch("user-1").active is False
