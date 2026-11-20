"""Staging-only settings: score model selection and the shared access key. Production unchanged."""

from __future__ import annotations

from dataclasses import replace

import pytest

from swingguru.service.config import ConfigError, Settings, validate

from test_service import A, FakeRunner, _upload, service  # noqa: F401  (pytest fixture)

KEY = "k" * 32


def test_production_cannot_override_the_score_model() -> None:
    with pytest.raises(ConfigError, match="SG_SCORE_VERSION"):
        validate(Settings(env="production", auth_mode="supabase", storage_mode="supabase", db_mode="supabase",
                          supabase_url="https://x.supabase.co", supabase_service_key="s", public_url="https://x",
                          score_version="swing_score_v2"))     # production scores with VERSION only
    with pytest.raises(ConfigError, match="SG_SCORE_VERSION"):
        validate(Settings(score_version="swing_score_v9"))
    with pytest.raises(ConfigError, match="SG_ACCESS_KEY"):
        validate(Settings(access_key="short"))
    validate(Settings(score_version="swing_score_v3", access_key=KEY))      # a development / staging server


def test_staging_scores_new_analyses_with_the_selected_model(service) -> None:
    client, app, _ = service(score_version="swing_score_v2")
    job_id = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/swings/{job_id}", headers=A).json()["score"]["version"] == "swing_score_v2"


def test_default_server_scores_with_the_production_model_v3(service) -> None:
    client, app, _ = service()
    job_id = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert app.state.worker.run_once() is True
    assert client.get(f"/v1/swings/{job_id}", headers=A).json()["score"]["version"] == "swing_score_v3"


def test_access_key_guards_signed_in_routes_only(service) -> None:
    client, app, _ = service(access_key=KEY)
    assert client.get("/v1/swings", headers=A).status_code == 401                          # no key
    assert client.get("/v1/swings", headers={**A, "X-SG-Access-Key": "wrong" * 6}).status_code == 401
    assert client.get("/v1/swings", headers={**A, "X-SG-Access-Key": KEY}).status_code == 200
    assert client.delete("/v1/account", headers=A).status_code == 401
    assert client.get("/health").status_code == 200 and client.get("/v1/viewer/index.html").status_code == 200
