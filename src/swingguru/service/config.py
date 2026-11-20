"""Service configuration from environment variables, validated at startup.

Production (``SG_ENV=production``) requires Supabase (Auth, Postgres via
PostgREST, Storage). Local development can run fully offline with SQLite,
local files and dev tokens - never in production.

Billing (RevenueCat) is **off by default**: with ``SG_BILLING_MODE=off`` the
analysis service behaves exactly as it did before billing existed. Turning it on
requires every piece of the contract (server secret key, entitlement, the three
accepted products, an explicitly accepted purchase environment and an
explicitly selected paid allowance policy); a half-configured deployment is a
startup error, never free paid access.

Apple account revocation is configured the same way: the four server-only values
(``SG_APPLE_TEAM_ID``, ``SG_APPLE_KEY_ID``, ``SG_APPLE_PRIVATE_KEY`` /
``SG_APPLE_PRIVATE_KEY_PATH`` and ``SG_APPLE_TOKEN_KEY``) are all-or-nothing. An
empty set leaves guests working exactly as before (account deletion then refuses
an Apple review with an explicit, retryable error instead of pretending it
revoked); a partial set is a startup error, never a half-working deletion path.
"""

from __future__ import annotations

import os
import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[3]


class ConfigError(RuntimeError):
    pass


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


#: The bundle identifier Apple signs the app with. Sign in with Apple's ``aud`` and the
#: authorization-code exchange's ``sub`` are both this value; it is not a Services ID and the
#: deployment may not invent a different one.
FIXED_APPLE_CLIENT_ID = "com.swingguru.app"


@dataclass(frozen=True)
class Settings:
    env: str = "development"                      # development | production
    public_url: str = "http://127.0.0.1:8790"     # how clients reach this service (signed local URLs, viewer)

    # auth: "supabase" (JWT from Supabase Auth, incl. anonymous users) or "dev" (local only)
    auth_mode: str = "dev"
    supabase_url: str = ""
    supabase_service_key: str = ""                # server-side only; never sent to clients
    supabase_jwt_secret: str = ""                 # legacy HS256 projects; empty -> JWKS (asymmetric keys)
    jwt_audience: str = "authenticated"

    # storage / database: "supabase" or "local"
    storage_mode: str = "local"
    db_mode: str = "sqlite"
    bucket: str = "swings"
    data_dir: Path = field(default_factory=lambda: ROOT / "var" / "service")
    signing_secret: str = ""                      # local-mode signed URLs

    # limits
    max_upload_bytes: int = 500 * 1024 * 1024
    allowed_types: tuple[str, ...] = ("video/mp4", "video/quicktime", "video/x-m4v")
    jobs_per_hour: int = 20
    max_active_jobs: int = 2
    signed_url_ttl_s: int = 3600
    upload_url_ttl_s: int = 2 * 3600

    # worker
    worker_enabled: bool = True
    worker_poll_s: float = 2.0
    analysis_timeout_s: float = 600.0
    stale_after_s: float = 900.0
    max_attempts: int = 2
    source_retention_hours: float = 0.0           # 0 = delete the original upload as soon as analysis ends
    awaiting_upload_ttl_hours: float = 24.0

    # billing (RevenueCat) - OFF unless explicitly configured and allowed
    billing_mode: str = "off"                     # off | on (ON refuses to start unless complete)
    revenuecat_secret_key: str = ""               # server-only sk_...; never sent to clients
    revenuecat_entitlement_id: str = "pro"
    revenuecat_monthly_product_id: str = "com.swingguru.app.pro.monthly"
    revenuecat_yearly_product_id: str = "com.swingguru.app.pro.yearly"
    revenuecat_lifetime_product_id: str = "com.swingguru.app.pro.lifetime"
    billing_allowed_stores: tuple[str, ...] = ("app_store",)
    billing_env: str = ""                         # deployment-selected: production | sandbox | both (App Review + customers)
    billing_paid_allowance: str = ""              # must be explicitly "unlimited" while billing is on
    billing_free_analyses: int = 3                # free analyses per Supabase account per UTC calendar month
    revenuecat_api_base: str = "https://api.revenuecat.com/v1"
    revenuecat_timeout_s: float = 10.0

    # Apple account revocation (Sign in with Apple private key). All four values must be present
    # together - see ``apple_configured``.
    apple_team_id: str = ""                       # 10-character Apple Developer team id
    apple_key_id: str = ""                        # the Sign in with Apple key's Key ID
    apple_private_key: str = field(default="", repr=False)   # PEM contents; never logged or exposed
    apple_client_id: str = FIXED_APPLE_CLIENT_ID  # the app bundle identifier
    apple_token_key: str = field(default="", repr=False)     # base64 32-byte AES-GCM key
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"

    #: A deletion that was persisted but never finished (the golfer's JWT expired, or the auth user
    #: was already removed) is reconciled by the janitor after this grace period. 24 h leaves plenty
    #: of room for an in-app retry first.
    # Staging only (production leaves both unset): the Swing Score model new analyses are scored with,
    # and a shared key every signed-in request must carry (a dev-auth staging server must not be an
    # open analysis endpoint).
    score_version: str = ""
    access_key: str = ""
    # Staging only, set for a one-off copy of a tester's own analysed swings (POST /v1/admin/import),
    # then removed. Refused in production at start-up.
    import_key: str = ""
    # Weekly leagues (1.0.1). Stored in SQLite: data_dir/league.sqlite3 by default, or SG_LEAGUE_DB. Production
    # requires SG_LEAGUE_DB to be an absolute path on a persistent volume (one replica): without one the league
    # data would be lost on every deploy (docs/agent-work/release-1.0.1/LEAGUES_IN_PRODUCTION.md).
    league_enabled: bool = False
    league_db: str = ""
    # One-shot restore of the league database from a backup key (league/backup.py); unset after use.
    league_restore_key: str = ""
    deletion_reconcile_hours: float = 24.0
    # The read-only example analysis new users can open (GET /v1/example): one completed swing whose
    # owner agreed to share it. Empty = no example (the app hides it). Never counts or bills anything.
    example_swing_id: str = ""
    # Whether an anonymous (not yet signed-in) account must Sign in with Apple before its free analysis.
    # Off = the first analysis runs on the silent anonymous account and sign-in is offered after the score.
    free_requires_sign_in: bool = True

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def apple_configured(self) -> bool:
        """Every server-only Apple value is present: revocation and short-lived secrets are usable."""

        return bool(self.apple_team_id and self.apple_key_id and self.apple_private_key
                    and self.apple_token_key)

    @property
    def apple_partial(self) -> bool:
        """Some, but not all, Apple values are set - a misconfiguration that must not start."""

        values = (self.apple_team_id, self.apple_key_id, self.apple_private_key, self.apple_token_key)
        return any(values) and not all(values)


def _read_apple_private_key() -> str:
    """The Sign in with Apple ``.p8`` contents, from an env var or a mounted file.

    The value is never logged, returned in an API response or written to a document. A file path is
    the deployment-preferred form; the literal env var keeps local development and tests simple.
    """

    inline = os.environ.get("SG_APPLE_PRIVATE_KEY", "")
    if inline.strip():
        return inline.strip()
    path = os.environ.get("SG_APPLE_PRIVATE_KEY_PATH", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(f"SG_APPLE_PRIVATE_KEY_PATH could not be read ({type(error).__name__})") from None


def _check_apple_token_key(value: str) -> str | None:
    """A base64 AES-256 key: 32 raw bytes (44 base64 chars with padding). Returns an error, if any."""

    if not value:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return "SG_APPLE_TOKEN_KEY must be base64"
    if len(raw) != 32:
        return "SG_APPLE_TOKEN_KEY must decode to exactly 32 bytes (AES-256)"
    return None


def _check_apple_private_key(value: str) -> str | None:
    """The ``.p8`` must be a usable EC private key; validate without ever echoing it."""

    if not value:
        return None
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(value.encode("utf-8"), password=None)
    except Exception as error:  # any parse/backing error is the same deployment problem
        return f"SG_APPLE_PRIVATE_KEY is not a readable PEM private key ({type(error).__name__})"
    # `key_size == 256` alone would also accept e.g. a 256-bit RSA key; Sign in with Apple needs an
    # EC key on the P-256 curve for ES256.
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        return "SG_APPLE_PRIVATE_KEY must be a P-256 (ES256) key"
    return None


def load() -> Settings:
    s = Settings(
        env=os.environ.get("SG_ENV", "development"),
        public_url=os.environ.get("SG_PUBLIC_URL", "http://127.0.0.1:8790").rstrip("/"),
        auth_mode=os.environ.get("SG_AUTH_MODE", "dev"),
        supabase_url=os.environ.get("SUPABASE_URL", "").rstrip("/"),
        supabase_service_key=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        supabase_jwt_secret=os.environ.get("SUPABASE_JWT_SECRET", ""),
        storage_mode=os.environ.get("SG_STORAGE", "local"),
        db_mode=os.environ.get("SG_DB", "sqlite"),
        bucket=os.environ.get("SG_BUCKET", "swings"),
        data_dir=Path(os.environ.get("SG_DATA_DIR", str(ROOT / "var" / "service"))),
        signing_secret=os.environ.get("SG_SIGNING_SECRET", ""),
        max_upload_bytes=int(os.environ.get("SG_MAX_UPLOAD_MB", "500")) * 1024 * 1024,
        jobs_per_hour=int(os.environ.get("SG_JOBS_PER_HOUR", "20")),
        max_active_jobs=int(os.environ.get("SG_MAX_ACTIVE_JOBS", "2")),
        worker_enabled=_bool("SG_WORKER_ENABLED", True),
        analysis_timeout_s=float(os.environ.get("SG_ANALYSIS_TIMEOUT_S", "600")),
        source_retention_hours=float(os.environ.get("SG_SOURCE_RETENTION_HOURS", "0")),
        billing_mode=os.environ.get("SG_BILLING_MODE", "off").strip().lower(),
        revenuecat_secret_key=os.environ.get("REVENUECAT_SECRET_KEY", ""),
        revenuecat_entitlement_id=os.environ.get("REVENUECAT_ENTITLEMENT_ID", "pro").strip(),
        revenuecat_monthly_product_id=os.environ.get("REVENUECAT_MONTHLY_PRODUCT_ID", "com.swingguru.app.pro.monthly").strip(),
        revenuecat_yearly_product_id=os.environ.get("REVENUECAT_YEARLY_PRODUCT_ID", "com.swingguru.app.pro.yearly").strip(),
        revenuecat_lifetime_product_id=os.environ.get("REVENUECAT_LIFETIME_PRODUCT_ID", "com.swingguru.app.pro.lifetime").strip(),
        billing_allowed_stores=_csv("SG_BILLING_ALLOWED_STORES", ("app_store",)),
        billing_env=os.environ.get("SG_BILLING_ENV", "").strip().lower(),
        billing_paid_allowance=os.environ.get("SG_BILLING_PAID_ALLOWANCE", "").strip().lower(),
        billing_free_analyses=int(os.environ.get("SG_BILLING_FREE_ANALYSES", "3")),
        apple_team_id=os.environ.get("SG_APPLE_TEAM_ID", "").strip(),
        apple_key_id=os.environ.get("SG_APPLE_KEY_ID", "").strip(),
        apple_private_key=_read_apple_private_key(),
        apple_client_id=os.environ.get("SG_APPLE_CLIENT_ID", FIXED_APPLE_CLIENT_ID).strip(),
        apple_token_key=os.environ.get("SG_APPLE_TOKEN_KEY", "").strip(),
        apple_jwks_url=os.environ.get("SG_APPLE_JWKS_URL", "https://appleid.apple.com/auth/keys").strip(),
        deletion_reconcile_hours=float(os.environ.get("SG_DELETION_RECONCILE_HOURS", "24")),
        score_version=os.environ.get("SG_SCORE_VERSION", "").strip(),
        access_key=os.environ.get("SG_ACCESS_KEY", "").strip(),
        import_key=os.environ.get("SG_IMPORT_KEY", "").strip(),
        league_enabled=_bool("SG_LEAGUE", False),
        example_swing_id=os.environ.get("SG_EXAMPLE_SWING_ID", "").strip(),
        free_requires_sign_in=_bool("SG_FREE_REQUIRES_SIGN_IN", True),
        league_db=os.environ.get("SG_LEAGUE_DB", "").strip(),
        league_restore_key=os.environ.get("SG_LEAGUE_RESTORE_KEY", "").strip(),
    )
    validate(s)
    return s


def validate(s: Settings) -> None:
    """Refuse to start with an unsafe or incomplete configuration."""

    problems = []
    if s.env not in {"development", "production"}:
        problems.append("SG_ENV must be development or production")
    if s.production:
        if s.auth_mode != "supabase":
            problems.append("production requires SG_AUTH_MODE=supabase (dev tokens are local only)")
        if s.storage_mode != "supabase" or s.db_mode != "supabase":
            problems.append("production requires SG_STORAGE=supabase and SG_DB=supabase")
        if not s.public_url.startswith("https://"):
            problems.append("production requires an https SG_PUBLIC_URL")
    if "supabase" in (s.auth_mode, s.storage_mode, s.db_mode):
        if not s.supabase_url.startswith("https://"):
            problems.append("SUPABASE_URL must be the https project URL")
        if (s.storage_mode == "supabase" or s.db_mode == "supabase") and not s.supabase_service_key:
            problems.append("SUPABASE_SERVICE_ROLE_KEY is required for Supabase storage/database")
    if s.storage_mode == "local" and not s.signing_secret and s.production:
        problems.append("SG_SIGNING_SECRET is required")
    if s.billing_mode not in {"off", "on"}:
        problems.append("SG_BILLING_MODE must be off or on")
    if s.billing_mode == "on":
        if not s.revenuecat_secret_key.startswith("sk_"):
            problems.append("REVENUECAT_SECRET_KEY (server-only sk_ key) is required when billing is on")
        if not s.revenuecat_entitlement_id:
            problems.append("REVENUECAT_ENTITLEMENT_ID is required when billing is on")
        if not all((s.revenuecat_monthly_product_id, s.revenuecat_yearly_product_id,
                    s.revenuecat_lifetime_product_id)):
            problems.append("the monthly, yearly and lifetime product ids are required when billing is on")
        if len({s.revenuecat_monthly_product_id, s.revenuecat_yearly_product_id,
                s.revenuecat_lifetime_product_id}) != 3:
            problems.append("the monthly, yearly and lifetime product ids must be distinct")
        if not s.billing_allowed_stores:
            problems.append("SG_BILLING_ALLOWED_STORES must list at least one accepted store")
        if s.billing_env not in {"production", "sandbox", "both"}:
            problems.append("SG_BILLING_ENV must be explicitly production, sandbox or both when billing is on")
        if s.billing_paid_allowance != "unlimited":
            problems.append("SG_BILLING_PAID_ALLOWANCE must be explicitly 'unlimited' (a monthly cap is not implemented)")
        if s.billing_free_analyses < 1:
            problems.append("SG_BILLING_FREE_ANALYSES must be at least 1")
        if not s.revenuecat_api_base.startswith("https://"):
            problems.append("REVENUECAT_API_BASE must be an https URL")
    if s.score_version:
        from ..scoring.config import MODELS, VERSION

        if s.score_version not in MODELS:
            problems.append(f"SG_SCORE_VERSION must be one of {sorted(MODELS)}")
        elif s.production and s.score_version != VERSION:
            # Production changes its scoring model only by changing scoring.config.VERSION in code.
            problems.append(f"production scores with {VERSION}; SG_SCORE_VERSION may not override it")
    if s.access_key and len(s.access_key) < 24:
        problems.append("SG_ACCESS_KEY must be at least 24 characters")
    if s.import_key:
        if s.production or s.db_mode != "sqlite" or s.storage_mode != "local":
            problems.append("SG_IMPORT_KEY is a staging-only setting (development env, SQLite and local storage)")
        elif len(s.import_key) < 32:
            problems.append("SG_IMPORT_KEY must be at least 32 characters")
    def _absolute(p: str) -> bool:
        return PurePosixPath(p).is_absolute() or Path(p).is_absolute()

    if s.league_enabled and s.production and not (s.league_db and _absolute(s.league_db)):
        problems.append("SG_LEAGUE in production requires SG_LEAGUE_DB: an absolute path on a persistent volume")
    if s.league_db and not _absolute(s.league_db):
        problems.append("SG_LEAGUE_DB must be an absolute path")
    validate_apple(s, problems)
    if problems:
        raise ConfigError("; ".join(problems))


def validate_apple(s: Settings, problems: list[str]) -> None:
    """Apple's all-or-nothing rule, kept separate so the reason stays readable.

    Nothing here is required to *start*: an unconfigured deployment keeps guest accounts and billing
    exactly as they are, and only the Apple deletion path reports an explicit ``apple_unavailable``.
    A partial configuration is refused because it would fail at the worst possible moment.
    """

    if s.apple_partial:
        missing = [name for name, value in (
            ("SG_APPLE_TEAM_ID", s.apple_team_id), ("SG_APPLE_KEY_ID", s.apple_key_id),
            ("SG_APPLE_PRIVATE_KEY", s.apple_private_key), ("SG_APPLE_TOKEN_KEY", s.apple_token_key),
        ) if not value]
        problems.append("Apple revocation needs all of " + ", ".join(missing)
                        + " (or none of the SG_APPLE_* values at all)")
        return
    if not s.apple_configured:
        return
    if error := _check_apple_token_key(s.apple_token_key):
        problems.append(error)
    if error := _check_apple_private_key(s.apple_private_key):
        problems.append(error)
    if not s.apple_client_id:
        problems.append("SG_APPLE_CLIENT_ID must not be empty when Apple revocation is configured")
    if s.production and s.apple_client_id != FIXED_APPLE_CLIENT_ID:
        problems.append(f"production SG_APPLE_CLIENT_ID must be {FIXED_APPLE_CLIENT_ID}")
    if not s.apple_jwks_url.startswith("https://"):
        problems.append("SG_APPLE_JWKS_URL must be an https URL")
    if s.production and s.apple_jwks_url != "https://appleid.apple.com/auth/keys":
        problems.append("production SG_APPLE_JWKS_URL must be Apple's published endpoint")


def local_signing_secret(s: Settings) -> str:
    return s.signing_secret or "dev-only-signing-secret"


__all__ = ["ConfigError", "Settings", "load", "local_signing_secret", "validate"]
