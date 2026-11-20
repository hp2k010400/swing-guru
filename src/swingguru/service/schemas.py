"""Versioned request/response models for the public API (``/v1``).

Nothing here exposes model names, file paths or internal diagnostics: errors
carry a stable ``code`` plus golfer-facing ``title``/``advice``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "v1"

# Mirrors app_api.CLUBS + LEGACY_CLUBS (kept valid for swings tagged with the first picker).
Club = Literal[
    "Driver", "3 Wood", "5 Wood", "7 Wood",
    "2 Hybrid", "3 Hybrid", "4 Hybrid", "5 Hybrid",
    "2 Iron", "3 Iron", "4 Iron", "5 Iron", "6 Iron", "7 Iron", "8 Iron", "9 Iron",
    "Pitching Wedge", "Gap Wedge", "Sand Wedge", "Lob Wedge",
    "Other", "Hybrid", "Irons", "Wedges",
]
JobState = Literal["awaiting_upload", "queued", "processing", "complete", "failed"]
StageState = Literal["pending", "active", "done"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- requests

class CreateJobRequest(Strict):
    file_name: str = Field(min_length=1, max_length=200)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(gt=0)
    captured_at: datetime | None = None
    club: Club | None = None


class UpdateSwingRequest(Strict):
    club: Club | None


class FeedbackRequest(Strict):
    """``POST /v1/feedback``: thumbs on one analysis, or a general message from Profile.

    The swing's score context is read on the server from the stored swing; the client may add only
    which checks it was shown as measured (validated against the known check ids) and its version.
    """

    kind: Literal["analysis", "general"]
    swing_id: str | None = Field(default=None, max_length=64)
    rating: Literal["up", "down"] | None = None
    category: Literal["swing_analysis", "bug", "feature", "other"] | None = None
    message: str | None = Field(default=None, max_length=2000)
    measured_checks: list[str] | None = Field(default=None, max_length=12)
    app_version: str | None = Field(default=None, max_length=40)


class BillingSyncRequest(Strict):
    """``POST /v1/billing/sync`` carries no client entitlement state.

    ``extra="forbid"`` means a forged ``entitlement_active``/``product_id`` body
    is a 422, not a hint the server will use: the customer is always the verified
    auth user and the entitlement always comes from RevenueCat.
    """


class AppleRegistrationRequest(Strict):
    """``POST /v1/account/apple``: the one-time code from the native Apple sheet.

    Only the authorization code and the raw nonce are accepted. The identity token is deliberately
    *not* part of this contract: it is already consumed by Supabase, and it is not a revocable
    credential. ``extra="forbid"`` keeps a client from smuggling an email, a user id or a claim the
    server might be tempted to trust.
    """

    #: Secret-by-construction: repr=False keeps it out of any accidental log or traceback.
    authorization_code: str = Field(min_length=8, max_length=4096, repr=False)
    nonce: str = Field(min_length=8, max_length=256, repr=False)


class LeagueNameRequest(Strict):
    """``POST /v1/league/join`` and ``PATCH /v1/league/profile``: the public league display name."""

    display_name: str = Field(min_length=1, max_length=40)


class DeleteAccountRequest(Strict):
    """``DELETE /v1/account`` may carry a fresh Apple authorization for a legacy account.

    A golfer whose Apple identity was linked before the server stored refresh tokens has to prove
    the Apple account one more time. Both fields are optional (guests and already-enrolled accounts
    delete without them); supplying only one is refused by the handler.

    ``manual_apple_revocation`` opts into Apple's documented TN3194 fallback for the case where no
    refresh token, access token or authorization code can be obtained: the data is deleted anyway and
    the golfer is told to stop using Sign in with Apple for this app themselves. It is never used to
    bypass a revocation that could have happened, and the response says so.
    """

    authorization_code: str | None = Field(default=None, min_length=8, max_length=4096, repr=False)
    nonce: str | None = Field(default=None, min_length=8, max_length=256, repr=False)
    manual_apple_revocation: bool = False


# ---------------------------------------------------------------- responses

class Stage(BaseModel):
    id: str
    label: str
    state: StageState


class ErrorInfo(BaseModel):
    code: str
    title: str
    advice: str


class UploadInstructions(BaseModel):
    method: Literal["PUT"]
    url: str
    headers: dict[str, str]
    expires_in_s: int


class Job(BaseModel):
    api_version: str = API_VERSION
    id: str
    status: JobState
    stages: list[Stage]
    error: ErrorInfo | None = None
    notice: str | None = None
    swing_id: str | None = None          # set when complete (same id: a swing is its job)
    created_at: datetime
    updated_at: datetime


class CreateJobResponse(BaseModel):
    api_version: str = API_VERSION
    job: Job
    upload: UploadInstructions


class Counts(BaseModel):
    good: int
    attention: int
    problem: int
    total: int


class ScoreBrief(BaseModel):
    """The stored Swing Score for list views (None for swings analysed before scoring existed)."""

    available: bool
    total: float | None
    version: str
    measured: int
    eligible: int


class SwingListItem(BaseModel):
    id: str
    captured_at: datetime | None
    created_at: datetime
    club: Club | None
    counts: Counts | None
    summary_text: str | None
    thumb_url: str | None
    notice: str | None
    score: ScoreBrief | None = None


class SwingList(BaseModel):
    api_version: str = API_VERSION
    swings: list[SwingListItem]


class Replay(BaseModel):
    viewer_url: str                      # the guided viewer, loaded with signed analysis + video URLs
    analysis_url: str
    video_url: str
    expires_in_s: int


class SwingDetail(BaseModel):
    """The Summary screen's data: real verdicts only. ``score`` is the versioned Swing Score
    (``swingguru.scoring``; ``available`` false with a reason when too few checks were measured
    reliably); ``focus`` is neutral unless an interpretation rule is eligible."""

    api_version: str = API_VERSION
    id: str
    captured_at: datetime | None
    created_at: datetime
    club: Club | None
    clubs: list[str]
    notice: str | None
    analysis_version: str | None
    headline: dict[str, Any]
    summary_text: str
    checkpoints: list[dict[str, Any]]
    withheld: list[dict[str, Any]]
    breakdown: list[dict[str, Any]]
    recommended: list[dict[str, Any]]
    hand_position: dict[str, Any] | None
    camera: dict[str, Any]
    score: dict[str, Any]
    focus: dict[str, Any]
    thumb_url: str | None
    replay: Replay


class ApiError(BaseModel):
    error: dict[str, str]


class BillingStatus(BaseModel):
    """The server-derived subscription picture. Never collected from a client.

    ``free_total``/``free_used``/``free_reserved``/``free_remaining`` describe the *current UTC
    calendar month*; ``allowance_resets_at`` is when the next month's allowance starts. The paid
    fields stay for compatibility, but the UI never shows a paid quota: Pro is unlimited.
    """

    api_version: str = API_VERSION
    enabled: bool
    access: Literal["free", "pro", "exhausted", "in_progress", "sign_in_required", "disabled"]
    free_total: int
    free_used: int
    free_reserved: int
    free_remaining: int
    can_analyse: bool
    entitlement_active: bool
    product_id: str | None = None
    expires_at: datetime | None = None
    management_url: str | None = None
    #: Explicit server policy ("unlimited" once shown); never a secret.
    paid_allowance: str | None = None
    #: True when free analyses need a durable (non-anonymous) account that survives a reinstall.
    #: This is an authentication state for the client, never a purchase prompt.
    requires_sign_in: bool = False
    #: The current allowance period, half-open [period_start, resets_at) in UTC.
    allowance_period_start: datetime | None = None
    allowance_resets_at: datetime | None = None
    allowance_timezone: str = "UTC"
    allowance_model: str = "calendar_month"


class AccountDeletionResult(BaseModel):
    """What really happened to the Apple grant (never an overclaim).

    ``revoked`` - Apple confirmed the revocation of this account's grant.
    ``not_required`` - the account never had a Sign in with Apple identity.
    ``manual_required`` - TN3194 fallback: no credential was available, so the golfer is shown how to
    stop using Sign in with Apple for this app in iOS Settings. The data is gone either way.
    """

    status: Literal["deleted"] = "deleted"
    apple_revocation: Literal["revoked", "not_required", "manual_required"]


__all__ = ["API_VERSION", "AccountDeletionResult", "ApiError", "AppleRegistrationRequest", "BillingStatus",
           "BillingSyncRequest", "Club", "CreateJobRequest", "CreateJobResponse", "DeleteAccountRequest",
           "ErrorInfo", "Job", "Replay", "Stage", "SwingDetail", "SwingList", "SwingListItem",
           "UpdateSwingRequest", "UploadInstructions"]
