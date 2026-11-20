"""Swing Score: a deterministic, explainable 0-10 score for one analysed swing.

Reads the analysis (with verdicts attached) and nothing else; never changes a
measurement. Formula, for the score-eligible checkpoints in ``config.CHECKS`` and
a model in ``config.MODELS``:

    points_i      = curve(measurement_i)                       0..10, see ``points``
    w_i           = model weight x confidence weight          (HIGH 1, MEDIUM 0.5, LOW excluded)
    u_j           = model weight x unmeasured_weight           for each check not measured (V2: half)
    Swing Score   = (sum(w_i * points_i) + sum(u_j) * neutral) / (sum(w_i) + sum(u_j))

rounded half-up to 1 decimal. V1 has no neutral term (missing checks simply left
out); V2 counts them at a neutral 6.0 with half weight. Fewer than
``MIN_MEASURED`` contributing checks, or (V2) no downswing / impact check -> no score.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping

from . import config as C

SCHEMA = "swing_score/1"


def points(check: C.ScoredCheck, value: float) -> float:
    """0-10 for one measurement: 10 at the ideal, 9 at the range edge, 4 at the slightly-out edge, then 0."""

    low, high = check.good
    inside = (low is None or value >= low) and (high is None or value < high)
    if inside:
        edge = high if value >= check.ideal else low
        if edge is None:  # the open side of a one-sided range (e.g. hips further from the ball)
            return C.POINTS_IDEAL
        fraction = abs(value - check.ideal) / abs(edge - check.ideal)
        return C.POINTS_IDEAL - (C.POINTS_IDEAL - C.POINTS_RANGE_EDGE) * fraction
    if high is not None and value >= high:
        distance, width = value - high, (check.slight_high - high)
    else:
        distance, width = low - value, (low - check.slight_low)
    if distance <= width:
        return C.POINTS_RANGE_EDGE - (C.POINTS_RANGE_EDGE - C.POINTS_SLIGHT_EDGE) * distance / width
    beyond = (distance - width) / (width * C.ZERO_AFTER_WIDTHS)
    return max(0.0, C.POINTS_SLIGHT_EDGE * (1.0 - beyond))


def _round(value: float, places: str = "0.1") -> float:
    return float(Decimal(repr(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _measured(analysis: Mapping) -> dict[str, dict]:
    """The guided checkpoint measurements, by check id: value, status and the engine's confidence level."""

    out = {}
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            verdict = check.get("verdict") or {}
            mapping = verdict.get("mapping") or {}
            if not check.get("guided") or mapping.get("input") is None:
                continue
            out[f"{phase['phase']}.{check['kind']}"] = {
                "value": float(mapping["input"]), "mapping": mapping.get("id"), "status": verdict.get("status"),
                "label": verdict.get("label"), "level": check.get("level"), "estimated": bool(check.get("estimated")),
            }
    return out


def score_swing(analysis: Mapping, version: str = C.VERSION) -> dict:
    """The Swing Score result for one analysis (verdicts must already be attached), with model ``version``."""

    from ..app_model import withheld

    if version == C.V3_VERSION:
        # Development model (validation only): its own combination, see scoring/v3.py.
        from .v3 import score_v3

        return score_v3(analysis)
    model = C.MODELS[version]
    measured = _measured(analysis)
    reasons = {w["id"]: w["reason"] for w in withheld(analysis)}
    checks, excluded = [], []
    for check in C.CHECKS:
        m = measured.get(check.id)
        if m is None or m["mapping"] != check.mapping_id:
            excluded.append({"id": check.id, "phase": check.phase, "name": check.name,
                             "reason": reasons.get(check.id, "Not measured on this video")})
            continue
        confidence = C.CONFIDENCE_WEIGHT.get(m["level"] or "")
        if confidence is None:
            excluded.append({"id": check.id, "phase": check.phase, "name": check.name,
                             "reason": "Measured, but not reliably enough to score"})
            continue
        pts = _round(points(check, m["value"]), "0.001")
        checks.append({
            "id": check.id, "phase": check.phase, "name": check.name, "measurement": m["value"], "unit": check.unit,
            "target": {"from": check.good[0], "to": check.good[1], "ideal": check.ideal},
            "status": m["status"], "label": m["label"], "confidence": m["level"], "estimated": m["estimated"],
            "points": pts, "weight": model.weights[check.id] * confidence,
        })
    unmeasured = 0.0
    if model.unmeasured_points is not None:
        for e in excluded:
            e["weight"] = model.weights[e["id"]] * model.unmeasured_weight
            e["points"] = model.unmeasured_points
            unmeasured += e["weight"]
    total_weight = sum(c["weight"] for c in checks) + unmeasured
    for c in checks:
        c["share"] = _round(c["weight"] / total_weight, "0.001") if total_weight else 0.0
    for e in excluded:
        if "weight" in e:
            e["share"] = _round(e["weight"] / total_weight, "0.001") if total_weight else 0.0
    medium = sum(1 for c in checks if c["confidence"] == "MEDIUM")
    result = {
        "schema": SCHEMA,
        "version": model.version,
        "source": "model",
        "scale": C.SCALE,
        "measured": len(checks),
        "eligible": len(C.CHECKS),
        "coverage": _round(len(checks) / len(C.CHECKS), "0.01"),
        "minimum": C.MIN_MEASURED,
        "confidence": {"high": len(checks) - medium, "medium": medium,
                       "label": None if not checks else "HIGH" if medium == 0 else "MEDIUM"},
        "checks": checks,
        "excluded": excluded,
        "method": model.method,
    }
    if model.unmeasured_points is not None:
        result["weights"] = dict(model.weights)
        result["unmeasured"] = {"points": model.unmeasured_points, "weight": model.unmeasured_weight}
    motion = not model.needs_motion or any(c["id"] in model.needs_motion for c in checks)
    if len(checks) < C.MIN_MEASURED or not motion:
        return {**result, "available": False, "total": None, "reason": C.NOT_ENOUGH}
    total = (sum(c["weight"] * c["points"] for c in checks) + unmeasured * (model.unmeasured_points or 0.0)) / total_weight
    return {**result, "available": True, "total": _round(min(C.SCALE, max(0.0, total))), "reason": None}


__all__ = ["SCHEMA", "points", "score_swing"]
