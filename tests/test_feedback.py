"""In-app feedback (Build 9) through the SQLite mirror: same rules as public.feedback_submit."""

from __future__ import annotations

from test_service import A, B, _upload, service  # noqa: F401  (pytest fixture)


def _finished(client, app) -> str:
    job_id = _upload(client)["job"]["id"]
    client.post(f"/v1/analysis/jobs/{job_id}/start", headers=A)
    assert app.state.worker.run_once() is True
    return job_id


def test_analysis_feedback_is_one_row_per_swing_with_server_side_context(service) -> None:
    client, app, _ = service()
    job_id = _finished(client, app)
    first = client.post("/v1/feedback", headers=A, json={"kind": "analysis", "swing_id": job_id, "rating": "down",
                                                         "message": " Too harsh ", "measured_checks": ["address.torso_angle"]})
    assert first.status_code == 201 and first.json() == {"status": "received"}
    client.post("/v1/feedback", headers=A, json={"kind": "analysis", "swing_id": job_id, "rating": "up"})
    rows = app.state.repo.db.execute("SELECT kind, rating, message, context FROM feedback").fetchall()
    assert len(rows) == 1 and rows[0]["rating"] == "up"
    assert client.post("/v1/feedback", headers=B, json={"kind": "analysis", "swing_id": job_id,
                                                        "rating": "up"}).status_code == 404


def test_general_feedback_needs_a_category_and_a_message(service) -> None:
    client, app, _ = service()
    assert client.post("/v1/feedback", headers=A, json={"kind": "general", "category": "feature",
                                                        "message": "Face-on view please"}).status_code == 201
    assert client.post("/v1/feedback", headers=A, json={"kind": "general", "category": "feature",
                                                        "message": "   "}).status_code == 422
    assert client.post("/v1/feedback", headers=A, json={"kind": "general", "category": "nonsense",
                                                        "message": "x"}).status_code == 422
    assert client.post("/v1/feedback", headers=A, json={"kind": "general", "category": "bug", "message": "x",
                                                        "user_id": "someone-else"}).status_code == 422
    assert client.post("/v1/feedback", json={"kind": "general", "category": "bug", "message": "x"}).status_code == 401


def test_account_deletion_removes_feedback(service) -> None:
    client, app, _ = service()
    client.post("/v1/feedback", headers=A, json={"kind": "general", "category": "other", "message": "hello"})
    assert client.delete("/v1/account", headers=A).status_code == 200
    assert app.state.repo.db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
