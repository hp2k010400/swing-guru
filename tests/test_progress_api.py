"""GET /v1/progress and /v1/compare over real stored analyses (seeded through the staging import route)."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import pytest

from test_import import KEY, _client
from test_service import A, B

ROOT = Path(__file__).resolve().parents[1] / "var" / "qa" / "validation_prod" / "candidate"
pytestmark = pytest.mark.skipif(not ROOT.is_dir(), reason="validation set not present")


def _seed(client, n=4, user="user-aaaaaaaa", club="7 Iron"):
    ids = []
    for i, path in enumerate(sorted(ROOT.glob("*/analysis.json"))[:n]):
        swing_id = str(uuid.uuid4())
        body = {"user_id": user,
                "swing": {"id": swing_id, "club": club, "created_at": f"2026-10-{10 + i:02d}T09:00:00+00:00"},
                "files": {"analysis": base64.b64encode(path.read_bytes()).decode(),
                          "video": base64.b64encode(b"\0" * 32).decode()}}
        assert client.post("/v1/admin/import", json=body, headers={"X-SG-Import-Key": KEY}).status_code == 201
        ids.append(swing_id)
    return ids


def test_progress_backfills_records_and_ranks(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    ids = _seed(client)
    out = client.get("/v1/progress", headers=A).json()
    assert [s["id"] for s in out["swings"]] == ids and out["pending"] == 0
    assert out["rank"]["rank"]["name"].startswith("Bronze") and out["rank"]["eligible_swings"] == 4
    assert out["personal_bests"]["score"]["score"] == max(s["score"] for s in out["swings"])
    assert "Not a handicap" in out["rank"]["disclaimer"]
    # The record was stored, so a second call does no backfill and returns the same.
    row = client.app.state.repo.get(ids[0])
    assert "record" in row["summary"]
    assert client.get("/v1/progress", headers=A).json()["rank"] == out["rank"]


def test_progress_and_compare_are_private(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    ids = _seed(client, n=2)
    assert client.get("/v1/progress", headers=B).json()["swings"] == []
    assert client.get(f"/v1/compare?a={ids[0]}&b={ids[1]}", headers=B).status_code == 404


def test_compare_two_swings(tmp_path):
    client = _client(tmp_path, import_key=KEY)
    ids = _seed(client, n=2)
    out = client.get(f"/v1/compare?a={ids[0]}&b={ids[1]}", headers=A).json()
    assert len(out["rows"]) == 8 and sum(out["counts"].values()) == 8
    assert out["swings"]["a"]["id"] == ids[0] and out["score"]["result"] in {"improved", "worse", "same"}
    assert all(r["result"] in {"improved", "worse", "same", "not_comparable"} for r in out["rows"])
