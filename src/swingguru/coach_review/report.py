"""Evidence-only comparison report: coach ratings against the stored Swing Score.

Rules the report keeps:

* every blind diagnostic uses each reviewer's **first blind revision**; later
  revisions appear only in a clearly labelled audit section;
* each check's weight and contribution are normalized from the stored weights at
  report time (sum of shares = 1); the rounded ``share`` the product stored is
  reported separately and never used to rebuild contributions;
* neither scale is rescaled, the model is never recomputed, and no result claims
  validation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from ..scoring import config as SC
from . import config as C
from . import stats as S

NOT_VALIDATED = ("This report is exploratory evidence, not a validation claim. No threshold here "
                 "establishes that the Swing Score is correct.")

SCALE_NOTE = ("The model score is 0-10 and the coach ratings are 1-10, compared in point units with no rescaling: "
              "model minus coach spans -10 (model 0, coach 10) to +9 (model 10, coach 1). The coach floor is 1, so "
              "the smallest single-point gap the coach can express is 1 point.")

SIMILAR_PREFIX = "similar:"
GOLFER_PREFIX = "golfer:"


def _round(value: float | None, places: int = 3) -> float | None:
    return None if value is None else round(float(value), places)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _blind_revision(revisions: Sequence[Mapping], review_id: int) -> Mapping | None:
    """Revision 1 - the original blind submission - for this review, if it exists."""

    for revision in revisions:
        if revision["review_id"] == review_id and int(revision["revision"]) == 1:
            return revision
    return None


def _score_checks(score: Mapping | None) -> list[dict]:
    """Checks with exact, normalized weight and contribution (never the rounded stored share)."""

    if not isinstance(score, Mapping):
        return []
    raw = [c for c in (score.get("checks") or []) if isinstance(c, Mapping)]
    shares = S.normalized_shares(raw)
    out = []
    for check, share in zip(raw, shares):
        stored_share = check.get("share")
        out.append({
            "id": check.get("id"), "phase": check.get("phase"), "name": check.get("name"),
            "measurement": check.get("measurement"), "unit": check.get("unit"),
            "status": check.get("status"), "confidence": check.get("confidence"),
            "estimated": bool(check.get("estimated")),
            "points": check.get("points"), "weight": S.check_weight(check),
            "share": _round(share, 6), "contribution": _round(S.check_contribution(check, share), 6),
            "stored_share": (float(stored_share) if isinstance(stored_share, (int, float))
                             and not isinstance(stored_share, bool) else None),
        })
    return out


def _mapping_of(check: Mapping) -> str:
    """The stored check id encodes the measurement; the model config maps ids to measurements."""

    for scored in SC.CHECKS:
        if scored.id == check.get("id"):
            return scored.mapping_id
    return ""


def _correlation(pairs: Sequence[tuple[float, float]]) -> dict:
    xs = [first for first, _ in pairs]
    ys = [second for _, second in pairs]
    result = {"pairs": len(pairs), "pearson": _round(S.pearson(xs, ys)), "spearman": _round(S.spearman(xs, ys)),
              "undefined_reason": None}
    if not pairs:
        result["undefined_reason"] = "no swing has both of these values"
    elif len(pairs) < C.MIN_PAIRS_FOR_CORRELATION:
        result["undefined_reason"] = f"fewer than {C.MIN_PAIRS_FOR_CORRELATION} comparable swings"
    elif result["pearson"] is None:
        result["undefined_reason"] = "one of the two series is constant (no spread to correlate)"
    return result


def build_report(dataset: Mapping, *, batch_name: str | None = None,
                 generated_at: str | None = None) -> dict:
    """Turn the stored rows into the machine-readable report payload."""

    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    items = list(dataset["items"])
    reviews = list(dataset["reviews"])
    revisions = list(dataset["revisions"])
    skips = list(dataset["skips"])
    reviewer_names = {str(row["reviewer_id"]): row.get("display_name") for row in dataset.get("reviewers", [])}

    reviews_by_item: dict[int, list[Mapping]] = {}
    for review in reviews:
        reviews_by_item.setdefault(int(review["item_id"]), []).append(review)
    skips_by_item: dict[int, list[Mapping]] = {}
    for skip in skips:
        skips_by_item.setdefault(int(skip["item_id"]), []).append(skip)
    revisions_by_review: dict[int, list[Mapping]] = {}
    for revision in revisions:
        revisions_by_review.setdefault(int(revision["review_id"]), []).append(revision)

    swings = []
    for item in items:
        item_id = int(item["id"])
        score = item.get("score")
        available = bool(isinstance(score, Mapping) and score.get("available")
                         and isinstance(score.get("total"), (int, float))
                         and not isinstance(score.get("total"), bool))
        checks = _score_checks(score)
        address_ids = {c.id for c in SC.CHECKS if c.phase == "address"}
        plane_ids = {c.id for c in SC.CHECKS if c.mapping_id == "swing.plane_vs_address_shaft"}
        motion_ids = {str(check["id"]) for check in checks} - address_ids
        shares = {check["id"]: check["share"] for check in checks}
        address_points = S.group_points(checks, address_ids)
        motion_points = S.group_points(checks, motion_ids)
        plane_points = S.group_points(checks, plane_ids)
        plane_share = (sum(shares[cid] or 0.0 for cid in plane_ids if cid in shares)
                       if any(cid in shares for cid in plane_ids) else None)

        item_reviews = reviews_by_item.get(item_id, [])
        blind, later, by_reviewer = [], [], {}
        for review in item_reviews:
            first = _blind_revision(revisions_by_review.get(int(review["id"]), []), int(review["id"]))
            if first is not None:
                blind.append({"reviewer_id": str(review["reviewer_id"]),
                              "reviewer_name": reviewer_names.get(str(review["reviewer_id"])),
                              "ratings": first["ratings"], "confidence": first["reviewer_confidence"],
                              "notes": first["notes"], "saved_at": first["saved_at"]})
            if int(review["revision"]) > 1:
                latest = revisions_by_review.get(int(review["id"]), [])
                head = max(latest, key=lambda row: int(row["revision"])) if latest else None
                later.append({"reviewer_id": str(review["reviewer_id"]),
                              "reviewer_name": reviewer_names.get(str(review["reviewer_id"])),
                              "revision": int(review["revision"]),
                              "ratings": (head or {}).get("ratings"), "confidence": (head or {}).get(
                                  "reviewer_confidence"), "saved_at": (head or {}).get("saved_at")})
            by_reviewer[str(review["reviewer_id"])] = {
                "ratings": {field: review[f"{field}_rating"] for field in C.RATING_FIELDS},
                "confidence": review["reviewer_confidence"], "revision": int(review["revision"]),
                "blind": bool(review["blind"]), "first_submitted_at": review["first_submitted_at"],
                "updated_at": review["updated_at"],
            }

        coach_by_field = {field: [float(r["ratings"][field]) for r in blind] for field in C.RATING_FIELDS}
        coach_motion = [float(r["ratings"][field]) for r in blind for field in ("backswing", "top", "downswing")]
        model_total = float(score["total"]) if available else None
        mean_overall = _mean(coach_by_field["overall"])
        difference = None if model_total is None or mean_overall is None else model_total - mean_overall

        plane_counterfactual = None
        if checks and plane_ids & {str(check["id"]) for check in checks}:
            remaining = [check for check in checks if str(check["id"]) not in plane_ids]
            minimum = int(score.get("minimum") or 0) if isinstance(score, Mapping) else 0
            if len(remaining) >= max(minimum, 1):
                total_without = S.weighted_total(remaining)
                plane_counterfactual = {
                    "swing_id": item["swing_id"],
                    "label": "counterfactual: the two plane checks removed from the stored points",
                    "checks_remaining": len(remaining),
                    "total_without_plane": _round(total_without),
                    "delta_vs_stored": _round(None if total_without is None or model_total is None
                                              else total_without - model_total),
                    "residual_delta": _round(None if total_without is None or difference is None
                                             else (total_without - model_total)),
                }
            else:
                plane_counterfactual = {
                    "swing_id": item["swing_id"],
                    "label": "counterfactual: the two plane checks removed from the stored points",
                    "checks_remaining": len(remaining), "total_without_plane": None,
                    "delta_vs_stored": None, "residual_delta": None,
                    "reason": f"fewer than the minimum {minimum} checks would remain",
                }

        swings.append({
            "batch": item["batch"], "swing_id": item["swing_id"], "item_id": item["public_id"],
            "club": item.get("club"), "golfer_id": item.get("golfer_id"), "similar_group": item.get("similar_group"),
            "model": {
                "stored": item.get("score_stored", score is not None) == 1 or score is not None,
                "available": available, "total": _round(model_total),
                "version": (score or {}).get("version") if isinstance(score, Mapping) else item.get("model_version"),
                "measured": (score or {}).get("measured") if isinstance(score, Mapping) else None,
                "eligible": (score or {}).get("eligible") if isinstance(score, Mapping) else None,
                "coverage": (score or {}).get("coverage") if isinstance(score, Mapping) else None,
                "confidence": ((score or {}).get("confidence") or {}).get("label") if isinstance(score, Mapping) else None,
                "reason": (score or {}).get("reason") if isinstance(score, Mapping) else None,
            },
            "checks": checks,
            "check_share_sum": _round(sum(check["share"] or 0.0 for check in checks), 9) if checks else None,
            "contribution_sum": _round(sum(check["contribution"] or 0.0 for check in checks), 6) if checks else None,
            "excluded": list((score or {}).get("excluded") or []) if isinstance(score, Mapping) else [],
            "minimum_checks": int((score or {}).get("minimum") or 0) if isinstance(score, Mapping) else 0,
            "model_sections": {
                "setup_points": _round(address_points), "motion_points": _round(motion_points),
                "setup_share": _round(sum(shares[cid] or 0.0 for cid in address_ids if cid in shares))
                if any(cid in shares for cid in address_ids) else None,
                "motion_share": _round(sum(shares[cid] or 0.0 for cid in motion_ids if cid in shares))
                if motion_ids & set(shares) else None,
                "plane_points": _round(plane_points), "plane_share": _round(plane_share),
                "plane_checks": sorted(cid for cid in plane_ids if cid in shares),
            },
            "plane_counterfactual": plane_counterfactual,
            "reviewers_blind": sorted(r["reviewer_id"] for r in blind),
            "reviewers_head": sorted(by_reviewer),
            "blind_reviews": blind,
            "later_revisions": later,
            "review_by_reviewer": by_reviewer,
            "coach": {
                "n_blind": len(blind),
                "overall_mean": _round(mean_overall),
                "overall_ratings": sorted(coach_by_field["overall"]),
                "setup_mean": _round(_mean(coach_by_field["setup"])),
                "motion_mean": _round(_mean(coach_motion)),
                "field_means": {field: _round(_mean(coach_by_field[field])) for field in C.RATING_FIELDS},
                "field_ranges": {field: [min(coach_by_field[field]), max(coach_by_field[field])]
                                 if coach_by_field[field] else None for field in C.RATING_FIELDS},
                "confidence": sorted({r["confidence"] for r in blind}),
                "repeated_reviews": sum(1 for review in item_reviews if int(review["revision"]) > 1),
            },
            "difference": _round(difference),
            "skipped_by": sorted(str(s["reviewer_id"]) for s in skips_by_item.get(item_id, [])),
        })

    # Primary aggregate: one value per swing (mean of the independent blind overall ratings).
    pairs = [(s["model"]["total"], s["coach"]["overall_mean"]) for s in swings
             if s["model"]["available"] and s["coach"]["n_blind"] > 0]
    xs = [model for model, _ in pairs]
    ys = [coach for _, coach in pairs]
    differences = [model - coach for model, coach in pairs]
    correlation = _correlation(pairs)
    if correlation["pairs"] == 0:
        correlation["undefined_reason"] = "no swing has both a stored score and a blind coach rating"
    correlation["mae"] = _round(S.mean_absolute_error(xs, ys))

    # Per-reviewer diagnostics, on each reviewer's first blind revision only.
    by_reviewer = []
    for reviewer_id in sorted({str(r["reviewer_id"]) for r in reviews}):
        sub = []
        for swing in swings:
            if not swing["model"]["available"]:
                continue
            for blind in swing["blind_reviews"]:
                if blind["reviewer_id"] == reviewer_id:
                    sub.append((swing["model"]["total"], float(blind["ratings"]["overall"])))
        later_count = sum(int(revision["revision"]) - 1 for swing in swings
                          for revision in swing["later_revisions"]
                          if revision["reviewer_id"] == reviewer_id)
        by_reviewer.append({
            "reviewer_id": reviewer_id,
            "display_name": reviewer_names.get(reviewer_id),
            "reviews": sum(1 for r in reviews if str(r["reviewer_id"]) == reviewer_id),
            "comparable": len(sub),
            "pearson": _round(S.pearson([a for a, _ in sub], [b for _, b in sub])),
            "spearman": _round(S.spearman([a for a, _ in sub], [b for _, b in sub])),
            "mae": _round(S.mean_absolute_error([a for a, _ in sub], [b for _, b in sub])),
            "later_revisions": later_count,
        })

    disagreements = sorted(
        ({"swing_id": s["swing_id"], "club": s["club"], "model": s["model"]["total"],
          "coach_overall_mean": s["coach"]["overall_mean"], "difference": s["difference"],
          "reviewers": s["reviewers_blind"]} for s in swings if s["difference"] is not None),
        key=lambda row: abs(row["difference"]), reverse=True)

    # Setup / motion / plane evidence.
    sections = []
    for swing in swings:
        if not swing["checks"]:
            continue
        sections.append({
            "swing_id": swing["swing_id"], "club": swing["club"],
            "setup_points": swing["model_sections"]["setup_points"],
            "motion_points": swing["model_sections"]["motion_points"],
            "setup_share": swing["model_sections"]["setup_share"],
            "plane_share": swing["model_sections"]["plane_share"],
            "plane_points": swing["model_sections"]["plane_points"],
            "coach_setup": swing["coach"]["setup_mean"], "coach_motion": swing["coach"]["motion_mean"],
            "coach_overall": swing["coach"]["overall_mean"], "difference": swing["difference"],
            "plane_counterfactual": swing["plane_counterfactual"],
        })
    setup_residual = [(s["model_sections"]["setup_points"], s["difference"]) for s in swings
                      if s["model"]["available"] and s["difference"] is not None
                      and s["model_sections"]["setup_points"] is not None]
    plane_residual = [(s["model_sections"]["plane_points"], s["difference"]) for s in swings
                      if s["model"]["available"] and s["difference"] is not None
                      and s["model_sections"]["plane_points"] is not None]
    setup_vs_motion = [(s["model_sections"]["setup_points"], s["model_sections"]["motion_points"]) for s in swings
                       if s["model_sections"]["setup_points"] is not None
                       and s["model_sections"]["motion_points"] is not None]
    section_evidence = {
        "rows": sections,
        "setup_points_vs_residual": _correlation(setup_residual),
        "plane_points_vs_residual": _correlation(plane_residual),
        "setup_vs_motion_points": _correlation(setup_vs_motion),
        "plane_counterfactuals": [s for s in (swing["plane_counterfactual"] for swing in swings) if s],
    }

    drop_one = []
    for swing in swings:
        if not swing["model"]["available"] or not swing["checks"]:
            continue
        rows = S.drop_one_totals({"checks": swing["checks"], "total": swing["model"]["total"], "scale": 10.0},
                                 swing["minimum_checks"])
        deltas = [abs(row["delta"]) for row in rows if row["delta"] is not None]
        drop_one.append({"swing_id": swing["swing_id"], "max_abs_delta": _round(max(deltas)) if deltas else None,
                         "rows": rows})

    flags = []
    for swing in swings:
        model_total, coach_mean = swing["model"]["total"], swing["coach"]["overall_mean"]
        if model_total is not None and coach_mean is not None:
            if (model_total >= C.FLAG_HIGH_MODEL_LOW_COACH["model_min"]
                    and coach_mean <= C.FLAG_HIGH_MODEL_LOW_COACH["coach_max"]):
                flags.append({"swing_id": swing["swing_id"], "kind": "high_model_low_coach", "model": model_total,
                              "coach": coach_mean,
                              "detail": f"model {model_total:.1f} >= {C.FLAG_HIGH_MODEL_LOW_COACH['model_min']:.1f} "
                                        f"while coach mean {coach_mean:.1f} <= "
                                        f"{C.FLAG_HIGH_MODEL_LOW_COACH['coach_max']:.1f}"})
            if (model_total <= C.FLAG_LOW_MODEL_HIGH_COACH["model_max"]
                    and coach_mean >= C.FLAG_LOW_MODEL_HIGH_COACH["coach_min"]):
                flags.append({"swing_id": swing["swing_id"], "kind": "low_model_high_coach", "model": model_total,
                              "coach": coach_mean,
                              "detail": f"model {model_total:.1f} <= {C.FLAG_LOW_MODEL_HIGH_COACH['model_max']:.1f} "
                                        f"while coach mean {coach_mean:.1f} >= "
                                        f"{C.FLAG_LOW_MODEL_HIGH_COACH['coach_min']:.1f}"})
            if swing["difference"] is not None and abs(swing["difference"]) >= C.FLAG_LARGE_DIFFERENCE:
                flags.append({"swing_id": swing["swing_id"], "kind": "large_difference", "model": model_total,
                              "coach": coach_mean,
                              "detail": f"model - coach = {swing['difference']:+.1f} "
                                        f"(threshold {C.FLAG_LARGE_DIFFERENCE:.1f} points)"})
            model_confidence = swing["model"]["confidence"]
            reviewer_confidence = swing["coach"]["confidence"]
            if model_confidence and reviewer_confidence and (
                    (model_confidence == "HIGH" and "LOW" in reviewer_confidence)
                    or (model_confidence == "MEDIUM" and reviewer_confidence == ["HIGH"])):
                flags.append({"swing_id": swing["swing_id"], "kind": "confidence_mismatch",
                              "model": model_total, "coach": coach_mean,
                              "detail": f"model confidence {model_confidence} vs reviewer confidence "
                                        f"{'/'.join(reviewer_confidence)} (different constructs - a prompt only)"})
        if swing["model"]["available"] and isinstance(swing["model"]["coverage"], (int, float)):
            if swing["model"]["coverage"] < C.FLAG_LOW_COVERAGE:
                flags.append({"swing_id": swing["swing_id"], "kind": "low_coverage",
                              "model": swing["model"]["total"], "coach": swing["coach"]["overall_mean"],
                              "detail": f"coverage {swing['model']['coverage']:.2f} < {C.FLAG_LOW_COVERAGE:.2f} "
                                        f"({swing['model']['measured']}/{swing['model']['eligible']} checks)"})
        for check in swing["checks"]:
            if (check["share"] or 0) >= C.FLAG_DOMINANT_SHARE:
                flags.append({"swing_id": swing["swing_id"], "kind": "dominant_check",
                              "model": swing["model"]["total"], "coach": swing["coach"]["overall_mean"],
                              "detail": f"{check['name']} ({check['id']}) carries {check['share']:.2f} of the weight "
                                        f"(threshold {C.FLAG_DOMINANT_SHARE:.2f})"})
        if swing["model"]["available"] and swing["model_sections"]["plane_share"] is not None:
            if swing["model_sections"]["plane_share"] >= C.FLAG_PLANE_SHARE:
                flags.append({"swing_id": swing["swing_id"], "kind": "plane_disproportionate",
                              "model": swing["model"]["total"], "coach": swing["coach"]["overall_mean"],
                              "detail": f"the two plane checks carry {swing['model_sections']['plane_share']:.2f} of the "
                                        f"weight (threshold {C.FLAG_PLANE_SHARE:.2f})"})

    # Same-golfer / similar-group evidence. Similar groups are namespaced so a golfer id
    # can never be mistaken for an explicit similarity label.
    groups: dict[str, dict] = {}
    for swing in swings:
        if swing.get("similar_group"):
            groups.setdefault(SIMILAR_PREFIX + str(swing["similar_group"]), {"kind": "similar", "members": []})
        if swing.get("golfer_id"):
            groups.setdefault(GOLFER_PREFIX + str(swing["golfer_id"]), {"kind": "golfer", "members": []})
        for key in (SIMILAR_PREFIX + str(swing["similar_group"]) if swing.get("similar_group") else None,
                    GOLFER_PREFIX + str(swing["golfer_id"]) if swing.get("golfer_id") else None):
            if key:
                groups[key]["members"].append(swing)

    instability = []
    for key, group in sorted(groups.items()):
        members = [m for m in group["members"] if m["model"]["available"]]
        if len(members) < 2:
            continue
        totals = [m["model"]["total"] for m in members]
        coach_means = [m["coach"]["overall_mean"] for m in members if m["coach"]["overall_mean"] is not None]
        spread = max(totals) - min(totals)
        coach_spread = (max(coach_means) - min(coach_means)) if len(coach_means) >= 2 else None
        flagged = (group["kind"] == "similar" and spread >= C.FLAG_INSTABILITY_MODEL_SPREAD
                   and coach_spread is not None and coach_spread <= C.FLAG_INSTABILITY_COACH_CLOSENESS)
        instability.append({
            "group": key, "kind": group["kind"], "swings": [m["swing_id"] for m in members], "totals": totals,
            "coach_means": coach_means, "spread": _round(spread), "coach_spread": _round(coach_spread),
            "flagged": flagged,
            "caveat": ("labelled similarity group: model spread is large while the coach means agree - review these "
                       "swings; identity alone never proves instability"
                       if group["kind"] == "similar" else
                       "same golfer id only: descriptive pair evidence, no instability flag; identity alone never "
                       "proves instability"),
        })
        if flagged:
            flags.append({"swing_id": ", ".join(m["swing_id"] for m in members), "kind": "similar_group_instability",
                          "model": None, "coach": None,
                          "detail": f"similar group {key}: model spread {spread:.1f} >= "
                                    f"{C.FLAG_INSTABILITY_MODEL_SPREAD:.1f} while coach means span "
                                    f"{coach_spread:.1f} <= {C.FLAG_INSTABILITY_COACH_CLOSENESS:.1f}"})

    rated_swings = len(pairs)
    report = {
        "schema": "coach_review/validation_report/1",
        "generated_at": stamp,
        "batch_filter": batch_name,
        "counts": {
            "batches": len(dataset["batches"]),
            "swings": len(swings),
            "swings_with_stored_score": sum(1 for s in swings if s["model"]["stored"]),
            "swings_with_available_score": sum(1 for s in swings if s["model"]["available"]),
            "swings_without_stored_score": sum(1 for s in swings if not s["model"]["stored"]),
            "swings_without_available_score": sum(1 for s in swings if not s["model"]["available"]),
            "reviewers": len({str(r["reviewer_id"]) for r in reviews}),
            "reviews_submitted": len(reviews),
            "blind_submissions": sum(1 for r in revisions if int(r["revision"]) == 1),
            "later_revisions": sum(1 for r in revisions if int(r["revision"]) > 1),
            "skipped": len(skips),
            "swings_rated": sum(1 for s in swings if s["coach"]["n_blind"] > 0),
            "rated_and_scored_swings": rated_swings,
        },
        "model_versions": sorted({str(s["model"]["version"]) for s in swings if s["model"]["version"]}),
        "reviewers": [{"reviewer_id": r["reviewer_id"], "display_name": r["display_name"], "reviews": r["reviews"],
                       "comparable": r["comparable"], "pearson": r["pearson"], "spearman": r["spearman"],
                       "mae": r["mae"], "later_revisions": r["later_revisions"]} for r in by_reviewer],
        "summary": {
            "scored_swings": rated_swings,
            "meets_planned_minimum": rated_swings >= C.MIN_RATED_SWINGS_FOR_SUMMARY,
            "planned_minimum": C.MIN_RATED_SWINGS_FOR_SUMMARY,
            "model_total": S.describe(xs),
            "coach_overall_mean": S.describe(ys),
            "difference_model_minus_coach": S.describe(differences),
            "scale_note": SCALE_NOTE,
        },
        "correlation": correlation,
        "disagreements": disagreements[:10],
        "section_evidence": section_evidence,
        "drop_one": drop_one,
        "flags": flags,
        "instability": instability,
        "thresholds": {
            "high_model_low_coach": C.FLAG_HIGH_MODEL_LOW_COACH,
            "low_model_high_coach": C.FLAG_LOW_MODEL_HIGH_COACH,
            "large_difference": C.FLAG_LARGE_DIFFERENCE,
            "low_coverage": C.FLAG_LOW_COVERAGE,
            "dominant_share": C.FLAG_DOMINANT_SHARE,
            "plane_share": C.FLAG_PLANE_SHARE,
            "instability_model_spread": C.FLAG_INSTABILITY_MODEL_SPREAD,
            "instability_coach_closeness": C.FLAG_INSTABILITY_COACH_CLOSENESS,
            "planned_minimum_rated_swings": C.MIN_RATED_SWINGS_FOR_SUMMARY,
        },
        "swings": swings,
        "not_validated": NOT_VALIDATED,
    }
    return report


def _fmt(value, places: int = 2, dash: str = "unavailable") -> str:
    if value is None:
        return dash
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:.{places}f}"
    return str(value)


def _reviewer_label(row: Mapping) -> str:
    name = row.get("display_name")
    return f"{row['reviewer_id']} ({name})" if name else str(row["reviewer_id"])


def render_markdown(report: Mapping) -> str:
    counts, summary, correlation = report["counts"], report["summary"], report["correlation"]
    lines = [
        "# Swing Score validation report (coach review)",
        "",
        f"Generated {report['generated_at']} by `scripts/coach_review_report.py`"
        + (f" for batch `{report['batch_filter']}`." if report.get("batch_filter") else " for every imported batch."),
        "",
        "> " + report["not_validated"],
        "",
    ]
    if not summary["meets_planned_minimum"]:
        lines += [f"**Below the planned exploratory minimum: {summary['scored_swings']} swing(s) have both a stored "
                  f"score and a blind coach rating; this pilot planned for {summary['planned_minimum']}. That is a "
                  f"planning convention for this work, not a universal statistical threshold, and the numbers below "
                  f"are exploratory either way.**", ""]

    lines += [
        "## Dataset",
        "",
        f"* batches: {counts['batches']}",
        f"* swings imported: {counts['swings']} (with a stored snapshot {counts['swings_with_stored_score']}, with a "
        f"usable score {counts['swings_with_available_score']}, no stored snapshot "
        f"{counts['swings_without_stored_score']}, no usable score {counts['swings_without_available_score']})",
        f"* reviewers: {counts['reviewers']}; reviews submitted: {counts['reviews_submitted']} "
        f"(blind first submissions {counts['blind_submissions']}, later revisions {counts['later_revisions']}, "
        f"skips {counts['skipped']})",
        f"* swings with at least one blind rating: {counts['swings_rated']}; comparable pairs: "
        f"{counts['rated_and_scored_swings']}",
        f"* model versions seen: {', '.join(report['model_versions']) or 'none'}",
        "",
        "## Reviewers",
        "",
        "| Reviewer | Name | First blind reviews | Comparable | Pearson | Spearman | MAE | Later revisions |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if report["reviewers"]:
        for reviewer in report["reviewers"]:
            lines.append(f"| {reviewer['reviewer_id']} | {reviewer['display_name'] or '-'} | {reviewer['reviews']} | "
                         f"{reviewer['comparable']} | {_fmt(reviewer['pearson'])} | {_fmt(reviewer['spearman'])} | "
                         f"{_fmt(reviewer['mae'])} | {reviewer['later_revisions']} |")
    else:
        lines.append("| - | - | 0 | 0 | unavailable | unavailable | unavailable | 0 |")
    lines += ["", "Only first blind revisions feed the diagnostics above. Repeated reviews are not independent "
                  "swings and later revisions are excluded from the primary comparison.", ""]

    lines += [
        "## Summary statistics",
        "",
        "| Series | n | min | max | mean | median | sd |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, key in (("Model total (0-10)", "model_total"), ("Coach overall mean (1-10)", "coach_overall_mean"),
                       ("Difference (model - coach)", "difference_model_minus_coach")):
        stats = summary[key]
        lines.append(f"| {label} | {stats['n']} | {_fmt(stats['min'])} | {_fmt(stats['max'])} | {_fmt(stats['mean'])} | "
                     f"{_fmt(stats['median'])} | {_fmt(stats['sd'])} |")
    lines += ["", summary["scale_note"], ""]

    lines += [
        "## Correlation diagnostics",
        "",
        f"* primary pairs (one value per swing, mean of independent blind ratings): {correlation['pairs']}",
        f"* Pearson r: {_fmt(correlation['pearson'])}",
        f"* Spearman rho (tie-aware): {_fmt(correlation['spearman'])}",
        f"* mean absolute error: {_fmt(correlation['mae'])} point(s)",
    ]
    if correlation["undefined_reason"]:
        lines.append(f"* undefined because: {correlation['undefined_reason']}")
    lines.append("")

    lines += ["## Disagreements (largest first)", "",
              "| Swing | Club | Model | Coach overall (mean) | Model - coach | Reviewers |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in report["disagreements"]:
        lines.append(f"| {row['swing_id']} | {row['club'] or '-'} | {_fmt(row['model'], 1)} | "
                     f"{_fmt(row['coach_overall_mean'], 1)} | {_fmt(row['difference'], 1, '-')} | "
                     f"{', '.join(row['reviewers']) or '-'} |")
    if not report["disagreements"]:
        lines.append("| - | - | unavailable | unavailable | unavailable | - |")
    lines.append("")

    lines += ["## Per-swing detail", "",
              "| Swing | Club | Model | Conf. | Coverage | Coach overall | Difference | Blind reviewers | Skips |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for swing in report["swings"]:
        model = swing["model"]
        coverage = (f"{model['measured']}/{model['eligible']}" if model["measured"] is not None else
                    ("no score" if not model["available"] else "-"))
        lines.append(f"| {swing['swing_id']} | {swing['club'] or '-'} | {_fmt(model['total'], 1)} | "
                     f"{model['confidence'] or '-'} | {coverage} | {_fmt(swing['coach']['overall_mean'], 1)} | "
                     f"{_fmt(swing['difference'], 1, '-')} | {', '.join(swing['reviewers_blind']) or '-'} | "
                     f"{', '.join(swing['skipped_by']) or '-'} |")
    lines.append("")

    lines += ["## Coach ratings by section", "",
              "Five 1-10 ratings per swing; means of the blind ratings.", "",
              "| Swing | Setup | Backswing / plane | Top | Downswing / impact | Overall | Confidence |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for swing in report["swings"]:
        means = swing["coach"]["field_means"]
        lines.append(f"| {swing['swing_id']} | {_fmt(means['setup'], 1)} | {_fmt(means['backswing'], 1)} | "
                     f"{_fmt(means['top'], 1)} | {_fmt(means['downswing'], 1)} | {_fmt(means['overall'], 1)} | "
                     f"{'/'.join(swing['coach']['confidence']) or '-'} |")
    lines.append("")

    lines += ["## Model checks (points, weight, contribution)", "",
              "Weight is normalized here from the stored weights (`w_i / sum w`, sum = 1); contribution is "
              "`share x points`. The product's rounded `share` is shown for comparison only.", "",
              "| Swing | Check | Phase | Points | Weight (normalized) | Contribution | Stored share | Confidence |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    any_check = False
    for swing in report["swings"]:
        for check in swing["checks"]:
            any_check = True
            lines.append(f"| {swing['swing_id']} | {check['name']} | {check['phase']} | {_fmt(check['points'], 1)} | "
                         f"{_fmt(check['share'], 4)} | {_fmt(check['contribution'], 4)} | "
                         f"{_fmt(check['stored_share'], 3)} | {check['confidence']} |")
    if not any_check:
        lines.append("| - | - | - | unavailable | unavailable | unavailable | unavailable | - |")
    lines.append("")

    evidence = report["section_evidence"]
    lines += ["## Setup / motion / plane evidence", "",
              "Address-group points and motion-group points are weighted points over the stored checks; the coach "
              "columns are the blind setup rating and the mean of the backswing / top / downswing ratings. Residual "
              "is model total minus coach overall. Nothing here changes a weight.", ""]
    if evidence["rows"]:
        lines += ["| Swing | Club | Setup points | Motion points | Setup share | Plane share | Coach setup | "
                  "Coach motion | Residual |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in evidence["rows"]:
            lines.append(f"| {row['swing_id']} | {row['club'] or '-'} | {_fmt(row['setup_points'], 2)} | "
                         f"{_fmt(row['motion_points'], 2)} | {_fmt(row['setup_share'], 3)} | "
                         f"{_fmt(row['plane_share'], 3)} | {_fmt(row['coach_setup'], 1)} | "
                         f"{_fmt(row['coach_motion'], 1)} | {_fmt(row['difference'], 1, '-')} |")
        lines.append("")
    else:
        lines += ["No stored check snapshots are available, so there is no per-swing setup / motion evidence.", ""]

    for label, key in (("Setup points vs residual", "setup_points_vs_residual"),
                       ("Plane points vs residual", "plane_points_vs_residual"),
                       ("Setup points vs motion points", "setup_vs_motion_points")):
        block = evidence[key]
        detail = (f"Pearson {_fmt(block['pearson'])}, Spearman {_fmt(block['spearman'])}, n={block['pairs']}"
                  + (f" (unavailable: {block['undefined_reason']})" if block["undefined_reason"] else ""))
        lines.append(f"* {label}: {detail}")
    lines.append("")

    counterfactuals = [row for row in evidence["plane_counterfactuals"]
                       if row.get("total_without_plane") is not None]
    lines += ["### Plane influence (counterfactual, stored points only)", "",
              "The two plane checks are the same measurement at two different moments (mid-back and mid-downswing), "
              "not one measurement counted twice; together they can carry about a quarter of the weight.", ""]
    if counterfactuals:
        lines += ["| Swing | Total without plane | Change vs stored | Checks remaining |", "| --- | --- | --- | --- |"]
        for row in counterfactuals:
            lines.append(f"| {row.get('swing_id', '-')} | {_fmt(row['total_without_plane'], 2)} | "
                         f"{_fmt(row['delta_vs_stored'], 2, '-')} | {row['checks_remaining']} |")
        lines.append("")
    else:
        lines += ["No swing had enough stored checks to remove the plane pair without dropping below the minimum, "
                  "so no leave-plane counterfactual is available.", ""]

    if report["drop_one"]:
        lines += ["## Missing-check / drop-one sensitivity (counterfactual)", "",
                  "Each row removes one stored check from that swing's stored points and recomputes the weighted "
                  "average. This is a counterfactual over a stored snapshot, never a re-run of the model.", "",
                  "| Swing | Largest change |", "| --- | --- |"]
        for row in report["drop_one"]:
            lines.append(f"| {row['swing_id']} | {_fmt(row['max_abs_delta'])} |")
        lines.append("")

    thresholds = report["thresholds"]
    lines += ["## Review flags", "",
              f"Thresholds (also in `coach_review/config.py`): high model / low coach = model >= "
              f"{thresholds['high_model_low_coach']['model_min']:.1f} and coach <= "
              f"{thresholds['high_model_low_coach']['coach_max']:.1f}; low model / high coach = model <= "
              f"{thresholds['low_model_high_coach']['model_max']:.1f} and coach >= "
              f"{thresholds['low_model_high_coach']['coach_min']:.1f}; large difference = |model - coach| >= "
              f"{thresholds['large_difference']:.1f}; low coverage < {thresholds['low_coverage']:.2f}; dominant check "
              f"share >= {thresholds['dominant_share']:.2f}; plane share >= {thresholds['plane_share']:.2f}; similar "
              f"group instability = model spread >= {thresholds['instability_model_spread']:.1f} and coach means within "
              f"{thresholds['instability_coach_closeness']:.1f}.", "",
              "Coverage, dominant-check and plane-share flags come from the stored snapshot alone; the "
              "model-versus-coach and confidence flags additionally need blind coach ratings.", ""]
    if report["flags"]:
        lines += ["| Swing | Flag | Detail |", "| --- | --- | --- |"]
        lines += [f"| {flag['swing_id']} | {flag['kind']} | {flag['detail']} |" for flag in report["flags"]]
    else:
        lines.append("No flags fired: no stored snapshot met the coverage, dominance or plane-share thresholds, and "
                     "no swing had both a usable score and a blind coach rating for the comparison flags.")
    lines.append("")

    lines += ["## Same-golfer / similar-group stability", ""]
    if report["instability"]:
        lines += ["| Group | Kind | Swings | Model totals | Spread | Coach means | Coach spread | Flagged |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for row in report["instability"]:
            lines.append(f"| {row['group']} | {row['kind']} | {', '.join(row['swings'])} | "
                         f"{', '.join(_fmt(t, 1) for t in row['totals'])} | {_fmt(row['spread'])} | "
                         f"{', '.join(_fmt(t, 1) for t in row['coach_means']) or '-'} | {_fmt(row['coach_spread'])} | "
                         f"{'yes' if row['flagged'] else 'no'} |")
        lines += [""] + [f"* {row['group']}: {row['caveat']}" for row in report["instability"]]
        lines.append("")
    else:
        lines += ["No labelled same-golfer or similar-swing group has two scored swings, so no pair evidence "
                  "exists.", ""]

    lines += ["## Limitations and possible causes", "",
              "* The comparison is exploratory: no threshold here validates (or invalidates) the Swing Score.",
              f"* {report['summary']['scale_note']}",
              "* Missing checks can move a total by roughly the share of the missing check; coverage is reported next "
              "to every score.",
              "* Blindness is enforced by the server, but anyone with filesystem access to `var/coach_review` or the "
              "analysis files can read the stored score; this is a trusted local operator tool.",
              "* Reviewer confidence is a different construct from the model's measurement confidence; a mismatch is a "
              "prompt for review, not an error.",
              "",
              "## Recommendations", ""]
    if not summary["meets_planned_minimum"]:
        lines.append(f"* Keep collecting: this pilot planned for {summary['planned_minimum']} blind coach-rated swings "
                     "with stored scores before reading anything into the correlation.")
    lines += ["* Do not change weights, target ranges or confidence handling on this evidence.",
              "* Use the flags above as a work list for reviewing individual swings, not as a quality verdict.",
              ""]
    return "\n".join(lines) + "\n"


def write_report(conn: sqlite3.Connection, *, out: str | Path | None = None, json_out: str | Path | None = None,
                 batch_name: str | None = None, dataset: Mapping | None = None) -> dict:
    from . import store  # local import keeps the module import-light

    payload = build_report(dataset or store.report_dataset(conn, batch_name), batch_name=batch_name)
    markdown = render_markdown(payload)
    if out:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"markdown": markdown, "payload": payload}


__all__ = ["NOT_VALIDATED", "SCALE_NOTE", "build_report", "render_markdown", "write_report"]
