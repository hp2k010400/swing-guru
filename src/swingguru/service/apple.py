"""Sign in with Apple, server side: the authorization-code exchange and token revocation.

Why this exists: the native Apple sheet gives the app a *short-lived authorization code*. Supabase
verifies the identity token and holds the session, but Supabase cannot revoke the golfer's Apple
grant, and the code itself is single-use and useless after a few minutes. To satisfy Apple's account
deletion requirement the service exchanges that code for the long-lived *refresh token*, keeps it
encrypted at rest (``tokenbox``), and can revoke it later even after the original code is consumed.

Protocol, per Apple's "Generate and validate tokens" and "Revoke tokens" documents:

* the client secret is an ES256 JWT signed with the Sign in with Apple ``.p8`` key, with
  ``iss`` = team id, ``sub`` = the app's bundle identifier, ``aud`` = ``https://appleid.apple.com``
  and a short expiry (Apple allows up to six months; five minutes is all a single call needs);
* ``POST /auth/token`` with ``grant_type=authorization_code`` returns ``id_token`` (a JWT) and
  ``refresh_token``;
* the ``id_token`` is validated against Apple's published JWKS, with the issuer, the audience (the
  bundle id), the expiry and the nonce all checked - the email and any client-supplied claim are
  never trusted;
* ``POST /auth/revoke`` with ``token_type_hint=refresh_token`` revokes the grant. **Only HTTP 200 is
  proof of a revocation**: Apple documents an already-invalid token as a *200* answer, so a 400
  (``invalid_grant``/``invalid_token``/``unsupported_token_type``/``invalid_client``) means the
  revoke did not happen and the deletion must stay retryable rather than claim success.

Nothing in this module logs the authorization code, the refresh token, the ``.p8`` key or the
client secret. Failures log a status code and a whitelisted error code and nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import jwt

from .config import Settings

log = logging.getLogger("swingguru.apple")

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
#: Apple's documented bound is six months; a single exchange or revoke needs seconds.
CLIENT_SECRET_TTL_S = 300
DEFAULT_TIMEOUT_S = 10.0
#: Clock skew allowed when checking the returned identity token's ``iat``/``exp``.
LECAY_S = 60
#: Bound on the JWKS fetch, so a hung Apple endpoint cannot pin a worker/request thread.
JWKS_TIMEOUT_S = 10.0

#: The only upstream ``error`` values we are willing to name in a log. Anything else is dropped, so
#: arbitrary upstream text can never reach (or forge meaning into) our logs.
_KNOWN_ERROR_CODES = frozenset({
    "invalid_request", "invalid_client", "invalid_grant", "invalid_token", "unsupported_token_type",
    "unauthorized_client", "invalid_scope",
})


class AppleError(Exception):
    """An Apple call that did not produce a usable, verified credential.

    ``kind`` is ``config`` (nothing to call with), ``unavailable`` (network/5xx/timeout - retry),
    ``rejected`` (Apple refused the code/token) or ``malformed`` (an answer we cannot reason about).
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


def client_secret(settings: Settings, *, now: float | None = None) -> str:
    """A short-lived ES256 client secret signed with the server-only ``.p8`` key."""

    if not settings.apple_configured:
        raise AppleError("config", "Apple revocation is not configured")
    issued = int(now if now is not None else time.time())
    payload = {
        "iss": settings.apple_team_id,
        "iat": issued,
        "exp": issued + CLIENT_SECRET_TTL_S,
        "aud": APPLE_ISSUER,
        "sub": settings.apple_client_id,
    }
    try:
        return jwt.encode(payload, settings.apple_private_key, algorithm="ES256",
                          headers={"kid": settings.apple_key_id})
    except jwt.PyJWTError as error:
        # The message can echo key material in pathological cases: keep only the exception type.
        raise AppleError("config", f"client secret could not be signed ({type(error).__name__})") from None


def _expected_nonce(raw_nonce: str) -> str:
    """What Apple echoes back when the app sends its SHA-256 hash (the Expo/Supabase convention)."""

    return hashlib.sha256(raw_nonce.encode("utf-8")).hexdigest()


def nonce_matches(claim: object, raw_nonce: str | None) -> bool:
    """Constant-time check of Apple's nonce claim against the raw nonce the app generated.

    The app sends Apple the SHA-256 hash of the raw nonce (the Expo/Supabase convention), so the
    claim is that hash. There is no "raw form" fallback: accepting a second form would only widen
    what an attacker may replay.
    """

    if not isinstance(claim, str) or not claim or not raw_nonce:
        return False
    return hmac.compare_digest(claim, _expected_nonce(raw_nonce))


@dataclass(frozen=True)
class AppleCredential:
    """The verified result of exchanging an authorization code."""

    subject: str
    #: Never part of a repr: this is the credential that can mint new Apple access tokens.
    refresh_token: str = field(repr=False)
    audience: str
    issuer: str
    expires_at: datetime


class AppleClient:
    """Duck-typed for tests: ``exchange`` and ``revoke`` are the whole surface."""

    def __init__(self, settings: Settings, *, http: httpx.Client | None = None,
                 jwks_client=None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.settings = settings
        self.timeout = timeout
        self.http = http or httpx.Client(timeout=timeout)
        self._jwks_client = jwks_client

    def _jwks(self):
        if self._jwks_client is None:
            self._jwks_client = jwt.PyJWKClient(self.settings.apple_jwks_url, cache_keys=True, lifespan=3600,
                                                timeout=JWKS_TIMEOUT_S)
        return self._jwks_client

    # -- authorization code -> refresh token -----------------------------------

    def exchange(self, code: str, *, nonce: str | None = None, now: float | None = None) -> AppleCredential:
        if not self.settings.apple_configured:
            raise AppleError("config", "Apple revocation is not configured")
        if not code:
            raise AppleError("rejected", "missing authorization code")
        secret = client_secret(self.settings, now=now)
        try:
            response = self.http.post(APPLE_TOKEN_URL, data={
                "client_id": self.settings.apple_client_id,
                "client_secret": secret,
                "code": code,
                "grant_type": "authorization_code",
            }, timeout=self.timeout)
        except httpx.HTTPError:
            log.warning("apple token exchange failed: transport error")
            raise AppleError("unavailable", "transport error") from None
        if response.status_code == 400:
            # The usual cause is an expired, replayed or mismatched code. The body can carry an
            # error_description; the stable error code is all we keep.
            log.info("apple token exchange refused (%s)", _error_code(response))
            raise AppleError("rejected", "authorization code refused")
        if response.status_code >= 400:
            log.warning("apple token exchange failed: status %s", response.status_code)
            raise AppleError("unavailable", f"status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise AppleError("malformed", "token response is not JSON") from None
        if not isinstance(payload, dict):
            raise AppleError("malformed", "token response is not an object")
        id_token = payload.get("id_token")
        refresh_token = payload.get("refresh_token")
        if not isinstance(id_token, str) or not id_token:
            raise AppleError("malformed", "id_token is missing")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AppleError("malformed", "refresh_token is missing")
        claims = self._validate_id_token(id_token)
        if not nonce_matches(claims.get("nonce"), nonce):
            log.info("apple token exchange refused: nonce mismatch")
            raise AppleError("rejected", "nonce mismatch")
        return AppleCredential(
            subject=str(claims["sub"]),
            refresh_token=refresh_token,
            audience=str(claims["aud"]),
            issuer=str(claims["iss"]),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
        )

    def _validate_id_token(self, id_token: str) -> dict:
        """Signature + issuer + audience + expiry, all against Apple's published keys.

        A key *fetch* failure is ``unavailable`` (retryable); a token we cannot verify against the
        keys we got is ``rejected``. Collapsing the two would either deny valid sign-ins during an
        Apple outage or accept an unverifiable token.
        """

        try:
            key = self._jwks().get_signing_key_from_jwt(id_token)
        except (jwt.PyJWKClientConnectionError, jwt.PyJWKSetError) as error:
            log.warning("apple id_token check unavailable (%s)", type(error).__name__)
            raise AppleError("unavailable", "signing keys unavailable") from None
        except jwt.PyJWKClientError as error:
            log.info("apple id_token rejected (%s)", type(error).__name__)
            raise AppleError("rejected", "no matching signing key") from None
        try:
            claims = jwt.decode(
                id_token, key.key, algorithms=["ES256", "RS256"],
                audience=self.settings.apple_client_id, issuer=APPLE_ISSUER,
                leeway=LECAY_S, options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.PyJWTError as error:
            log.info("apple id_token rejected (%s)", type(error).__name__)
            raise AppleError("rejected", f"id_token invalid ({type(error).__name__})") from None
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AppleError("malformed", "id_token has no subject")
        return claims

    # -- revoke ----------------------------------------------------------------

    def revoke(self, refresh_token: str, *, now: float | None = None) -> None:
        """Revoke one refresh token. Already-revoked tokens are success (idempotent)."""

        if not self.settings.apple_configured:
            raise AppleError("config", "Apple revocation is not configured")
        if not refresh_token:
            raise AppleError("malformed", "missing refresh token")
        secret = client_secret(self.settings, now=now)
        try:
            response = self.http.post(APPLE_REVOKE_URL, data={
                "client_id": self.settings.apple_client_id,
                "client_secret": secret,
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            }, timeout=self.timeout)
        except httpx.HTTPError:
            log.warning("apple revoke failed: transport error")
            raise AppleError("unavailable", "transport error") from None
        if response.status_code == 200:
            return
        # Apple answers 200 for a token that is already invalid, so anything else - including a 400
        # invalid_grant/invalid_token - means we have NOT proven the grant is dead. Stay retryable.
        log.warning("apple revoke failed: status %s (%s)", response.status_code, _error_code(response))
        raise AppleError("rejected" if response.status_code < 500 else "unavailable",
                         f"status {response.status_code}")


def _error_code(response: httpx.Response) -> str:
    """A whitelisted documented ``error`` value, or an empty string. Never the description body."""

    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        code = payload["error"]
        return code if code in _KNOWN_ERROR_CODES else ""
    return ""


__all__ = [
    "APPLE_ISSUER", "APPLE_REVOKE_URL", "APPLE_TOKEN_URL", "CLIENT_SECRET_TTL_S", "AppleClient",
    "AppleCredential", "AppleError", "JWKS_TIMEOUT_S", "client_secret", "nonce_matches",
]
