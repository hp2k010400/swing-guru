"""The Swing Guru analysis service: HTTP API (``/v1``) + in-process worker.

Security model:
* every ``/v1`` route except the signed local file routes requires a verified
  user (Supabase JWT; dev tokens only outside production);
* every swing read/write filters on the caller's ``user_id`` - another user's
  swing is indistinguishable from a missing one (404);
* uploads: allowed content types, declared and stored size limits, per-user
  rate and concurrency limits; object keys are generated server-side;
* errors are a stable envelope ``{"error": {"code", "message"}}`` - no
  exception text, paths or model details ever reach a client.
"""

from __future__ import annotations

import hmac
import json
import logging
import tempfile
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response

from .. import app_drills, ranks, swing_history
from ..league.service import LeagueError
from ..analysis_runner import STAGES, error_text
from ..app_api import CLUBS, product_summary, quality_notice
from .accounts import AccountError, AccountService
from . import config as config_module
from .auth import AuthError, User, verify
from .billing import (
    BillingExhausted, BillingInProgress, BillingService, BillingSignInRequired, BillingUnavailable,
)
from .config import Settings
from .repo import AccountBlocked, Repo, RepoError, make_repo, now
from .schemas import (
    AccountDeletionResult, AppleRegistrationRequest, BillingStatus, BillingSyncRequest, CreateJobRequest,
    CreateJobResponse, DeleteAccountRequest, ErrorInfo, FeedbackRequest, Job, LeagueNameRequest, Replay, Stage, SwingDetail, SwingList,
    SwingListItem, UpdateSwingRequest, UploadInstructions,
)
from .storage import LocalStorage, Storage, StorageError, make_storage
from .worker import Worker

log = logging.getLogger("swingguru.api")
VIEWER = Path(__file__).resolve().parents[1] / "viewer"
VIEWER_FILES = {"index.html": "text/html", "app.js": "text/javascript", "app.css": "text/css"}
EXTENSIONS = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-m4v": ".m4v"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class RateLimiter:
    """In-memory sliding window per key (per process). Coarse abuse protection only."""

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        stamp = time.monotonic()
        hits = self.hits[key]
        while hits and stamp - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(stamp)
        return True


def create_app(settings: Settings | None = None, *, repo: Repo | None = None, storage: Storage | None = None,
               start_worker: bool | None = None, runner=None, billing: BillingService | None = None,
               accounts: AccountService | None = None) -> FastAPI:
    settings = settings or config_module.load()
    repo = repo or make_repo(settings)
    storage = storage or make_storage(settings)
    billing = billing or BillingService(settings, repo)
    accounts = accounts or AccountService(settings, repo, storage)
    worker = Worker(settings, repo, storage, **({"runner": runner} if runner else {}), billing=billing,
                    accounts=accounts)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("service starting env=%s auth=%s storage=%s db=%s", settings.env, settings.auth_mode,
                 settings.storage_mode, settings.db_mode)
        if billing.enabled:
            log.info("billing enabled entitlement=%s env=%s policy=%s", billing.products.entitlement_id,
                     settings.billing_env, billing.policy)
            if settings.billing_env == "sandbox":
                log.warning("billing accepts SANDBOX purchases - TestFlight/sandbox testing only, not production")
            elif settings.billing_env == "both":
                log.warning("billing accepts PRODUCTION and SANDBOX purchases (App Review / TestFlight use the "
                            "sandbox); each verified purchase is logged with its own environment")
        if (settings.worker_enabled if start_worker is None else start_worker):
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Swing Guru analysis service", version="1.0", docs_url=None if settings.production else "/docs",
                  redoc_url=None, openapi_url=None if settings.production else "/openapi.json", lifespan=lifespan)
    app.state.settings, app.state.repo, app.state.storage, app.state.worker = settings, repo, storage, worker
    app.state.billing = billing
    app.state.accounts = accounts
    app.state.clock = lambda: datetime.now(timezone.utc)
    limiter = RateLimiter(240, 60.0)
    league = None
    if settings.league_enabled:
        from ..league import League, LeagueStore

        from ..league.backup import LeagueBackups

        league_path = Path(settings.league_db) if settings.league_db else settings.data_dir / "league.sqlite3"
        league_store = LeagueStore(league_path)
        league_backups = LeagueBackups(league_store, storage)
        started = datetime.now(timezone.utc)
        if settings.league_restore_key:
            league_backups.restore(settings.league_restore_key, started)     # one-shot; refuses unless verified
        league_backups.backup(started, "startup")                             # every deploy / restart
        league = League(league_store, lambda uid: user_history(uid)[0],
                        on_week_closed=lambda when: league_backups.backup(when, "weekly reset"))
        app.state.league_backups = league_backups
        log.info("leagues enabled (store on %s)", "volume path" if settings.league_db else "data dir")
    app.state.league = league

    # -- plumbing ------------------------------------------------------------

    @app.middleware("http")
    async def _envelope(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            return _error(429, "rate_limited", "Too many requests. Please wait a moment.")
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request %s %s %s failed", request_id, request.method, request.url.path)
            response = _error(500, "server_error", "Something went wrong on our side. Please try again.")
        response.headers["X-Request-Id"] = request_id
        log.info("%s %s %s %s %.0fms", request_id, request.method, request.url.path, response.status_code,
                 1000 * (time.monotonic() - started))
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, error: ApiError):
        return _error(error.status, error.code, error.message)

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, error: HTTPException):
        return _error(error.status_code, "http_error", str(error.detail) if error.status_code < 500 else "Server error")

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, error: RequestValidationError):
        fields = sorted({".".join(str(p) for p in e.get("loc", [])[1:]) for e in error.errors()})
        return _error(422, "invalid_request", "Invalid request: " + ", ".join(f for f in fields if f))

    def _verified(authorization: str | None) -> User:
        try:
            return verify(authorization, settings)
        except AuthError:
            raise ApiError(401, "unauthorized", "Please sign in again.") from None

    def _account_status(user_id: str) -> str:
        try:
            return repo.account_status(user_id)
        except Exception:
            log.exception("account status check failed")
            raise ApiError(503, "temporarily_unavailable", "Please try again in a moment.") from None

    def _access(key: str | None) -> None:
        # Staging only: with SG_ACCESS_KEY set, every signed-in request must carry it. Viewer files
        # and signed media links do not go through here (they are already signed).
        if settings.access_key and not hmac.compare_digest((key or "").encode(), settings.access_key.encode()):
            raise ApiError(401, "unauthorized", "Please sign in again.")

    def user(authorization: str | None = Header(default=None),
             x_sg_access_key: str | None = Header(default=None)) -> User:
        _access(x_sg_access_key)
        who = _verified(authorization)
        # A JWT stays cryptographically valid until it expires even after the account is gone
        # (Supabase documents this), so the durable tombstone - not the signature - is what denies
        # a deleted or deleting account every /v1 route.
        if _account_status(who.id) in ("deleting", "deleted"):
            raise ApiError(403, "account_deleted", "This account has been deleted. Please sign in again.")
        return who

    def deleting_user(authorization: str | None = Header(default=None),
                      x_sg_access_key: str | None = Header(default=None)) -> User:
        """The deletion endpoint's own guard: any deletion state may (re)read its own outcome.

        Every other route (including Apple registration) still refuses a ``deleting`` account. Without
        this, a golfer whose deletion stopped at "re-authorize Apple" could never complete it: the
        tombstone that blocks the account would also block the retry. A ``deleted`` account keeps
        access to this one endpoint so a retry gets the stored outcome (including the TN3194
        ``manual_required`` state) instead of losing it behind a 403.
        """

        _access(x_sg_access_key)
        return _verified(authorization)

    # -- billing ------------------------------------------------------------

    def _payment_required() -> ApiError:
        return ApiError(402, "purchase_required",
                        "You've used your free analyses for this month. Choose a plan to keep analysing swings.")

    def _free_in_progress() -> ApiError:
        # Not a paywall: the golfer still has free analyses this month, they are just not finished.
        return ApiError(409, "free_analyses_in_progress",
                        "Your earlier free analyses are still finishing. Please try again in a moment.")

    def _sign_in_required() -> ApiError:
        # Free access needs an account that survives a reinstall; this is an auth state, so it must
        # never be shown as a purchase prompt.
        return ApiError(403, "sign_in_required",
                        "Free analyses need an account that stays with you. Sign-in is coming soon.")

    def _billing_unavailable() -> ApiError:
        return ApiError(503, "billing_unavailable",
                        "We couldn't check your plan just now. Please try again in a moment.")

    def require_access(work) -> dict:
        """Run an admission step, mapping the billing failures to stable codes."""

        try:
            return work()
        except ApiError:
            raise
        except AccountBlocked:
            # The account entered a delete lifecycle mid-request; a refusal, not a billing outage.
            raise ApiError(403, "account_deleted",
                           "This account has been deleted. Please sign in again.") from None
        except BillingExhausted:
            raise _payment_required() from None
        except BillingInProgress:
            raise _free_in_progress() from None
        except BillingSignInRequired:
            raise _sign_in_required() from None
        except BillingUnavailable:
            raise _billing_unavailable() from None

    def billing_status_view(who: User) -> BillingStatus:
        # The billing layer decides from the *verified* identity: an anonymous Supabase user cannot
        # spend free analyses while billing is on, and no request field can claim otherwise.
        payload = require_access(lambda: billing.status(who.id, anonymous=who.anonymous))
        return BillingStatus(**payload)

    @app.get("/v1/billing", response_model=BillingStatus)
    def billing_status(who: User = Depends(user)) -> BillingStatus:
        return billing_status_view(who)

    @app.post("/v1/billing/sync", response_model=BillingStatus)
    def billing_sync(_: BillingSyncRequest | None = None, who: User = Depends(user)) -> BillingStatus:
        """After a purchase or restore: re-verify the same user server-side.

        The body is empty by contract - entitlement state is never accepted from
        a client.
        """

        return billing_status_view(who)

    # -- health ------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        checks = {"database": False, "models": False}
        try:
            checks["database"] = repo.ping()
        except Exception:
            log.exception("readiness: database")
        checks["models"] = models_present()
        ok = all(checks.values())
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)

    # -- jobs ----------------------------------------------------------------

    def job_view(row: dict) -> Job:
        error = None
        if row["status"] == "failed":
            title, advice = error_text(row.get("error_code") or "failed")
            error = ErrorInfo(code=row.get("error_code") or "failed", title=title, advice=advice)
        if row["status"] == "complete":
            stages = [Stage(id=k, label=l, state="done") for k, l in STAGES]
        elif row.get("progress"):
            stages = [Stage(**s) for s in row["progress"]]
        else:
            stages = [Stage(id=k, label=l, state="pending") for k, l in STAGES]
        return Job(id=row["id"], status=row["status"], stages=stages, error=error, notice=row.get("notice"),
                   swing_id=row["id"] if row["status"] == "complete" else None,
                   created_at=row["created_at"], updated_at=row["updated_at"])

    @app.post("/v1/analysis/jobs", status_code=201, response_model=CreateJobResponse)
    def create_job(body: CreateJobRequest, who: User = Depends(user)) -> CreateJobResponse:
        if body.content_type not in settings.allowed_types:
            raise ApiError(415, "unsupported_type", "Please choose an MP4 or MOV video.")
        if body.size_bytes > settings.max_upload_bytes:
            title, advice = error_text("too_large")
            raise ApiError(413, "too_large", f"{title}. {advice}")
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        if repo.count_since(who.id, since) >= settings.jobs_per_hour:
            raise ApiError(429, "too_many_analyses", "You've analysed a lot of swings this hour. Please try again later.")
        if repo.count_active(who.id) >= settings.max_active_jobs:
            raise ApiError(429, "analysis_in_progress", "An analysis is already in progress. Let it finish first.")
        if billing.enabled:
            # Preflight before an upload starts: the authoritative charge happens
            # in start_job once the bytes are verified.
            require_access(lambda: billing.preflight(who.id, anonymous=who.anonymous))
        swing_id = str(uuid.uuid4())
        key = f"{who.id}/{swing_id}/source{EXTENSIONS[body.content_type]}"
        try:
            row = repo.insert({"id": swing_id, "user_id": who.id, "status": "awaiting_upload", "club": body.club,
                               "file_name": body.file_name[-200:], "content_type": body.content_type,
                               "size_bytes": body.size_bytes, "source_key": key,
                               "captured_at": body.captured_at.isoformat() if body.captured_at else None})
        except AccountBlocked:
            # The deletion started between the guard above and this insert: the write is refused
            # atomically rather than creating a row the deletion would have to chase.
            raise ApiError(403, "account_deleted", "This account has been deleted. Please sign in again.") from None
        upload = storage.upload_instructions(key, body.content_type)
        return CreateJobResponse(job=job_view(row), upload=UploadInstructions(**upload))

    @app.post("/v1/analysis/jobs/{swing_id}/start", response_model=Job)
    def start_job(swing_id: str, who: User = Depends(user)) -> Job:
        row = owned(swing_id, who)
        if row["status"] != "awaiting_upload":
            return job_view(row)
        size = storage.size(row["source_key"])
        if not size:
            raise ApiError(409, "upload_missing", "Your video didn't finish uploading. Please try again.")
        declared = int(row.get("size_bytes") or 0)
        if declared and size < declared:
            # Fewer bytes stored than the app said it would send: the upload is still arriving (or was
            # cut off). Starting now would analyse a truncated video, which reads as "no swing found".
            # Sizes only in the log: no key, URL or content.
            log.warning("start %s refused: %d of %d bytes stored", swing_id[:8], size, declared)
            raise ApiError(409, "upload_missing", "Your video is still uploading. Please wait a moment.")
        if size > settings.max_upload_bytes:
            storage.delete([row["source_key"]])
            repo.update(swing_id, status="failed", error_code="too_large", finished_at=now())
            return job_view(repo.get(swing_id))
        if billing.enabled:
            # billing.admit raises BillingExhausted / BillingInProgress / BillingSignInRequired,
            # which require_access turns into stable 402 / 409 / 403 codes.
            result = require_access(lambda: billing.admit(who.id, swing_id, size, anonymous=who.anonymous))
            if result.get("status") == "not_found":
                raise ApiError(404, "not_found", "We couldn't find that swing.")
            # "admitted" reserved the credit and queued the swing atomically;
            # "already"/"not_awaiting" mean a concurrent /start got there first.
            app.state.worker.wake()
            return job_view(repo.get(swing_id) or row)
        # Conditional transition: two concurrent /start calls (upload finished + screen opened) must not
        # put a job a worker has already claimed back to "queued".
        try:
            queued = repo.queue_if_awaiting(swing_id, who.id, size)
        except AccountBlocked:
            raise ApiError(403, "account_deleted", "This account has been deleted. Please sign in again.") from None
        if queued is None:
            return job_view(repo.get(swing_id) or row)
        app.state.worker.wake()  # claim it now, not at the next poll
        return job_view(queued)

    @app.get("/v1/analysis/jobs/{swing_id}", response_model=Job)
    def get_job(swing_id: str, who: User = Depends(user)) -> Job:
        return job_view(owned(swing_id, who))

    # -- swings ----------------------------------------------------------------

    def owned(swing_id: str, who: User) -> dict:
        try:
            uuid.UUID(swing_id)
        except ValueError:
            raise ApiError(404, "not_found", "We couldn't find that swing.") from None
        row = repo.get_owned(swing_id, who.id)
        if row is None:
            raise ApiError(404, "not_found", "We couldn't find that swing.")
        return row

    def thumb_url(row: dict, signed: dict[str, str] | None = None) -> str | None:
        if not row.get("thumb_key"):
            return None
        return (signed or {}).get(row["thumb_key"]) or storage.signed_url(row["thumb_key"])

    @app.get("/v1/swings", response_model=SwingList)
    def list_swings(who: User = Depends(user)) -> SwingList:
        items = []
        rows = repo.list_owned(who.id, ("complete",))
        signed = storage.signed_urls([row["thumb_key"] for row in rows if row.get("thumb_key")])
        for row in rows:
            lite = row.get("summary") or {}
            items.append(SwingListItem(id=row["id"], captured_at=row.get("captured_at"), created_at=row["created_at"],
                                       club=row.get("club"), counts=lite.get("counts"),
                                       summary_text=lite.get("summary_text"), thumb_url=thumb_url(row, signed),
                                       score=lite.get("score"),
                                       notice=row.get("notice")))
        return SwingList(swings=items)

    _analysis_cache: dict[tuple[str, str], dict] = {}

    def load_analysis(row: dict) -> dict:
        key = (row["id"], row["result_key"])
        if key not in _analysis_cache:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "analysis.json"
                storage.download_to(row["result_key"], path)
                _analysis_cache[key] = json.loads(path.read_text(encoding="utf-8"))
            if len(_analysis_cache) > 64:
                _analysis_cache.pop(next(iter(_analysis_cache)))
        return _analysis_cache[key]

    @app.get("/v1/swings/{swing_id}", response_model=SwingDetail)
    def get_swing(swing_id: str, who: User = Depends(user)) -> SwingDetail:
        row = owned(swing_id, who)
        if row["status"] != "complete":
            raise ApiError(409, "not_ready", "This swing is still being analysed.")
        return swing_detail(row)

    @app.get("/v1/example", response_model=SwingDetail)
    def get_example(who: User = Depends(user)) -> SwingDetail:
        """The shared example analysis: read-only, no job, no billing, nothing about its owner."""

        example_id = settings.example_swing_id
        try:
            uuid.UUID(example_id)
        except ValueError:
            example_id = ""                                   # unset or malformed: no example
        row = repo.get(example_id) if example_id else None
        if row is None or row.get("status") != "complete":
            raise ApiError(404, "not_found", "There's no example analysis right now.")
        return swing_detail(row, example=True)

    def swing_detail(row: dict, *, example: bool = False) -> SwingDetail:
        analysis = load_analysis(row)
        when = datetime.fromisoformat(row["captured_at"] or row["created_at"])
        s = product_summary(analysis, swing_id=row["id"], when=when,
                            meta={"club": row.get("club"), "notice": row.get("notice")}, demo=False)
        ttl = settings.signed_url_ttl_s
        signed = storage.signed_urls([k for k in (row["result_key"], row["video_key"], row.get("thumb_key")) if k], ttl)
        replay = Replay(viewer_url=f"{settings.public_url}/v1/viewer/index.html", analysis_url=signed[row["result_key"]],
                        video_url=signed[row["video_key"]], expires_in_s=ttl)
        notice = None if example else (" ".join(n for n in (row.get("notice"), quality_notice(analysis)) if n) or None)
        return SwingDetail(id=row["id"], captured_at=row.get("captured_at"), created_at=row["created_at"], club=row.get("club"),
                           clubs=list(CLUBS), notice=notice, analysis_version=row.get("analysis_version"),
                           headline=s["headline"], summary_text=s["summary_text"], checkpoints=s["checkpoints"],
                           withheld=s["withheld"], breakdown=s["breakdown"], recommended=s["recommended"],
                           hand_position=s["hand_position"], camera=s["camera"], score=s["score"], focus=s["focus"],
                           thumb_url=thumb_url(row, signed), replay=replay)

    @app.patch("/v1/swings/{swing_id}", response_model=SwingListItem)
    def update_swing(swing_id: str, body: UpdateSwingRequest, who: User = Depends(user)) -> SwingListItem:
        owned(swing_id, who)
        row = repo.update_owned(swing_id, who.id, club=body.club)
        lite = row.get("summary") or {}
        return SwingListItem(id=row["id"], captured_at=row.get("captured_at"), created_at=row["created_at"],
                             club=row.get("club"), counts=lite.get("counts"), summary_text=lite.get("summary_text"),
                             thumb_url=thumb_url(row), notice=row.get("notice"), score=lite.get("score"))

    def swing_keys(row: dict) -> list[str]:
        return [k for k in (row.get("source_key"), row.get("result_key"), row.get("video_key"), row.get("thumb_key")) if k]

    @app.delete("/v1/swings/{swing_id}", status_code=204)
    def delete_swing(swing_id: str, who: User = Depends(user)) -> Response:
        row = owned(swing_id, who)
        if league is not None and row["status"] == "complete":
            # Keep it as a withdrawn entry of the open league week, so deleting can never raise LP.
            try:
                rec = swing_record(row, allow_backfill=True)
                league.swing_deleted(who.id, {"id": row["id"], "created_at": row["created_at"], "record": rec},
                                     app.state.clock())
            except Exception:
                log.exception("league: could not record a deleted swing")
        storage.delete(swing_keys(row))
        repo.delete_owned(swing_id, who.id)
        return Response(status_code=204)

    @app.post("/v1/account/apple", status_code=204)
    def register_apple(body: AppleRegistrationRequest, who: User = Depends(user)) -> Response:
        """Store the Apple grant for this account, bound to the verified Apple subject.

        Called by the app immediately after a successful ``linkIdentity`` / ``signInWithIdToken``.
        The code is single-use, so a failure means the golfer must run the Apple sheet again; nothing
        about the code or the refresh token is stored on the device or written to a log.
        """

        try:
            accounts.register_apple(who.id, code=body.authorization_code, nonce=body.nonce)
        except AccountError as error:
            raise ApiError(error.status, error.code, error.message) from None
        return Response(status_code=204)

    @app.delete("/v1/account", response_model=AccountDeletionResult)
    def delete_account(body: DeleteAccountRequest | None = None,
                       who: User = Depends(deleting_user)) -> AccountDeletionResult:
        """Durably delete the account: revoke Apple, remove storage and rows, then the auth user.

        A retry after a partial failure resumes where it stopped. ``authorization_code``/``nonce``
        are only needed for an Apple account linked before the server stored refresh tokens; the
        ``manual_apple_revocation`` fallback is Apple's documented TN3194 path for when no credential
        can be obtained at all. The response states which of those really happened.
        """

        if body is not None and bool(body.authorization_code) != bool(body.nonce):
            raise ApiError(422, "invalid_request", "Invalid request: authorization_code, nonce")
        if league is not None:
            try:
                league.leave(who.id)        # display name, memberships, captured entries (idempotent)
            except Exception:
                log.exception("league: could not remove league data during account deletion")
                raise ApiError(503, "temporarily_unavailable", "Please try again in a moment.") from None
        try:
            result = accounts.delete_account(
                who.id, code=body.authorization_code if body else None,
                nonce=body.nonce if body else None,
                manual_apple_revocation=bool(body and body.manual_apple_revocation))
        except AccountError as error:
            raise ApiError(error.status, error.code, error.message) from None
        return AccountDeletionResult(**result)

    @app.post("/v1/feedback", status_code=201)
    def feedback(body: FeedbackRequest, who: User = Depends(user)) -> dict:
        """Store in-app feedback. Context comes from the stored swing, never from the client's word."""

        from ..scoring.config import CHECKS

        message = (body.message or "").strip() or None
        context: dict = {}
        swing_id = None
        if body.kind == "analysis":
            if not body.swing_id or not body.rating:
                raise ApiError(422, "invalid_request", "Choose thumbs up or thumbs down.")
            row = owned(body.swing_id, who)
            swing_id = row["id"]
            summary = row.get("summary") or {}
            if isinstance(summary, str):
                try:
                    summary = json.loads(summary)
                except ValueError:
                    summary = {}
            score = summary.get("score") if isinstance(summary, dict) else None
            context = {"score": score if isinstance(score, dict) else None, "club": row.get("club"),
                       "analysis_version": row.get("analysis_version"), "swing_created_at": row.get("created_at")}
            known = {c.id for c in CHECKS}
            if body.measured_checks:
                context["measured_checks"] = sorted({c for c in body.measured_checks if c in known})
        else:
            if not body.category or not message:
                raise ApiError(422, "invalid_request", "Choose a category and write a message.")
        try:
            result = repo.feedback_submit(user_id=who.id, kind=body.kind, swing_id=swing_id, rating=body.rating,
                                          category=body.category if body.kind == "general" else None,
                                          message=message, context=context, app_version=body.app_version)
        except RepoError:
            log.exception("feedback: could not be stored")
            raise ApiError(503, "feedback_unavailable", "We couldn't send your feedback just now. Please try again.") from None
        if result.get("blocked"):
            raise ApiError(403, "account_deleted", "This account has been deleted. Please sign in again.")
        if result.get("not_found"):
            raise ApiError(404, "not_found", "We couldn't find that swing.")
        return {"status": "received"}

    @app.get("/v1/drills")
    def drills(_: User = Depends(user)) -> dict:
        return app_drills.catalogue()

    # -- progress, comparisons and ranks (1.0.1; additive) ---------------------------

    BACKFILL_PER_REQUEST = 40

    def user_history(user_id: str, budget: int = BACKFILL_PER_REQUEST) -> tuple[list[dict], int]:
        """The user's complete swings with their history records (oldest first), and how many still lack one."""

        rows = sorted(repo.list_owned(user_id, ("complete",)), key=lambda r: r["created_at"])
        history, missing = [], 0
        for row in rows:
            lite = row.get("summary") or {}
            stored = (isinstance(lite, dict) and isinstance(lite.get("record"), dict)
                      and lite["record"].get("version", 1) >= swing_history.RECORD_VERSION)
            rec = swing_record(row, allow_backfill=budget > 0)
            if not stored and rec is not None:
                budget -= 1
            if rec is None:
                missing += 1
                continue
            history.append({"id": row["id"], "created_at": row["created_at"], "captured_at": row.get("captured_at"),
                            "club": row.get("club"), "record": rec})
        return history, missing

    def swing_record(row: dict, *, allow_backfill: bool) -> dict | None:
        """The swing's compact history record: stored, else built from its stored analysis and saved."""

        lite = row.get("summary") or {}
        if (isinstance(lite, dict) and isinstance(lite.get("record"), dict)
                and lite["record"].get("version", 1) >= swing_history.RECORD_VERSION):
            return lite["record"]
        if not allow_backfill or not row.get("result_key"):
            return None
        try:
            rec = swing_history.record(load_analysis(row))
        except Exception:
            log.exception("progress: could not build a record for swing %s", row["id"][:8])
            return None
        try:
            repo.update_owned(row["id"], row["user_id"], summary={**(lite if isinstance(lite, dict) else {}), "record": rec})
        except Exception:
            log.exception("progress: could not store the record for swing %s", row["id"][:8])
        return rec

    @app.get("/v1/progress")
    def progress(who: User = Depends(user)) -> dict:
        """Score history, personal bests and the Swing Guru Rank, from the caller's own swings only."""

        history, missing = user_history(who.id)
        rank = ranks.compute(history)
        swings_out = [{"id": h["id"], "created_at": h["created_at"], "club": h["club"],
                       "score": h["record"]["score"].get("total"),
                       "checks": [{k: c[k] for k in ("id", "status", "value", "unit")} for c in h["record"]["checks"]]}
                      for h in history]
        totals = [s for s in swings_out if s["score"] is not None]
        best = max(totals, key=lambda s: s["score"], default=None)
        best_by_club: dict[str, dict] = {}
        for s in totals:
            group = swing_history.club_group(s["club"]) or "unknown"
            if group not in best_by_club or s["score"] > best_by_club[group]["score"]:
                best_by_club[group] = {"swing_id": s["id"], "score": s["score"], "date": s["created_at"]}
        return {"swings": swings_out, "pending": missing, "rank": rank,
                "personal_bests": {"score": {"swing_id": best["id"], "score": best["score"], "date": best["created_at"]}
                                   if best else None, "by_club": best_by_club}}

    @app.get("/v1/compare")
    def compare_swings(a: str, b: str, who: User = Depends(user)) -> dict:
        """Two of the caller's swings: ``a`` the earlier/baseline, ``b`` the later. Fair comparisons only."""

        row_a, row_b = owned(a, who), owned(b, who)
        if row_a["status"] != "complete" or row_b["status"] != "complete":
            raise ApiError(409, "not_ready", "Both swings need to finish analysing first.")
        rec_a = swing_record(row_a, allow_backfill=True)
        rec_b = swing_record(row_b, allow_backfill=True)
        if rec_a is None or rec_b is None:
            raise ApiError(503, "temporarily_unavailable", "Please try again in a moment.")
        out = swing_history.compare(rec_a, rec_b, club_a=row_a.get("club"), club_b=row_b.get("club"))
        out["swings"] = {k: {"id": r["id"], "created_at": r["created_at"], "club": r.get("club")}
                         for k, r in (("a", row_a), ("b", row_b))}
        # Where the matching positions are in each swing's replay video (the analysis' own moment times).
        out["moments"] = {}
        for k, r in (("a", row_a), ("b", row_b)):
            try:
                phases = load_analysis(r).get("phases", [])
            except Exception:
                phases = []
            out["moments"][k] = {p["phase"]: p["time_s"] for p in phases
                                 if p.get("phase") in ("address", "top", "impact") and p.get("time_s") is not None}
        return out

    # -- weekly leagues (1.0.1, staging only; SG_LEAGUE) -------------------------------

    if league is not None:
        def league_call(work):
            try:
                return work()
            except LeagueError as error:
                raise ApiError(error.status, error.code, error.message) from None

        @app.get("/v1/league")
        def league_view(who: User = Depends(user)) -> dict:
            """This week's group, the caller's LP breakdown and last week's result (rivals clearly labelled)."""

            out = league_call(lambda: league.view(who.id, app.state.clock()))
            app.state.league_backups.maybe_backup(app.state.clock())          # at least daily; never raises
            return out

        @app.get("/v1/league/names")
        def league_names(page: int = 0, who: User = Depends(user)) -> dict:
            """Generated league names to choose from (players don't type names: no user-generated content)."""

            return {"names": league.name_suggestions(who.id, page)}

        @app.post("/v1/league/join")
        def league_join(body: LeagueNameRequest, who: User = Depends(user)) -> dict:
            return league_call(lambda: league.join(who.id, body.display_name, app.state.clock()))

        @app.patch("/v1/league/profile")
        def league_rename(body: LeagueNameRequest, who: User = Depends(user)) -> dict:
            return league_call(lambda: league.rename(who.id, body.display_name, app.state.clock()))

        @app.delete("/v1/league/profile", status_code=204)
        def league_leave(who: User = Depends(user)) -> Response:
            league.leave(who.id)
            return Response(status_code=204)

    # -- the guided viewer (loaded by the app's WebView for replay) -------------------

    @app.get("/v1/viewer/{name}")
    def viewer(name: str) -> Response:
        if name not in VIEWER_FILES:
            raise ApiError(404, "not_found", "Not found")
        return FileResponse(VIEWER / name, media_type=VIEWER_FILES[name], headers={"Cache-Control": "public, max-age=300"})

    # -- test data import (staging only) --------------------------------------------

    if settings.import_key and not settings.production and isinstance(storage, LocalStorage):
        import base64
        import re

        test_user = re.compile(r"^[A-Za-z0-9-]{8,64}$")
        files_in = {"analysis": ("analysis.json", "application/json"), "video": ("replay.mp4", "video/mp4"),
                    "thumb": ("thumb.jpg", "image/jpeg")}
        copied = ("club", "notice", "captured_at", "created_at", "finished_at", "analysis_version", "summary",
                  "file_name")

        @app.post("/v1/admin/import", status_code=201)
        async def import_swing(request: Request, x_sg_import_key: str | None = Header(default=None)) -> dict:
            """Copy one already-analysed swing (a tester's own, exported from production) onto this server.

            Staging only: the route exists only with SG_IMPORT_KEY set, which config refuses in production.
            Idempotent on the swing id. Only the swing's own files and display columns are accepted."""

            if not hmac.compare_digest((x_sg_import_key or "").encode(), settings.import_key.encode()):
                raise ApiError(401, "unauthorized", "Not allowed.")
            body = await request.json()
            user_id, swing = str(body.get("user_id") or ""), body.get("swing") or {}
            if not test_user.match(user_id):
                raise ApiError(422, "invalid_request", "Invalid request: user_id")
            try:
                swing_id = str(uuid.UUID(str(swing.get("id"))))
            except ValueError:
                raise ApiError(422, "invalid_request", "Invalid request: swing.id") from None
            if repo.get(swing_id) is not None:
                return {"status": "exists", "id": swing_id}
            blobs = body.get("files") or {}
            if not blobs.get("analysis") or not blobs.get("video"):
                raise ApiError(422, "invalid_request", "Invalid request: files")
            keys = {}
            with tempfile.TemporaryDirectory() as tmp:
                for name, (file_name, content_type) in files_in.items():
                    if not blobs.get(name):
                        continue
                    data = base64.b64decode(blobs[name], validate=True)
                    if len(data) > settings.max_upload_bytes:
                        raise ApiError(413, "too_large", "Too large.")
                    path = Path(tmp) / file_name
                    path.write_bytes(data)
                    key = f"{user_id}/{swing_id}/imported-{file_name}"
                    storage.put_file(key, path, content_type)
                    keys[name] = key
            row = {k: swing.get(k) for k in copied if swing.get(k) is not None}
            repo.insert({**row, "id": swing_id, "user_id": user_id, "status": "complete",
                         "result_key": keys["analysis"], "video_key": keys["video"], "thumb_key": keys.get("thumb"),
                         "content_type": "video/mp4"})
            log.info("import: swing %s copied for a test account", swing_id[:8])
            return {"status": "imported", "id": swing_id}

    # -- local signed files (development storage only) ----------------------------

    if isinstance(storage, LocalStorage):
        local: LocalStorage = storage

        @app.put("/v1/files/{key:path}")
        async def put_file(key: str, exp: int, sig: str, request: Request):
            if not local.verify(key, "PUT", exp, sig):
                raise ApiError(403, "forbidden", "This upload link has expired. Please try again.")
            length = int(request.headers.get("content-length") or 0)
            if length <= 0 or length > settings.max_upload_bytes:
                raise ApiError(413, "too_large", "This video is too large.")
            # Written to a side file and moved into place only when complete, so the object appears
            # all at once (as in Supabase Storage). Writing in place let /start see a partly received
            # upload as present and queue a truncated video (staging, 2026-11-07: 131071-byte uploads).
            try:
                target = local.open(key)
            except StorageError:
                raise ApiError(400, "invalid_key", "Invalid upload.") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(target.name + f".{uuid.uuid4().hex[:8]}.part")
            written = 0
            try:
                with partial.open("wb") as handle:
                    async for chunk in request.stream():
                        written += len(chunk)
                        if written > settings.max_upload_bytes:
                            raise ApiError(413, "too_large", "This video is too large.")
                        handle.write(chunk)
                if written != length:
                    log.warning("upload %s incomplete: %d of %d bytes", key.rsplit("/", 2)[-2][:8], written, length)
                    raise ApiError(400, "upload_incomplete", "Your video didn't finish uploading. Please try again.")
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
            return {"stored": written}

        @app.get("/v1/files/{key:path}")
        def get_file(key: str, exp: int, sig: str):
            if not local.verify(key, "GET", exp, sig):
                raise ApiError(403, "forbidden", "This link has expired.")
            try:
                path = local.open(key)
            except StorageError:
                raise ApiError(404, "not_found", "Not found") from None
            if not path.is_file():
                raise ApiError(404, "not_found", "Not found")
            types = {".json": "application/json", ".mp4": "video/mp4", ".jpg": "image/jpeg", ".mov": "video/quicktime"}
            return FileResponse(path, media_type=types.get(path.suffix, "application/octet-stream"),
                                headers={"Access-Control-Allow-Origin": "*"})

    return app


def models_present() -> bool:
    """The model files the engine needs are in the image (no download at request time)."""

    try:
        from ..pose import default_model_path
        from ..pose_rtm import local_model_paths

        if not Path(default_model_path()).is_file():
            return False
        if any(p.startswith("missing:") for p in local_model_paths("wholebody", "balanced")):
            return False
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache("google/owlv2-base-patch16-ensemble", "config.json"), str)
    except Exception:
        return False


__all__ = ["create_app", "models_present"]
