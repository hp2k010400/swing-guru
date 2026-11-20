"""Admission policy for analysis jobs: fresh verification + the atomic ledger.

The whole point of this module is to make one decision safely:

* billing off -> the service is inert and nothing about analysis changes;
* billing on -> the *verified* Supabase user's RevenueCat entitlement is fetched
  fresh, a permitted active entitlement admits as ``paid``, otherwise the free
  analysis account is consulted and a reservation is written atomically by the
  database (never a count-then-insert in Python).

Failures refuse admission (``billing_unavailable``) instead of guessing; the
free allowance is never silently bypassed.

The free allowance is three analyses per UTC calendar month, charged when a job
*completes*. The database owns the clock and the month arithmetic; this module
only reads the picture the database returns, so a device date can never move a
golfer's allowance. When the month's completions are used up the answer is
``exhausted``; when the remaining slots are merely held by jobs that have not
finished yet the answer is ``in_progress`` - a retryable wait, not a paywall.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..repo import AccountBlocked, Repo, RepoError
from .models import AdmissionDecision, BillingProducts, Entitlement, FreeQuota
from .policy import paid_can_analyse, parse_paid_allowance
from .revenuecat import RevenueCatClient, RevenueCatError

log = logging.getLogger("swingguru.billing")


class BillingError(Exception):
    """Base for admission failures the API maps to a stable error code."""


class BillingUnavailable(BillingError):
    """We could not establish the golfer's access: refuse, never fail open."""


class BillingExhausted(BillingError):
    """No active entitlement and no free analyses left."""


class BillingInProgress(BillingError):
    """The remaining free slots are held by jobs that have not finished yet."""


class BillingSignInRequired(BillingError):
    """Free access needs a durable account; this verified user is anonymous."""


class BillingService:
    def __init__(self, settings: Settings, repo: Repo, client: RevenueCatClient | None = None) -> None:
        self.settings = settings
        self.repo = repo
        self.enabled = settings.billing_mode == "on"
        self.free_total = max(1, int(settings.billing_free_analyses))
        # Anonymous Supabase accounts can be recreated by reinstalling the app, so while billing is
        # ON a real Supabase project must back free access with a durable (non-anonymous) user.
        # "dev" auth is local development only and keeps working unchanged; billing OFF is inert.
        # SG_FREE_REQUIRES_SIGN_IN=0 trades that protection for a first score before any sign-in.
        self.permanent_identity_required = (self.enabled and settings.auth_mode == "supabase"
                                            and settings.free_requires_sign_in)
        self.products = BillingProducts(
            entitlement_id=settings.revenuecat_entitlement_id,
            monthly=settings.revenuecat_monthly_product_id,
            yearly=settings.revenuecat_yearly_product_id,
            lifetime=settings.revenuecat_lifetime_product_id,
            allowed_stores=settings.billing_allowed_stores,
        )
        self.policy = parse_paid_allowance(settings.billing_paid_allowance) if self.enabled else ""
        self.client = client
        if self.enabled and self.client is None:
            self.client = RevenueCatClient(
                settings.revenuecat_secret_key, self.products, settings.billing_env,
                base_url=settings.revenuecat_api_base, timeout=settings.revenuecat_timeout_s,
            )

    def sign_in_required(self, anonymous: bool) -> bool:
        """Whether this verified user must sign in before spending a free analysis."""

        return self.permanent_identity_required and anonymous

    # -- verification ----------------------------------------------------------

    def entitlement(self, user_id: str) -> Entitlement:
        if not self.enabled or self.client is None:
            return Entitlement.none()
        try:
            return self.client.fetch(str(user_id))
        except RevenueCatError as error:
            raise BillingUnavailable(f"verification {error.kind}") from None

    def quota(self, user_id: str) -> FreeQuota:
        try:
            return FreeQuota.from_row(self.repo.billing_status(str(user_id), self.free_total))
        except RepoError:
            log.exception("billing: quota read failed")
            raise BillingUnavailable("quota unavailable") from None

    # -- decisions -------------------------------------------------------------

    def decide(self, user_id: str, *, anonymous: bool = False) -> AdmissionDecision:
        """Fresh verification + the free account, with no ledger write."""

        entitlement = self.entitlement(user_id)
        if entitlement.active and paid_can_analyse(self.policy, True):
            return AdmissionDecision(kind="paid", entitlement=entitlement)
        if self.sign_in_required(anonymous):
            raise BillingSignInRequired("free access needs a permanent account")
        quota = self.quota(user_id)
        if quota.exhausted:
            raise BillingExhausted("free analyses used this month")
        if quota.in_progress:
            raise BillingInProgress("free analyses still in progress")
        return AdmissionDecision(kind="free", entitlement=entitlement)

    def preflight(self, user_id: str, *, anonymous: bool = False) -> AdmissionDecision:
        """Used before an upload starts so a golfer without credit is told early."""

        return self.decide(user_id, anonymous=anonymous)

    def admit(self, user_id: str, swing_id: str, size_bytes: int, *, anonymous: bool = False) -> dict:
        """Reserve the allowance and move the swing to ``queued`` atomically.

        The free-quota decision is deliberately *not* taken here: ``billing_admit``
        checks an existing ledger row (idempotence) before the allowance and does the
        count inside the transaction, so two concurrent /start calls for the same job
        cannot turn "already admitted" into a spurious error at the last free credit.
        Only the paid/free choice happens here, because that needs RevenueCat, and the
        ledger's verdict (exhausted vs still in progress) becomes a typed error.
        """

        entitlement = self.entitlement(user_id)
        kind = "paid" if (entitlement.active and paid_can_analyse(self.policy, True)) else "free"
        # An active verified Pro/trial keeps working as a guest while a sign-in flow is prepared;
        # only the *free* path needs the durable account.
        if kind == "free" and self.sign_in_required(anonymous):
            raise BillingSignInRequired("free access needs a permanent account")
        try:
            result = self.repo.billing_admit(
                user_id=str(user_id), swing_id=str(swing_id), kind=kind,
                product_id=entitlement.product_id if kind == "paid" else None,
                size_bytes=int(size_bytes),
                free_total=self.free_total,
            )
        except AccountBlocked:
            # The account entered a delete lifecycle: a refusal, not a billing outage.
            raise
        except RepoError:
            log.exception("billing: admission failed")
            raise BillingUnavailable("admission unavailable") from None
        verdict = result.get("status")
        if verdict == "exhausted":
            raise BillingExhausted("free analyses used this month")
        if verdict == "in_progress":
            raise BillingInProgress("free analyses still in progress")
        return result

    # -- settlement ------------------------------------------------------------

    def settle(self, swing_id: str, attempts: int, **fields) -> dict | None:
        try:
            return self.repo.billing_finish(str(swing_id), status="complete", attempts=int(attempts), **fields)
        except RepoError:
            log.exception("billing: settlement failed")
            return None

    def release(self, swing_id: str, attempts: int, error_code: str,
                expected_updated_at: str | None = None, older_than: str | None = None) -> dict | None:
        try:
            return self.repo.billing_finish(str(swing_id), status="failed", attempts=int(attempts),
                                            expected_updated_at=expected_updated_at,
                                            older_than=older_than,
                                            error_code=error_code)
        except RepoError:
            log.exception("billing: release failed")
            return None

    # -- status ----------------------------------------------------------------

    def status(self, user_id: str, *, anonymous: bool = False) -> dict:
        if not self.enabled:
            return {
                "enabled": False, "access": "disabled", "free_total": self.free_total,
                "free_used": 0, "free_reserved": 0, "free_remaining": self.free_total,
                "can_analyse": True, "entitlement_active": False, "product_id": None,
                "expires_at": None, "management_url": None, "paid_allowance": None,
                "requires_sign_in": False, "allowance_period_start": None,
                "allowance_resets_at": None, "allowance_timezone": "UTC",
                "allowance_model": "calendar_month",
            }
        entitlement = self.entitlement(user_id)
        quota = self.quota(user_id)
        sign_in_required = self.sign_in_required(anonymous) and not entitlement.active
        if entitlement.active:
            access = "pro"
        elif sign_in_required:
            access = "sign_in_required"
        elif quota.exhausted:
            access = "exhausted"
        elif quota.in_progress:
            access = "in_progress"
        else:
            access = "free"
        return {
            "enabled": True, "access": access, "free_total": quota.total,
            "free_used": quota.used, "free_reserved": quota.reserved, "free_remaining": quota.remaining,
            "can_analyse": bool(entitlement.active and paid_can_analyse(self.policy, True))
            or (not sign_in_required and quota.accounted < quota.total),
            "requires_sign_in": sign_in_required,
            "entitlement_active": entitlement.active, "product_id": entitlement.product_id,
            "expires_at": entitlement.expires_at, "management_url": entitlement.management_url,
            "paid_allowance": self.policy or None,
            "allowance_period_start": quota.period_start,
            "allowance_resets_at": quota.period_resets_at,
            "allowance_timezone": quota.timezone,
            "allowance_model": quota.model,
        }


__all__ = ["BillingError", "BillingExhausted", "BillingInProgress", "BillingService",
           "BillingSignInRequired", "BillingUnavailable"]
