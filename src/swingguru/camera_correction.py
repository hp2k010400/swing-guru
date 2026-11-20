"""Camera-drift correction for Address-relative positions (head, hips, rear pelvis).

The camera check registers the background of each later key frame to Address
with a similarity transform ``M`` (pan, zoom, roll; ``p_later = M p_address``).
A body point measured in the later frame is expressed in Address camera
coordinates with ``M^-1`` and then compared with its Address reference, so
camera drift no longer reads as body movement. Angles (posture, shaft) are
unaffected by the correction and are not touched.

A background transform is exact for camera rotation and only approximate for
camera translation (parallax: the golfer is nearer than the background). So a
correction is trusted only when an independent witness at the golfer's own
depth agrees: a planted foot. After correction, the foot that moved least
(the trail foot at the Top, the lead foot through Impact) must be back within
``foot_tolerance`` of its Address position. The tolerance is set just above
what tripod clips show for genuine foot motion plus pose noise (robustness
batch: at most 1.66 % of body height), so a correction that leaves the feet
drifting further is not trusted.

Per phase status (fails closed):

* ``VALID`` - the camera did not move; positions are used as measured;
* ``CORRECTED_VALID`` - the camera moved and the correction passed every test;
* ``UNCERTAIN`` - measurable but not confirmed (feet disagree, high residual,
  missing ankles); positions stay withheld;
* ``INVALID`` - no reliable registration, or an implausible transform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

VALID = "VALID"
CORRECTED_VALID = "CORRECTED_VALID"
UNCERTAIN = "UNCERTAIN"
INVALID = "INVALID"

PHASES = ("top", "downswing", "impact")


@dataclass(frozen=True)
class CorrectionConfig:
    max_residual_px: float = 1.5      # median reprojection error of background inliers
    max_rotation_deg: float = 2.0     # a handheld phone rolls a little, not a lot
    scale_range: tuple[float, float] = (0.95, 1.05)
    max_shift_share: float = 0.15     # of frame height
    foot_tolerance_pct: float = 2.0   # planted foot back at Address after correction, % body height
    # Engine profile ``next`` only (None / ankle = production behaviour):
    # * small_shift_pct: when the registration itself passed and the camera shift at the golfer is
    #   below this (% of body height), a foot that moved by itself cannot tell camera from golfer,
    #   so it no longer vetoes the correction (``accepted_by = "small_shift"``, checks count MEDIUM);
    # * foot_parts: the witness uses the most-planted point of each foot (ankle, heel or toe).
    small_shift_pct: float | None = None
    foot_parts: tuple[str, ...] = ("ankle",)


def to_address(point, matrix) -> np.ndarray:
    """A later-frame point in Address camera coordinates (inverse similarity)."""

    m = np.asarray(matrix, float)
    return np.linalg.solve(m[:, :2], np.asarray(point, float) - m[:, 2])


def to_later(point, matrix) -> np.ndarray:
    """An Address-frame point drawn in the later frame (forward similarity)."""

    m = np.asarray(matrix, float)
    return m[:, :2] @ np.asarray(point, float) + m[:, 2]


def _px(record, name):
    points = getattr(record, "points", None)
    value = None if points is None else points.pixels.get(name)
    return None if value is None else np.asarray(value, float)


def planted_foot_residual(address, later, matrix, body_height_px: float,
                          parts: tuple[str, ...] = ("ankle",)) -> float | None:
    """Smallest corrected foot-point displacement (% of body height), or None."""

    moved = []
    for side in ("left", "right"):
        for part in parts:
            a, b = _px(address, f"{side}_{part}"), _px(later, f"{side}_{part}")
            if a is not None and b is not None:
                corrected = b if matrix is None else to_address(b, matrix)
                moved.append(float(np.linalg.norm(corrected - a)))
    if not moved or not body_height_px:
        return None
    return 100.0 * min(moved) / body_height_px


def assess(comparison: Mapping | None, address, later, *, frame_height: int, body_height_px: float | None,
           motion_status: str, config: CorrectionConfig | None = None) -> dict:
    """The correction status for one Address -> later comparison."""

    config = config or CorrectionConfig()
    base = {"status": INVALID, "reason": "", "matrix": None}
    if motion_status == VALID and comparison and comparison.get("status") == VALID:
        return {**base, "status": VALID, "reason": "camera did not move", "matrix": None,
                "foot_residual_pct": _round(planted_foot_residual(address, later, None, body_height_px or 0))}
    if not comparison or not comparison.get("reliable") or not comparison.get("matrix"):
        return {**base, "reason": "background registration not reliable"}
    matrix = comparison["matrix"]
    checks = {
        "residual_px": comparison.get("residual_px"),
        "rotation_deg": comparison.get("rotation_deg"),
        "scale": comparison.get("scale"),
        "shift_share": (comparison.get("shift_px") or 0.0) / max(frame_height, 1),
    }
    if checks["residual_px"] is None or checks["residual_px"] > config.max_residual_px:
        return {**base, **checks, "reason": f"registration residual {checks['residual_px']} px too high"}
    if abs(checks["rotation_deg"] or 0) > config.max_rotation_deg or not (
            config.scale_range[0] <= (checks["scale"] or 0) <= config.scale_range[1]) or \
            checks["shift_share"] > config.max_shift_share:
        return {**base, **checks, "reason": "implausible camera movement for a handheld phone"}
    foot = planted_foot_residual(address, later, matrix, body_height_px or 0, config.foot_parts)
    raw_foot = planted_foot_residual(address, later, None, body_height_px or 0, config.foot_parts)
    out = {**checks, "matrix": matrix, "foot_residual_pct": _round(foot), "foot_raw_pct": _round(raw_foot)}
    shift_pct = (100.0 * float(comparison.get("shift_px") or 0.0) / body_height_px) if body_height_px else None
    if shift_pct is not None:
        out["shift_pct_body"] = _round(shift_pct)
    small = (config.small_shift_pct is not None and shift_pct is not None and shift_pct <= config.small_shift_pct)
    if foot is None:
        if small:
            return {**out, "status": CORRECTED_VALID, "accepted_by": "small_shift",
                    "reason": f"corrected; camera shift {shift_pct:.2f} % of height is below the measurement noise"}
        return {**out, "status": UNCERTAIN, "reason": "no ankles to confirm the correction"}
    if foot > config.foot_tolerance_pct:
        if small:
            return {**out, "status": CORRECTED_VALID, "accepted_by": "small_shift",
                    "reason": (f"corrected; camera shift {shift_pct:.2f} % of height is below the measurement noise "
                               f"(foot moved {foot:.1f} %, which cannot separate camera from golfer)")}
        return {**out, "status": UNCERTAIN,
                "reason": f"planted foot still {foot:.1f} % of height from Address after correction"}
    return {**out, "status": CORRECTED_VALID, "reason": "corrected; planted foot back on its Address position"}


def _round(value, digits=2):
    return None if value is None else round(float(value), digits)


def correction_for(camera_check: Mapping, records_by_phase: Mapping[str, object], *, frame_height: int,
                   body_height_px: float | None, config: CorrectionConfig | None = None) -> dict:
    """Per-phase correction results for Top, Mid-Down and Impact."""

    motion = (camera_check.get("signals") or {}).get("camera_motion") or {}
    comparisons = motion.get("comparisons") or {}
    address = records_by_phase.get("address")
    out = {}
    for phase in PHASES:
        later = records_by_phase.get(phase)
        if address is None or later is None:
            out[phase] = {"status": INVALID, "reason": "moment not found", "matrix": None}
            continue
        out[phase] = assess(comparisons.get(f"address->{phase}"), address, later, frame_height=frame_height,
                            body_height_px=body_height_px, motion_status=motion.get("status", UNCERTAIN), config=config)
    return out


def usable(result: Mapping | None) -> bool:
    return bool(result) and result.get("status") in (VALID, CORRECTED_VALID)


__all__ = ["CORRECTED_VALID", "INVALID", "UNCERTAIN", "VALID", "CorrectionConfig", "assess", "correction_for",
           "planted_foot_residual", "to_address", "to_later", "usable"]
