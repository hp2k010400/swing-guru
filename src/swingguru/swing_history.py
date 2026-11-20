"""Swing history: compact per-swing records, fair two-swing comparisons, and duplicate detection.

Reads stored analyses only; never measures or rescores anything. A comparison only calls a check
"improved" or "worse" when both swings measured it reliably and the change is larger than the
measurement noise (``scoring.config.V3_NOISE``, doubled for two separate recordings, never below a
practical minimum). Everything else is "about the same" or "can't compare", with the reason.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .app_model import PHASE_LABELS, checkpoints
from .scoring.config import CHECKS, V3_MEDIUM_NOISE_FACTOR, V3_NOISE

RECORD_VERSION = 2          # 2: measured-check count and video size, for rank eligibility
MIN_CHANGE = {"deg": 3.0, "%": 1.0}
SCORE_MIN_CHANGE = 0.3
PLANE_CHECKS = {"mid_back.shaft", "downswing.shaft"}
_SCORED = {c.id: c for c in CHECKS}

CLUB_GROUPS = {
    "woods": ("Driver", "3 Wood", "5 Wood", "7 Wood"),
    "hybrids": ("2 Hybrid", "3 Hybrid", "4 Hybrid", "5 Hybrid", "Hybrid"),
    "irons": ("2 Iron", "3 Iron", "4 Iron", "5 Iron", "6 Iron", "7 Iron", "8 Iron", "9 Iron", "Irons"),
    "wedges": ("Pitching Wedge", "Gap Wedge", "Sand Wedge", "Lob Wedge", "Wedges"),
}


def club_group(club: str | None) -> str | None:
    return next((g for g, clubs in CLUB_GROUPS.items() if club in clubs), None)


# ---------------------------------------------------------------- records

def record(analysis: Mapping, score: Mapping | None = None) -> dict:
    """The compact per-swing record kept in the swing row's ``summary`` (no measurement is recomputed)."""

    from .app_model import swing_score

    score = score or swing_score(analysis)
    checks = []
    for c in checkpoints(analysis):
        mapping = _mapping_input(analysis, c["id"])
        if mapping is None:
            continue
        value, unit = mapping
        checks.append({"id": c["id"], "phase": c["phase"], "name": c["name"], "status": c["status"],
                       "label": c["label"], "value": round(value, 2), "unit": unit,
                       "confidence": c.get("confidence"), "estimated": bool(c.get("estimated"))})
    w, h = analysis.get("width"), analysis.get("height")
    return {"version": RECORD_VERSION,
            "score": {"available": bool(score.get("available")), "total": score.get("total"),
                      "version": score.get("version"), "measured": score.get("measured"),
                      "eligible": score.get("eligible")},
            "video": {"long_side": max(int(w), int(h)) if w and h else None},
            "checks": checks, "fingerprint": fingerprint(score.get("total"), checks)}


def _mapping_input(analysis: Mapping, check_id: str) -> tuple[float, str] | None:
    phase, kind = check_id.split(".", 1)
    for p in analysis.get("phases", []):
        if p.get("phase") != phase:
            continue
        for check in p.get("checks", []):
            if check.get("kind") == kind:
                m = (check.get("verdict") or {}).get("mapping") or {}
                if m.get("input") is not None:
                    return float(m["input"]), m.get("unit", "")
    return None


def fingerprint(total: float | None, checks: Sequence[Mapping]) -> str:
    """Identical analyses (the same video uploaded again) give the same fingerprint."""

    parts = [f"{total:.1f}" if total is not None else "-"]
    parts += [f"{c['id']}={c['value']:.1f}" for c in sorted(checks, key=lambda c: c["id"])]
    return "|".join(parts)


def near_duplicate(a: Mapping, b: Mapping) -> bool:
    """Same swing re-uploaded (possibly re-trimmed): every shared check within noise, same score."""

    if a.get("fingerprint") and a.get("fingerprint") == b.get("fingerprint"):
        return True
    sa, sb = (a.get("score") or {}).get("total"), (b.get("score") or {}).get("total")
    if sa is None or sb is None or abs(sa - sb) > 0.1:
        return False
    va = {c["id"]: c["value"] for c in a.get("checks", [])}
    vb = {c["id"]: c["value"] for c in b.get("checks", [])}
    shared = set(va) & set(vb)
    if len(shared) < 4 or len(shared) < 0.8 * max(len(va), len(vb)):
        return False
    return all(abs(va[k] - vb[k]) <= max(0.5, V3_NOISE.get(k, 0.5)) for k in shared)


# ---------------------------------------------------------------- comparison

def _distance(check_id: str, value: float) -> float:
    """How far a reading sits outside its target band (0 inside), in the check's units."""

    scored = _SCORED.get(check_id)
    if scored is None:
        return 0.0
    low, high = scored.good
    if low is not None and value < low:
        return low - value
    if high is not None and value > high:
        return value - high
    return 0.0


def threshold(check_id: str, unit: str, confidence: Sequence[str | None]) -> float:
    noise = V3_NOISE.get(check_id, 0.5) * (V3_MEDIUM_NOISE_FACTOR if "MEDIUM" in confidence else 1.0)
    return max(2 * noise, MIN_CHANGE.get(unit, 1.0))


def compare(a: Mapping, b: Mapping, *, club_a: str | None = None, club_b: str | None = None) -> dict:
    """Compare record ``a`` (earlier / baseline) with record ``b`` (later). Never claims more than measured."""

    ga, gb = club_group(club_a), club_group(club_b)
    notes = []
    if ga and gb and ga != gb:
        notes.append("Different club types: plane checks aren't compared, and set-up naturally differs a little.")
    elif not (ga and gb):
        notes.append("One swing has no club set, so we can't check the clubs match.")
    by_a = {c["id"]: c for c in a.get("checks", [])}
    by_b = {c["id"]: c for c in b.get("checks", [])}
    rows = []
    for scored in CHECKS:
        ca, cb = by_a.get(scored.id), by_b.get(scored.id)
        row = {"id": scored.id, "phase": scored.phase, "phase_label": PHASE_LABELS.get(scored.phase, scored.phase),
               "name": scored.name, "a": _side(ca), "b": _side(cb)}
        if ca is None or cb is None:
            row.update(result="not_comparable", reason="Not measured on " + (
                "either swing" if ca is None and cb is None else "the first swing" if ca is None else "the second swing"))
        elif scored.id in PLANE_CHECKS and ga and gb and ga != gb:
            row.update(result="not_comparable", reason="Different club types")
        else:
            limit = threshold(scored.id, scored.unit, (ca.get("confidence"), cb.get("confidence")))
            da, db = _distance(scored.id, ca["value"]), _distance(scored.id, cb["value"])
            change = da - db                          # > 0: the later swing is closer to its target
            if da == 0 and db == 0:
                row.update(result="same", reason="In range on both swings")
            elif abs(change) < limit:
                row.update(result="same", reason=f"Change smaller than measurement noise (±{limit:g}{_u(scored.unit)})")
            else:
                row.update(result="improved" if change > 0 else "worse",
                           reason=f"{abs(change):.0f}{_u(scored.unit)} {'closer to' if change > 0 else 'further from'} "
                                  f"the target range")
            row["threshold"] = limit
        rows.append(row)
    score = _score_change(a.get("score") or {}, b.get("score") or {})
    counts = {k: sum(1 for r in rows if r["result"] == k) for k in ("improved", "worse", "same", "not_comparable")}
    return {"score": score, "rows": rows, "counts": counts, "notes": notes,
            "duplicate": near_duplicate(a, b)}


def _u(unit: str) -> str:
    return "°" if unit == "deg" else "%"


def _side(c: Mapping | None) -> dict | None:
    if c is None:
        return None
    return {k: c.get(k) for k in ("status", "label", "value", "unit", "confidence", "estimated")}


def _score_change(a: Mapping, b: Mapping) -> dict:
    ta, tb = a.get("total"), b.get("total")
    if ta is None or tb is None:
        return {"a": ta, "b": tb, "delta": None, "result": "not_comparable", "reason": "No Swing Score on one swing"}
    if a.get("version") != b.get("version"):
        return {"a": ta, "b": tb, "delta": None, "result": "not_comparable",
                "reason": "Scored with different Swing Score versions"}
    delta = round(tb - ta, 1)
    result = "same" if abs(delta) < SCORE_MIN_CHANGE else "improved" if delta > 0 else "worse"
    return {"a": ta, "b": tb, "delta": delta, "result": result, "reason": None}


__all__ = ["CLUB_GROUPS", "RECORD_VERSION", "club_group", "compare", "fingerprint", "near_duplicate", "record",
           "threshold"]
