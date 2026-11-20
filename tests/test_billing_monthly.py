"""Monthly free allowance: UTC calendar-month charging, reservations across the boundary, deletion
forfeit and the phase-1 sign-in guard.

The SQLite mirror takes an injected server clock, so "the last instant of October" and "the first
instant of November" are exact instants rather than sleeps. The API + worker run for real against
local signed storage; only RevenueCat is faked.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

from swingguru.service.api import create_app
from swingguru.service.billing import BillingExhausted, BillingInProgress, BillingService, Entitlement, FreeQuota
from swingguru.service.config import Settings
from swingguru.service import repo as repo_module
from swingguru.service.repo import SqliteRepo, month_bounds, month_key, parse_stamp
from swingguru.service.storage import LocalStorage
from test_billing import PRODUCTS, A, B, FakeRevenueCat, _upload
from test_service import FakeRunner

UTC = timezone.utc
JWT_SECRET = "monthly-allowance-test-secret-0123456789"

OCT_END = datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC)
NOV_START = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
OCT_START = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)


class Clock:
    """The injected server clock: the database, not the device, decides the month."""

    def __init__(self, when: datetime) -> None:
        self.when = when

    def __call__(self) -> datetime:
        return self.when

    def set(self, when: datetime) -> None:
        self.when = when


class QueueClock(Clock):
    """A clock whose next readings can be queued: proves how many times an operation reads it.

    The queue is a loud signal, not a simulation gimmick: a transaction that captures one instant
    consumes exactly one reading, so a test can assert that no second read happened at all (and with
    it, that no month can be derived from a different instant than the timestamp).
    """

    def __init__(self, when: datetime) -> None:
        super().__init__(when)
        self.queue: list[datetime] = []

    def push(self, *values: datetime) -> None:
        self.queue.extend(values)

    def __call__(self) -> datetime:
        if self.queue:
            self.when = self.queue.pop(0)
        return self.when


def monthly_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "svc", public_url="http://testserver",
        billing_mode="on", revenuecat_secret_key="sk_test", billing_env="production",
        billing_paid_allowance="unlimited",
    )
    return replace(Settings(), **{**base, **overrides})


def supabase_settings(tmp_path: Path, **overrides) -> Settings:
    return monthly_settings(
        tmp_path, auth_mode="supabase", supabase_url="https://proj.supabase.co",
        supabase_jwt_secret=JWT_SECRET, **overrides,
    )


def token(user: str, *, anonymous: bool = False) -> str:
    claims = {"sub": user, "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + 600}
    if anonymous:
        claims["is_anonymous"] = True
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def auth(user: str, *, anonymous: bool = False) -> dict:
    """The Authorization header for a signed Supabase-style JWT."""

    return {"Authorization": f"Bearer {token(user, anonymous=anonymous)}"}


@pytest.fixture
def monthly(tmp_path):
    """API + worker with billing on, a fake RevenueCat client and a controllable clock."""

    def make(runner=None, client=None, clock=None, when=OCT_START, settings=None, **overrides):
        clock = clock or Clock(when)
        settings = settings or monthly_settings(tmp_path, **overrides)
        repo = SqliteRepo(tmp_path / "svc" / "db.sqlite3", clock=clock)
        storage = LocalStorage(settings)
        fake = client or FakeRevenueCat()
        billing = BillingService(settings, repo, client=fake)
        runner = runner or FakeRunner()
        app = create_app(settings, repo=repo, storage=storage, start_worker=False,
                         runner=runner, billing=billing)
        return TestClient(app), app, fake, clock

    return make


def _complete(client, app, headers=A) -> str:
    """Upload, start and run one analysis to completion; returns the swing id."""

    job_id = _upload(client, headers=headers)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers).status_code == 200
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=headers).json()["status"] == "complete"
    return job_id


def _create(client, headers=A, **body):
    payload = {"file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10, **body}
    return client.post("/v1/analysis/jobs", headers=headers, json=payload)


def _status(client, headers=A) -> dict:
    response = client.get("/v1/billing", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _swing_id(n: int) -> str:
    return f"30000000-0000-4000-8000-{n:012d}"


def _ledger(repo, swing: str) -> dict:
    row = repo.db.execute(
        "SELECT state, kind, admitted_month, charge_month, settled_at, created_at, updated_at"
        " FROM billing_usage WHERE swing_id=?", (swing,)).fetchone()
    return dict(row) if row is not None else {}


# ---------------------------------------------------------------- month arithmetic


def test_month_arithmetic_is_utc_half_open_and_handles_december_and_february() -> None:
    assert month_key(datetime(2026, 10, 15, 12, tzinfo=UTC)) == "2026-10-01"
    assert month_bounds(OCT_END) == ("2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00")
    # The instant the month turns belongs to the new month.
    assert month_key(NOV_START) == "2026-11-01"
    assert month_bounds(NOV_START) == ("2026-11-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00")
    # December -> January.
    assert month_bounds(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == (
        "2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    # Leap and non-leap February.
    assert month_bounds(datetime(2028, 2, 29, 12, 0, tzinfo=UTC))[1] == "2028-03-01T00:00:00+00:00"
    assert month_bounds(datetime(2027, 2, 28, 12, 0, tzinfo=UTC))[1] == "2027-03-01T00:00:00+00:00"
    # A timezone-flagged device clock cannot move a month: the value is normalised to UTC.
    assert month_key(datetime(2026, 11, 1, 0, 30, tzinfo=timezone(timedelta(hours=2)))) == "2026-10-01"


# ---------------------------------------------------------------- charging months


def test_a_completion_is_charged_to_the_month_it_finishes_in(monthly) -> None:
    client, app, _, clock = monthly(when=OCT_END)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    # The job is admitted on the last instant of October and finishes a second later, in November.
    clock.set(NOV_START)
    assert app.state.worker.run_once() is True
    row = app.state.repo.db.execute("SELECT state, charge_month, admitted_month FROM billing_usage"
                                    " WHERE swing_id=?", (job_id,)).fetchone()
    assert (row["state"], row["charge_month"], row["admitted_month"]) == ("settled", "2026-11-01", "2026-10-01")
    # It is charged to November; October keeps its unused allowance.
    assert _status(client)["free_used"] == 1
    assert _status(client)["allowance_period_start"].startswith("2026-11-01")
    assert _status(client)["allowance_resets_at"].startswith("2026-12-01")


def test_an_unused_month_is_not_carried_over(monthly) -> None:
    client, app, _, clock = monthly()
    _complete(client, app)
    _complete(client, app)
    assert _status(client)["free_used"] == 2
    assert _status(client)["free_remaining"] == 1
    clock.set(NOV_START)
    # September/October never rolls over: November simply starts with the full allowance again.
    assert _status(client)["free_used"] == 0
    assert _status(client)["free_remaining"] == 3


def test_exhausted_resets_at_the_first_instant_of_the_next_month(monthly) -> None:
    client, app, _, clock = monthly(billing_free_analyses=1)
    _complete(client, app)
    assert _status(client)["access"] == "exhausted"
    blocked = _create(client)
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "purchase_required"
    clock.set(datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC))
    assert _create(client).status_code == 402          # still October
    clock.set(NOV_START)
    # The new month's allowance: a fresh upload can be admitted and started.
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    assert app.state.worker.run_once() is True


def test_deleted_completions_stay_spent_for_the_month_and_reset_next_month(monthly) -> None:
    client, app, _, clock = monthly(billing_free_analyses=1)
    swing = _complete(client, app)
    assert client.delete(f"/v1/swings/{swing}", headers=A).status_code == 204
    status = _status(client)
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (1, 0, 0)
    assert _create(client).status_code == 402
    clock.set(NOV_START)
    assert _create(client).status_code == 201


# ---------------------------------------------------------------- reservations across a boundary


def test_a_pending_reservation_holds_its_slot_into_the_new_month_and_is_charged_there(monthly) -> None:
    client, app, _, clock = monthly(billing_free_analyses=1, when=OCT_END)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    assert _status(client)["free_reserved"] == 1

    clock.set(NOV_START)
    status = _status(client)
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (0, 1, 0)
    assert status["access"] == "in_progress" and status["can_analyse"] is False
    # Not a paywall: the golfer is told to wait, not to buy.
    blocked = _create(client)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "free_analyses_in_progress"
    assert blocked.json()["error"]["code"] != "purchase_required"

    assert app.state.worker.run_once() is True
    settled = _status(client)
    assert (settled["free_used"], settled["free_reserved"], settled["access"]) == (1, 0, "exhausted")
    assert _create(client).status_code == 402


def test_a_last_month_reservation_that_fails_frees_the_new_month(monthly) -> None:
    client, app, _, clock = monthly(billing_free_analyses=1, when=OCT_END,
                                    runner=FakeRunner(error="no_clear_swing"))
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    clock.set(NOV_START)
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=A).json()["status"] == "failed"
    status = _status(client)
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (0, 0, 1)
    assert _create(client).status_code == 201


def test_new_completions_after_an_old_released_reservation_are_charged_normally(monthly) -> None:
    client, app, _, clock = monthly(billing_free_analyses=2, when=OCT_END,
                                    runner=FakeRunner(error="no_clear_swing"))
    old = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{old}/start", headers=A).status_code == 200
    clock.set(NOV_START)
    assert app.state.worker.run_once() is True                 # released, not charged
    assert client.delete(f"/v1/swings/{old}", headers=A).status_code == 204  # deleting a released row changes nothing
    assert _status(client)["free_used"] == 0
    # A working runner from here on: two fresh analyses fill November.
    app.state.worker.runner = FakeRunner()
    _complete(client, app)
    _complete(client, app)
    assert _status(client)["free_used"] == 2
    assert _create(client).status_code == 402


# ---------------------------------------------------------------- deletion and worker races


def test_delete_then_late_worker_settlement_cannot_resurrect_or_refund(monthly) -> None:
    client, app, _, _ = monthly(billing_free_analyses=1)
    job_id = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A).status_code == 200
    app.state.repo.update(job_id, status="processing", attempts=1)
    attempts = app.state.repo.get(job_id)["attempts"]
    assert client.delete(f"/v1/swings/{job_id}", headers=A).status_code == 204
    # The late "complete" (and a late failure) can never move the forfeited row.
    assert app.state.billing.settle(job_id, attempts) is None
    assert app.state.billing.release(job_id, attempts, "failed") is None
    row = app.state.repo.db.execute("SELECT state, charge_month FROM billing_usage WHERE swing_id=?",
                                    (job_id,)).fetchone()
    assert row["state"] == "forfeited" and row["charge_month"] == "2026-10-01"
    assert app.state.repo.get(job_id) is None
    assert _status(client)["free_used"] == 1


def test_settle_then_delete_keeps_the_charged_month(monthly) -> None:
    client, app, _, _ = monthly(billing_free_analyses=1)
    swing = _complete(client, app)
    assert client.delete(f"/v1/swings/{swing}", headers=A).status_code == 204
    row = app.state.repo.db.execute("SELECT state, charge_month FROM billing_usage WHERE swing_id=?",
                                    (swing,)).fetchone()
    assert row["state"] == "settled" and row["charge_month"] == "2026-10-01"
    assert _status(client)["free_used"] == 1


# ---------------------------------------------------------------- plan transitions


def test_free_then_pro_then_expired_pro_in_the_same_and_the_next_month(monthly) -> None:
    fake = FakeRevenueCat()
    client, app, _, clock = monthly(client=fake)
    _complete(client, app)
    _complete(client, app)
    assert _status(client)["free_used"] == 2

    fake.entitlement = Entitlement(active=True, product_id=PRODUCTS.monthly, source="subscription",
                                   expires_at="2026-11-20T00:00:00+00:00", environment="production")
    pro = _status(client)
    assert pro["access"] == "pro" and pro["entitlement_active"] is True
    assert pro["free_used"] == 2                       # untouched by paid work
    assert pro["paid_allowance"] == "unlimited"
    _complete(client, app)                             # a paid analysis
    assert _status(client)["free_used"] == 2
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE kind='paid'").fetchone()[0] == 1

    fake.entitlement = Entitlement(active=False, product_id=PRODUCTS.monthly,
                                   expires_at="2026-09-30T00:00:00+00:00", environment="production")
    assert _status(client)["access"] == "free"
    assert _status(client)["free_remaining"] == 1
    _complete(client, app)
    assert _status(client)["access"] == "exhausted"
    assert _create(client).status_code == 402

    clock.set(NOV_START)
    assert _status(client)["free_remaining"] == 3
    assert _create(client).status_code == 201


def test_a_mocked_trial_entitlement_is_unlimited_and_keeps_rate_limits(monthly) -> None:
    fake = FakeRevenueCat(Entitlement(active=True, product_id=PRODUCTS.monthly, source="subscription",
                                      in_grace=False, expires_at="2026-10-16T00:00:00+00:00",
                                      environment="production"))
    # The injected clock sits at the real "now" so the per-hour window (which reads the real clock)
    # behaves as it would in production.
    client, app, _, _ = monthly(client=fake, jobs_per_hour=3,
                                when=datetime.now(UTC).replace(microsecond=0))
    for _ in range(3):
        _complete(client, app)
    status = _status(client)
    assert status["access"] == "pro" and status["free_used"] == 0 and status["free_remaining"] == 3
    # The subscription allowance is unlimited, but the operational rate limit is untouched.
    blocked = _create(client)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "too_many_analyses"


# ---------------------------------------------------------------- identity and durability


def test_a_restarted_process_with_the_same_verified_user_keeps_the_usage(monthly, tmp_path) -> None:
    client, app, _, _ = monthly(when=OCT_START)
    _complete(client, app)
    assert _status(client)["free_used"] == 1
    # A brand-new repo object, service and API client against the same database: same UUID, same count.
    repo = SqliteRepo(tmp_path / "svc" / "db.sqlite3", clock=Clock(OCT_START))
    billing = BillingService(monthly_settings(tmp_path), repo, client=FakeRevenueCat())
    assert billing.status("user-aaaaaaaa")["free_used"] == 1
    assert billing.status("user-bbbbbbbb")["free_used"] == 0


def test_two_verified_users_have_independent_allowances(monthly) -> None:
    client, app, _, _ = monthly(billing_free_analyses=1)
    _complete(client, app, headers=A)
    assert _status(client, headers=A)["free_remaining"] == 0
    assert _status(client, headers=B)["free_remaining"] == 1
    assert _create(client, headers=B).status_code == 201
    assert _create(client, headers=A).status_code == 402


def test_anonymous_free_access_is_refused_and_pro_guests_keep_working(monthly, tmp_path) -> None:
    settings = supabase_settings(tmp_path)
    fake = FakeRevenueCat()
    client, app, _, _ = monthly(settings=settings, client=fake)
    guest = auth("0c6f5d8e-1111-2222-3333-444455556666", anonymous=True)
    owner = auth("11111111-2222-3333-4444-555555555555")

    status = _status(client, headers=guest)
    assert status["access"] == "sign_in_required"
    assert status["requires_sign_in"] is True and status["can_analyse"] is False
    refused = _create(client, headers=guest)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "sign_in_required"
    assert refused.json()["error"]["code"] != "purchase_required"
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage").fetchone()[0] == 0

    # A durable account is unaffected.
    assert _create(client, headers=owner).status_code == 201

    # An active verified Pro/trial guest keeps working while the conversion flow is prepared.
    fake.entitlement = Entitlement(active=True, product_id=PRODUCTS.lifetime, source="lifetime",
                                   environment="production")
    guest_status = _status(client, headers=guest)
    assert guest_status["access"] == "pro" and guest_status["requires_sign_in"] is False
    assert _create(client, headers=guest).status_code == 201


def test_free_analysis_without_sign_in_when_relaxed(monthly, tmp_path) -> None:
    """SG_FREE_REQUIRES_SIGN_IN=0: a guest spends the monthly free analysis, then hits the paywall (not sign-in)."""

    settings = replace(supabase_settings(tmp_path), free_requires_sign_in=False)
    client, app, _, _ = monthly(settings=settings, client=FakeRevenueCat())
    guest = auth("0c6f5d8e-1111-2222-3333-444455556666", anonymous=True)
    status = _status(client, headers=guest)
    assert status["access"] == "free" and status["requires_sign_in"] is False and status["can_analyse"] is True
    created = _create(client, headers=guest)
    assert created.status_code == 201 and created.json().get("error") is None
    # The monthly allowance itself is unchanged (still counted when the analysis starts).
    assert status["free_total"] == settings.billing_free_analyses


def test_forged_durability_claims_are_ignored(monthly, tmp_path) -> None:
    settings = supabase_settings(tmp_path)
    client, app, _, _ = monthly(settings=settings)
    guest = auth("0c6f5d8e-1111-2222-3333-444455556666", anonymous=True)
    forged = client.post("/v1/billing/sync", headers=guest, json={"is_anonymous": False, "durable": True})
    assert forged.status_code == 422
    claimed = client.post("/v1/analysis/jobs?is_anonymous=false", headers={
        **guest, "X-Is-Anonymous": "false", "X-Supabase-Anonymous": "false"},
        json={"file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10,
              "is_anonymous": False, "allowance_resets_at": "2099-01-01T00:00:00Z"})
    # Extra request fields are rejected outright; the identity still comes only from the signed token.
    assert claimed.status_code == 422
    still_guest = _status(client, headers=guest)
    assert still_guest["requires_sign_in"] is True


def test_dev_auth_and_billing_off_keep_local_development_working(monthly, tmp_path) -> None:
    # billing ON + dev auth: a dev token is anonymous by construction, so the guard must not apply.
    dev = monthly_settings(tmp_path, auth_mode="dev")
    client, app, _, _ = monthly(settings=dev)
    assert _status(client)["access"] == "free"
    assert _create(client).status_code == 201

    # billing OFF + supabase anonymous: billing is inert, nothing is gated.
    off = supabase_settings(tmp_path, billing_mode="off")
    client_off, app_off, _, _ = monthly(settings=off)
    guest = auth("0c6f5d8e-1111-2222-3333-444455556666", anonymous=True)
    status = _status(client_off, headers=guest)
    assert status["enabled"] is False and status["access"] == "disabled"
    assert status["can_analyse"] is True and status["requires_sign_in"] is False
    assert _create(client_off, headers=guest).status_code == 201


def test_no_client_field_can_move_the_allowance_period(monthly) -> None:
    client, app, _, clock = monthly()
    status = _status(client)
    assert status["allowance_model"] == "calendar_month"
    assert status["allowance_timezone"] == "UTC"
    assert status["allowance_period_start"].startswith("2026-10-01")
    forged = client.post("/v1/billing/sync", json={"allowance_period_start": "2020-01-01T00:00:00Z"})
    assert forged.status_code == 401  # no token at all, but the body is never trusted either way
    forged_with_token = client.post("/v1/billing/sync", headers=A,
                                    json={"allowance_resets_at": "2099-01-01T00:00:00Z"})
    assert forged_with_token.status_code == 422
    clock.set(NOV_START)
    assert _status(client)["allowance_period_start"].startswith("2026-11-01")


def test_free_quota_vocabulary_distinguishes_exhausted_from_in_progress() -> None:
    assert FreeQuota(total=3, settled=3).exhausted and not FreeQuota(total=3, settled=3).in_progress
    assert FreeQuota(total=3, settled=2, reserved=1).in_progress
    assert not FreeQuota(total=3, settled=2, reserved=1).exhausted
    assert FreeQuota(total=3, forfeited=1, reserved=1).used == 1
    assert FreeQuota(total=3, settled=2, reserved=1).remaining == 0
    row = {"free_total": 3, "free_settled": 1, "free_forfeited": 1, "free_reserved": 0,
           "allowance_period_start": "2026-10-01T00:00:00+00:00",
           "allowance_resets_at": "2026-11-01T00:00:00+00:00"}
    quota = FreeQuota.from_row(row)
    assert (quota.used, quota.remaining, quota.model, quota.timezone) == (2, 1, "calendar_month", "UTC")
    # A legacy-shaped row (no monthly keys) reads as an empty month rather than crashing.
    assert FreeQuota.from_row({"free_total": 3}).used == 0


# ---------------------------------------------------------------- SQL and adapter contracts


def test_monthly_migration_is_additive_and_keeps_the_old_one_untouched() -> None:
    root = Path(__file__).resolve().parents[1]
    previous = root / "supabase" / "migrations" / "20261002000000_billing.sql"
    migration = root / "supabase" / "migrations" / "20261003000000_billing_monthly.sql"
    assert migration.exists() and "20261003000000" > "20261002000000"
    old = previous.read_text(encoding="utf-8")
    assert "charge_month" not in old and "forfeited" not in old  # the historical migration is not edited
    sql = migration.read_text(encoding="utf-8")
    assert "add column if not exists charge_month date" in sql
    assert "add column if not exists admitted_month date" in sql
    assert "'forfeited'" in sql
    assert "create or replace function public.billing_status" in sql
    assert "create or replace function public.billing_admit" in sql
    assert "create or replace function public.billing_finish" in sql
    assert "create or replace function public.billing_delete" in sql
    assert "p_user_id uuid, p_swing_id uuid" in sql
    assert "security definer" in sql and sql.count("set search_path = ''") == 4
    assert "grant execute on function public.billing_delete(uuid, uuid) to service_role" in sql
    assert "revoke all on function public.billing_delete(uuid, uuid) from public, anon, authenticated" in sql
    # Still additive: no historical object is dropped and the swings table is not altered.
    assert "alter table public.swings " not in sql
    assert "drop table" not in sql.lower()
    assert "references public.swings" not in sql
    # The monthly logic is not a stale second interface: the same signatures are replaced.
    assert "billing_status(uuid, integer)" in sql
    assert "billing_admit(uuid, uuid, text, text, bigint, integer)" in sql


def test_postgrest_delete_uses_the_forfeiting_rpc() -> None:
    import httpx

    from swingguru.service.repo import PostgrestRepo

    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("billing_delete"):
            return httpx.Response(200, json={"status": "deleted", "forfeited": 1, "swing": {"id": "s1"}})
        if request.url.path.endswith("billing_status"):
            return httpx.Response(200, json={"free_total": 3, "free_settled": 1, "free_forfeited": 0,
                                             "free_reserved": 0,
                                             "allowance_period_start": "2026-10-01T00:00:00+00:00",
                                             "allowance_resets_at": "2026-11-01T00:00:00+00:00",
                                             "allowance_timezone": "UTC", "allowance_model": "calendar_month"})
        return httpx.Response(200, json=[])

    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert repo.delete_owned("s1", "u1")["id"] == "s1"
    assert repo.billing_status("u1", 3)["allowance_model"] == "calendar_month"
    assert [c.url.path for c in calls] == ["/rest/v1/rpc/billing_delete", "/rest/v1/rpc/billing_status"]
    assert json.loads(calls[0].content) == {"p_user_id": "u1", "p_swing_id": "s1"}
    # A missing swing is not an error: the API has already checked ownership.
    missing = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "not_found"}))))
    assert missing.delete_owned("s9", "u1") is None


def test_admission_verdicts_map_to_typed_errors(monthly) -> None:
    """The ledger's verdict (not a Python count) decides exhausted vs in-progress."""

    client, app, _, _ = monthly(billing_free_analyses=1)
    # Both uploads are created before either is admitted, so the second create is not itself
    # refused by the preflight.
    first = _upload(client)["job"]["id"]
    second = _upload(client)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{first}/start", headers=A).status_code == 200
    with pytest.raises(BillingInProgress):
        app.state.billing.decide("user-aaaaaaaa")
    with pytest.raises(BillingInProgress):
        app.state.billing.admit("user-aaaaaaaa", second, 10)
    # The refused job is untouched: it still owns its uploaded bytes and can be retried later.
    assert app.state.repo.get(second)["status"] == "awaiting_upload"
    # A retried /start for the already-admitted job is a plain 200 (the job is no longer
    # awaiting_upload, so the ledger is never consulted again) and it is never charged twice.
    again = client.post(f"/v1/analysis/jobs/{first}/start", headers=A)
    assert again.status_code == 200 and again.json()["status"] == "queued"
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage").fetchone()[0] == 1
    # Once the first job finishes, the same attempt is a genuine paywall for the month.
    app.state.repo.update(first, status="processing", attempts=1)
    assert app.state.billing.settle(first, 1) is not None
    with pytest.raises(BillingExhausted):
        app.state.billing.admit("user-aaaaaaaa", second, 10)


# ---------------------------------------------------------------- unlimited normal use


@pytest.mark.parametrize("product,source,days", [
    (PRODUCTS.monthly, "subscription", 20),
    (PRODUCTS.yearly, "subscription", 300),
    (PRODUCTS.lifetime, "lifetime", None),
    # The 14-day introductory offer is not a separate server concept: the verified entitlement is
    # just an active subscription, so it is exercised with that exact shape (no client trial flag).
    (PRODUCTS.monthly, "subscription", 14),
])
def test_every_active_plan_admits_more_than_three_without_consuming_free(monthly, product, source, days) -> None:
    """Confirmed policy: all paid plans (and the eligible trial) are unlimited normal use."""

    expires = (datetime.now(UTC) + timedelta(days=days)).isoformat() if days else None
    fake = FakeRevenueCat(Entitlement(active=True, product_id=product, source=source, expires_at=expires,
                                      environment="production"))
    client, app, _, _ = monthly(client=fake, when=datetime.now(UTC).replace(microsecond=0))
    for _ in range(5):  # more than the three free analyses
        _complete(client, app)
    status = _status(client)
    assert status["access"] == "pro"
    assert status["paid_allowance"] == "unlimited"
    assert (status["free_used"], status["free_reserved"], status["free_remaining"]) == (0, 0, 3)
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE kind='paid'").fetchone()[0] == 5
    # Not one free row, so nothing about the paid work touched the monthly allowance.
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE kind='free'").fetchone()[0] == 0


def test_trial_expiry_restores_the_current_free_remainder(monthly) -> None:
    fake = FakeRevenueCat()
    client, app, _, _ = monthly(client=fake, when=datetime.now(UTC).replace(microsecond=0))
    _complete(client, app)  # one free analysis of this month is used
    assert (_status(client)["free_used"], _status(client)["free_remaining"]) == (1, 2)

    # The eligible 14-day trial, as the server sees it: an active verified subscription entitlement.
    fake.entitlement = Entitlement(active=True, product_id=PRODUCTS.monthly, source="subscription",
                                   expires_at=(datetime.now(UTC) + timedelta(days=14)).isoformat(),
                                   environment="production")
    for _ in range(5):
        _complete(client, app)
    pro = _status(client)
    assert pro["access"] == "pro"
    assert (pro["free_used"], pro["free_reserved"], pro["free_remaining"]) == (1, 0, 2)
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE kind='free'").fetchone()[0] == 1

    # Trial expiry is the entitlement going inactive - nothing else. The current month's remainder is
    # exactly what it was, and the next free analysis is available.
    fake.entitlement = Entitlement(active=False, product_id=PRODUCTS.monthly,
                                   expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
                                   environment="production")
    after = _status(client)
    assert after["access"] == "free"
    assert (after["free_used"], after["free_reserved"], after["free_remaining"]) == (1, 0, 2)
    assert _create(client).status_code == 201


# ---------------------------------------------------------------- concurrency and the clock


def test_a_settlement_racing_an_admission_never_admits_a_fourth(tmp_path) -> None:
    """Two real connections: charged completions + every reservation must stay within the month.

    SQLite serialises writers with ``BEGIN IMMEDIATE``, so this pins the *policy* on the mirror. The
    PostgreSQL equivalent comes from the account-row lock those functions share (asserted in
    ``test_the_monthly_rpcs_serialise_on_the_account_row``); a single-connection PGlite run cannot
    prove multi-session contention, and this test does not claim to.
    """

    path = tmp_path / "db.sqlite3"
    settle_conn, admit_conn = SqliteRepo(path), SqliteRepo(path)
    for n in (1, 2, 3, 4):
        settle_conn.insert({"id": _swing_id(n), "user_id": "user-aaaaaaaa", "status": "awaiting_upload"})
    for n in (1, 2, 3):
        assert settle_conn.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(n), kind="free",
                                         product_id=None, size_bytes=10, free_total=3)["status"] == "admitted"
    for n in (1, 2):  # the month is now at 2 charged + 1 reserved
        settle_conn.update(_swing_id(n), status="processing", attempts=1)
        assert settle_conn.billing_finish(_swing_id(n), status="complete", attempts=1) is not None

    verdicts: list[dict] = []
    barrier = threading.Barrier(2)

    def settle() -> None:
        barrier.wait(timeout=10)
        settle_conn.update(_swing_id(3), status="processing", attempts=1)
        settle_conn.billing_finish(_swing_id(3), status="complete", attempts=1)

    def admit() -> None:
        barrier.wait(timeout=10)
        verdicts.append(admit_conn.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(4), kind="free",
                                                 product_id=None, size_bytes=10, free_total=3))

    threads = [threading.Thread(target=settle), threading.Thread(target=admit)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    # Whichever order the two transactions landed in, the fourth job is refused - never admitted.
    assert [v["status"] for v in verdicts] in (["in_progress"], ["exhausted"])
    assert settle_conn.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(4), kind="free",
                                     product_id=None, size_bytes=10, free_total=3)["status"] == "exhausted"
    assert settle_conn.get(_swing_id(4))["status"] == "awaiting_upload"
    charged, reserved = settle_conn.db.execute(
        "SELECT COALESCE(SUM(state='settled'),0), COALESCE(SUM(state='reserved'),0) FROM billing_usage"
        " WHERE kind='free'").fetchone()
    assert (charged, reserved) == (3, 0)


def test_admission_reads_the_clock_once_and_keeps_month_and_stamps_together(tmp_path) -> None:
    clock = QueueClock(OCT_START)
    repo = SqliteRepo(tmp_path / "db.sqlite3", clock=clock)
    repo.insert({"id": _swing_id(1), "user_id": "user-aaaaaaaa", "status": "awaiting_upload"})

    # The next two readings straddle midnight: a transaction that reads twice would record October in
    # the ledger and November on the swing.
    clock.push(OCT_END, NOV_START)
    assert repo.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(1), kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "admitted"
    assert clock.queue == [NOV_START]  # exactly one reading
    usage = _ledger(repo, _swing_id(1))
    assert usage["admitted_month"] == "2026-10-01"
    assert usage["created_at"] == usage["updated_at"] == OCT_END.isoformat(timespec="seconds")
    assert month_key(parse_stamp(usage["created_at"])) == usage["admitted_month"]
    swing = repo.get(_swing_id(1))
    assert month_key(parse_stamp(swing["updated_at"])) == usage["admitted_month"]


def test_settlement_reads_the_clock_once_across_midnight(tmp_path) -> None:
    clock = QueueClock(OCT_END)
    repo = SqliteRepo(tmp_path / "db.sqlite3", clock=clock)
    repo.insert({"id": _swing_id(1), "user_id": "user-aaaaaaaa", "status": "awaiting_upload"})
    assert repo.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(1), kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "admitted"
    repo.update(_swing_id(1), status="processing", attempts=1)

    clock.push(OCT_END, NOV_START)
    assert repo.billing_finish(_swing_id(1), status="complete", attempts=1) is not None
    assert clock.queue == [NOV_START]  # exactly one reading
    swing, usage = repo.get(_swing_id(1)), _ledger(repo, _swing_id(1))
    assert usage["state"] == "settled" and usage["charge_month"] == "2026-10-01"
    # finished_at and settled_at are the same instant, and the charge month is that instant's month.
    assert swing["finished_at"] == usage["settled_at"]
    assert month_key(parse_stamp(usage["settled_at"])) == usage["charge_month"]


def test_deletion_reads_the_clock_once_across_midnight(tmp_path) -> None:
    clock = QueueClock(OCT_END)
    repo = SqliteRepo(tmp_path / "db.sqlite3", clock=clock)
    repo.insert({"id": _swing_id(1), "user_id": "user-aaaaaaaa", "status": "awaiting_upload"})
    assert repo.billing_admit(user_id="user-aaaaaaaa", swing_id=_swing_id(1), kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "admitted"

    clock.push(OCT_END, NOV_START)
    assert repo.delete_owned(_swing_id(1), "user-aaaaaaaa") is not None
    assert clock.queue == [NOV_START]  # exactly one reading
    usage = _ledger(repo, _swing_id(1))
    assert usage["state"] == "forfeited" and usage["charge_month"] == "2026-10-01"
    assert month_key(parse_stamp(usage["updated_at"])) == usage["charge_month"]


def test_status_reads_the_clock_once_and_reports_that_month(tmp_path) -> None:
    clock = QueueClock(NOV_START)
    repo = SqliteRepo(tmp_path / "db.sqlite3", clock=clock)
    repo.insert({"id": _swing_id(1), "user_id": "user-aaaaaaaa", "status": "complete",
                 "finished_at": "2026-11-05T10:00:00+00:00"})
    repo.db.execute("INSERT INTO billing_usage (swing_id,user_id,kind,state,charge_month,created_at,updated_at)"
                    " VALUES (?,?, 'free','settled','2026-11-01',?,?)",
                    (_swing_id(1), "user-aaaaaaaa", NOV_START.isoformat(timespec="seconds"),
                     NOV_START.isoformat(timespec="seconds")))

    clock.push(NOV_START, OCT_END)  # a second reading would move the period into October
    status = repo.billing_status("user-aaaaaaaa", 3)
    assert clock.queue == [OCT_END]  # exactly one reading
    assert status["allowance_period_start"].startswith("2026-11-01")
    assert status["allowance_resets_at"].startswith("2026-12-01")
    assert status["free_settled"] == 1  # the November row is counted for the November period


# ---------------------------------------------------------------- SQLite upgrade from the old schema

_LEGACY_DDL = """
CREATE TABLE IF NOT EXISTS swings (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL, progress TEXT, error_code TEXT,
    notice TEXT, club TEXT, file_name TEXT, content_type TEXT, size_bytes INTEGER, captured_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, source_key TEXT, result_key TEXT, video_key TEXT,
    thumb_key TEXT, summary TEXT, analysis_version TEXT, source_deleted_at TEXT);
CREATE INDEX IF NOT EXISTS swings_user ON swings (user_id, created_at);
CREATE INDEX IF NOT EXISTS swings_status ON swings (status, created_at);
CREATE TABLE IF NOT EXISTS billing_accounts (
    user_id TEXT PRIMARY KEY, free_total INTEGER NOT NULL, free_used_seed INTEGER NOT NULL DEFAULT 0,
    grandfather_seeded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS billing_usage (
    swing_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL,
    product_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, settled_at TEXT);
CREATE INDEX IF NOT EXISTS billing_usage_user ON billing_usage (user_id, kind, state);
"""


def _legacy_database(path: Path) -> None:
    """A database exactly as the pre-monthly code created it (no month columns, no month index)."""

    db = sqlite3.connect(str(path), isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")  # the old code set this on every open
        db.executescript(_LEGACY_DDL)
        db.execute("INSERT INTO swings (id,user_id,status,created_at,updated_at,finished_at,attempts)"
                   " VALUES ('le-settled','user-legacy','complete','2026-09-15T09:00:00+00:00',"
                   "'2026-09-15T10:00:00+00:00','2026-09-15T10:00:00+00:00',1)")
        db.execute("INSERT INTO swings (id,user_id,status,created_at,updated_at,attempts)"
                   " VALUES ('le-flight','user-legacy','queued','2026-09-30T08:00:00+00:00',"
                   "'2026-09-30T08:00:00+00:00',1)")
        db.execute("INSERT INTO billing_accounts (user_id,free_total,free_used_seed,grandfather_seeded,"
                   "created_at,updated_at) VALUES ('user-legacy',3,2,1,'2026-08-01T00:00:00+00:00',"
                   "'2026-08-01T00:00:00+00:00')")
        rows = [
            ("le-settled", "free", "settled", "2026-09-15T09:00:00+00:00", "2026-09-15T10:00:00+00:00"),
            # A completed swing the golfer deleted: the swing row is gone, the usage is not.
            ("le-deleted", "free", "settled", "2026-09-20T09:00:00+00:00", "2026-09-20T10:00:00+00:00"),
            # Legacy orphan reservations (no swing row at all) and a failed legacy job.
            ("le-orphan", "free", "reserved", "2026-08-20T07:00:00+00:00", None),
            ("le-paid", "paid", "reserved", "2026-08-20T07:00:00+00:00", None),
            ("le-released", "free", "released", "2026-08-21T07:00:00+00:00", "2026-08-21T08:00:00+00:00"),
            # Still in flight: its reservation must survive the upgrade.
            ("le-flight", "free", "reserved", "2026-09-30T08:00:00+00:00", None),
        ]
        for swing, kind, state, created, settled in rows:
            db.execute("INSERT INTO billing_usage (swing_id,user_id,kind,state,product_id,created_at,"
                       "updated_at,settled_at) VALUES (?,?,?,?,NULL,?,?,?)",
                       (swing, "user-legacy", kind, state, created, created, settled))
    finally:
        db.close()


def test_a_pre_monthly_sqlite_database_upgrades_and_reopens_idempotently(tmp_path) -> None:
    """Opening an existing pre-monthly development database must upgrade it, not fail or reset it."""

    path = tmp_path / "legacy.sqlite3"
    _legacy_database(path)
    clock = Clock(NOV_START)

    repo = SqliteRepo(path, clock=clock)  # the upgrade happens here
    columns = {row["name"] for row in repo.db.execute("PRAGMA table_info(billing_usage)")}
    assert {"admitted_month", "charge_month"} <= columns
    assert repo.db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='billing_usage_month'"
                           ).fetchone() is not None

    # Settled usage keeps its completion month, deleted or not; nothing is reset or refunded.
    assert _ledger(repo, "le-settled")["charge_month"] == "2026-09-01"
    assert _ledger(repo, "le-deleted")["charge_month"] == "2026-09-01"
    assert _ledger(repo, "le-settled")["admitted_month"] == "2026-09-01"
    # Legacy orphan reservations are forfeited into their admission month; the paid/released rows are
    # left exactly as they were, and an in-flight job keeps its reservation.
    assert (_ledger(repo, "le-orphan")["state"], _ledger(repo, "le-orphan")["charge_month"]) == (
        "forfeited", "2026-08-01")
    assert _ledger(repo, "le-orphan")["settled_at"] is not None
    assert (_ledger(repo, "le-paid")["state"], _ledger(repo, "le-paid")["charge_month"]) == ("reserved", None)
    assert (_ledger(repo, "le-released")["state"], _ledger(repo, "le-released")["charge_month"]) == (
        "released", None)
    assert (_ledger(repo, "le-flight")["state"], _ledger(repo, "le-flight")["admitted_month"]) == (
        "reserved", "2026-09-01")
    # The lifetime seed column is preserved and no longer read.
    account = repo.db.execute("SELECT free_total, free_used_seed FROM billing_accounts WHERE user_id=?",
                              ("user-legacy",)).fetchone()
    assert (account["free_total"], account["free_used_seed"]) == (3, 2)
    status = repo.billing_status("user-legacy", 3)
    assert (status["free_settled"], status["free_forfeited"], status["free_reserved"]) == (0, 0, 1)

    snapshot = [tuple(row) for row in repo.db.execute(
        "SELECT swing_id, kind, state, admitted_month, charge_month, settled_at FROM billing_usage"
        " ORDER BY swing_id")]
    reopened = SqliteRepo(path, clock=clock)  # a second open must be a no-op
    assert [tuple(row) for row in reopened.db.execute(
        "SELECT swing_id, kind, state, admitted_month, charge_month, settled_at FROM billing_usage"
        " ORDER BY swing_id")] == snapshot
    assert reopened.billing_status("user-legacy", 3)["free_reserved"] == 1


class _HookConnection:
    """A connection proxy that runs ``hook`` just before the backfill's ledger read."""

    def __init__(self, connection, hook) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_hook", hook)

    def __setattr__(self, name, value) -> None:  # row_factory etc. must reach the real connection
        setattr(self._connection, name, value)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, *args, **kwargs):
        if "SELECT swing_id, kind, state" in sql:
            self._hook()
        return self._connection.execute(sql, *args, **kwargs)


def test_a_settlement_cannot_land_between_the_backfills_two_reads(tmp_path, monkeypatch) -> None:
    """Deterministic injected ordering: the exact interleave the review found.

    The hook fires just after the ledger snapshot is taken, in the middle of the upgrade. A second
    connection tries to settle the in-flight job right there. Because the whole upgrade (introspection,
    ALTERs, backfill, month index) runs inside one ``BEGIN IMMEDIATE``, that write is refused rather
    than landing between the ledger read and the swing read - which is what let a stale `reserved`
    snapshot overwrite the row as `forfeited` after the job had actually settled.
    """

    path = tmp_path / "legacy.sqlite3"
    _legacy_database(path)
    stamp = "2026-09-30T12:00:00+00:00"
    other = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    other.execute("PRAGMA busy_timeout=150")
    injected: list = []

    def settle_from_another_connection() -> None:
        try:
            other.execute("UPDATE swings SET status='complete', attempts=1, finished_at=?, updated_at=?"
                          " WHERE id='le-flight'", (stamp, stamp))
            other.execute("UPDATE billing_usage SET state='settled', charge_month='2026-09-01',"
                          " settled_at=?, updated_at=? WHERE swing_id='le-flight'", (stamp, stamp))
            other.commit()
            injected.append("committed")
        except sqlite3.OperationalError as error:  # the upgrade's transaction is protecting the read
            other.rollback()
            injected.append(error)

    real_connect = sqlite3.connect

    def hooked_connect(*args, **kwargs):
        return _HookConnection(real_connect(*args, **kwargs), settle_from_another_connection)

    monkeypatch.setattr(repo_module.sqlite3, "connect", hooked_connect)
    try:
        repo = SqliteRepo(path, clock=Clock(NOV_START))
        assert len(injected) == 1 and isinstance(injected[0], sqlite3.OperationalError), injected
        assert "locked" in str(injected[0]).lower()
        # Nothing was half-applied: the in-flight job kept its reservation (its swing was still
        # queued), and the genuine orphan reservation was still upgraded.
        assert _ledger(repo, "le-flight")["state"] == "reserved"
        assert _ledger(repo, "le-orphan")["state"] == "forfeited"

        # The upgrade's lock is released, so the same settlement now succeeds and sticks.
        settle_from_another_connection()
        assert injected[1] == "committed"
        assert (_ledger(repo, "le-flight")["state"], _ledger(repo, "le-flight")["charge_month"]) == (
            "settled", "2026-09-01")
    finally:
        other.close()


def test_two_concurrent_sqlite_upgrades_do_not_race(tmp_path) -> None:
    """Two connections opening the same pre-monthly database must both succeed, upgrading once."""

    path = tmp_path / "legacy.sqlite3"
    _legacy_database(path)
    barrier = threading.Barrier(2)
    opened: list = []

    def open_repo() -> None:
        barrier.wait(timeout=10)
        try:
            opened.append(SqliteRepo(path, clock=Clock(NOV_START)))
        except Exception as error:  # pragma: no cover - the race this test exists to prevent
            opened.append(error)

    threads = [threading.Thread(target=open_repo), threading.Thread(target=open_repo)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert [type(item).__name__ for item in opened] == ["SqliteRepo", "SqliteRepo"], opened
    repo = opened[0]
    assert {"admitted_month", "charge_month"} <= {
        row["name"] for row in repo.db.execute("PRAGMA table_info(billing_usage)")}
    assert repo.db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='billing_usage_month'"
                           ).fetchone() is not None
    assert (_ledger(repo, "le-settled")["state"], _ledger(repo, "le-settled")["charge_month"]) == (
        "settled", "2026-09-01")
    assert (_ledger(repo, "le-orphan")["state"], _ledger(repo, "le-orphan")["charge_month"]) == (
        "forfeited", "2026-08-01")
    assert _ledger(repo, "le-flight")["state"] == "reserved"
    assert repo.billing_status("user-legacy", 3)["free_reserved"] == 1


def test_the_monthly_rpcs_serialise_on_the_account_row() -> None:
    """Static proof of the locking/aggregation shape the single-connection harness cannot exercise."""

    root = Path(__file__).resolve().parents[1]
    sql = (root / "supabase" / "migrations" / "20261003000000_billing_monthly.sql").read_text(encoding="utf-8")

    # One aggregate per read: the charged and reserved counts can no longer come from two snapshots.
    assert sql.count("filter (where u.state = 'reserved')") == 2  # billing_status + billing_admit
    assert "select count(*) into v_reserved" not in sql

    admit = sql[sql.index("create or replace function public.billing_admit"):
                sql.index("create or replace function public.billing_finish")]
    assert admit.index("select * into v_swing from public.swings s where s.id = p_swing_id for update") \
        < admit.index("insert into public.billing_accounts") \
        < admit.index("filter (where u.state = 'reserved')")

    finish = sql[sql.index("create or replace function public.billing_finish"):
                 sql.index("create or replace function public.billing_delete")]
    assert finish.index("select * into v_row from public.swings s where s.id = p_swing_id for update") \
        < finish.index("insert into public.billing_accounts") \
        < finish.index("update public.swings s") \
        < finish.index("update public.billing_usage u")

    delete = sql[sql.index("create or replace function public.billing_delete"):]
    assert delete.index("select * into v_row from public.swings s") \
        < delete.index("insert into public.billing_accounts") \
        < delete.index("update public.billing_usage u")

    # The constraint sweep is scoped to this table, not to any table named billing_usage anywhere.
    assert "con.conrelid = 'public.billing_usage'::regclass" in sql
    assert "rel.relname" not in sql
