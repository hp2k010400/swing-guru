"""Small, explicit statistics for the validation report.

Nothing here rescales the two scales: the model score is 0-10 and the coach
ratings are 1-10, so the comparison happens in point units and the report calls
out the floor difference. Every function returns ``None`` (never a made-up
number) when the sample cannot support the statistic.
"""

from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

from . import config as C


def _clean(values: Iterable[float | int | None]) -> list[float]:
    return [float(value) for value in values if value is not None and not isinstance(value, bool)]


def describe(values: Sequence[float | int | None]) -> dict:
    """Count, min, max, mean, median and sample standard deviation (None when undefined)."""

    clean = _clean(values)
    if not clean:
        return {"n": 0, "min": None, "max": None, "mean": None, "median": None, "sd": None}
    ordered = sorted(clean)
    mean = sum(clean) / len(clean)
    if len(clean) % 2:
        median = ordered[len(clean) // 2]
    else:
        median = (ordered[len(clean) // 2 - 1] + ordered[len(clean) // 2]) / 2
    sd = math.sqrt(sum((value - mean) ** 2 for value in clean) / (len(clean) - 1)) if len(clean) > 1 else None
    return {"n": len(clean), "min": round(ordered[0], 3), "max": round(ordered[-1], 3), "mean": round(mean, 3),
            "median": round(median, 3), "sd": None if sd is None else round(sd, 3)}


def ranks(values: Sequence[float]) -> list[float]:
    """1-based average ranks, so ties share the mean of the positions they occupy."""

    order = sorted(range(len(values)), key=lambda index: values[index])
    out = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index
        while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
            end += 1
        average = (index + end) / 2 + 1
        for position in range(index, end + 1):
            out[order[position]] = average
        index = end + 1
    return out


def _variance(values: Sequence[float]) -> float:
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values)


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Pearson correlation, or None when the pairs are too few or either side is constant."""

    if len(xs) != len(ys) or len(xs) < C.MIN_PAIRS_FOR_CORRELATION:
        return None
    if _variance(xs) == 0 or _variance(ys) == 0:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denominator = math.sqrt(_variance(xs) * _variance(ys))
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, numerator / denominator))


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Tie-aware Spearman rho: Pearson correlation of average ranks."""

    if len(xs) != len(ys) or len(xs) < C.MIN_PAIRS_FOR_CORRELATION:
        return None
    return pearson(ranks(xs), ranks(ys))


def mean_absolute_error(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or not xs:
        return None
    return sum(abs(x - y) for x, y in zip(xs, ys)) / len(xs)


def check_weight(check: Mapping) -> float | None:
    """The check's stored weight, exactly as imported. Never guessed from another field."""

    weight = check.get("weight")
    if isinstance(weight, (int, float)) and not isinstance(weight, bool):
        return float(weight)
    return None


def normalized_shares(checks: Sequence[Mapping]) -> list[float | None]:
    """Each check's share of the score, from the *actual* stored weights (sum = 1).

    The stored ``share`` field is a rounded copy; it is reported separately, never used
    to reconstruct contributions.
    """

    weights = [check_weight(check) for check in checks]
    if not weights or any(weight is None for weight in weights):
        return [None for _ in checks]
    total = sum(float(weight) for weight in weights)
    if total <= 0:
        return [None for _ in checks]
    return [float(weight) / total for weight in weights]


def check_contribution(check: Mapping, share: float | None) -> float | None:
    """points x share: this check's contribution in score points."""

    points = check.get("points")
    if share is None or not isinstance(points, (int, float)) or isinstance(points, bool):
        return None
    return float(share) * float(points)


def weighted_total(checks: Sequence[Mapping]) -> float | None:
    """Sum(w x points) / sum(w) over the given checks, from their stored values only."""

    weights = [check_weight(check) for check in checks]
    if not weights or any(weight is None for weight in weights):
        return None
    total_weight = sum(float(weight) for weight in weights)
    if total_weight <= 0:
        return None
    total = 0.0
    for check, weight in zip(checks, weights):
        points = check.get("points")
        if not isinstance(points, (int, float)) or isinstance(points, bool):
            return None
        total += float(weight) * float(points)
    return total / total_weight


def group_points(checks: Sequence[Mapping], ids: set[str]) -> float | None:
    """The weighted points of a subset of checks (the group's own score contribution)."""

    subset = [check for check in checks if check.get("id") in ids]
    return weighted_total(subset)


def drop_one_totals(score: Mapping, minimum_checks: int) -> list[dict]:
    """Counterfactual totals with one stored check removed (stored points only, never recomputed)."""

    checks = [c for c in (score.get("checks") or []) if isinstance(c, Mapping)]
    out = []
    for dropped in checks:
        remaining = [c for c in checks if c is not dropped]
        total_weight = sum(float(c.get("weight") or 0.0) for c in remaining)
        if len(remaining) < minimum_checks or total_weight <= 0:
            out.append({"id": dropped.get("id"), "name": dropped.get("name"), "total_without": None,
                        "delta": None, "reason": "fewer than the minimum checks would remain"})
            continue
        total = sum(float(c.get("weight") or 0.0) * float(c.get("points") or 0.0) for c in remaining) / total_weight
        total = max(0.0, min(float(score.get("scale") or 10.0), total))
        base = score.get("total")
        delta = None if not isinstance(base, (int, float)) or isinstance(base, bool) else total - float(base)
        out.append({"id": dropped.get("id"), "name": dropped.get("name"), "total_without": round(total, 3),
                    "delta": None if delta is None else round(delta, 3), "reason": None})
    return out


__all__ = ["check_contribution", "check_weight", "describe", "drop_one_totals", "group_points",
           "mean_absolute_error", "normalized_shares", "pearson", "ranks", "spearman", "weighted_total"]
