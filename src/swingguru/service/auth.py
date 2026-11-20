"""Who is calling: a verified Supabase Auth user id (production) or a dev user (local only).

Supabase Auth issues a JWT for every signed-in user, including *anonymous*
users (``signInAnonymously``). The app starts with an anonymous session, so
every swing belongs to a real ``auth.users`` id from the first launch; linking
Sign in with Apple later (``linkIdentity``) keeps the same id, so nothing has
to be migrated.

Verification: HS256 with ``SUPABASE_JWT_SECRET`` (legacy projects) or the
project's JWKS (asymmetric signing keys), audience ``authenticated``, expiry
enforced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import jwt

from .config import Settings

_DEV_TOKEN = re.compile(r"^dev:([A-Za-z0-9-]{8,64})$")


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: str
    anonymous: bool = False


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def verify(authorization: str | None, settings: Settings) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if settings.auth_mode == "dev":
        if settings.production:
            raise AuthError("dev auth is disabled in production")
        match = _DEV_TOKEN.match(token)
        if not match:
            raise AuthError("invalid dev token")
        return User(match.group(1), anonymous=True)
    try:
        if settings.supabase_jwt_secret:
            claims = jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"],
                                audience=settings.jwt_audience, options={"require": ["exp", "sub"]})
        else:
            key = _jwks_client(f"{settings.supabase_url}/auth/v1/.well-known/jwks.json").get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["ES256", "RS256"], audience=settings.jwt_audience,
                                options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as error:
        raise AuthError(f"invalid token: {type(error).__name__}") from None
    if claims.get("role") not in (None, "authenticated"):
        raise AuthError("token is not for a signed-in user")
    return User(str(claims["sub"]), anonymous=bool(claims.get("is_anonymous")))


__all__ = ["AuthError", "User", "verify"]
