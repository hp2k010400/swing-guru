"""Typed billing vocabulary shared by the RevenueCat client, the ledger and the API.

Nothing here talks to the network or the database: these are the plain values the
service reasons about, so the security rules ("only a verified, permitted,
unexpired entitlement counts") live in one readable place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

AccessState = Literal["free", "pro", "exhausted", "in_progress", "sign_in_required", "disabled"]
EntitlementSource = Literal["subscription", "lifetime"]
PurchaseEnvironment = Literal["production", "sandbox"]

#: The only paid allowance policy this bundle activates. A monthly cap is a
#: documented extension point, never a silent default: see ``policy.py``.
PAID_ALLOWANCE_UNLIMITED = "unlimited"

#: The free allowance is three analyses per UTC calendar month, charged when a job completes.
#: Server/database clocks only; a device date or timezone is never trusted.
FREE_ALLOWANCE_MODEL = "calendar_month"
FREE_ALLOWANCE_TIMEZONE = "UTC"


@dataclass(frozen=True)
class BillingProducts:
    """The product identifiers the server accepts, mapped to the entitlement ``pro``."""

    entitlement_id: str
    monthly: str
    yearly: str
    lifetime: str
    allowed_stores: tuple[str, ...] = ("app_store",)

    @property
    def subscriptions(self) -> tuple[str, str]:
        return (self.monthly, self.yearly)

    @property
    def known(self) -> tuple[str, ...]:
        return (self.monthly, self.yearly, self.lifetime)

    def permits(self, product_id: str | None) -> bool:
        return bool(product_id) and product_id in self.known

    def is_lifetime(self, product_id: str | None) -> bool:
        return product_id == self.lifetime


@dataclass(frozen=True)
class Entitlement:
    """A freshly verified RevenueCat entitlement for one Supabase user id.

    ``active`` is the only field an admission decision trusts; the rest is
    exposure for the status endpoint and the paywall.
    """

    active: bool = False
    product_id: str | None = None
    expires_at: str | None = None
    in_grace: bool = False
    source: EntitlementSource | None = None
    environment: PurchaseEnvironment | None = None
    management_url: str | None = None

    @staticmethod
    def none(management_url: str | None = None) -> "Entitlement":
        return Entitlement(management_url=management_url)


@dataclass(frozen=True)
class FreeQuota:
    """The per-account free allowance for the *current* UTC calendar month.

    ``settled`` and ``forfeited`` are the analyses charged to this month; ``reserved`` counts every
    outstanding reservation whatever month it was admitted in, because a pending job still holds a
    slot (a job admitted on the 31st keeps holding it into the next month until it finishes).
    """

    total: int
    settled: int = 0
    forfeited: int = 0
    reserved: int = 0
    period_start: str | None = None
    period_resets_at: str | None = None
    timezone: str = FREE_ALLOWANCE_TIMEZONE
    model: str = FREE_ALLOWANCE_MODEL

    @property
    def used(self) -> int:
        """Analyses charged to the current month (completed or voluntarily deleted)."""

        return self.settled + self.forfeited

    @property
    def accounted(self) -> int:
        return self.used + self.reserved

    @property
    def remaining(self) -> int:
        return max(0, self.total - min(self.accounted, self.total))

    @property
    def exhausted(self) -> bool:
        """The month's completions/forfeits are used up."""

        return self.used >= self.total

    @property
    def in_progress(self) -> bool:
        """Charged < total, but every remaining slot is held by unfinished work."""

        return not self.exhausted and self.accounted >= self.total

    @classmethod
    def from_row(cls, row: dict) -> "FreeQuota":
        return cls(
            total=int(row.get("free_total") or 0),
            settled=int(row.get("free_settled") or 0),
            forfeited=int(row.get("free_forfeited") or 0),
            reserved=int(row.get("free_reserved") or 0),
            period_start=_iso_text(row.get("allowance_period_start")),
            period_resets_at=_iso_text(row.get("allowance_resets_at")),
        )


def _iso_text(value) -> str | None:
    """jsonb dates/timestamps arrive as ISO strings; anything else is not exposed."""

    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


@dataclass(frozen=True)
class AdmissionDecision:
    """``kind`` is ``paid`` only for an active entitlement under the accepted policy."""

    kind: Literal["free", "paid"]
    entitlement: Entitlement = field(default_factory=Entitlement.none)


__all__ = [
    "AccessState",
    "AdmissionDecision",
    "BillingProducts",
    "Entitlement",
    "EntitlementSource",
    "FREE_ALLOWANCE_MODEL",
    "FREE_ALLOWANCE_TIMEZONE",
    "FreeQuota",
    "PAID_ALLOWANCE_UNLIMITED",
    "PurchaseEnvironment",
]
