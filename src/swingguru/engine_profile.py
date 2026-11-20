"""Engine profiles: ``current`` (production, unchanged) and ``next`` (1.0.1 candidate fixes).

Selected with ``SG_ENGINE_PROFILE`` (or ``--engine-profile``); anything unset is ``current``, so
production behaviour never changes by accident. ``next`` switches on the audited coverage fixes
(docs/agent-work/analysis-quality/AUDIT.md, section C), each individually so the validation harness
can attribute its effect:

* ``refined_club``   C1  the plane checks read the temporally refined club track (recovered frames
                         have their own image evidence and are capped at MEDIUM);
* ``small_shift``    C2/C3 a confirmed camera registration whose shift at the golfer is below the
                         measurement noise is accepted even when an ankle moved; such checks count
                         as MEDIUM; the foot witness also considers heel and toe;
* ``handedness``     C4  left/right-handed inferred from the down-the-line ball side;
* ``side_confidence`` C5 a one-sided check's confidence uses only that side's joints;
* ``stature``        C6  head / hip movement normalised by a posture-independent stature estimate.

None of them manufactures a measurement: each either uses evidence the engine already had, or
relaxes a veto only where the veto could not distinguish the camera from the golfer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

CURRENT = "current"
NEXT = "next"
# ``candidate``: the next release under development (staging only). Every ``next`` fix plus:
#   address_consensus  the Address shaft reference is checked against the Address +-2 frames and an
#                      isolated outlier is replaced by a neighbouring real detection
#                      (swingguru/club/address_reference.py);
#   observations       extra, unscored replay observations are marked in the bundle
#                      (swingguru/observations.py).
#   plane_neighbour    a plane check whose moment frame has no usable club is measured on the
#                      clear frame immediately beside it, under strict rules, as MEDIUM
#                      (swingguru/club/plane_neighbour.py);
#   slowmo             a slow-motion clip with no swing at its own speed is searched on a rescaled
#                      clock; measurements are unchanged (swingguru/slowmo.py).
#   head_direction     head checks say which way the head moved (down / up, toward / away from the
#                      ball), by how much and, at Impact, in which half of the swing; wording only,
#                      the scored value and verdict are unchanged (swingguru/head_direction.py).
#   address_recheck    EXPERIMENTAL, not part of the candidate profile: an Address more than 0.6 s before
#                      the takeaway is re-checked with the body-wide stillness test. Approved for staging
#                      2026-11-08, then held back: on 2 of the 4 swings it moves, the club is already
#                      moving at the new frame (docs/agent-work/analysis-v2/FIX_address_recheck.md).
#                      Selectable only explicitly (harness: --engine-fixes).
# ``next`` keeps exactly FIXES, so production (SG_ENGINE_PROFILE=next) is unchanged by this code.
CANDIDATE = "candidate"
FIXES = ("refined_club", "small_shift", "handedness", "side_confidence", "stature")
CANDIDATE_FIXES = FIXES + ("address_consensus", "observations", "plane_neighbour", "slowmo", "head_direction")
# Studied but not switched on: only an explicit fix list (the validation harness) can select these.
EXPERIMENTAL_FIXES = ("address_recheck",)
PROFILES = (CURRENT, NEXT, CANDIDATE)


@dataclass(frozen=True)
class EngineProfile:
    name: str = CURRENT
    fixes: frozenset[str] = field(default_factory=frozenset)
    # C2: camera shift at the golfer (% of body height) below which a confirmed registration is
    # trusted without the foot witness. Half the tightest movement band (extension: 2 %).
    small_shift_pct: float = 1.0

    def on(self, fix: str) -> bool:
        return fix in self.fixes

    def to_dict(self) -> dict:
        return {"name": self.name, "fixes": sorted(self.fixes)}


def profile(name: str | None = None, *, only: Sequence[str] | None = None) -> EngineProfile:
    """``current`` (no fixes), ``next`` / ``candidate`` (all their fixes) or with ``only`` a subset (harness)."""

    chosen = (name or os.environ.get("SG_ENGINE_PROFILE") or CURRENT).strip().lower()
    if chosen not in PROFILES:
        raise ValueError(f"unknown engine profile {chosen!r} (expected one of {', '.join(PROFILES)})")
    if chosen == CURRENT:
        return EngineProfile()
    default = CANDIDATE_FIXES if chosen == CANDIDATE else FIXES
    allowed = default + EXPERIMENTAL_FIXES if chosen == CANDIDATE else FIXES
    fixes = frozenset(default if only is None else only)
    unknown = fixes - set(allowed)
    if unknown:
        raise ValueError(f"unknown engine fixes {sorted(unknown)} for profile {chosen}")
    return EngineProfile(chosen, fixes)


# ---------------------------------------------------------------------------- C4 handedness


def infer_handedness(toward_ball: float, toward_ball_known: bool) -> tuple[str, str]:
    """``(handedness, source)`` from the down-the-line ball side.

    Down the line, the camera is behind the hands looking at the target: a right-handed golfer
    stands left of the ball, so the hands sit image-right of the hips (``toward_ball`` +1); a
    left-handed golfer is the mirror image. Unknown ball side keeps the default (right).
    """

    if not toward_ball_known:
        return "right", "default"
    return ("right" if toward_ball > 0 else "left"), "inferred_ball_side"


def rebind_sides(records: Sequence[object], handedness: str) -> None:
    """Point the ``lead_*`` / ``trail_*`` aliases at the right physical side for ``handedness``.

    The aliases are written when the pose records are created (always right-handed today), so a
    left-handed golfer's records are rebound in place before measurements are recomputed.
    """

    lead, trail = ("left", "right") if handedness == "right" else ("right", "left")
    for record in records:
        points = getattr(record, "points", None)
        if points is None:
            continue
        pixels = points.pixels
        for part in ("shoulder", "hip", "knee", "ankle"):
            for alias, side in (("lead", lead), ("trail", trail)):
                source = pixels.get(f"{side}_{part}")
                if source is None:
                    pixels.pop(f"{alias}_{part}", None)
                else:
                    pixels[f"{alias}_{part}"] = source


def physical_side(alias_side: str, handedness: str) -> str:
    """``"trail"`` -> ``"right"`` for a right-handed golfer (``"left"`` for a left-handed one)."""

    if alias_side not in ("lead", "trail"):
        return alias_side
    right_handed = handedness == "right"
    return ("left" if right_handed else "right") if alias_side == "lead" else ("right" if right_handed else "left")


# ---------------------------------------------------------------------------- C6 stature


def stature_px(record: object) -> float | None:
    """Posture-independent standing-height estimate: the sum of the body chain at Address.

    nose -> shoulder midpoint -> hip midpoint -> knee -> ankle (mean of the visible legs). Bending
    over shortens nose-to-ankle but not the chain. Returns None if the chain is incomplete.
    """

    points = getattr(record, "points", None)
    if points is None:
        return None
    px = points.pixels

    def p(name):
        value = px.get(name)
        return None if value is None else np.asarray(value, float)

    nose, shoulder, hip = p("nose"), p("shoulder_mid"), p("hip_mid")
    if nose is None or shoulder is None or hip is None:
        return None
    legs = []
    for side in ("left", "right"):
        h, k, a = p(f"{side}_hip"), p(f"{side}_knee"), p(f"{side}_ankle")
        if h is not None and k is not None and a is not None:
            legs.append(float(np.linalg.norm(k - h) + np.linalg.norm(a - k)))
    if not legs:
        return None
    return float(np.linalg.norm(shoulder - nose) + np.linalg.norm(hip - shoulder) + float(np.mean(legs)))


# ---------------------------------------------------------------------------- reason codes

REASON_CODES: tuple[tuple[str, str], ...] = (
    ("camera moved", "camera_uncorrected"),
    ("club not detected", "club_not_detected"),
    ("club detection confidence", "club_low_confidence"),
    ("club tracking not run", "club_not_run"),
    ("moment not found", "moment_missing"),
    ("pose confidence", "pose_low_confidence"),
    ("could not be measured", "pose_points_missing"),
    ("could not be compared", "pose_points_missing"),
    ("ball could not be told", "ball_side_unknown"),
    ("ball side", "ball_side_unknown"),
    ("hands not below", "hands_above_shoulder"),
    ("no address moment", "moment_missing"),
)


def reason_code(reason: str) -> str:
    """A stable code for a withheld check's reason text (diagnostics and the harness)."""

    text = (reason or "").lower()
    for needle, code in REASON_CODES:
        if needle in text:
            return code
    return "other"


__all__ = ["CANDIDATE", "CANDIDATE_FIXES", "CURRENT", "FIXES", "NEXT", "PROFILES", "EngineProfile", "infer_handedness", "physical_side", "profile",
           "reason_code", "rebind_sides", "stature_px"]
