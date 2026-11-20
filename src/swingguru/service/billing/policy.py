"""The paid analysis allowance policy.

Billing never activates without an explicitly selected policy. Today the only
accepted value is ``unlimited``: an active, verified entitlement means the
golfer can analyse without a server-side monthly count.

A monthly cap is deliberately *not* implemented here. It needs a product
decision (how many analyses, counted from when, what happens mid-cycle) and a
paid-usage ledger the API can settle against; inventing one silently would
promise usage the product has not agreed to. ``parse_paid_allowance`` therefore
refuses anything it does not recognise instead of defaulting to a policy.
"""

from __future__ import annotations

from .models import PAID_ALLOWANCE_UNLIMITED


class AllowanceError(ValueError):
    pass


def parse_paid_allowance(value: str) -> str:
    """Return the accepted policy, or raise so startup fails loudly."""

    policy = (value or "").strip().lower()
    if policy == PAID_ALLOWANCE_UNLIMITED:
        return policy
    raise AllowanceError(
        "SG_BILLING_PAID_ALLOWANCE must be explicitly 'unlimited' while billing is on"
        " (a monthly cap is not implemented until a count is agreed)"
    )


def paid_can_analyse(policy: str, entitlement_active: bool, paid_used: int = 0) -> bool:
    """Whether an admission may proceed for a paid golfer under ``policy``.

    ``paid_used`` is accepted so a future capped policy can be added here without
    changing the call sites; the unlimited policy ignores it.
    """

    if not entitlement_active:
        return False
    if policy == PAID_ALLOWANCE_UNLIMITED:
        return True
    return False


__all__ = ["AllowanceError", "paid_can_analyse", "parse_paid_allowance"]
