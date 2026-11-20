"""Launch guards: billing stays OFF unless explicitly configured, the commercial defaults are intact,
free access with billing ON needs a durable (non-anonymous) identity, and the stored Swing Score is
what every consumer shows."""

from __future__ import annotations

from dataclasses import replace

from swingguru.service import config as cfg
from swingguru.service.billing import BillingService
from swingguru.service.config import Settings
from swingguru.service.repo import SqliteRepo


def test_billing_is_off_by_default_everywhere(monkeypatch, tmp_path) -> None:
    for name in [k for k in list(__import__("os").environ) if k.startswith("SG_BILLING")]:
        monkeypatch.delenv(name, raising=False)
    settings = cfg.load()
    assert settings.billing_mode == "off"
    billing = BillingService(settings, SqliteRepo(tmp_path / "db.sqlite3"))
    assert billing.enabled is False
    status = billing.status("someone")
    assert status["enabled"] is False and status["can_analyse"] is True


def test_commercial_defaults_are_three_free_monthly_and_unlimited_paid() -> None:
    settings = Settings()
    assert settings.billing_free_analyses == 3
    assert settings.revenuecat_monthly_product_id.startswith("com.swingguru.app")
    assert settings.revenuecat_yearly_product_id.startswith("com.swingguru.app")
    assert settings.revenuecat_lifetime_product_id.startswith("com.swingguru.app")


def test_billing_on_requires_a_durable_identity_with_real_auth(tmp_path) -> None:
    repo = SqliteRepo(tmp_path / "db.sqlite3")
    on = replace(Settings(), billing_mode="on", auth_mode="supabase", billing_paid_allowance="unlimited")
    assert BillingService(on, repo).permanent_identity_required is True
    assert BillingService(replace(on, billing_mode="off"), repo).permanent_identity_required is False
    # SG_FREE_REQUIRES_SIGN_IN=0: the free analysis runs on the anonymous account (sign-in after the score).
    relaxed = BillingService(replace(on, free_requires_sign_in=False), repo)
    assert relaxed.permanent_identity_required is False and relaxed.sign_in_required(True) is False


def test_the_stored_swing_score_is_what_consumers_show() -> None:
    """Replay end and Summary read the score stored when the job finished - never a re-score."""

    from swingguru.app_model import swing_score

    stored = {"schema": "swing_score/1", "version": "swing_score_v1", "available": True, "total": 7.4,
              "measured": 6, "eligible": 8, "checks": [], "excluded": []}
    analysis = {"phases": [], "hidden": [], "swing_score": stored}
    assert swing_score(analysis) == stored
