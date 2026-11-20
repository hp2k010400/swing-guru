"""Billing: RevenueCat verification, the free-analysis ledger and admission policy.

Off by default (``SG_BILLING_MODE=off``), in which case the analysis service
behaves exactly as it did before this module existed.
"""

from .models import (
    AccessState, AdmissionDecision, BillingProducts, Entitlement, EntitlementSource, FreeQuota,
    FREE_ALLOWANCE_MODEL, FREE_ALLOWANCE_TIMEZONE, PAID_ALLOWANCE_UNLIMITED, PurchaseEnvironment,
)
from .policy import AllowanceError, paid_can_analyse, parse_paid_allowance
from .revenuecat import RevenueCatClient, RevenueCatError
from .service import (
    BillingError, BillingExhausted, BillingInProgress, BillingService, BillingSignInRequired,
    BillingUnavailable,
)

__all__ = [
    "AccessState", "AdmissionDecision", "AllowanceError", "BillingError", "BillingExhausted",
    "BillingInProgress", "BillingProducts", "BillingService", "BillingSignInRequired",
    "BillingUnavailable", "Entitlement", "EntitlementSource", "FREE_ALLOWANCE_MODEL",
    "FREE_ALLOWANCE_TIMEZONE", "FreeQuota", "PAID_ALLOWANCE_UNLIMITED", "PurchaseEnvironment",
    "RevenueCatClient", "RevenueCatError", "paid_can_analyse", "parse_paid_allowance",
]
