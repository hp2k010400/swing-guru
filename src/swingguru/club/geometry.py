"""Pure shaft geometry: angles, line distances and body constraints.

Conventions
-----------
* Points are image pixels ``(x, y)`` with ``y`` pointing down.
* A shaft direction is the vector from the grip (hands) toward the clubhead.
* ``shaft_angle_deg`` is that direction measured counter-clockwise from the
  image's +x axis with *y up*, so a club pointing straight up the screen is
  90 and straight down is -90. Range (-180, 180].

Nothing here knows about golf phases or ideal positions.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np


def direction_angle_deg(vector: Iterable[float]) -> float:
    """Screen angle of a direction vector, counter-clockwise from +x, y up."""

    dx, dy = (float(v) for v in vector)
    if not (math.isfinite(dx) and math.isfinite(dy)) or (dx == 0.0 and dy == 0.0):
        raise ValueError("direction must be a finite, non-zero vector")
    return wrap_deg(math.degrees(math.atan2(-dy, dx)))


def unit_from_angle(angle_deg: float) -> np.ndarray:
    """Unit direction vector (image coordinates, y down) for a screen angle."""

    radians = math.radians(angle_deg)
    return np.array([math.cos(radians), -math.sin(radians)], dtype=float)


def wrap_deg(angle: float) -> float:
    """Wrap an angle to (-180, 180]."""

    wrapped = (float(angle) + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def angle_difference(a: float, b: float) -> float:
    """Signed smallest difference ``a - b`` in degrees, in (-180, 180]."""

    return wrap_deg(float(a) - float(b))


def point_line_distance(point, start, end) -> float:
    """Distance from ``point`` to the infinite line through start and end."""

    p = np.asarray(point, float)
    a = np.asarray(start, float)
    b = np.asarray(end, float)
    direction = b - a
    length = float(np.linalg.norm(direction))
    if length == 0.0:
        return float(np.linalg.norm(p - a))
    cross = direction[0] * (p - a)[1] - direction[1] * (p - a)[0]
    return abs(float(cross)) / length


def orient_from(origin, start, end) -> tuple[np.ndarray, np.ndarray]:
    """Return the segment's endpoints ordered nearest-to-``origin`` first."""

    o = np.asarray(origin, float)
    a = np.asarray(start, float)
    b = np.asarray(end, float)
    if np.linalg.norm(a - o) <= np.linalg.norm(b - o):
        return a, b
    return b, a


def overlaps_forearm(
    shaft_angle_deg: float,
    wrist,
    elbow,
    *,
    tolerance_deg: float,
) -> bool:
    """True when a shaft direction runs back along the forearm.

    The shaft leaves the hands on the side *away* from the elbow; a candidate
    pointing from the wrist toward the elbow is the forearm's own edge.
    """

    if wrist is None or elbow is None:
        return False
    toward_elbow = np.asarray(elbow, float) - np.asarray(wrist, float)
    if not np.all(np.isfinite(toward_elbow)) or np.linalg.norm(toward_elbow) < 1e-6:
        return False
    forearm_angle = direction_angle_deg(toward_elbow)
    return abs(angle_difference(shaft_angle_deg, forearm_angle)) <= tolerance_deg


def allowed_rotation_deg(
    hand_step_torso: float | None,
    *,
    base_deg: float,
    per_torso_deg: float,
    cap_deg: float,
) -> float:
    """Largest plausible shaft rotation between consecutive frames.

    The club turns fastest when the hands move fastest, so the allowance grows
    with the hands' own displacement (in torso lengths per frame). This is an
    engineering bound for tracking, not a golf speed model.
    """

    step = 0.0 if hand_step_torso is None or not math.isfinite(hand_step_torso) else max(0.0, hand_step_torso)
    return min(cap_deg, base_deg + per_torso_deg * step)


__all__ = [
    "allowed_rotation_deg",
    "angle_difference",
    "direction_angle_deg",
    "orient_from",
    "overlaps_forearm",
    "point_line_distance",
    "unit_from_angle",
    "wrap_deg",
]
