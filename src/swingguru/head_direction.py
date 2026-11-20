"""Direction-aware head movement feedback (engine profile ``candidate``, fix ``head_direction``).

Wording only. The scored number (``raw.movement_pct_height``), the verdict label, its colour and the
Swing Score are untouched: this splits the same movement into the parts a down-the-line camera can
see and says which ones moved, by how much, and (at Impact) in which part of the swing.

What one down-the-line camera can measure:

* **down / up**: image vertical;
* **toward / away from the ball**: image horizontal, signed by the ball side (needs it known);
* **toward / away from the target** is depth along the camera axis, so it is never reported.

A part is only named when it is at least ``COMPONENT_MIN_PCT`` (the head's frame-to-frame noise on the
batch, ``observations.MAX_FRAME_NOISE["head"]``), and the timing only when one half of the swing holds
at least ``TIMING_SHARE`` of a movement of at least ``TIMING_MIN_PCT``, both moments are measured and
both are in the same camera state. Otherwise the feedback says less rather than guess.
"""

from __future__ import annotations

import math
from typing import Mapping

COMPONENT_MIN_PCT = 1.0
TIMING_MIN_PCT = 2.0
TIMING_SHARE = 2.0 / 3.0
DEPTH_NOTE = "Movement toward or away from the target can't be seen from a down-the-line camera."
SEGMENTS = {"backswing": "between Address and the Top", "downswing": "between the Top and Impact"}


def components(check: Mapping, toward_ball: float | None) -> dict | None:
    """Down (+) / toward-ball (+) parts of a head check's movement, in % of height; None if not measured."""

    raw = check.get("raw") or {}
    pct, dx, dy = raw.get("movement_pct_height"), raw.get("dx_px"), raw.get("dy_px")
    if pct is None or dx is None or dy is None:
        return None
    size = math.hypot(float(dx), float(dy))
    if size == 0:
        return {"down_pct": 0.0, "toward_ball_pct": 0.0 if toward_ball else None, "total_pct": float(pct)}
    scale = float(pct) / size
    return {
        "down_pct": round(float(dy) * scale, 2),
        "toward_ball_pct": None if not toward_ball else round(float(dx) * float(toward_ball) * scale, 2),
        "total_pct": round(float(pct), 2),
    }


def _parts(c: Mapping) -> list[str]:
    """The measurable parts, larger first; only the first carries "of your height"."""

    found = []
    down = c["down_pct"]
    if abs(down) >= COMPONENT_MIN_PCT:
        found.append((abs(down), "dropped" if down > 0 else "rose", ""))
    ball = c.get("toward_ball_pct")
    if ball is not None and abs(ball) >= COMPONENT_MIN_PCT:
        found.append((abs(ball), "moved", f" {'toward' if ball > 0 else 'away from'} the ball"))
    found.sort(key=lambda p: -p[0])
    return [f"{verb} {size:.1f}%{' of your height' if i == 0 else ''}{where}"
            for i, (size, verb, where) in enumerate(found)]


def _timing(top: Mapping | None, impact: Mapping) -> dict | None:
    """Which half of the swing holds most of the larger movement part, when that is clear."""

    if top is None:
        return None
    keys = [k for k in ("down_pct", "toward_ball_pct") if impact.get(k) is not None and top.get(k) is not None]
    if not keys:
        return None
    key = max(keys, key=lambda k: abs(impact[k]))
    total = impact[key]
    if abs(total) < TIMING_MIN_PCT:
        return None
    back, down = top[key], total - top[key]
    for segment, part in (("backswing", back), ("downswing", down)):
        if part * total > 0 and abs(part) >= TIMING_SHARE * abs(total):
            return {"component": key, "segment": segment, "share": round(part / total, 2)}
    return None


def _sentence(c: Mapping, timing: Mapping | None, phase_label: str) -> str:
    parts = _parts(c)
    if not parts:
        return f"By {phase_label} your head stayed within {COMPONENT_MIN_PCT:.0f}% of your height of its Address position."
    text = f"By {phase_label} your head {' and '.join(parts)}, compared with Address"
    if timing:
        what = "drop" if timing["component"] == "down_pct" and c["down_pct"] > 0 else \
            "rise" if timing["component"] == "down_pct" else "movement"
        text += f"; most of the {what} came {SEGMENTS[timing['segment']]}"
    return text + "."


def _same_camera(a: Mapping, b: Mapping) -> bool:
    return a.get("camera") == b.get("camera")


def describe_head_direction(analysis: dict, toward_ball: float | None) -> None:
    """Add ``check["head_direction"]`` and direction-aware ``verdict.support`` to every head check."""

    labels = {"takeaway": "Takeaway", "mid_back": "Mid-Back", "top": "the Top", "impact": "Impact"}
    heads = {p["phase"]: c for p in analysis.get("phases", []) for c in p.get("checks", []) if c.get("kind") == "head"}
    measured = {name: components(c, toward_ball) for name, c in heads.items()}
    for name, check in heads.items():
        c = measured[name]
        if c is None:
            continue
        timing = None
        if name == "impact" and "top" in heads and measured.get("top") is not None and _same_camera(heads["top"], check):
            timing = _timing(measured["top"], c)
        sentence = _sentence(c, timing, labels.get(name, name))
        check["head_direction"] = {
            **c,
            "toward_target_pct": None,
            "toward_target_note": DEPTH_NOTE,
            "timing": timing,
            "text": sentence,
            "component_min_pct": COMPONENT_MIN_PCT,
        }
        verdict = check.get("verdict")
        if isinstance(verdict, dict):
            verdict["support_before_direction"] = verdict.get("support")
            verdict["support"] = sentence
        check["detail"] = sentence


__all__ = ["COMPONENT_MIN_PCT", "components", "describe_head_direction"]
