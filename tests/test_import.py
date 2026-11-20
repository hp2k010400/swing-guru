"""Staging test-data import: off unless SG_IMPORT_KEY is set, never in production, scoped to one swing."""

from __future__ import annotations

import base64
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swingguru.service.api import create_app
from swingguru.service.config import ConfigError, Settings, validate
from swingguru.service.repo import SqliteRepo
from swingguru.service.storage import LocalStorage
from test_service import A, FakeRunner

KEY = "k" * 40
ANALYSIS = Path(__file__).resolve().parents[1] / "var" / "qa" / "validation_prod" / "candidate" / "01" / "analysis.json"


def _client(tmp_path, **overrides):
    settings = replace(Settings(), data_dir=tmp_path / "svc", public_url="http://testserver", **overrides)
    repo, storage = SqliteRepo(tmp_path / "svc" / "db.sqlite3"), LocalStorage(settings)
    return TestClient(create_app(settings, repo=repo, storage=storage, start_worker=False, runner=FakeRunner()))


def _body(swing_id: str, user: str = "user-aaaaaaaa") -> dict:
    analysis = ANALYSIS.read_bytes() if ANALYSIS.is_file() else b'{"phases": []}'
    return {"user_id": user,
            "swing": {"id": swing_id, "club": "7 Iron", "created_at": "2026-10-20T09:00:00+00:00",
                      "summary": {"counts": {"good": 5, "attention": 1, "problem": 0, "total": 6}},
                      "user_id": "someone-else", "status": "failed"},
            "files": {"analysis": base64.b64encode(analysis).decode(), "video": base64.b64encode(b"\0" * 64).decode()}}


def test_route_absent_without_key(tmp_path):
    client = _client(tmp_path)
    assert client.post("/v1/admin/import", json=_body(str(uuid.uuid4()))).status_code == 404


def test_key_required_and_compared(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    body = _body(str(uuid.uuid4()))
    assert client.post("/v1/admin/import", json=body).status_code == 401
    assert client.post("/v1/admin/import", json=body, headers={"X-SG-Import-Key": "x" * 40}).status_code == 401


def test_import_is_owned_by_the_given_test_user_and_idempotent(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    swing_id = str(uuid.uuid4())
    first = client.post("/v1/admin/import", json=_body(swing_id), headers={"X-SG-Import-Key": KEY})
    assert first.status_code == 201 and first.json()["status"] == "imported"
    again = client.post("/v1/admin/import", json=_body(swing_id), headers={"X-SG-Import-Key": KEY})
    assert again.json()["status"] == "exists"
    listed = client.get("/v1/swings", headers=A).json()["swings"]
    assert [s["id"] for s in listed] == [swing_id]          # owner from user_id, status forced complete
    assert listed[0]["club"] == "7 Iron" and listed[0]["created_at"].startswith("2026-10-20")
    if ANALYSIS.is_file():
        detail = client.get(f"/v1/swings/{swing_id}", headers=A)
        assert detail.status_code == 200 and detail.json()["checkpoints"]


def test_bad_user_or_swing_id_refused(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    headers = {"X-SG-Import-Key": KEY}
    assert client.post("/v1/admin/import", json=_body(str(uuid.uuid4()), user="../etc"), headers=headers).status_code == 422
    assert client.post("/v1/admin/import", json=_body("not-a-uuid"), headers=headers).status_code == 422


def test_config_refuses_import_key_outside_staging():
    with pytest.raises(ConfigError):
        validate(Settings(import_key=KEY, env="production", auth_mode="supabase", storage_mode="supabase",
                          db_mode="supabase", supabase_url="https://x.supabase.co", supabase_service_key="s",
                          public_url="https://x"))
    with pytest.raises(ConfigError):
        validate(Settings(import_key=KEY, db_mode="supabase", supabase_url="https://x.supabase.co",
                          supabase_service_key="s"))
    with pytest.raises(ConfigError):
        validate(Settings(import_key="short"))
    validate(Settings(import_key=KEY))
