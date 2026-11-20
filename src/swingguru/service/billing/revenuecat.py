"""Server-only RevenueCat verification.

One request, one meaning: ``GET /v1/subscribers/{app_user_id}`` with a secret
API key for the *verified* Supabase user id. The SDK's client-side result is
never trusted here, no receipt or paid flag is accepted from a request, and the
store environment comes from deployment configuration only.

Verified against the official API v1 reference and customer-info model
(https://www.revenuecat.com/docs/api-v1/customers,
https://www.revenuecat.com/docs/api-v1/customer-info-model):

* auth is ``Authorization: Bearer <secret key>`` on ``https://api.revenuecat.com/v1``;
* ``app_user_id`` is URL-encoded;
* ``subscriber.entitlements[<id>]`` carries ``product_identifier``,
  ``expires_date``, ``grace_period_expires_date`` and ``purchase_date``;
* ``subscriber.subscriptions[<product>]`` carries the per-purchase
  ``store``, ``is_sandbox``, ``purchase_date`` and ``refunded_at``;
* ``subscriber.non_subscriptions[<product>]`` is a *list* of records for
  non-consumables (the lifetime purchase), with ``store`` and ``is_sandbox``;
* ``X-Is-Sandbox: true`` is **not** part of the official v1 reference (the
  documented ``GET /subscribers/{app_user_id}`` header is ``X-Platform`` only).
  It comes from a RevenueCat support answer (Dec 2023) about StoreKit
  test-framework transactions being excluded without it; treat it as
  unverified-against-primary-docs and confirm with a sandbox purchase before
  relying on it. This client sends it only when the deployment explicitly
  selects sandbox purchases, and never sends it for production or ``both``.
  Checked 2026-10-06 against a real sandbox purchase: the subscriber response
  (entitlement and ``is_sandbox: true`` record) is identical with and without
  the header, so ``both`` needs only the one request.

Purchase environment (``SG_BILLING_ENV``, deployment configuration only):
``production`` accepts only ``is_sandbox: false`` records, ``sandbox`` only
``is_sandbox: true`` records, and ``both`` accepts either (App Review and
TestFlight buy in the sandbox against the same build real customers use). The
environment of an accepted purchase is always the record's own ``is_sandbox``
flag, written by RevenueCat from Apple's receipt, never anything from a request.

Cancellation keeps access until expiry (``unsubscribe_detected_at`` is
informational), a refund removes it, and a provider error is reported as
``unavailable`` so admission fails closed rather than inventing either state.

Refund semantics for the one-time product: RevenueCat's official customer-info
model documents a non-subscription record as ``{id, is_sandbox, purchase_date,
store}`` with no refund field, and refunding a non-subscription removes access to
its entitlements (so the entitlement disappears). A null ``expires_date`` alone
therefore proves nothing: this client requires both the entitlement and the
non-consumable purchase record, and additionally rejects a record that carries
any revocation signal, should the API ever expose one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from .models import BillingProducts, Entitlement

log = logging.getLogger("swingguru.billing")


class RevenueCatError(Exception):
    """A verification attempt that must not be read as "not entitled".

    ``kind`` is one of ``unavailable`` (network, 404, 5xx, rate limit, timeout)
    or ``malformed`` (the response did not have the documented shape). Both fail
    admission closed with ``billing_unavailable``.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


# RevenueCat's documented non-subscription record has no refund field (a refunded
# non-consumable simply stops being entitled). These keys are defensive: if the
# API ever carries a revocation marker, it must never read as active.
_REVOCATION_KEYS = ("refunded_at", "revoked_at", "revocation_date")


def _revoked(record: dict) -> bool:
    if any(record.get(key) for key in _REVOCATION_KEYS):
        return True
    return record.get("is_revoked") is True or record.get("revoked") is True


def _required_bool(record: dict, key: str) -> bool:
    """``is_sandbox`` must be a real boolean: "false" is not False."""

    if key not in record:
        raise RevenueCatError("malformed", f"{key} is missing")
    value = record[key]
    if not isinstance(value, bool):
        raise RevenueCatError("malformed", f"{key} is not a boolean")
    return value


def _parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RevenueCatError("malformed", "time field is not a string")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise RevenueCatError("malformed", "time field is not ISO 8601") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class RevenueCatClient:
    """Duck-typed for tests: only :meth:`fetch` is used."""

    def __init__(self, secret_key: str, products: BillingProducts, environment: str, *,
                 base_url: str = "https://api.revenuecat.com/v1", timeout: float = 10.0,
                 client: httpx.Client | None = None) -> None:
        self.products = products
        self.environment = environment
        self.key = secret_key
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = client or httpx.Client(timeout=timeout)

    # -- request ---------------------------------------------------------------

    def fetch(self, app_user_id: str, *, now: datetime | None = None) -> Entitlement:
        # The secret key is attached per request (never to a shared/global client),
        # so an injected client cannot accidentally drop or leak it.
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if self.environment == "sandbox":  # never for "production" or "both" (see the module notes)
            headers["X-Is-Sandbox"] = "true"
        try:
            response = self.http.get(f"{self.base}/subscribers/{quote(app_user_id, safe='')}", headers=headers)
        except httpx.HTTPError:
            # Never log the URL with the id, the key or the exception text.
            log.warning("revenuecat verification failed: transport error")
            raise RevenueCatError("unavailable", "transport error") from None
        if response.status_code in (401, 403):
            log.error("revenuecat verification failed: key rejected (%s)", response.status_code)
            raise RevenueCatError("unavailable", "secret key rejected")
        # 200 is the normal answer; 201 is the documented "Get or Create Customer"
        # success for a customer RevenueCat has not seen before.
        if response.status_code not in (200, 201):
            log.warning("revenuecat verification failed: status %s", response.status_code)
            raise RevenueCatError("unavailable", f"status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise RevenueCatError("malformed", "response is not JSON") from None
        return self._parse(payload, now=now or datetime.now(timezone.utc))

    # -- parsing ---------------------------------------------------------------

    def _parse(self, payload, *, now: datetime) -> Entitlement:
        if not isinstance(payload, dict):
            raise RevenueCatError("malformed", "payload is not an object")
        subscriber = payload.get("subscriber")
        if not isinstance(subscriber, dict):
            raise RevenueCatError("malformed", "subscriber is missing")
        management_url = subscriber.get("management_url")
        if management_url is not None and not isinstance(management_url, str):
            raise RevenueCatError("malformed", "management_url is not a string")
        entitlements = subscriber.get("entitlements")
        if not isinstance(entitlements, dict):
            raise RevenueCatError("malformed", "entitlements is missing")
        record = entitlements.get(self.products.entitlement_id)
        if record is None:
            self._diagnose_missing_entitlement(subscriber, now)
            return Entitlement.none(management_url)
        if not isinstance(record, dict):
            raise RevenueCatError("malformed", "entitlement is not an object")
        if "product_identifier" not in record:
            raise RevenueCatError("malformed", "product_identifier is missing")
        product_id = record["product_identifier"]
        if not isinstance(product_id, str) or not product_id:
            raise RevenueCatError("malformed", "product_identifier is not a string")
        if not self.products.permits(product_id):
            # A promotional grant (or any product this deployment did not accept)
            # must never unlock analysis.
            log.warning("revenuecat: entitlement pro maps to an unpermitted product")
            return Entitlement.none(management_url)
        # A *missing* expires_date is not the same as an explicit null: the former is
        # a shape we do not understand, the latter is the lifetime/promotional case.
        if "expires_date" not in record:
            raise RevenueCatError("malformed", "expires_date is missing")
        expires_at = _parse_time(record["expires_date"])
        grace_until = _parse_time(record.get("grace_period_expires_date"))
        if expires_at is None:
            return self._lifetime(subscriber, product_id, management_url)
        return self._subscription(subscriber, product_id, expires_at, grace_until, management_url, now)

    def _diagnose_missing_entitlement(self, subscriber: dict, now: datetime) -> None:
        """Diagnostics only (never grants access): an accepted product the customer really owns but
        that RevenueCat does not map to the entitlement means the product is not attached to the
        entitlement in the RevenueCat dashboard. Logs product ids and flags only, no customer data."""

        owned = []
        subscriptions = subscriber.get("subscriptions")
        if isinstance(subscriptions, dict):
            for product_id, record in subscriptions.items():
                if not isinstance(record, dict) or product_id not in self.products.subscriptions:
                    continue
                expires = _parse_time(record.get("expires_date")) if isinstance(record.get("expires_date"), str) else None
                if expires is not None and expires > now:
                    owned.append(product_id)
        purchases = subscriber.get("non_subscriptions")
        if isinstance(purchases, dict):
            owned += [p for p, records in purchases.items() if self.products.is_lifetime(p) and records]
        if owned:
            log.warning("revenuecat: customer owns %s but has no '%s' entitlement - attach these products to "
                        "the entitlement in the RevenueCat dashboard", ",".join(sorted(owned)),
                        self.products.entitlement_id)

    def _lifetime(self, subscriber: dict, product_id: str, management_url: str | None) -> Entitlement:
        """A null expiry alone proves nothing: require the non-consumable record."""

        if not self.products.is_lifetime(product_id):
            log.warning("revenuecat: pro entitlement has no expiry for a subscription product")
            return Entitlement.none(management_url)
        purchases = subscriber.get("non_subscriptions")
        if not isinstance(purchases, dict):
            raise RevenueCatError("malformed", "non_subscriptions is missing")
        records = purchases.get(product_id)
        if not isinstance(records, list) or not records:
            log.warning("revenuecat: lifetime entitlement has no non-subscription purchase")
            return Entitlement.none(management_url)
        for entry in records:
            if not isinstance(entry, dict):
                raise RevenueCatError("malformed", "non-subscription record is not an object")
            if _revoked(entry):
                log.info("revenuecat: revoked non-consumable is not entitled")
                continue
            verified = self._record_environment(entry)
            if verified is not None:
                log.info("revenuecat: verified lifetime purchase (environment=%s)", verified)
                return Entitlement(active=True, product_id=product_id, source="lifetime",
                                   environment=verified, management_url=management_url)
        return Entitlement.none(management_url)

    def _record_environment(self, record: dict) -> str | None:
        """The purchase's own environment if this deployment accepts it, else None (with a log line)."""

        store = record.get("store")
        if not isinstance(store, str) or store not in self.products.allowed_stores:
            log.warning("revenuecat: purchase is from a store this deployment does not accept")
            return None
        verified = "sandbox" if _required_bool(record, "is_sandbox") else "production"
        if self.environment != "both" and verified != self.environment:
            log.warning("revenuecat: %s purchase refused by a %s-only deployment", verified, self.environment)
            return None
        return verified

    def _subscription(self, subscriber: dict, product_id: str, expires_at: datetime,
                      grace_until: datetime | None, management_url: str | None, now: datetime) -> Entitlement:
        if product_id not in self.products.subscriptions:
            log.warning("revenuecat: pro entitlement maps to an unpermitted subscription")
            return Entitlement.none(management_url)
        subscriptions = subscriber.get("subscriptions")
        if not isinstance(subscriptions, dict):
            raise RevenueCatError("malformed", "subscriptions is missing")
        record = subscriptions.get(product_id)
        if not isinstance(record, dict):
            # An entitlement with an expiry but no matching subscription record is
            # not something we can reason about (store, sandbox, refund).
            raise RevenueCatError("malformed", "subscription record is missing")
        if "expires_date" not in record or _parse_time(record["expires_date"]) is None:
            raise RevenueCatError("malformed", "subscription expires_date is missing")
        verified = self._record_environment(record)
        if verified is None:
            return Entitlement.none(management_url)
        purchase_date = _parse_time(record.get("purchase_date"))
        refunded_at = _parse_time(record.get("refunded_at"))
        if refunded_at is not None and (purchase_date is None or refunded_at >= purchase_date):
            log.info("revenuecat: refunded purchase is not entitled")
            return Entitlement.none(management_url)
        # Cancellation (unsubscribe_detected_at) does not remove access early.
        in_grace = expires_at <= now < (grace_until or expires_at)
        active = expires_at > now or (grace_until is not None and grace_until > now)
        if active:
            log.info("revenuecat: verified active subscription (environment=%s)", verified)
        return Entitlement(active=active, product_id=product_id,
                           expires_at=(grace_until if in_grace else expires_at).isoformat(),
                           in_grace=in_grace, source="subscription",
                           environment=verified, management_url=management_url)


__all__ = ["RevenueCatClient", "RevenueCatError"]
