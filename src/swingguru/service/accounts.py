"""Durable Apple linking and the server-side account deletion workflow.

The Apple side is modelled as an append-only list of *grants* (one row per minted refresh token, each
with its own revocation state), never as "the account's token". That matters for two real races:

* two authorization-code exchanges can overlap, and Apple does not promise that a new authorization
  invalidates the previous refresh token - a single row per user would silently lose one of them;
* a token can be minted by an exchange that finishes *after* a deletion already recorded its Apple
  step - an account-level "revoked" flag must never be accepted as proof that this newer token is
  dead.

So: registration appends a grant; deletion revokes each active grant for real (Apple HTTP 200), marks
exactly that grant revoked through a compare-and-set, and completion is refused while any grant for
the account is still active. A revoked grant row is only removed after that per-grant confirmation.

The rest of the workflow is unchanged in intent: a tombstone is written first (which is also what
denies the account's still-valid JWTs), storage and the app rows are removed, the Supabase auth user
is deleted, required checkpoints fail closed, and a deletion no client can finish is completed by
:meth:`AccountService.reconcile` from the worker's janitor.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from .apple import AppleClient, AppleError
from .config import Settings
from .repo import AccountBlocked, Repo, RepoError
from .storage import Storage, StorageError
from .tokenbox import TokenBox, TokenBoxError

log = logging.getLogger("swingguru.accounts")

#: Golfer-facing copy for each failure. Every message is nontechnical, states whether anything was
#: removed, and (where true) tells the golfer that retrying is safe.
APPLE_UNAVAILABLE = ("apple_unavailable",
                     "We couldn't reach Apple just now, so your account isn't deleted yet. Please try again in a moment.")
DELETE_INCOMPLETE = ("delete_incomplete",
                     "Your account isn't fully deleted yet. Please delete your account again - it is safe to repeat.")
REAUTH_REQUIRED = ("apple_reauthorization_required",
                   "For your security, please sign in with Apple again to finish deleting your account.")
IDENTITY_MISMATCH = ("identity_mismatch",
                     "We couldn't confirm that Apple account for this profile. Please try again.")

#: How long after the storage sweep the janitor keeps re-clearing an owner's prefix. The horizon uses
#: the storage backend's own upload-URL lifetime (see ``Storage.upload_url_lifetime_s``), plus this
#: margin, so a signed upload URL can never outlive the final sweep.
CLEANUP_MARGIN_S = 3600


class AccountError(Exception):
    """A stable, golfer-facing account failure the API maps to one error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class _IdentityUnavailable(Exception):
    """The live Supabase identity lookup failed: the caller must fail closed (and not guess)."""


def apple_subject_of(user: dict | None) -> str | None:
    """The Apple subject Supabase holds for this user, or None.

    Only ``identities`` is read: never ``email``, ``user_metadata`` or any client-supplied value.
    """

    if not isinstance(user, dict):
        return None
    for identity in user.get("identities") or []:
        if not isinstance(identity, dict) or identity.get("provider") != "apple":
            continue
        data = identity.get("identity_data")
        subject = (data or {}).get("sub") if isinstance(data, dict) else None
        if not isinstance(subject, str) or not subject:
            subject = identity.get("id")
        if isinstance(subject, str) and subject:
            return subject
    return None


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


class AccountService:
    """Apple registration + durable deletion. Duck-typed collaborators keep the tests offline."""

    def __init__(self, settings: Settings, repo: Repo, storage: Storage, *,
                 apple: AppleClient | None = None, box: TokenBox | None = None,
                 http: httpx.Client | None = None, timeout: float = 30.0) -> None:
        self.settings, self.repo, self.storage = settings, repo, storage
        self.timeout = timeout
        self.http = http or httpx.Client(timeout=timeout)
        self.apple = apple
        if self.apple is None and settings.apple_configured:
            self.apple = AppleClient(settings, http=http, timeout=min(timeout, 10.0))
        self.box = box
        if self.box is None and settings.apple_configured:
            try:
                self.box = TokenBox(settings.apple_token_key)
            except TokenBoxError as error:
                # A bad key must never crash startup: it fails the Apple paths closed instead.
                log.error("apple token key is unusable (%s); Apple deletion will be refused", type(error).__name__)
                self.box = None

    @property
    def configured(self) -> bool:
        """True only when the Apple call *and* the encryption key are both usable."""

        return bool(self.settings.apple_configured and self.apple is not None and self.box is not None)

    # -- live Supabase identity -------------------------------------------------

    def _supabase_user(self, user_id: str) -> dict | None:
        """The auth user as Supabase knows it right now (None when it no longer exists)."""

        if not (self.settings.supabase_url and self.settings.supabase_service_key):
            raise _IdentityUnavailable("Supabase admin is not configured")
        try:
            response = self.http.get(
                f"{self.settings.supabase_url}/auth/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {self.settings.supabase_service_key}",
                         "apikey": self.settings.supabase_service_key}, timeout=self.timeout)
        except httpx.HTTPError:
            raise _IdentityUnavailable("identity lookup transport error") from None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise _IdentityUnavailable(f"identity lookup status {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise _IdentityUnavailable("identity lookup is not JSON") from None
        return payload if isinstance(payload, dict) else None

    def apple_subject(self, user_id: str) -> str | None:
        """The Apple subject for this Supabase user, verified live. Raises when unverifiable."""

        return apple_subject_of(self._supabase_user(user_id))

    # -- registration -----------------------------------------------------------

    def register_apple(self, user_id: str, *, code: str, nonce: str) -> None:
        """Exchange the app's authorization code and append the sealed refresh token."""

        if not self.configured:
            raise AccountError(503, *APPLE_UNAVAILABLE)
        try:
            subject = self.apple_subject(user_id)
        except _IdentityUnavailable as error:
            log.info("apple registration: identity lookup failed (%s)", error)
            raise AccountError(503, *APPLE_UNAVAILABLE) from None
        if not subject:
            # The account has no Apple identity: refuse rather than store a token for the wrong user.
            log.info("apple registration refused: no Apple identity on this account")
            raise AccountError(403, *IDENTITY_MISMATCH)
        try:
            credential = self.apple.exchange(code, nonce=nonce)
        except AppleError as error:
            raise _map_apple_register(error) from None
        if not hmac.compare_digest(credential.subject, subject):
            log.warning("apple registration refused: subject does not match the Supabase identity")
            raise AccountError(403, *IDENTITY_MISMATCH)

        grant_id = str(uuid.uuid4())
        sealed = self._seal(credential.refresh_token)
        try:
            stored = self.repo.apple_grant_put(user_id, grant_id, credential.subject,
                                               self.settings.apple_client_id, sealed)
        except RepoError:
            log.warning("apple registration: grant store failed (%s)", type(RepoError).__name__)
            raise AccountError(503, *APPLE_UNAVAILABLE) from None
        if not stored:
            # A deletion owns this account. The code was already spent, so the token we just minted is
            # live: keep it and revoke it rather than refuse the row and leak the grant.
            self._quarantine_after_deletion(user_id, grant_id, credential.subject, sealed,
                                            credential.refresh_token)
            raise AccountError(409, "account_deleted", "This account has been deleted. Please sign in again.")
        log.info("apple grant stored for user %s", user_id)

    def _quarantine_after_deletion(self, user_id: str, grant_id: str, subject: str, sealed: str,
                                   refresh_token: str) -> None:
        """A grant minted while the account was being deleted: keep it, then revoke it now."""

        kept = False
        try:
            kept = self.repo.apple_grant_put_for_deletion(user_id, grant_id, subject,
                                                          self.settings.apple_client_id, sealed)
        except RepoError:
            kept = False
        if kept:
            log.info("apple registration: a token minted during deletion was retained for revocation")
            try:
                self.apple.revoke(refresh_token)
            except AppleError as error:
                self._note(user_id, last_error_code=f"apple_revoke_{error.kind}")
                return          # the stored grant row is the retry protection
            try:
                self.repo.apple_grant_mark_revoked(grant_id, user_id)
                self._record(user_id, apple_revocation="revoked", apple_revoked_at=True)
            except RepoError:
                self._note(user_id, last_error_code="revoked_checkpoint_failed")
            return
        # We could not even record it: revoke with no retry protection, and if that also fails say so
        # rather than let a live, untracked grant look like a clean sign-in failure.
        try:
            self.apple.revoke(refresh_token)
        except AppleError:
            log.warning("apple registration: an orphaned grant could not be revoked")
            raise AccountError(503, *APPLE_UNAVAILABLE) from None

    # -- deletion ---------------------------------------------------------------

    def delete_account(self, user_id: str, *, code: str | None = None, nonce: str | None = None,
                       manual_apple_revocation: bool = False) -> dict:
        """Durable, idempotent deletion. Raises :class:`AccountError` when not fully done."""

        try:
            state = self.repo.account_deletion_begin(user_id)
        except RepoError:
            log.warning("account deletion: tombstone could not be written")
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        if state.get("status") == "deleted":
            return self._result(state)

        self._settle_apple_if_needed(user_id, state, code=code, nonce=nonce,
                                     manual=manual_apple_revocation)
        try:
            state = self.repo.account_deletion_get(user_id) or state
        except RepoError:
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        return self._complete(user_id, state)

    @staticmethod
    def _result(state: dict) -> dict:
        revocation = state.get("apple_revocation")
        if state.get("status") != "deleted" or revocation not in ("revoked", "not_required",
                                                                 "manual_required"):
            raise AccountError(502, *DELETE_INCOMPLETE)
        return {"status": "deleted", "apple_revocation": revocation}

    def _settle_apple_if_needed(self, user_id: str, state: dict, *, code: str | None, nonce: str | None,
                                manual: bool) -> str:
        """Bring every grant to a decided state, then record the account-level summary."""

        active = self._active_grants(user_id)
        if not active and code and nonce:
            # A fresh authorization proves the Apple account and mints a *new* grant; append it, then
            # revoke it like any other (a timeout after this point still leaves a revocable row).
            self._adopt_fresh_code(user_id, code, nonce)
            active = self._active_grants(user_id)

        recorded = state.get("apple_revocation")
        if active:
            self._revoke_grants(user_id)
            summary = "revoked"
        elif recorded in ("revoked", "not_required", "manual_required"):
            summary = recorded
        elif self.settings.auth_mode != "supabase":
            summary = "not_required"
        else:
            try:
                subject = self.apple_subject(user_id)
            except _IdentityUnavailable:
                self._apple_unavailable(user_id)
            if not subject:
                summary = "not_required"
            elif manual:
                # TN3194: with no credential obtainable at all, delete anyway and tell the golfer to
                # finish the Apple step in Settings. Allowed without server Apple configuration too -
                # there is nothing to revoke - but the normal revoke path below stays a release
                # blocker when configuration is missing.
                summary = "manual_required"
            else:
                if not self.configured:
                    self._apple_unavailable(user_id)
                self._note(user_id, last_error_code=REAUTH_REQUIRED[0])
                raise AccountError(409, *REAUTH_REQUIRED)
        self._record(user_id, apple_revocation=summary)
        return summary

    def _adopt_fresh_code(self, user_id: str, code: str, nonce: str) -> str:
        """Persist the grant a fresh authorization mints, before Apple is called."""

        if not self.configured:
            self._apple_unavailable(user_id)
        try:
            subject = self.apple_subject(user_id)
        except _IdentityUnavailable:
            self._apple_unavailable(user_id)
        if not subject:
            raise AccountError(403, *IDENTITY_MISMATCH)
        try:
            fresh = self.apple.exchange(code, nonce=nonce)
        except AppleError as error:
            self._note(user_id, last_error_code=f"apple_{error.kind}")
            if error.kind == "rejected":
                raise AccountError(409, *REAUTH_REQUIRED) from None
            raise AccountError(503, *APPLE_UNAVAILABLE) from None
        if not hmac.compare_digest(fresh.subject, subject):
            log.warning("account deletion refused: fresh Apple subject does not match")
            self._note(user_id, last_error_code="apple_identity_mismatch")
            raise AccountError(403, *IDENTITY_MISMATCH)
        grant_id = str(uuid.uuid4())
        sealed = self._seal(fresh.refresh_token)
        try:
            kept = self.repo.apple_grant_put_for_deletion(user_id, grant_id, fresh.subject,
                                                          self.settings.apple_client_id, sealed)
        except RepoError:
            self._note(user_id, last_error_code="grant_store_failed")
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        if not kept:
            raise AccountError(502, *DELETE_INCOMPLETE)
        return grant_id

    def _active_grants(self, user_id: str) -> list[dict]:
        try:
            return self.repo.apple_grants_unrevoked(user_id)
        except RepoError:
            self._incomplete(user_id, "grant_read_failed")

    def _revoke_grants(self, user_id: str) -> None:
        """Revoke every active grant for real, then confirm none is left. Never guesses."""

        active = self._active_grants(user_id)
        if active and not self.configured:
            # Keep every token; the janitor retries. Never delete a known grant unrevoked.
            self._apple_unavailable(user_id)
        for grant in active:
            grant_id = str(grant.get("id") or "")
            try:
                token = self.box.open(grant.get("refresh_token_ciphertext") or "")
            except TokenBoxError:
                log.warning("account deletion: stored grant could not be decrypted")
                self._apple_unavailable(user_id)
            try:
                self.apple.revoke(token)
            except AppleError as error:
                self._note(user_id, last_error_code=f"apple_revoke_{error.kind}")
                raise AccountError(503, *APPLE_UNAVAILABLE) from None
            try:
                # Per-grant compare-and-set: this row (and only this row) is marked, and only after
                # Apple confirmed it. A concurrent attempt marking the same row first is a no-op.
                self.repo.apple_grant_mark_revoked(grant_id, user_id)
            except RepoError:
                self._note(user_id, last_error_code="grant_checkpoint_failed")
                raise AccountError(502, *DELETE_INCOMPLETE) from None
        remaining = self._active_grants(user_id)
        if remaining:
            log.warning("account deletion: %d grant(s) could not be confirmed revoked", len(remaining))
            self._apple_unavailable(user_id)
        try:
            counts = self.repo.apple_grant_counts(user_id)
        except RepoError:
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        self._record(user_id, apple_revocation="revoked" if counts.get("revoked") else "not_required",
                     apple_revoked_at=True if counts.get("revoked") else None)

    def _complete(self, user_id: str, state: dict) -> dict:
        """The steps after the Apple decision. Every required checkpoint is persisted fail-closed."""

        # A registration exchange can land a grant between any of these steps; the account predicate
        # and this check on both sides of the auth-user delete are what keep it from being destroyed
        # unrevoked.
        self._ensure_no_active_grant(user_id)
        if not state.get("storage_cleared_at"):
            try:
                self._clear_storage(user_id)
            except StorageError:
                self._note(user_id, last_error_code="storage_clear_failed")
                raise AccountError(502, *DELETE_INCOMPLETE) from None
        if not state.get("data_cleared_at"):
            try:
                self.repo.account_wipe(user_id)
            except RepoError:
                self._note(user_id, last_error_code="data_wipe_failed")
                raise AccountError(502, *DELETE_INCOMPLETE) from None
            self._record(user_id, data_cleared_at=True)
        if self.settings.auth_mode == "supabase" and not state.get("auth_deleted_at"):
            # Local/dev accounts have no Supabase user to delete (dev auth is never production).
            self._delete_auth_user(user_id)
            self._record(user_id, auth_deleted_at=True)
        self._ensure_no_active_grant(user_id)
        try:
            # Only revoked rows are removed; a live grant can never leave the table this way.
            self.repo.apple_grants_delete_revoked(user_id)
        except RepoError:
            self._note(user_id, last_error_code="grant_delete_failed")
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        try:
            final = self.repo.account_deletion_finish(user_id)
        except RepoError:
            self._note(user_id, last_error_code="finish_failed")
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        if not isinstance(final, dict) or final.get("status") != "deleted":
            raise AccountError(502, *DELETE_INCOMPLETE)
        revocation = final.get("apple_revocation") or state.get("apple_revocation")
        if revocation not in ("revoked", "not_required", "manual_required"):
            raise AccountError(502, *DELETE_INCOMPLETE)
        log.info("account deleted for user %s", user_id)
        return {"status": "deleted", "apple_revocation": revocation}

    def _ensure_no_active_grant(self, user_id: str) -> None:
        if self._active_grants(user_id):
            self._revoke_grants(user_id)

    def _clear_storage(self, user_id: str) -> None:
        try:
            rows = self.repo.owned_rows(user_id)
        except RepoError:
            self._incomplete(user_id, "storage_index_failed")
        keys = [key for row in rows for key in _swing_keys(row)]
        if keys:
            self.storage.delete(keys)
        # Owner-scoped, recursive listing then exact-name deletion (never one folder-prefix call).
        self.storage.delete_prefix(f"{user_id}/")
        lifetime = getattr(self.storage, "upload_url_lifetime_s", self.settings.upload_url_ttl_s)
        cleanup_until = _iso(datetime.now(timezone.utc)
                             + timedelta(seconds=float(lifetime) + CLEANUP_MARGIN_S))
        self._record(user_id, storage_cleared_at=True, cleanup_until=cleanup_until)

    def _delete_auth_user(self, user_id: str) -> None:
        if not (self.settings.supabase_url and self.settings.supabase_service_key):
            self._incomplete(user_id, "supabase_not_configured")
        try:
            response = self.http.delete(
                f"{self.settings.supabase_url}/auth/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {self.settings.supabase_service_key}",
                         "apikey": self.settings.supabase_service_key}, timeout=self.timeout)
        except httpx.HTTPError:
            self._incomplete(user_id, "auth_delete_transport")
        if response.status_code >= 400 and response.status_code != 404:
            log.warning("account deletion: auth user not deleted (%s)", response.status_code)
            self._incomplete(user_id, f"auth_delete_status_{response.status_code}")

    # -- janitor reconciliation -------------------------------------------------

    def reconcile(self, *, stuck_before: str, cleanup_before: str) -> None:
        """Finish deletions no client can finish, re-sweep prefixes, and revoke late grants.

        Called by the worker's janitor. It never invents a revocation: a stored grant is revoked for
        real or the tombstone stays pending; a credential-less Apple account records the TN3194
        manual state; a plain guest account records ``not_required``. It also covers an account that
        is already ``deleted`` but has since gained a grant (an exchange that raced the deletion).
        """

        try:
            rows = self.repo.account_deletions_for_reconcile(stuck_before=stuck_before,
                                                             cleanup_before=cleanup_before)
        except RepoError:
            log.warning("deletion reconciliation: tombstones unreadable")
            return
        for row in rows:
            user_id = str(row.get("user_id") or "")
            if not user_id:
                continue
            try:
                if row.get("status") == "deleted":
                    self._sweep_deleted(user_id)
                else:
                    self._reconcile_pending(user_id, row)
            except AccountError as error:
                # ``_revoke_grants`` already recorded the precise upstream failure; keep that rather
                # than replacing it with the less specific code.
                if error.code != "apple_unavailable":
                    self._note(user_id, last_error_code=f"reconcile_{error.code}")
            except StorageError:
                self._note(user_id, last_error_code="reconcile_storage_failed")

    def _sweep_deleted(self, user_id: str) -> None:
        # A grant can be appended to an already-deleted account by an exchange that raced the
        # deletion: revoke it before declaring the cleanup done.
        if self._active_grants(user_id):
            if not self.configured:
                raise AccountError(503, *APPLE_UNAVAILABLE)
            self._revoke_grants(user_id)
            try:
                self.repo.apple_grants_delete_revoked(user_id)
            except RepoError:
                raise AccountError(502, *DELETE_INCOMPLETE) from None
        self.storage.delete_prefix(f"{user_id}/")
        # Informational: if this write is lost the next janitor pass simply sweeps again.
        self._note(user_id, cleanup_done_at=True)

    def _reconcile_pending(self, user_id: str, row: dict) -> None:
        if self._active_grants(user_id):
            if not self.configured:
                # Release blocker, and safe: retry on a later janitor pass.
                raise AccountError(503, *APPLE_UNAVAILABLE)
            self._revoke_grants(user_id)
        elif not row.get("apple_revocation"):
            self._decide_without_client(user_id)
        try:
            state = self.repo.account_deletion_get(user_id) or row
        except RepoError:
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        self._complete(user_id, state)

    def _decide_without_client(self, user_id: str) -> None:
        """Decide the Apple state with no live client: there is no grant left to revoke here."""

        if self.settings.auth_mode != "supabase":
            self._record(user_id, apple_revocation="not_required")
            return
        try:
            auth_user = self._supabase_user(user_id)
        except _IdentityUnavailable:
            raise AccountError(503, *APPLE_UNAVAILABLE) from None
        if auth_user is None or apple_subject_of(auth_user):
            # Either the account had an Apple identity (and its authorization code is long gone), or
            # the auth row is already gone so we cannot tell. Both are TN3194's manual case: finish
            # the deletion and never claim a revocation we did not perform.
            self._record(user_id, apple_revocation="manual_required")
        else:
            self._record(user_id, apple_revocation="not_required")

    # -- helpers ----------------------------------------------------------------

    def _record(self, user_id: str, **fields) -> dict:
        """A required checkpoint. If it cannot be persisted, the deletion did not happen."""

        try:
            row = self.repo.account_deletion_update(user_id, **fields)
        except RepoError:
            log.warning("account deletion: checkpoint could not be persisted")
            raise AccountError(502, *DELETE_INCOMPLETE) from None
        if not isinstance(row, dict) or not row:
            raise AccountError(502, *DELETE_INCOMPLETE)
        return row

    def _note(self, user_id: str, **fields) -> None:
        """An error note. Informational only; losing it does not change what happened."""

        try:
            self.repo.account_deletion_update(user_id, **fields)
        except RepoError:
            log.warning("account deletion: error note could not be persisted")

    def _apple_unavailable(self, user_id: str) -> None:
        self._note(user_id, last_error_code=APPLE_UNAVAILABLE[0])
        raise AccountError(503, *APPLE_UNAVAILABLE)

    def _incomplete(self, user_id: str, detail: str) -> None:
        self._note(user_id, last_error_code=detail)
        raise AccountError(502, *DELETE_INCOMPLETE)

    def _seal(self, refresh_token: str) -> str:
        try:
            return self.box.seal(refresh_token)
        except TokenBoxError:
            raise AccountError(503, *APPLE_UNAVAILABLE) from None


def _swing_keys(row: dict) -> list[str]:
    """The private objects one swing owns (mirrors ``api.swing_keys`` without importing it)."""

    return [key for key in (row.get("source_key"), row.get("result_key"), row.get("video_key"),
                            row.get("thumb_key")) if key]


def _map_apple_register(error: AppleError) -> AccountError:
    """Map an Apple failure to the registration endpoint's stable error."""

    if error.kind == "rejected":
        # The code was expired, replayed, or issued for a different client: the app must run the
        # Apple sheet again, so this is an actionable retry, not a server fault.
        return AccountError(409, "apple_code_rejected",
                            "We couldn't verify that Apple sign-in. Please try again.")
    return AccountError(503, *APPLE_UNAVAILABLE)


__all__ = ["APPLE_UNAVAILABLE", "CLEANUP_MARGIN_S", "DELETE_INCOMPLETE", "IDENTITY_MISMATCH",
           "REAUTH_REQUIRED", "AccountBlocked", "AccountError", "AccountService", "apple_subject_of"]
