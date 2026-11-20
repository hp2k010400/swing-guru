"""Statistics and the evidence-only validation report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swingguru.coach_review import fixtures, report, stats, store
from swingguru.scoring import config as SC


def _submit(db: Path, batch: str, reviewer: str, index: int, *, overall: int, name: str | None = None,
            edit_to: int | None = None, confidence: str = "HIGH") -> None:
    conn = store.connect(db)
    try:
        if name:
            store.remember_reviewer(conn, batch, reviewer, name)
        row = conn.execute("SELECT public_id FROM swing_item s JOIN batch b ON b.id = s.batch_id"
                           " WHERE b.name = ? ORDER BY s.ordinal LIMIT 1 OFFSET ?", (batch, index)).fetchone()
        store.submit_review(conn, batch, reviewer, row["public_id"],
                            ratings={"setup": overall, "backswing": overall, "top": overall, "downswing": overall,
                                     "overall": overall}, confidence=confidence)
        if edit_to is not None:
            store.submit_review(conn, batch, reviewer, row["public_id"],
                                ratings={"setup": edit_to, "backswing": edit_to, "top": edit_to, "downswing": edit_to,
                                         "overall": edit_to}, confidence="LOW", allow_edit=True)
    finally:
        conn.close()


def _dataset(tmp_path: Path, specs: list[dict], *, batch: str = "pilot", reviews: list[tuple] = ()) -> tuple[Path, dict]:
    db = tmp_path / "review.sqlite3"
    fixtures.build_db(db, batch, specs, root=tmp_path / "media")
    for index, review in enumerate(reviews):
        reviewer, item_index, overall = review[:3]
        kwargs = review[3] if len(review) > 3 else {}
        _submit(db, batch, reviewer, item_index, overall=overall, **kwargs)
        del index
    conn = store.connect(db)
    try:
        return db, store.report_dataset(conn, batch)
    finally:
        conn.close()


def test_ranks_use_average_positions_for_ties() -> None:
    assert stats.ranks([5, 5, 9]) == [1.5, 1.5, 3.0]
    assert stats.ranks([3, 1, 2]) == [3.0, 1.0, 2.0]


def test_revision_counts_are_per_reviewer_and_do_not_change_blind_metrics(tmp_path: Path) -> None:
    _dataset(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score(total=7.4)}],
             reviews=[("coachA", 0, 2), ("coachB", 0, 8)])
    conn = store.connect(tmp_path / "review.sqlite3")
    try:
        item_id = conn.execute("SELECT public_id FROM swing_item").fetchone()["public_id"]
        for overall in (6, 9):
            store.submit_review(conn, "pilot", "coachA", item_id,
                                ratings={field: overall for field in ("setup", "backswing", "top", "downswing", "overall")},
                                confidence="HIGH", allow_edit=True)
        payload = report.build_report(store.report_dataset(conn, "pilot"))
    finally:
        conn.close()
    reviewers = {row["reviewer_id"]: row for row in payload["reviewers"]}
    assert reviewers["coachA"]["later_revisions"] == 2
    assert reviewers["coachB"]["later_revisions"] == 0
    assert reviewers["coachA"]["mae"] == pytest.approx(5.4)
    assert reviewers["coachB"]["mae"] == pytest.approx(0.6)


def test_correlations_are_unavailable_when_undefined() -> None:
    assert stats.pearson([1, 1, 1], [2, 3, 4]) is None
    assert stats.pearson([1, 2], [3, 4]) is None  # too few pairs
    assert stats.spearman([9, 8, 4], [3, 6, 9]) == pytest.approx(-1.0)
    assert stats.spearman([5, 5, 5], [1, 2, 3]) is None
    assert stats.mean_absolute_error([9, 8, 4], [3, 6, 9]) == pytest.approx((6 + 2 + 5) / 3)


def test_normalized_shares_come_from_the_actual_weights() -> None:
    checks = [{"id": "a", "weight": 3.0, "points": 9.0, "share": 0.99},
              {"id": "b", "weight": 1.0, "points": 1.0, "share": 0.01}]
    shares = stats.normalized_shares(checks)
    assert shares == pytest.approx([0.75, 0.25])
    assert sum(shares) == pytest.approx(1.0)
    assert sum(stats.check_contribution(check, share) for check, share in zip(checks, shares)) == pytest.approx(7.0)
    assert stats.weighted_total(checks) == pytest.approx(7.0)
    # A missing or non-positive weight makes the share unavailable rather than a raw fallback.
    assert stats.normalized_shares([{"id": "a", "weight": 0}]) == [None]
    assert stats.normalized_shares([{"id": "a", "share": 0.5}]) == [None]
    assert stats.group_points(checks, {"a"}) == pytest.approx(9.0)


def test_describe_reports_spread_and_sample_deviation() -> None:
    described = stats.describe([4, 6, 8])
    assert described == {"n": 3, "min": 4.0, "max": 8.0, "mean": 6.0, "median": 6.0, "sd": 2.0}
    assert stats.describe([])["n"] == 0
    assert stats.describe([7])["sd"] is None


def test_drop_one_respects_the_minimum_coverage() -> None:
    score = fixtures.synthetic_score(points={c.id: 9.0 for c in SC.CHECKS})
    rows = stats.drop_one_totals(score, 5)
    assert len(rows) == len(score["checks"])
    assert all(row["total_without"] is not None for row in rows)

    thin = fixtures.synthetic_score(excluded_ids=["top.posture", "impact.pelvis", "impact.head"])
    thin_rows = stats.drop_one_totals(thin, 5)
    assert all(row["total_without"] is None and "minimum" in row["reason"] for row in thin_rows)


def test_report_without_reviews_is_honest_about_being_unavailable(tmp_path: Path) -> None:
    _, dataset = _dataset(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score(total=7.4)},
                                     {"swing_id": "s2", "score": None}])
    payload = report.build_report(dataset, batch_name="pilot")
    assert payload["counts"]["swings"] == 2
    assert payload["counts"]["swings_with_stored_score"] == 1
    assert payload["counts"]["swings_with_available_score"] == 1
    assert payload["counts"]["swings_without_stored_score"] == 1
    assert payload["counts"]["reviewers"] == 0 and payload["counts"]["reviews_submitted"] == 0
    assert payload["correlation"]["pearson"] is None
    assert payload["correlation"]["undefined_reason"] == "no swing has both a stored score and a blind coach rating"
    assert payload["summary"]["meets_planned_minimum"] is False
    assert payload["flags"] == [] and payload["instability"] == []
    markdown = report.render_markdown(payload)
    assert "Below the planned exploratory minimum" in markdown
    assert "not a validation claim" in markdown
    assert "No flags fired" in markdown
    assert "coverage, dominance or plane-share thresholds" in markdown
    assert "same measurement at two different moments" in markdown
    assert "spans -10 (model 0, coach 10) to +9" in markdown


def test_shares_and_contributions_are_normalized_and_consistent(tmp_path: Path) -> None:
    _, dataset = _dataset(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score(total=7.4, version="swing_score_v1")}])
    payload = report.build_report(dataset, batch_name="pilot")
    swing = payload["swings"][0]
    assert swing["check_share_sum"] == pytest.approx(1.0, abs=1e-9)
    assert swing["contribution_sum"] == pytest.approx(7.4, abs=0.001)
    assert swing["model_sections"]["setup_share"] == pytest.approx(0.375, abs=0.001)   # V1: equal weights
    assert swing["model_sections"]["plane_share"] == pytest.approx(0.25, abs=0.001)
    # V2 (the 2026-10-05 audit): set-up 20 %, the two plane checks 23 %.
    _, v2 = _dataset(tmp_path / "v2", [{"swing_id": "s2", "score": fixtures.synthetic_score(total=7.4)}])
    sections = report.build_report(v2, batch_name="pilot")["swings"][0]["model_sections"]
    assert sections["setup_share"] == pytest.approx(0.20, abs=0.001)
    assert sections["plane_share"] == pytest.approx(0.23, abs=0.001)
    # The product's rounded share is reported separately and is not used for contributions.
    check = swing["checks"][0]
    assert check["stored_share"] is not None
    assert check["share"] == pytest.approx(check["weight"] / sum(c["weight"] for c in swing["checks"]))


def test_an_inconsistent_stored_total_is_still_authoritative(tmp_path: Path) -> None:
    score = fixtures.synthetic_score(total=7.0, points={c.id: 9.0 for c in SC.CHECKS})
    _, dataset = _dataset(tmp_path, [{"swing_id": "s1", "score": score}])
    payload = report.build_report(dataset, batch_name="pilot")
    swing = payload["swings"][0]
    assert swing["model"]["total"] == 7.0                    # stored is authoritative
    assert swing["contribution_sum"] == pytest.approx(9.0)   # what the stored points imply


def test_report_with_reviews_computes_diagnostics_and_flags(tmp_path: Path) -> None:
    specs = [{"swing_id": "high-good", "score": fixtures.synthetic_score(total=9.0), "club": "Driver"},
             {"swing_id": "mid", "score": fixtures.synthetic_score(total=8.0), "club": "7 Iron"},
             {"swing_id": "low-bad", "score": fixtures.synthetic_score(total=4.0), "club": "Putter"}]
    _, dataset = _dataset(tmp_path, specs, reviews=[("coachA", 0, 3), ("coachA", 1, 6), ("coachA", 2, 9)])
    payload = report.build_report(dataset, batch_name="pilot")
    assert payload["correlation"]["pairs"] == 3
    assert payload["correlation"]["pearson"] == pytest.approx(-0.9449, abs=0.001)
    assert payload["correlation"]["mae"] == pytest.approx((6 + 2 + 5) / 3, abs=0.01)
    assert payload["summary"]["model_total"]["mean"] == pytest.approx(7.0, abs=0.01)
    assert payload["summary"]["difference_model_minus_coach"]["mean"] == pytest.approx(1.0, abs=0.01)
    first = payload["disagreements"][0]
    assert first["swing_id"] == "high-good" and first["difference"] == pytest.approx(6.0)
    kinds = {(flag["swing_id"], flag["kind"]) for flag in payload["flags"]}
    assert ("high-good", "high_model_low_coach") in kinds
    assert ("low-bad", "low_model_high_coach") in kinds
    assert ("high-good", "large_difference") in kinds
    markdown = report.render_markdown(payload)
    assert "| high-good |" in markdown and "Driver" in markdown


def test_reviewer_names_and_first_blind_revisions_are_used(tmp_path: Path) -> None:
    specs = [{"swing_id": "s1", "score": fixtures.synthetic_score(total=8.0)}]
    db, dataset = _dataset(tmp_path, specs)
    _submit(db, "pilot", "coachA", 0, overall=2, name="Coach Ada", edit_to=9, confidence="LOW")
    conn = store.connect(db)
    try:
        dataset = store.report_dataset(conn, "pilot")
    finally:
        conn.close()
    payload = report.build_report(dataset, batch_name="pilot")
    swing = payload["swings"][0]
    assert swing["coach"]["overall_mean"] == pytest.approx(2.0)  # revision 1, not the edit
    assert swing["later_revisions"][0]["ratings"]["overall"] == 9
    reviewer = payload["reviewers"][0]
    assert reviewer["reviewer_id"] == "coachA" and reviewer["display_name"] == "Coach Ada"
    assert reviewer["comparable"] == 1 and reviewer["later_revisions"] == 1
    assert payload["counts"]["blind_submissions"] == 1 and payload["counts"]["later_revisions"] == 1
    markdown = report.render_markdown(payload)
    assert "| coachA | Coach Ada |" in markdown


def test_coverage_confidence_and_plane_flags(tmp_path: Path) -> None:
    specs = [{"swing_id": "thin", "score": fixtures.synthetic_score(total=8.0,
                                                                    excluded_ids=["top.posture", "impact.pelvis",
                                                                                  "impact.head"])},
             {"swing_id": "mismatch", "score": fixtures.synthetic_score(total=7.0)}]
    db, dataset = _dataset(tmp_path, specs)
    # No coach ratings at all: the snapshot-only flags must still be able to fire.
    payload = report.build_report(dataset, batch_name="pilot")
    assert "low_coverage" in {flag["kind"] for flag in payload["flags"]}

    _submit(db, "pilot", "coachA", 0, overall=7, confidence="LOW")
    _submit(db, "pilot", "coachA", 1, overall=7, confidence="LOW")
    conn = store.connect(db)
    try:
        dataset = store.report_dataset(conn, "pilot")
    finally:
        conn.close()
    payload = report.build_report(dataset, batch_name="pilot")
    assert "confidence_mismatch" in {flag["kind"] for flag in payload["flags"]}

    # The plane flag was designed against V1's equal weights, where this swing's two plane checks carry 1/3.
    plane_heavy = fixtures.synthetic_score(total=7.0, excluded_ids=["address.trail_knee", "address.arm_hang"],
                                           version="swing_score_v1")
    _, plane_dataset = _dataset(tmp_path / "plane", [{"swing_id": "plane", "score": plane_heavy}])
    plane_payload = report.build_report(plane_dataset, batch_name="pilot")
    assert "plane_disproportionate" in {flag["kind"] for flag in plane_payload["flags"]}


def test_similar_groups_get_the_instability_flag_but_golfer_ids_do_not(tmp_path: Path) -> None:
    specs = [{"swing_id": "a1", "score": fixtures.synthetic_score(total=9.0), "similar_group": "group-a"},
             {"swing_id": "a2", "score": fixtures.synthetic_score(total=6.0), "similar_group": "group-a"},
             {"swing_id": "g1", "score": fixtures.synthetic_score(total=9.0), "golfer_id": "group-a"},
             {"swing_id": "g2", "score": fixtures.synthetic_score(total=6.0), "golfer_id": "group-a"}]
    db, dataset = _dataset(tmp_path, specs)
    for index, overall in enumerate((7, 7, 7, 7)):
        _submit(db, "pilot", "coachA", index, overall=overall)
    conn = store.connect(db)
    try:
        dataset = store.report_dataset(conn, "pilot")
    finally:
        conn.close()
    payload = report.build_report(dataset, batch_name="pilot")
    groups = {row["group"]: row for row in payload["instability"]}
    # Namespaced keys: the golfer id "group-a" can never collide with the similar group "group-a".
    assert set(groups) == {"similar:group-a", "golfer:group-a"}
    assert groups["similar:group-a"]["kind"] == "similar" and groups["similar:group-a"]["flagged"] is True
    assert groups["golfer:group-a"]["kind"] == "golfer" and groups["golfer:group-a"]["flagged"] is False
    assert groups["golfer:group-a"]["caveat"].startswith("same golfer id only")
    assert "similar_group_instability" in {flag["kind"] for flag in payload["flags"]}
    markdown = report.render_markdown(payload)
    assert "similar:group-a" in markdown and "golfer:group-a" in markdown


def test_plane_counterfactual_and_section_evidence(tmp_path: Path) -> None:
    _, dataset = _dataset(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score(total=7.0)}])
    payload = report.build_report(dataset, batch_name="pilot")
    counterfactual = payload["swings"][0]["plane_counterfactual"]
    assert counterfactual["swing_id"] == "s1"
    assert counterfactual["total_without_plane"] == pytest.approx(7.0)  # flat fixture: removing checks changes nothing
    assert counterfactual["checks_remaining"] == 6
    evidence = payload["section_evidence"]
    assert evidence["rows"][0]["setup_points"] == pytest.approx(7.0)
    assert evidence["setup_points_vs_residual"]["undefined_reason"] == "no swing has both of these values"
    thin = fixtures.synthetic_score(total=7.0, excluded_ids=["top.posture", "impact.pelvis", "impact.head"])
    _, thin_dataset = _dataset(tmp_path / "thin", [{"swing_id": "thin", "score": thin}])
    thin_payload = report.build_report(thin_dataset, batch_name="pilot")
    assert thin_payload["swings"][0]["plane_counterfactual"]["reason"].startswith("fewer than the minimum")


def test_write_report_writes_markdown_and_json(tmp_path: Path) -> None:
    db, dataset = _dataset(tmp_path, [{"swing_id": "s1", "score": fixtures.synthetic_score(total=7.0)}])
    markdown_path = tmp_path / "VALIDATION_REPORT.md"
    json_path = tmp_path / "validation_report.json"
    conn = store.connect(db)
    try:
        result = report.write_report(conn, out=markdown_path, json_out=json_path, batch_name="pilot", dataset=dataset)
    finally:
        conn.close()
    assert markdown_path.read_text(encoding="utf-8").startswith("# Swing Score validation report")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "coach_review/validation_report/1"
    assert payload["counts"]["swings"] == 1
    assert result["payload"]["counts"]["swings"] == 1
