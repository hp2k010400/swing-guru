"""Swing Score v3 (development; not the production model). Same checks, bands and coverage rule as
v2, combined differently. Every number lives in ``config`` (``V3_*``).

For each measured check i (value x, target band [lo, hi], "slightly out" band width w on the side x
falls, measurement noise s):

    e   = how far x lies outside the target band (0 inside);  d = e / w  ("slightly out" band widths)
    q(x)= 0.95..1 inside the band, else FLOOR + (0.95 - FLOOR) * exp(-K * d ** P)
    Q   = max(q(x), E[q(x + err)]), err ~ Normal(0, s)
          noise can only soften a deduction (a borderline reading might really be in range); it never
          adds one to a reading that is in range
    l_i = weight_i x confidence_i x (1 - Q)      the check's deduction (confidence: HIGH 1, MEDIUM 0.5)

Classification (reporting): "within_noise" when x is outside the band by less than s, else
"slightly_out" (d < 1) or "clearly_out" (d >= 1, a significant fault, using the raw reading).

Families of related checks (``V3_FAMILIES``): family deduction = largest l + RHO x the others.
An unmeasured check adds its neutral deduction (half weight at 6.0, as in v2).

    D     = (sum of family deductions + neutral deductions) / (sum of weights used)
    score = 10 x (1 - D) - MULTI x max(0, families with a significant fault - 1)

A fault is significant when d >= 1 (beyond the "slightly out" band even after noise). Rounded half
up to 1 decimal, clamped to 0-10. Needs >= 5 measured checks including a downswing/impact one.
"""

from __future__ import annotations

import math
from typing import Mapping

from . import config as C
from .model import SCHEMA, _measured, _round


def _point_quality(check: C.ScoredCheck, value: float) -> tuple[float, float, float]:
    """(q, d, deviation) at an exact value."""

    low, high = check.good
    inside = (low is None or value >= low) and (high is None or value < high)
    if inside:
        edge = high if value >= check.ideal else low
        if edge is None or edge == check.ideal:
            return 1.0, 0.0, 0.0
        frac = min(1.0, abs(value - check.ideal) / abs(edge - check.ideal))
        return 1.0 - (1.0 - C.V3_EDGE_QUALITY) * frac ** 2, 0.0, 0.0
    if high is not None and value >= high:
        deviation, width = value - high, check.slight_high - high
    else:
        deviation, width = low - value, low - check.slight_low
    d = deviation / width
    return C.V3_FLOOR + (C.V3_EDGE_QUALITY - C.V3_FLOOR) * math.exp(-C.V3_K * d ** C.V3_P), d, deviation


def _expected_quality(check: C.ScoredCheck, value: float, noise: float) -> float:
    """E[q(value + err)] for err ~ Normal(0, noise): a fixed, deterministic quadrature."""

    if noise <= 0:
        return _point_quality(check, value)[0]
    n = C.V3_NOISE_SAMPLES
    total = weight_sum = 0.0
    for k in range(n):
        z = -4.0 + 8.0 * k / (n - 1)
        w = math.exp(-0.5 * z * z)
        total += w * _point_quality(check, value + z * noise)[0]
        weight_sum += w
    return total / weight_sum


def _quality(check: C.ScoredCheck, value: float, noise: float) -> tuple[float, float, str, float]:
    """(expected q, d, class, deviation) for one measurement."""

    point, d, deviation = _point_quality(check, value)
    q = max(point, _expected_quality(check, value, noise))
    if deviation <= 0:
        return q, 0.0, "in_range", 0.0
    if deviation < noise:
        return q, d, "within_noise", deviation
    return q, d, ("clearly_out" if d >= C.V3_SIGNIFICANT_D else "slightly_out"), deviation


def score_v3(analysis: Mapping) -> dict:
    from ..app_model import withheld

    model = C.MODELS[C.V3_VERSION]
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
        noise = C.V3_NOISE[check.id] * (C.V3_MEDIUM_NOISE_FACTOR if m["level"] == "MEDIUM" else 1.0)
        q, d, cls, deviation = _quality(check, m["value"], noise)
        weight = model.weights[check.id] * confidence
        checks.append({
            "id": check.id, "phase": check.phase, "name": check.name, "measurement": m["value"], "unit": check.unit,
            "target": {"from": check.good[0], "to": check.good[1], "ideal": check.ideal},
            "status": m["status"], "label": m["label"], "confidence": m["level"], "estimated": m["estimated"],
            "noise": round(noise, 3), "deviation": round(deviation, 3), "d": round(d, 3), "class": cls,
            "points": _round(10.0 * q, "0.001"), "weight": weight, "loss": weight * (1.0 - q),
        })
    by_id = {c["id"]: c for c in checks}
    families, total_loss = [], 0.0
    significant = 0
    for key, family in C.V3_FAMILIES.items():
        losses = sorted((by_id[i]["loss"] for i in family["checks"] if i in by_id), reverse=True)
        loss = (losses[0] + family["rho"] * sum(losses[1:])) if losses else 0.0
        has_significant = any(by_id[i]["class"] == "clearly_out" for i in family["checks"] if i in by_id)
        significant += int(has_significant)
        total_loss += loss
        families.append({"id": key, "label": family["label"], "measured": [i for i in family["checks"] if i in by_id],
                         "loss": round(loss, 3), "independent_sum": round(sum(losses), 3),
                         "significant_fault": has_significant})
    neutral_weight = sum(model.weights[e["id"]] * model.unmeasured_weight for e in excluded)
    neutral_loss = neutral_weight * (1.0 - model.unmeasured_points / C.SCALE)
    for e in excluded:
        e["weight"] = model.weights[e["id"]] * model.unmeasured_weight
        e["points"] = model.unmeasured_points
    weight_used = sum(c["weight"] for c in checks) + neutral_weight
    multi = C.V3_MULTI_FAULT * max(0, significant - 1)
    for c in checks:
        c["share"] = _round(c["weight"] / weight_used, "0.001") if weight_used else 0.0
        c["loss"] = round(c["loss"], 3)
    medium = sum(1 for c in checks if c["confidence"] == "MEDIUM")
    result = {
        "schema": SCHEMA, "version": C.V3_VERSION, "source": "model", "scale": C.SCALE,
        "measured": len(checks), "eligible": len(C.CHECKS),
        "coverage": _round(len(checks) / len(C.CHECKS), "0.01"), "minimum": C.MIN_MEASURED,
        "confidence": {"high": len(checks) - medium, "medium": medium,
                       "label": None if not checks else "HIGH" if medium == 0 else "MEDIUM"},
        "checks": checks, "excluded": excluded, "families": families,
        "deductions": {"families": round(total_loss, 3), "neutral": round(neutral_loss, 3),
                       "weight_used": round(weight_used, 3), "significant_families": significant,
                       "multi_fault_penalty": multi},
        "weights": dict(model.weights), "unmeasured": {"points": model.unmeasured_points, "weight": model.unmeasured_weight},
        "method": C.V3_METHOD,
    }
    motion = any(c["id"] in model.needs_motion for c in checks)
    if len(checks) < C.MIN_MEASURED or not motion:
        return {**result, "available": False, "total": None, "reason": C.NOT_ENOUGH}
    raw = 10.0 * (1.0 - (total_loss + neutral_loss) / weight_used) - multi
    return {**result, "available": True, "total": _round(min(C.SCALE, max(0.0, raw))), "reason": None}


__all__ = ["score_v3"]
