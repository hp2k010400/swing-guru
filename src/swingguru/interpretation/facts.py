"""Measurement facts: the only input the interpretation layer accepts.

Facts are read from a built ``analysis.json`` payload, so interpretation stays
independent of pose models, trackers and UI. A fact is a measured value with
its unit, its reference, its phase and its confidence; nothing more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

CONFIDENCE_ORDER = {"UNAVAILABLE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass(frozen=True)
class MeasurementFact:
    """One measured value, e.g. posture change at Impact vs Address."""

    key: str  # e.g. "posture_change_deg", "pelvis_toward_ball_pct"
    phase: str  # "address", "takeaway", "mid_back", "top", "downswing", "impact"
    value: float
    unit: str  # "deg" or "pct_body_height"
    reference: str  # "address", "vertical", "straight_leg", "horizontal", ...
    confidence: str  # HIGH / MEDIUM / LOW / UNAVAILABLE
    source: str = "measured"
    extra: Mapping[str, float] = field(default_factory=dict)

    def meets(self, required: str) -> bool:
        return CONFIDENCE_ORDER.get(self.confidence, 0) >= CONFIDENCE_ORDER[required]


@dataclass(frozen=True)
class FactSet:
    """All facts of one swing plus the context rules need to fail closed."""

    facts: tuple[MeasurementFact, ...]
    camera_view: str
    toward_ball_known: bool
    address_available: bool
    camera_validity: str = "UNCERTAIN"  # VALID / UNCERTAIN / INVALID from the DTL camera check

    def get(self, key: str, phase: str) -> MeasurementFact | None:
        for fact in self.facts:
            if fact.key == key and fact.phase == phase:
                return fact
        return None


def facts_from_analysis(analysis: Mapping) -> FactSet:
    """Build facts from ``analysis.json`` (shown checks only)."""

    facts: list[MeasurementFact] = []
    context = analysis.get("geometry_context") or {}
    toward = context.get("toward_ball")
    toward_known = bool(context.get("toward_ball_known")) and toward in (1, -1, 1.0, -1.0)
    address_available = False
    for phase in analysis.get("phases", []):
        name = phase.get("phase")
        if name == "address" and phase.get("frame_index") is not None:
            address_available = True
        for check in phase.get("checks", []):
            raw = check.get("raw", {}) or {}
            level = check.get("level", "UNAVAILABLE")
            kind = check.get("kind")
            source = check.get("source", "measured")
            if kind == "torso_angle" and "torso_inclination_deg" in raw:
                facts.append(MeasurementFact("torso_angle_deg", name, raw["torso_inclination_deg"], "deg", "vertical", level, source))
            elif kind in ("lead_knee", "trail_knee") and "knee_flex_deg" in raw:
                facts.append(MeasurementFact(f"{kind}_flex_deg", name, raw["knee_flex_deg"], "deg", "straight_leg", level, source))
            elif kind == "posture" and "torso_angle_change_deg" in raw:
                facts.append(MeasurementFact("posture_change_deg", name, raw["torso_angle_change_deg"], "deg", "address", level, source))
            elif kind in ("head", "pelvis") and "movement_pct_height" in raw:
                total = float(raw["movement_pct_height"])
                dx, dy = float(raw.get("dx_px", 0.0)), float(raw.get("dy_px", 0.0))
                size = math.hypot(dx, dy)
                facts.append(MeasurementFact(f"{kind}_movement_pct", name, total, "pct_body_height", "address", level, source,
                                             {"dx_px": dx, "dy_px": dy}))
                if toward_known and size > 0:
                    along = total * (dx * float(toward)) / size  # + = toward the ball
                    vertical = total * dy / size  # + = down
                    facts.append(MeasurementFact(f"{kind}_toward_ball_pct", name, along, "pct_body_height", "address", level, source))
                    facts.append(MeasurementFact(f"{kind}_down_pct", name, vertical, "pct_body_height", "address", level, source))
            elif kind == "shaft" and "shaft_from_horizontal_deg" in raw:
                facts.append(MeasurementFact("shaft_angle_deg", name, raw["shaft_from_horizontal_deg"], "deg", "horizontal", level, source))
                if "change_vs_address_deg" in raw:
                    facts.append(MeasurementFact("shaft_vs_address_ref_deg", name, raw["change_vs_address_deg"], "deg",
                                                 "address_shaft_reference", level, source))
    # Rear-of-pelvis (tush line) displacement, measured from the silhouette.
    depth = (analysis.get("pelvis_depth") or {}).get("phases", {}) or {}
    for phase, result in depth.items():
        if result.get("available") and result.get("displacement_pct_height") is not None:
            facts.append(MeasurementFact("pelvis_rear_toward_ball_pct", phase, float(result["displacement_pct_height"]),
                                         "pct_body_height", "address_tush_line", str(result.get("level", "UNAVAILABLE")),
                                         "silhouette"))
    camera = str((analysis.get("camera_check") or {}).get("status", "UNCERTAIN"))
    return FactSet(tuple(facts), str(analysis.get("camera_view", "unknown")), toward_known, address_available, camera)


__all__ = ["CONFIDENCE_ORDER", "FactSet", "MeasurementFact", "facts_from_analysis"]
