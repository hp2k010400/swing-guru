"""Release accounts: durable Apple credential registration and end-to-end account deletion.

Runs the real API, worker, SQLite repository and local storage with Apple and the Supabase admin API
replaced by deterministic fakes and a mock transport. No live Apple call, Supabase project, purchase
or real deletion is performed.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from swingguru.service import apple as apple_module
from swingguru.service.accounts import AccountError, AccountService, apple_subject_of
from swingguru.service.apple import AppleClient, AppleCredential, AppleError, client_secret, nonce_matches
from swingguru.service.config import ConfigError, Settings, validate
from swingguru.service.repo import AccountBlocked, PostgrestRepo, RepoError, SqliteRepo
from swingguru.service.storage import LocalStorage, StorageError, check_prefix
from swingguru.service.tokenbox import TokenBox, TokenBoxError
from test_service import FakeRunner, _upload

UTC = timezone.utc
SECRET = "s" * 40
USER_1 = "0c6f5d8e-1111-2222-3333-444455556666"
USER_2 = "0c6f5d8e-aaaa-bbbb-cccc-000011112222"
APPLE_SUB = "001234.abcdef0123456789.0900"
REFRESH = "apple-refresh-token-value"
TOKEN_KEY = base64.b64encode(bytes(range(32))).decode("ascii")


def _pem() -> tuple[object, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode("ascii")
    return key, pem


_PRIVATE_KEY, _PRIVATE_PEM = _pem()


def token(user_id: str) -> dict[str, str]:
    claims = {"sub": user_id, "aud": "authenticated", "role": "authenticated", "is_anonymous": True,
              "exp": int(time.time()) + 600}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


A1, A2 = token(USER_1), token(USER_2)


def account_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        data_dir=tmp_path / "svc", public_url="http://testserver", auth_mode="supabase",
        supabase_url="https://proj.supabase.co", supabase_service_key="service-key",
        supabase_jwt_secret=SECRET, apple_team_id="TEAM123456", apple_key_id="KEY1234567",
        apple_private_key=_PRIVATE_PEM, apple_token_key=TOKEN_KEY,
    )
    return replace(Settings(), **{**base, **overrides})


class FakeApple:
    """Duck-typed ``AppleClient``: records the exchanges and revocations, never calls Apple."""

    def __init__(self, subject: str = APPLE_SUB, refresh: str = REFRESH) -> None:
        self.subject, self.refresh = subject, refresh
        self.exchanges: list[dict] = []
        self.revoked: list[str] = []
        self.exchange_error: AppleError | None = None
        self.revoke_errors: list[AppleError] = []

    def exchange(self, code: str, *, nonce: str | None = None, now: float | None = None) -> AppleCredential:
        self.exchanges.append({"code": code, "nonce": nonce})
        if self.exchange_error is not None:
            raise self.exchange_error
        return AppleCredential(subject=self.subject, refresh_token=self.refresh,
                               audience="com.swingguru.app", issuer=apple_module.APPLE_ISSUER,
                               expires_at=datetime.now(UTC) + timedelta(hours=1))

    def revoke(self, refresh_token: str, *, now: float | None = None) -> None:
        self.revoked.append(refresh_token)
        if self.revoke_errors:
            raise self.revoke_errors.pop(0)


class FakeAdmin:
    """The Supabase admin API surface the service uses: get user, delete user."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {
            # Both start as guests: the Apple identity is linked explicitly by the tests that need it.
            USER_1: {"id": USER_1, "identities": [{"provider": "email"}]},
            USER_2: {"id": USER_2, "identities": [{"provider": "email"}]},
        }
        self.deleted: list[str] = []
        self.get_failures: list[int] = []
        self.delete_status = 200

    def link_apple(self, user_id: str, subject: str = APPLE_SUB) -> None:
        user = self.users.setdefault(user_id, {"id": user_id})
        user["identities"] = [{"provider": "apple", "identity_data": {"sub": subject}}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/auth/v1/admin/users/"):
            return httpx.Response(404, json={})
        user_id = path.rsplit("/", 1)[-1]
        if request.method == "GET":
            if self.get_failures:
                return httpx.Response(self.get_failures.pop(0), json={})
            user = self.users.get(user_id)
            return httpx.Response(200, json=user) if user else httpx.Response(404, json={})
        if request.method == "DELETE":
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status, json={})
            self.deleted.append(user_id)
            self.users.pop(user_id, None)
            return httpx.Response(200, json={})
        return httpx.Response(405, json={})


@pytest.fixture
def service(tmp_path):
    def make(*, apple: FakeApple | None = None, admin: FakeAdmin | None = None, **overrides):
        settings = account_settings(tmp_path, **overrides)
        repo = SqliteRepo(tmp_path / "svc" / "db.sqlite3")
        storage = LocalStorage(settings)
        fake, fake_admin = apple or FakeApple(), admin or FakeAdmin()
        accounts = AccountService(
            settings, repo, storage, apple=fake,
            box=TokenBox(settings.apple_token_key) if settings.apple_token_key else None,
            http=httpx.Client(transport=httpx.MockTransport(fake_admin.handler)))
        app = create_app_for(settings, repo, storage, accounts)
        return TestClient(app), app, fake, fake_admin, settings

    return make


def create_app_for(settings, repo, storage, accounts):
    from swingguru.service.api import create_app

    return create_app(settings, repo=repo, storage=storage, start_worker=False, runner=FakeRunner(),
                      accounts=accounts)


def _analyse(client, app, headers=A1) -> str:
    data = _upload(client, headers=headers)
    job_id = data["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=headers).status_code == 200
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/analysis/jobs/{job_id}", headers=headers).json()["status"] == "complete"
    return job_id


def _register(client, headers=A1, code="apple-auth-code-1234", nonce="nonce-abc-12345") -> dict:
    response = client.post("/v1/account/apple", headers=headers,
                           json={"authorization_code": code, "nonce": nonce})
    assert response.status_code == 204, response.text
    return {"code": code, "nonce": nonce}


def _delete(client, headers=A1, body=None):
    return client.request("DELETE", "/v1/account", headers=headers, json=body) if body is not None \
        else client.delete("/v1/account", headers=headers)


def _grants(app, user_id: str = USER_1) -> list[dict]:
    """The account's still-active Apple grants (the only thing that must be revoked)."""

    return app.state.repo.apple_grants_unrevoked(user_id)


def _put_grant(app, *, user_id: str = USER_1, token: str = REFRESH, subject: str = APPLE_SUB,
               for_deletion: bool = False) -> str:
    """Directly append one grant, as a registration exchange would."""

    import uuid as uuid_module

    grant_id = str(uuid_module.uuid4())
    put = app.state.repo.apple_grant_put_for_deletion if for_deletion else app.state.repo.apple_grant_put
    assert put(user_id, grant_id, subject, "com.swingguru.app", TokenBox(TOKEN_KEY).seal(token)) is True
    return grant_id


# ---------------------------------------------------------------- registration


def test_registration_stores_only_an_encrypted_token_bound_to_the_verified_subject(service) -> None:
    client, app, apple, admin, settings = service()
    admin.link_apple(USER_1)
    _register(client, code="single-use-code", nonce="raw-nonce-value")

    assert apple.exchanges == [{"code": "single-use-code", "nonce": "raw-nonce-value"}]
    row = _grants(app)[0]
    assert row["apple_subject"] == APPLE_SUB
    assert row["client_id"] == "com.swingguru.app"
    assert row["refresh_token_ciphertext"].startswith("v1.")
    assert REFRESH not in json.dumps(row), "the refresh token must never be stored in the clear"
    assert TokenBox(settings.apple_token_key).open(row["refresh_token_ciphertext"]) == REFRESH
    assert admin.deleted == []


def test_registration_is_refused_for_an_account_without_an_apple_identity(service) -> None:
    client, app, apple, admin, _ = service()
    admin.users[USER_1] = {"id": USER_1, "identities": [{"provider": "email"}]}
    response = client.post("/v1/account/apple", headers=A1,
                           json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "identity_mismatch"
    assert _grants(app) == []
    assert apple.exchanges == [], "the one-time code is not burned for a mismatched account"


def test_registration_refuses_a_subject_that_does_not_match_supabase(service) -> None:
    client, app, apple, admin, _ = service(apple=FakeApple(subject="someone-elses-subject"))
    admin.link_apple(USER_1)
    response = client.post("/v1/account/apple", headers=A1,
                           json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "identity_mismatch"
    assert _grants(app) == []


def test_registration_fails_closed_without_apple_configuration(service) -> None:
    client, app, _, _, _ = service(apple_team_id="", apple_key_id="", apple_private_key="",
                                   apple_token_key="")
    response = client.post("/v1/account/apple", headers=A1,
                           json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "apple_unavailable"
    # Guest use is untouched by the missing Apple configuration.
    assert _analyse(client, app)  # a swing still analyses end to end


def test_registration_maps_apple_failures_to_actionable_codes(service) -> None:
    client, _, apple, admin, _ = service()
    admin.link_apple(USER_1)
    apple.exchange_error = AppleError("rejected", "code consumed")
    rejected = client.post("/v1/account/apple", headers=A1,
                           json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "apple_code_rejected"
    apple.exchange_error = AppleError("unavailable", "upstream down")
    down = client.post("/v1/account/apple", headers=A1,
                       json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert down.status_code == 503 and down.json()["error"]["code"] == "apple_unavailable"


def test_a_failed_identity_lookup_never_guesses_and_never_destroys_data(service) -> None:
    client, app, apple, admin, _ = service()
    admin.get_failures.append(503)
    refused = client.post("/v1/account/apple", headers=A1,
                          json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "apple_unavailable"
    assert apple.exchanges == []

    job_id = _analyse(client, app)
    admin.get_failures.append(503)
    blocked = _delete(client)  # no stored credential: the live identity check must succeed first
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "apple_unavailable"
    assert app.state.repo.get(job_id) is not None
    assert app.state.repo.account_deletion_get(USER_1)["apple_revoked_at"] is None


def test_registration_is_refused_while_the_account_is_being_deleted(service) -> None:
    client, app, _, _, _ = service()
    app.state.repo.account_deletion_begin(USER_1)
    response = client.post("/v1/account/apple", headers=A1,
                           json={"authorization_code": "code-12345678", "nonce": "nonce-12345678"})
    # The tombstone denies the request before any Apple call: a live credential can never be
    # enrolled into an account that is already going away.
    assert response.status_code == 403 and response.json()["error"]["code"] == "account_deleted"
    assert _grants(app) == []


# ---------------------------------------------------------------- deletion


def test_deletion_revokes_apple_and_removes_storage_rows_and_auth_user(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)
    job_id = _analyse(client, app)
    objects = app.state.storage.root
    assert any(objects.rglob("*"))

    done = _delete(client)
    assert done.status_code == 200 and done.json() == {"status": "deleted", "apple_revocation": "revoked"}
    assert apple.revoked == [REFRESH], "the stored refresh token is the one Apple revokes"
    assert admin.deleted == [USER_1]
    assert app.state.repo.get(job_id) is None
    assert _grants(app) == []
    assert app.state.repo.account_status(USER_1) == "deleted"
    assert not any(path.is_file() for path in objects.rglob("*"))


def test_a_deleted_accounts_jwt_is_denied_on_every_v1_route(service) -> None:
    client, app, _, _, _ = service()
    assert _delete(client).json()["apple_revocation"] == "not_required"
    for method, path, body in (("get", "/v1/swings", None), ("get", "/v1/billing", None),
                               ("post", "/v1/analysis/jobs",
                                {"file_name": "a.mp4", "content_type": "video/mp4", "size_bytes": 10}),
                               ("post", "/v1/billing/sync", {})):
        response = getattr(client, method)(path, headers=A1, **({"json": body} if body is not None else {}))
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["error"]["code"] == "account_deleted"
    assert client.get("/health").status_code == 200


def test_deletion_is_retryable_and_never_revokes_twice(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)
    _analyse(client, app)
    admin.delete_status = 500  # the last step fails after storage and rows are already gone

    first = _delete(client)
    assert first.status_code == 502 and first.json()["error"]["code"] == "delete_incomplete"
    assert apple.revoked == [REFRESH]
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["status"] == "deleting" and state["apple_revoked_at"] and state["data_cleared_at"]
    assert state["auth_deleted_at"] is None and state["last_error_code"] == "auth_delete_status_500"
    # The blocked JWT cannot reach the API while the deletion is unfinished.
    assert client.get("/v1/swings", headers=A1).status_code == 403

    admin.delete_status = 200
    assert _delete(client).status_code == 200
    assert apple.revoked == [REFRESH], "a successful revocation is never repeated (codes are one-time)"
    assert app.state.repo.account_status(USER_1) == "deleted"
    assert _grants(app) == []


def test_deletion_fails_closed_when_the_revocation_is_unavailable(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)
    job_id = _analyse(client, app)
    apple.revoke_errors.append(AppleError("unavailable", "timeout"))
    response = _delete(client)
    assert response.status_code == 503 and response.json()["error"]["code"] == "apple_unavailable"
    # Nothing was destroyed: the golfer can retry and the Apple grant is still accountable.
    assert app.state.repo.get(job_id) is not None
    assert _grants(app)
    assert app.state.repo.account_deletion_get(USER_1)["apple_revoked_at"] is None
    assert _delete(client).status_code == 200
    assert apple.revoked == [REFRESH, REFRESH]


def test_legacy_apple_account_needs_one_fresh_authorization(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    # Linked before the service stored refresh tokens: Supabase knows the identity, we have no token.
    assert _grants(app) == []
    blocked = _delete(client)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "apple_reauthorization_required"
    assert "sign in with Apple" in blocked.json()["error"]["message"]

    done = _delete(client, body={"authorization_code": "fresh-code-1234", "nonce": "fresh-nonce-1234"})
    assert done.status_code == 200 and done.json()["apple_revocation"] == "revoked"
    assert apple.exchanges == [{"code": "fresh-code-1234", "nonce": "fresh-nonce-1234"}]
    assert apple.revoked == [REFRESH]


def test_legacy_reauthorization_must_match_the_same_apple_subject(service) -> None:
    client, _, _, admin, _ = service(apple=FakeApple(subject="a-different-apple-subject"))
    admin.link_apple(USER_1)
    response = _delete(client, body={"authorization_code": "fresh-code-1234", "nonce": "fresh-nonce-1234"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "identity_mismatch"


def test_legacy_account_can_delete_with_the_explicit_manual_apple_fallback(service) -> None:
    """Apple TN3194: no refresh/access token and no authorization code available -> still delete.

    The golfer is offered the fresh authorization first; only an explicit acknowledgement uses the
    fallback, and the server never claims a revocation it did not perform.
    """

    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    job_id = _analyse(client, app)

    offered = _delete(client)
    assert offered.status_code == 409 and offered.json()["error"]["code"] == "apple_reauthorization_required"
    assert app.state.repo.get(job_id) is not None, "nothing is destroyed before the golfer chooses"

    result = _delete(client, body={"manual_apple_revocation": True})
    assert result.status_code == 200
    assert result.json() == {"status": "deleted", "apple_revocation": "manual_required"}
    assert apple.revoked == [], "the fallback never claims a revocation it did not perform"
    assert app.state.repo.get(job_id) is None
    assert _grants(app) == [], "the fallback retains no token"
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["apple_revocation"] == "manual_required" and state["apple_revoked_at"] is None
    assert admin.deleted == [USER_1]
    assert client.get("/v1/swings", headers=A1).status_code == 403


def test_a_stored_credential_is_never_bypassed_by_the_manual_fallback(service) -> None:
    """A transient upstream failure stays a retry; the fallback flag cannot skip a real revocation."""

    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)
    apple.revoke_errors.append(AppleError("unavailable", "timeout"))
    blocked = _delete(client, body={"manual_apple_revocation": True})
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "apple_unavailable"
    assert _grants(app)
    retried = _delete(client, body={"manual_apple_revocation": True})
    assert retried.status_code == 200 and retried.json()["apple_revocation"] == "revoked"


def test_a_fresh_authorization_beats_the_manual_fallback(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    done = _delete(client, body={"authorization_code": "fresh-code-1234", "nonce": "fresh-nonce-1234",
                                 "manual_apple_revocation": True})
    assert done.status_code == 200 and done.json()["apple_revocation"] == "revoked"
    assert apple.revoked == [REFRESH]


def test_deletion_without_configuration_leaves_data_and_credentials_intact(service) -> None:
    client, app, _, admin, _ = service(apple_team_id="", apple_key_id="", apple_private_key="",
                                       apple_token_key="")
    admin.link_apple(USER_1)
    # A stored credential exists but this deployment has no Apple key: deletion must refuse.
    _put_grant(app)
    job_id = _analyse(client, app)
    response = _delete(client)
    assert response.status_code == 503 and response.json()["error"]["code"] == "apple_unavailable"
    assert app.state.repo.get(job_id) is not None
    assert _grants(app)


def test_deleting_one_account_never_touches_anothers_credential(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    admin.link_apple(USER_2)
    _register(client, headers=A1)
    _register(client, headers=A2)
    second = _grants(app, USER_2)
    assert second
    assert _delete(client, headers=A1).json()["apple_revocation"] == "revoked"
    assert apple.revoked == [REFRESH]
    assert _grants(app, USER_2) == second
    assert admin.deleted == [USER_1]
    assert _analyse(client, app, headers=A2)


def test_account_wipe_removes_only_that_golfers_app_data(tmp_path) -> None:
    """Swings and the allowance/ledger rows go; another golfer's history is untouched."""

    import uuid

    repo = SqliteRepo(tmp_path / "db.sqlite3")
    stamp = "2026-10-01T00:00:00+00:00"
    swings = {USER_1: str(uuid.uuid4()), USER_2: str(uuid.uuid4())}
    for user, swing in swings.items():
        repo.insert({"id": swing, "user_id": user, "status": "complete"})
        repo.db.execute("INSERT INTO billing_usage (swing_id,user_id,kind,state,created_at,updated_at)"
                        " VALUES (?,?,'free','settled',?,?)", (swing, user, stamp, stamp))
        repo.db.execute("INSERT INTO billing_accounts (user_id,free_total,free_used_seed,grandfather_seeded,"
                        " created_at,updated_at) VALUES (?,3,0,1,?,?)", (user, stamp, stamp))

    assert repo.account_wipe(USER_1) == 1
    assert repo.get(swings[USER_1]) is None and repo.get(swings[USER_2]) is not None
    counts = lambda user: (  # noqa: E731 - a local read helper keeps the assertions short
        repo.db.execute("SELECT COUNT(*) FROM billing_usage WHERE user_id=?", (user,)).fetchone()[0],
        repo.db.execute("SELECT COUNT(*) FROM billing_accounts WHERE user_id=?", (user,)).fetchone()[0])
    assert counts(USER_1) == (0, 0)
    assert counts(USER_2) == (1, 1)


def test_deletion_removes_orphan_objects_written_under_the_owner_prefix(service) -> None:
    client, app, _, _, _ = service()
    orphan = app.state.storage.root / USER_1 / "0c6f5d8e-0000-0000-0000-0000000000ff"
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "a2-replay.mp4").write_bytes(b"racing worker output")
    assert _delete(client).status_code == 200
    assert not (app.state.storage.root / USER_1).exists()


def test_a_deleting_account_is_denied_before_its_deletion_finishes(service) -> None:
    client, app, _, _, _ = service()
    app.state.repo.account_deletion_begin(USER_1)
    denied = client.get("/v1/swings", headers=A1)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "account_deleted"
    assert client.get("/v1/swings", headers=A2).status_code == 200


def test_deletion_request_must_supply_both_apple_fields(service) -> None:
    client, _, _, _, _ = service()
    partial = _delete(client, body={"authorization_code": "fresh-code-1234"})
    assert partial.status_code == 422 and partial.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------- worker race


def test_worker_never_claims_or_stores_work_for_a_deleting_account(service) -> None:
    client, app, _, _, _ = service()
    job_id = _upload(client, headers=A1)["job"]["id"]
    assert client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A1).status_code == 200
    app.state.repo.account_deletion_begin(USER_1)
    assert app.state.worker.run_once() is False, "a deleting owner's job is not even claimed"
    row = app.state.repo.get(job_id)
    assert row is not None and row["status"] == "queued", "the claim itself is refused atomically"
    # No analysis result may exist; the original upload is the janitor's (and deletion's) business.
    assert row["result_key"] is None and row["video_key"] is None and row["thumb_key"] is None
    assert not any(path.name.endswith(("analysis.json", "replay.mp4", "thumb.jpg"))
                   for path in app.state.storage.root.rglob("*"))


# ---------------------------------------------------------------- Apple protocol


class _JwksStub:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str):
        return type("Key", (), {"key": self.key.public_key()})()


def _id_token(*, subject=APPLE_SUB, audience="com.swingguru.app", issuer=apple_module.APPLE_ISSUER,
              nonce="hashed-nonce", expires_in=300, key=None) -> str:
    import hashlib

    claims = {"sub": subject, "aud": audience, "iss": issuer,
              "iat": int(time.time()), "exp": int(time.time()) + expires_in,
              "nonce": hashlib.sha256(nonce.encode()).hexdigest() if nonce else None}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key or _PRIVATE_KEY,
                      algorithm="ES256", headers={"kid": "KEY1234567"})


def _apple_client(*, response=None, handler=None, settings=None) -> tuple[AppleClient, list]:
    calls: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request) if handler else (response or httpx.Response(200, json={}))

    settings = settings or account_settings(Path("."))
    client = AppleClient(settings, http=httpx.Client(transport=httpx.MockTransport(record)),
                         jwks_client=_JwksStub(_PRIVATE_KEY))
    return client, calls


def test_exchange_validates_the_id_token_and_returns_the_refresh_token() -> None:
    client, calls = _apple_client(response=httpx.Response(
        200, json={"id_token": _id_token(nonce="raw-nonce-value", subject=APPLE_SUB),
                   "refresh_token": REFRESH, "access_token": "a", "token_type": "Bearer"}))
    credential = client.exchange("the-code", nonce="raw-nonce-value")
    assert credential.subject == APPLE_SUB and credential.refresh_token == REFRESH
    assert credential.audience == "com.swingguru.app" and credential.issuer == apple_module.APPLE_ISSUER
    form = dict(httpx.QueryParams(calls[0].content.decode()))
    assert form["client_id"] == "com.swingguru.app" and form["code"] == "the-code"
    assert form["grant_type"] == "authorization_code"
    headers = jwt.get_unverified_header(form["client_secret"])
    assert headers["alg"] == "ES256" and headers["kid"] == "KEY1234567"
    assert headers["typ"] == "JWT"


@pytest.mark.parametrize("case", ["audience", "issuer", "expired", "nonce", "signature"])
def test_exchange_refuses_an_untrusted_id_token(case: str) -> None:
    kwargs = {"nonce": "raw-nonce-value"}
    token_kwargs: dict = {"nonce": "raw-nonce-value"}
    if case == "audience":
        token_kwargs["audience"] = "com.attacker.app"
    elif case == "issuer":
        token_kwargs["issuer"] = "https://attacker.example"
    elif case == "expired":
        token_kwargs["expires_in"] = -600
    elif case == "nonce":
        token_kwargs["nonce"] = "a-different-nonce"
    else:
        other, _ = _pem()
        token_kwargs["key"] = other
    client, _ = _apple_client(response=httpx.Response(
        200, json={"id_token": _id_token(**token_kwargs), "refresh_token": REFRESH}))
    with pytest.raises(AppleError) as error:
        client.exchange("the-code", **kwargs)
    assert error.value.kind == "rejected"


def test_exchange_refuses_malformed_and_upstream_failures() -> None:
    missing_refresh, _ = _apple_client(response=httpx.Response(
        200, json={"id_token": _id_token(nonce="n"), "refresh_token": ""}))
    with pytest.raises(AppleError) as error:
        missing_refresh.exchange("code", nonce="n")
    assert error.value.kind == "malformed"

    refused, _ = _apple_client(response=httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(AppleError) as error:
        refused.exchange("code", nonce="n")
    assert error.value.kind == "rejected"

    down, _ = _apple_client(response=httpx.Response(503, json={}))
    with pytest.raises(AppleError) as error:
        down.exchange("code", nonce="n")
    assert error.value.kind == "unavailable"

    def broken(_request):
        raise httpx.ConnectError("no route")

    offline, _ = _apple_client(handler=broken)
    with pytest.raises(AppleError) as error:
        offline.exchange("code", nonce="n")
    assert error.value.kind == "unavailable"


def test_revoke_only_accepts_a_confirmed_200() -> None:
    ok, calls = _apple_client(response=httpx.Response(200, json={}))
    ok.revoke(REFRESH)
    form = httpx.QueryParams(calls[0].content.decode())
    assert form["token"] == REFRESH and form["token_type_hint"] == "refresh_token"
    assert form["client_id"] == "com.swingguru.app"

    # Apple documents an already-invalid token as a 200. A 400 is not proof that the grant is dead,
    # so none of these may be treated as a successful revocation.
    for status, body, kind in ((400, {"error": "invalid_grant"}, "rejected"),
                               (400, {"error": "invalid_token"}, "rejected"),
                               (400, {"error": "invalid_client"}, "rejected"),
                               (500, {}, "unavailable")):
        failed, _ = _apple_client(response=httpx.Response(status, json=body))
        with pytest.raises(AppleError) as error:
            failed.revoke(REFRESH)
        assert error.value.kind == kind, (status, body)
    # Arbitrary upstream text can never reach a log through the error code.
    assert apple_module._error_code(httpx.Response(400, json={"error": "attacker text"})) == ""


def test_client_secret_is_short_lived_and_scoped_to_the_bundle() -> None:
    settings = account_settings(Path("."))
    secret = client_secret(settings, now=time.time())
    claims = jwt.decode(secret, _PRIVATE_KEY.public_key(), algorithms=["ES256"],
                        audience=apple_module.APPLE_ISSUER)
    assert claims["iss"] == "TEAM123456" and claims["sub"] == "com.swingguru.app"
    assert 0 < claims["exp"] - claims["iat"] <= 900


def test_nonce_comparison_accepts_only_the_hash_the_app_sent() -> None:
    import hashlib

    raw = "raw-nonce-value"
    assert nonce_matches(hashlib.sha256(raw.encode()).hexdigest(), raw)
    assert not nonce_matches(raw, raw), "the raw form is not what the app sends Apple"
    assert not nonce_matches("something-else", raw)
    assert not nonce_matches(None, raw)
    assert not nonce_matches(hashlib.sha256(raw.encode()).hexdigest(), None)


# ---------------------------------------------------------------- token box


def test_token_box_round_trips_and_refuses_tampering_or_the_wrong_key() -> None:
    box = TokenBox(TOKEN_KEY)
    sealed = box.seal(REFRESH)
    assert sealed.startswith("v1.") and REFRESH not in sealed
    assert box.open(sealed) == REFRESH
    with pytest.raises(TokenBoxError):
        box.open(sealed[:-2] + ("aa" if not sealed.endswith("aa") else "bb"))
    with pytest.raises(TokenBoxError):
        TokenBox(base64.b64encode(bytes(range(1, 33))).decode()).open(sealed)
    with pytest.raises(TokenBoxError):
        TokenBox("not-base64!!")
    with pytest.raises(TokenBoxError):
        TokenBox(base64.b64encode(b"short").decode())


def test_apple_subject_extraction_only_reads_verified_identities() -> None:
    assert apple_subject_of({"identities": [{"provider": "apple", "identity_data": {"sub": APPLE_SUB}}]}) == APPLE_SUB
    assert apple_subject_of({"identities": [{"provider": "apple", "id": APPLE_SUB}]}) == APPLE_SUB
    assert apple_subject_of({"identities": [{"provider": "email", "identity_data": {"sub": "x"}}]}) is None
    assert apple_subject_of({"email": "a@b.c", "user_metadata": {"sub": APPLE_SUB}}) is None
    assert apple_subject_of(None) is None


# ---------------------------------------------------------------- configuration


def test_apple_configuration_is_all_or_nothing_and_optional() -> None:
    validate(Settings())  # nothing set: guests work exactly as before
    validate(account_settings(Path(".")))  # complete: revocation is available
    with pytest.raises(ConfigError):
        validate(Settings(apple_team_id="TEAM123456"))
    with pytest.raises(ConfigError):
        validate(replace(account_settings(Path(".")), apple_token_key="not-base64"))
    with pytest.raises(ConfigError):
        validate(replace(account_settings(Path(".")), apple_private_key="not a pem"))
    with pytest.raises(ConfigError):
        validate(replace(account_settings(Path(".")), env="production", storage_mode="supabase",
                         db_mode="supabase", public_url="https://api.example.com",
                         apple_client_id="com.someone.else"))


# ---------------------------------------------------------------- secret hygiene


def test_no_secret_is_logged_or_returned(service, caplog) -> None:
    import logging

    client, app, _, admin, _ = service()
    admin.link_apple(USER_1)
    with caplog.at_level(logging.DEBUG):
        _register(client, code="super-secret-code", nonce="super-secret-nonce")
        assert _delete(client).status_code == 200
    text = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (REFRESH, "super-secret-code", "super-secret-nonce", _PRIVATE_PEM.splitlines()[1],
                   TOKEN_KEY):
        assert secret not in text, f"{secret[:12]}... leaked into the logs"


# ---------------------------------------------------------------- adapters


def test_postgrest_account_calls_the_right_rpcs() -> None:
    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "account_status":
            return httpx.Response(200, json="deleting")
        if name == "account_wipe":
            return httpx.Response(200, json={"deleted": 2})
        if name in ("apple_grant_put", "apple_grant_put_for_deletion"):
            return httpx.Response(200, json={"stored": True})
        if name == "apple_grants_unrevoked":
            return httpx.Response(200, json=[{"id": "g1", "user_id": "u1", "state": "active"}])
        if name == "apple_grant_counts":
            return httpx.Response(200, json={"active": 1, "revoked": 1})
        if name == "apple_grant_mark_revoked":
            return httpx.Response(200, json={"id": "g1", "user_id": "u1", "state": "revoked"})
        if name == "apple_grants_delete_revoked":
            return httpx.Response(200, json={"deleted": 1, "active": 0})
        if name == "account_deletion_finish":
            return httpx.Response(200, json={"user_id": "u1", "status": "deleted",
                                             "apple_revocation": "revoked"})
        if name == "account_deletions_for_reconcile":
            return httpx.Response(200, json=[{"user_id": "u1", "status": "deleting"}])
        return httpx.Response(200, json={"user_id": "u1", "status": "deleting"})

    repo = PostgrestRepo(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert repo.account_status("u1") == "deleting"
    assert repo.account_deletion_begin("u1")["status"] == "deleting"
    repo.account_deletion_update("u1", apple_revoked_at=True, last_error_code="apple_unavailable")
    repo.account_deletion_finish("u1")
    assert repo.account_wipe("u1") == 2
    assert repo.apple_grant_put("u1", "g1", APPLE_SUB, "com.swingguru.app", "v1.x.y") is True
    assert repo.apple_grants_unrevoked("u1")[0]["id"] == "g1"
    assert repo.apple_grant_counts("u1") == {"active": 1, "revoked": 1}
    assert repo.apple_grant_mark_revoked("g1", "u1")["state"] == "revoked"
    assert repo.apple_grants_delete_revoked("u1") == 1
    assert repo.apple_grant_put_for_deletion("u1", "g2", APPLE_SUB, "com.swingguru.app", "v1.x.y") is True
    assert repo.account_deletions_for_reconcile(stuck_before="2026-10-05T00:00:00+00:00",
                                                cleanup_before="2026-10-05T00:00:00+00:00")[0]["user_id"] == "u1"
    paths = [request.url.path for request in calls]
    assert paths == [f"/rest/v1/rpc/{name}" for name in
                     ("account_status", "account_deletion_begin", "account_deletion_update",
                      "account_deletion_finish", "account_wipe", "apple_grant_put",
                      "apple_grants_unrevoked", "apple_grant_counts", "apple_grant_mark_revoked",
                      "apple_grants_delete_revoked", "apple_grant_put_for_deletion",
                      "account_deletions_for_reconcile")]
    payloads = [json.loads(request.content) for request in calls]
    assert payloads[0] == {"p_user_id": "u1"}
    assert payloads[2] == {"p_user_id": "u1", "p_apple_revoked": True, "p_storage_cleared": False,
                           "p_data_cleared": False, "p_auth_deleted": False,
                           "p_apple_revocation": None, "p_error_code": "apple_unavailable",
                           "p_cleanup_until": None, "p_cleanup_done": False}
    assert payloads[5] == {"p_user_id": "u1", "p_subject": APPLE_SUB, "p_client_id": "com.swingguru.app",
                           "p_grant_id": "g1", "p_token": "v1.x.y"}
    # An unknown column is refused locally, before any request is sent.
    with pytest.raises(RepoError):
        repo.account_deletion_update("u1", role="service_role")


def test_postgrest_refuses_a_malformed_grant_or_finish_answer() -> None:
    """A malformed answer must never read as "no live grant" or as a completed deletion."""

    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    malformed_list = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=["not", "a", "row"]))))
    with pytest.raises(RepoError):
        malformed_list.apple_grants_unrevoked("u1")

    malformed_counts = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"active": "one"}))))
    with pytest.raises(RepoError):
        malformed_counts.apple_grant_counts("u1")

    unconfirmed_finish = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"user_id": "u1",
                                                                        "status": "deleting"}))))
    with pytest.raises(RepoError):
        unconfirmed_finish.account_deletion_finish("u1")

    refused_finish = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"error": "prerequisites_missing"}))))
    with pytest.raises(RepoError):
        refused_finish.account_deletion_finish("u1")

    live_left = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"deleted": 0, "active": 1}))))
    with pytest.raises(RepoError):
        live_left.apple_grants_delete_revoked("u1")


def test_postgrest_account_status_refuses_an_unexpected_shape() -> None:
    settings = Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")
    repo = PostgrestRepo(settings, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "active"}))))
    with pytest.raises(RepoError):
        repo.account_status("u1")


def test_storage_prefix_validation_rejects_traversal() -> None:
    for bad in ("../x/", "user-aaaaaaaa/../", "/etc/", "short/"):
        with pytest.raises(StorageError):
            check_prefix(bad)
    assert check_prefix("0c6f5d8e-1111-2222-3333-444455556666/")


def test_migration_is_additive_and_locked_down() -> None:
    root = Path(__file__).resolve().parents[1]
    migration = root / "supabase" / "migrations" / "20261004000000_release_accounts.sql"
    assert migration.exists()
    sql = migration.read_text(encoding="utf-8")
    assert "create table if not exists public.apple_grants" in sql
    assert "create table if not exists public.account_deletions" in sql
    assert "apple_revocation   text check (apple_revocation in ('revoked', 'not_required', 'manual_required'))" in sql
    assert "alter table public.apple_grants enable row level security" in sql
    assert "alter table public.account_deletions enable row level security" in sql
    # The tombstone has no FK to auth.users: it must outlive the auth row to deny an unexpired JWT.
    assert "user_id            uuid primary key," in sql
    # Neither table cascades with the auth user: a credential must outlive it (a registration that
    # raced the deletion could otherwise lose a live grant), and the tombstone must stay to deny the
    # account's still-valid JWT.
    assert "references auth.users (id) on delete cascade" not in sql
    assert "no foreign key to `auth.users`" in sql.lower()
    # Per-grant tracking: append-only rows with a per-row compare-and-set revocation, and completion
    # refused while any grant is still active.
    assert "state                    text not null default 'active' check (state in ('active', 'revoked'))" in sql
    assert "where g.id = p_grant_id and g.user_id = p_user_id and g.state = 'active'" in sql
    assert "if v_active > 0 then" in sql and "'unrevoked_grant'" in sql
    assert "delete from public.apple_grants g where g.user_id = p_user_id and g.state = 'revoked'" in sql
    assert "where g.user_id = d.user_id and g.state <> 'revoked'" in sql
    # One lock order: lifecycle advisory lock, then swing rows, then the billing account row.
    wipe = sql[sql.index("create or replace function public.account_wipe"):]
    wipe = wipe[:wipe.index("$$;")]
    assert wipe.index("pg_advisory_xact_lock") < wipe.index("from public.swings s where s.user_id = p_user_id order by s.id for update")
    assert wipe.index("from public.swings s where s.user_id = p_user_id order by s.id for update") \
        < wipe.index("insert into public.billing_accounts")
    assert sql.count("set search_path = ''") >= 9
    assert sql.count("security definer") >= 9
    assert "pg_advisory_xact_lock" in sql
    assert "if exists (select 1 from public.account_deletions" in sql
    assert "to_regclass('public.billing_usage') is not null" in sql
    for name in ("account_status", "account_deletion_begin", "account_deletion_update",
                 "account_deletion_finish", "account_wipe", "apple_grant_put",
                 "apple_grant_put_for_deletion", "apple_grants_unrevoked", "apple_grant_counts",
                 "apple_grant_mark_revoked", "apple_grants_delete_revoked"):
        assert f"grant execute on function public.{name}" in sql
        assert f"from public, anon, authenticated" in sql
    assert "to service_role;" in sql
    # The only drop is the documented cleanup of a never-released earlier revision of this file.
    assert sql.lower().count("drop table") == 1
    assert "drop table if exists public.apple_credentials;" in sql
    assert "alter table public.swings" not in sql


# ---------------------------------------------------------------- storage removal


def _storage_settings() -> Settings:
    return Settings(supabase_url="https://proj.supabase.co", supabase_service_key="service-key")


def test_supabase_delete_prefix_lists_recursively_then_deletes_exact_names() -> None:
    """The delete API takes exact object names, so removal must list first - page by page."""

    from swingguru.service.storage import SupabaseStorage

    calls: list[httpx.Request] = []
    owner = f"{USER_1}/"
    page_one = [{"name": f"swing-{index:03d}", "id": f"id-{index}"} for index in range(100)]
    page_two = [{"name": "swing-100", "id": "id-100"}, {"name": "nested", "id": None}]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/object/list/swings"):
            body = json.loads(request.content)
            if body["prefix"] == owner and body["offset"] == 0:
                return httpx.Response(200, json=page_one)
            if body["prefix"] == owner:
                return httpx.Response(200, json=page_two)
            if body["prefix"] == f"{owner}nested/":
                return httpx.Response(200, json=[{"name": "deep.mp4", "id": "id-deep"}])
            return httpx.Response(200, json=[])
        if request.method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    storage = SupabaseStorage(_storage_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    storage.delete_prefix(owner)

    listed = [json.loads(call.content) for call in calls
              if call.url.path.endswith("/object/list/swings")]
    assert [page["offset"] for page in listed] == [0, 100, 0], "the owner prefix is paged"
    assert listed[2]["prefix"] == f"{owner}nested/", "folders are walked explicitly"
    deletes = [json.loads(call.content)["prefixes"] for call in calls if call.method == "DELETE"]
    flat = [name for batch in deletes for name in batch]
    assert len(deletes) == 2 and all(len(batch) <= storage.DELETE_BATCH for batch in deletes)
    assert flat[0] == f"{owner}swing-000" and flat[-1] == f"{owner}nested/deep.mp4"
    assert owner not in flat, "a folder path is never sent as if it were an object name"


def test_supabase_delete_prefix_propagates_failures_and_retries_only_transiently() -> None:
    from swingguru.service.storage import StorageError, SupabaseStorage

    attempts: list[int] = []

    def transient(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/object/list/swings"):
            body = json.loads(request.content)
            return httpx.Response(200, json=[{"name": "a.mp4", "id": "id"}] if body["prefix"].count("/") == 1
                                  else [])
        attempts.append(1)
        return httpx.Response(500, json={})

    storage = SupabaseStorage(_storage_settings(), httpx.Client(transport=httpx.MockTransport(transient)))
    with pytest.raises(StorageError):
        storage.delete_prefix(f"{USER_1}/")
    assert len(attempts) == storage.DELETE_ATTEMPTS, "a 5xx is retried within the bound"

    attempts.clear()

    def refused(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/object/list/swings"):
            return httpx.Response(200, json=[{"name": "a.mp4", "id": "id"}])
        attempts.append(1)
        return httpx.Response(403, json={})

    storage = SupabaseStorage(_storage_settings(), httpx.Client(transport=httpx.MockTransport(refused)))
    with pytest.raises(StorageError):
        storage.delete_prefix(f"{USER_1}/")
    assert len(attempts) == 1, "a hard refusal is not retried"


def test_local_delete_prefix_removes_everything_or_raises(tmp_path) -> None:
    from swingguru.service.storage import LocalStorage, StorageError

    settings = Settings(data_dir=tmp_path / "svc", public_url="http://testserver")
    storage = LocalStorage(settings)
    target = storage.root / USER_1 / "swing-1"
    target.mkdir(parents=True)
    (target / "a1-replay.mp4").write_bytes(b"video")
    storage.delete_prefix(f"{USER_1}/")
    assert not (storage.root / USER_1).exists()
    storage.delete_prefix(f"{USER_1}/")  # already gone is not an error
    with pytest.raises(StorageError):
        storage.delete_prefix(f"../{USER_1}/")


def test_janitor_sweeps_a_late_upload_after_the_deletion_settles(service, monkeypatch) -> None:
    """A pre-issued upload URL can still write after the sweep; the janitor keeps re-clearing."""

    from swingguru.service import accounts as accounts_module

    monkeypatch.setattr(accounts_module, "CLEANUP_MARGIN_S", 0)
    client, app, _, _, _ = service(upload_url_ttl_s=0)
    assert client.delete("/v1/account", headers=A1).status_code == 200
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["cleanup_until"] and state["cleanup_done_at"] is None
    # Simulate a racing worker (or a still-valid upload URL) writing under the owner prefix.
    late = app.state.storage.root / USER_1 / "orphan-swing"
    late.mkdir(parents=True, exist_ok=True)
    (late / "a2-replay.mp4").write_bytes(b"late")
    app.state.worker.janitor()
    assert not (app.state.storage.root / USER_1).exists()
    assert app.state.repo.account_deletion_get(USER_1)["cleanup_done_at"] is not None


# ---------------------------------------------------------------- durable checkpoints


def test_a_failed_checkpoint_write_fails_closed(service, monkeypatch) -> None:
    client, app, _, _, _ = service()
    original = app.state.repo.account_deletion_update

    def broken(*_args, **_kwargs):
        raise RepoError("checkpoint store down")

    monkeypatch.setattr(app.state.repo, "account_deletion_update", broken)
    try:
        response = _delete(client)
    finally:
        monkeypatch.setattr(app.state.repo, "account_deletion_update", original)
    assert response.status_code == 502 and response.json()["error"]["code"] == "delete_incomplete"


def test_a_fresh_authorization_is_persisted_before_apple_is_called(service) -> None:
    """A timeout must not consume the one-time code and lose the credential the retry needs."""

    client, app, apple, admin, settings = service()
    admin.link_apple(USER_1)
    apple.revoke_errors.append(AppleError("unavailable", "timeout"))
    first = _delete(client, body={"authorization_code": "fresh-code-1234", "nonce": "fresh-nonce-1234"})
    assert first.status_code == 503 and first.json()["error"]["code"] == "apple_unavailable"
    stored = _grants(app)
    assert stored, "the code's grant is kept even though the revoke timed out"
    stored = stored[0]
    assert TokenBox(settings.apple_token_key).open(stored["refresh_token_ciphertext"]) == REFRESH

    # The retry needs no new code: it revokes the credential the first attempt persisted.
    retried = _delete(client)
    assert retried.status_code == 200 and retried.json()["apple_revocation"] == "revoked"
    assert apple.exchanges == [{"code": "fresh-code-1234", "nonce": "fresh-nonce-1234"}]


def test_a_finished_deletion_retry_returns_the_stored_outcome(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    first = _delete(client, body={"manual_apple_revocation": True})
    assert first.status_code == 200 and first.json()["apple_revocation"] == "manual_required"
    # The JWT is still cryptographically valid, but the tombstone already says 'deleted': the retry
    # must get the stored outcome back, not a 403 that loses it.
    again = _delete(client)
    assert again.status_code == 200 and again.json() == {"status": "deleted",
                                                        "apple_revocation": "manual_required"}
    assert apple.revoked == []


def test_a_confirmed_revocation_can_never_be_overwritten(service) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)
    assert _delete(client).json()["apple_revocation"] == "revoked"
    # A stale concurrent attempt trying to record a guess must not downgrade the confirmed result.
    app.state.repo.account_deletion_update(USER_1, apple_revocation="manual_required")
    assert app.state.repo.account_deletion_get(USER_1)["apple_revocation"] == "revoked"


def test_a_credential_delete_failure_keeps_the_deletion_unfinished(service, monkeypatch) -> None:
    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)

    def broken(*_args, **_kwargs):
        raise RepoError("credential store down")

    monkeypatch.setattr(app.state.repo, "apple_grants_delete_revoked", broken)
    response = _delete(client)
    assert response.status_code == 502 and response.json()["error"]["code"] == "delete_incomplete"
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["status"] == "deleting"
    # The revocation was confirmed (so the row is marked), but its removal failed: the evidence of
    # the grant is still in the table and the deletion is unfinished.
    assert app.state.repo.apple_grant_counts(USER_1)["revoked"] == 1
    assert apple.revoked == [REFRESH], "the revocation itself did happen and is not repeated"


# ---------------------------------------------------------------- atomic access guards


def test_creating_and_starting_a_job_are_refused_once_a_deletion_begins(service) -> None:
    client, app, _, admin, _ = service()
    admin.link_apple(USER_1)
    data = _upload(client, headers=A1)
    job_id = data["job"]["id"]

    app.state.repo.account_deletion_begin(USER_1)
    created = client.post("/v1/analysis/jobs", headers=A1, json={
        "file_name": "b.mp4", "content_type": "video/mp4", "size_bytes": 10})
    assert created.status_code == 403 and created.json()["error"]["code"] == "account_deleted"
    started = client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A1)
    assert started.status_code == 403 and started.json()["error"]["code"] == "account_deleted"
    assert app.state.repo.get(job_id)["status"] == "awaiting_upload"


def test_an_insert_racing_a_tombstone_is_either_refused_or_unsettleable(service) -> None:
    """The guard is atomic: a row created in the same instant as the tombstone can never settle."""

    import threading
    import uuid as uuid_module

    _, app, _, _, _ = service()
    repo = app.state.repo
    swing_id = str(uuid_module.uuid4())
    barrier = threading.Barrier(2)
    outcome: list[str] = []

    def create() -> None:
        barrier.wait(timeout=10)
        try:
            repo.insert({"id": swing_id, "user_id": USER_1, "status": "processing", "attempts": 1})
            outcome.append("created")
        except AccountBlocked:
            outcome.append("blocked")

    def begin() -> None:
        barrier.wait(timeout=10)
        repo.account_deletion_begin(USER_1)

    threads = [threading.Thread(target=create), threading.Thread(target=begin)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert outcome in (["created"], ["blocked"]), outcome
    if outcome == ["created"]:
        assert repo.finish_job(swing_id, status="complete", attempts=1) is None, \
            "a settlement that races the tombstone must not land"


def test_a_deleting_owner_never_gets_an_admission_or_a_claim(service) -> None:
    """The billing ledger and the claim query carry the same predicate as the swing writes."""

    _, app, _, _, _ = service()
    repo = app.state.repo
    swing_id = str(__import__("uuid").uuid4())
    repo.insert({"id": swing_id, "user_id": USER_1, "status": "awaiting_upload"})
    assert repo.billing_admit(user_id=USER_1, swing_id=swing_id, kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "admitted"
    repo.account_deletion_begin(USER_1)
    assert repo.billing_admit(user_id=USER_1, swing_id=swing_id, kind="free", product_id=None,
                              size_bytes=10, free_total=3)["status"] == "blocked"
    assert repo.claim_next("2999-01-01T00:00:00+00:00", 2) is None


# ---------------------------------------------------------------- reconciliation


def _make_stuck(app, *, hours: float = 2.0) -> None:
    """Backdate the tombstone so the janitor treats it as abandoned by any client."""

    old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    app.state.repo.db.execute("UPDATE account_deletions SET updated_at=? WHERE user_id=?", (old, USER_1))


def test_the_janitor_finishes_a_deletion_no_client_can_finish(service) -> None:
    client, app, apple, admin, _ = service(deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    _register(client)
    job_id = _analyse(client, app)
    app.state.repo.account_deletion_begin(USER_1)
    _make_stuck(app)

    app.state.worker.janitor()

    assert apple.revoked == [REFRESH], "it revokes the stored credential for real"
    assert app.state.repo.account_status(USER_1) == "deleted"
    assert app.state.repo.get(job_id) is None
    assert _grants(app) == []
    assert admin.deleted == [USER_1]


def test_the_janitor_leaves_a_stuck_deletion_pending_while_apple_is_down(service) -> None:
    client, app, apple, admin, _ = service(deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    _register(client)
    job_id = _analyse(client, app)
    app.state.repo.account_deletion_begin(USER_1)
    _make_stuck(app)
    apple.revoke_errors.append(AppleError("unavailable", "outage"))

    app.state.worker.janitor()

    state = app.state.repo.account_deletion_get(USER_1)
    assert state["status"] == "deleting" and state["apple_revocation"] is None
    assert state["last_error_code"] == "apple_revoke_unavailable"
    assert app.state.repo.get(job_id) is not None, "a real credential is never bypassed"
    assert _grants(app)


def test_the_janitor_records_the_manual_state_for_a_legacy_account(service) -> None:
    """No credential, no client: TN3194's manual path is recorded - never a claimed revocation."""

    client, app, apple, admin, _ = service(deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    job_id = _analyse(client, app)
    app.state.repo.account_deletion_begin(USER_1)
    _make_stuck(app)

    app.state.worker.janitor()

    state = app.state.repo.account_deletion_get(USER_1)
    assert state["status"] == "deleted" and state["apple_revocation"] == "manual_required"
    assert state["apple_revoked_at"] is None
    assert apple.revoked == []
    assert app.state.repo.get(job_id) is None


def test_the_manual_fallback_works_without_server_apple_configuration(service) -> None:
    """TN3194 must not be blocked by absent config; the normal revoke path still is."""

    client, app, _, admin, _ = service(apple_team_id="", apple_key_id="", apple_private_key="",
                                       apple_token_key="", deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    job_id = _analyse(client, app)

    # Without configuration the normal revoke path is a release blocker.
    blocked = _delete(client)
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "apple_unavailable"
    assert app.state.repo.get(job_id) is not None
    # The explicit manual fallback still fulfils the deletion, honestly labelled.
    done = _delete(client, body={"manual_apple_revocation": True})
    assert done.status_code == 200 and done.json()["apple_revocation"] == "manual_required"


# ---------------------------------------------------------------- registration vs deletion race


class BlockingApple(FakeApple):
    """An Apple client whose exchange can be held open while the test starts a deletion."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def exchange(self, code: str, *, nonce: str | None = None, now: float | None = None):
        self.entered.set()
        assert self.release.wait(timeout=10), "the test must release the exchange"
        return super().exchange(code, nonce=nonce, now=now)


def _race_registration_and_deletion(app, apple: BlockingApple) -> list[str]:
    """Run registration while a deletion begins mid-exchange; return the registration outcome."""

    outcome: list[str] = []

    def register() -> None:
        try:
            app.state.accounts.register_apple(USER_1, code="race-code-1234", nonce="race-nonce-1234")
            outcome.append("stored")
        except AccountError as error:
            outcome.append(error.code)

    thread = threading.Thread(target=register)
    thread.start()
    assert apple.entered.wait(timeout=10), "the exchange should be in flight"
    app.state.repo.account_deletion_begin(USER_1)
    apple.release.set()
    thread.join(timeout=15)
    return outcome


def test_a_grant_minted_while_the_deletion_begins_is_revoked(service) -> None:
    """Refusing the row is not enough: the code was spent, so the minted token must be dealt with."""

    apple = BlockingApple()
    _, app, _, admin, _ = service(apple=apple)
    admin.link_apple(USER_1)

    assert _race_registration_and_deletion(app, apple) == ["account_deleted"]
    assert apple.revoked == [REFRESH], "the freshly minted grant is revoked, not dropped"
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["apple_revocation"] == "revoked" and state["apple_revoked_at"]
    assert app.state.repo.apple_grant_counts(USER_1) == {"active": 0, "revoked": 1}


def test_a_minted_grant_that_cannot_be_revoked_yet_is_kept_for_the_janitor(service) -> None:
    """An unreachable Apple must not lose the token: it stays stored, and reconciliation revokes it."""

    apple = BlockingApple()
    client, app, _, admin, _ = service(apple=apple, deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    job_id = _analyse(client, app)
    apple.revoke_errors.append(AppleError("unavailable", "outage"))

    assert _race_registration_and_deletion(app, apple) == ["account_deleted"]
    stored = _grants(app)
    assert stored, "the minted token is retained, never dropped on the floor"
    state = app.state.repo.account_deletion_get(USER_1)
    assert state["apple_revocation"] is None and state["last_error_code"] == "apple_revoke_unavailable"

    _make_stuck(app)
    app.state.worker.janitor()

    assert apple.revoked == [REFRESH, REFRESH]
    assert app.state.repo.account_deletion_get(USER_1)["status"] == "deleted"
    assert _grants(app) == []
    assert app.state.repo.get(job_id) is None


def test_a_live_grant_is_never_dropped_without_a_confirmed_revocation(service) -> None:
    """A grant row is the evidence of a live token; only a confirmed revocation lets it leave."""

    _, app, _, _, _ = service()
    repo = app.state.repo
    repo.account_deletion_begin(USER_1)
    repo.account_deletion_update(USER_1, apple_revocation="manual_required")
    assert repo.apple_grant_put_for_deletion(USER_1, "g-live", APPLE_SUB, "com.swingguru.app",
                                             TokenBox(TOKEN_KEY).seal(REFRESH)) is True

    # Only revoked rows are ever removed: the live one survives the bulk delete untouched.
    assert repo.apple_grants_delete_revoked(USER_1) == 0
    assert [grant["id"] for grant in repo.apple_grants_unrevoked(USER_1)] == ["g-live"]

    assert repo.apple_grant_mark_revoked("g-live", USER_1)["state"] == "revoked"
    assert repo.apple_grants_delete_revoked(USER_1) == 1
    assert repo.apple_grants_unrevoked(USER_1) == []


def test_registration_still_refuses_a_tombstone_but_quarantines_the_token(service) -> None:
    """A non-racing registration that finds a tombstone keeps the grant instead of leaking it."""

    _, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    app.state.repo.account_deletion_begin(USER_1)
    with pytest.raises(AccountError) as error:
        app.state.accounts.register_apple(USER_1, code="late-code-1234", nonce="late-nonce-1234")
    assert error.value.code == "account_deleted"
    assert apple.revoked == [REFRESH]
    assert app.state.repo.account_deletion_get(USER_1)["apple_revocation"] == "revoked"
    # The token is retained (revoked, so its removal is allowed) until the deletion's completion step.
    assert app.state.repo.apple_grant_counts(USER_1) == {"active": 0, "revoked": 1}


def test_a_grant_appended_after_the_account_was_marked_revoked_is_still_revoked(service) -> None:
    """The security hole: an account-level 'revoked' must never vouch for a newer token."""

    client, app, apple, admin, _ = service()
    admin.link_apple(USER_1)
    _register(client)                                     # grant A
    app.state.repo.account_deletion_begin(USER_1)
    first = _grants(app)[0]
    assert app.state.repo.apple_grant_mark_revoked(first["id"], USER_1)["state"] == "revoked"
    app.state.repo.account_deletion_update(USER_1, apple_revocation="revoked", apple_revoked_at=True)

    # A second exchange mints a newer grant. It must not inherit the account's summary.
    _put_grant(app, token="newer-refresh-token", for_deletion=True)
    app.state.repo.account_deletion_update(USER_1, storage_cleared_at=True, data_cleared_at=True)
    with pytest.raises(RepoError):
        # Every other prerequisite is satisfied; only the live newer grant refuses completion.
        app.state.repo.account_deletion_finish(USER_1)

    done = _delete(client)
    assert done.status_code == 200 and done.json()["apple_revocation"] == "revoked"
    # Grant A was already revoked (recorded above); only the still-active newer grant is revoked now,
    # and it is revoked for real rather than being waved through by the account-level summary.
    assert apple.revoked == ["newer-refresh-token"]
    assert _grants(app) == [], "no live grant survives the completion"


def test_two_overlapping_exchanges_append_two_grants_and_lose_neither(service) -> None:
    """Append-only rows: the second exchange can never overwrite the first token."""

    _, app, _, _, _ = service()
    first = _put_grant(app, token="token-one")
    second = _put_grant(app, token="token-two")
    assert first != second
    stored = {grant["id"]: TokenBox(TOKEN_KEY).open(grant["refresh_token_ciphertext"])
              for grant in app.state.repo.apple_grants_unrevoked(USER_1)}
    assert stored == {first: "token-one", second: "token-two"}

    # Revoking one leaves the other live, and the summary can never hide that.
    assert app.state.repo.apple_grant_mark_revoked(first, USER_1)["state"] == "revoked"
    assert [grant["id"] for grant in app.state.repo.apple_grants_unrevoked(USER_1)] == [second]
    assert app.state.repo.apple_grant_counts(USER_1) == {"active": 1, "revoked": 1}


def test_the_janitor_revokes_a_grant_appended_after_the_account_was_deleted(service) -> None:
    """Reconciliation must include a pending grant even when the tombstone already says deleted."""

    client, app, apple, admin, _ = service(deletion_reconcile_hours=1.0)
    admin.link_apple(USER_1)
    _register(client)
    assert _delete(client).status_code == 200
    assert app.state.repo.account_status(USER_1) == "deleted"

    # An exchange that raced the deletion appends a grant to the already-deleted account.
    _put_grant(app, token="late-refresh-token", for_deletion=True)
    assert app.state.repo.apple_grants_unrevoked(USER_1)

    app.state.worker.janitor()

    assert "late-refresh-token" in apple.revoked
    assert _grants(app) == []


def test_the_cleanup_horizon_uses_the_storage_backends_own_upload_lifetime(service, monkeypatch) -> None:
    """Our configured TTL is not what Supabase grants: the horizon must use the backend's real one."""

    from swingguru.service.storage import SUPABASE_SIGNED_UPLOAD_TTL_S, SupabaseStorage

    assert SUPABASE_SIGNED_UPLOAD_TTL_S == 2 * 3600
    supabase_storage = SupabaseStorage(Settings(supabase_url="https://proj.supabase.co",
                                                supabase_service_key="k"))
    assert supabase_storage.upload_url_lifetime_s == SUPABASE_SIGNED_UPLOAD_TTL_S

    client, app, _, _, settings = service(upload_url_ttl_s=60)   # a very short local preference
    storage = app.state.storage
    monkeypatch.setattr(storage, "upload_url_lifetime_s", SUPABASE_SIGNED_UPLOAD_TTL_S)
    assert client.delete("/v1/account", headers=A1).status_code == 200
    state = app.state.repo.account_deletion_get(USER_1)
    from datetime import datetime as dt

    cleared = dt.fromisoformat(state["storage_cleared_at"])
    cleanup = dt.fromisoformat(state["cleanup_until"])
    assert (cleanup - cleared).total_seconds() >= SUPABASE_SIGNED_UPLOAD_TTL_S
    assert (cleanup - cleared).total_seconds() > settings.upload_url_ttl_s
