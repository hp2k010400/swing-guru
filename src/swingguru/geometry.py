"""Deterministic 2D geometry used by every swing measurement.

All angles and distances are computed in aspect-corrected pixel space.
MediaPipe returns normalised coordinates, so a displacement of ``0.1`` along
``x`` and along ``y`` describe different physical distances unless the frame is
square. Multiplying ``x`` by the frame width and ``y`` by the frame height
before any angle or distance calculation keeps the geometry isometric with the
recorded image.

Every function here is pure: given the same inputs it returns the same outputs,
and it raises :class:`DegenerateGeometryError` rather than returning a
meaningless number when the inputs cannot support an answer.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

# Segments shorter than this (in pixels) are treated as collapsed joints. The
# threshold is far below any real body segment at any usable resolution, so it
# only fires on genuinely degenerate input.
DEFAULT_MIN_SEGMENT_PX = 1e-6

IMAGE_UP = np.array([0.0, -1.0], dtype=float)


class DegenerateGeometryError(ValueError):
    """Raised when a geometry request has no defined answer."""


def as_point(value: Sequence[float] | np.ndarray) -> np.ndarray:
    """Coerce a 2D point-like value into a float ``(2,)`` array."""

    array = np.asarray(value, dtype=float).reshape(-1)
    if array.size < 2:
        raise ValueError(f"expected a 2D point, got {value!r}")
    return array[:2].astype(float, copy=True)


def is_finite_point(point: Sequence[float] | np.ndarray) -> bool:
    return bool(np.all(np.isfinite(as_point(point))))


def to_pixel(
    point: Sequence[float] | np.ndarray, width: int, height: int
) -> np.ndarray:
    """Convert a normalised image coordinate to aspect-corrected pixels."""

    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    x, y = as_point(point)
    return np.array([x * float(width), y * float(height)], dtype=float)


def midpoint(
    first: Sequence[float] | np.ndarray, second: Sequence[float] | np.ndarray
) -> np.ndarray:
    return (as_point(first) + as_point(second)) / 2.0


def vector(
    start: Sequence[float] | np.ndarray, end: Sequence[float] | np.ndarray
) -> np.ndarray:
    return as_point(end) - as_point(start)


def distance(
    first: Sequence[float] | np.ndarray, second: Sequence[float] | np.ndarray
) -> float:
    return float(np.linalg.norm(vector(first, second)))


def angle_between_vectors(
    first: Sequence[float] | np.ndarray,
    second: Sequence[float] | np.ndarray,
    *,
    min_norm: float = DEFAULT_MIN_SEGMENT_PX,
) -> float:
    """Unsigned angle between two vectors, in degrees, in ``[0, 180]``."""

    a = as_point(first)
    b = as_point(second)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise DegenerateGeometryError("vector components must be finite")
    norm_a = float(np.linalg.norm(a))
    norm_b = float(np.linalg.norm(b))
    if norm_a <= min_norm or norm_b <= min_norm:
        raise DegenerateGeometryError("vector has zero length")
    cosine = float(np.dot(a, b) / (norm_a * norm_b))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def angle_at_vertex(
    first: Sequence[float] | np.ndarray,
    vertex: Sequence[float] | np.ndarray,
    third: Sequence[float] | np.ndarray,
    *,
    min_segment_length: float = DEFAULT_MIN_SEGMENT_PX,
) -> float:
    """Interior angle at ``vertex`` for the joint path ``first-vertex-third``.

    Returns degrees in ``[0, 180]``. A perfectly straight limb gives ``180``.
    """

    incoming = vector(vertex, first)
    outgoing = vector(vertex, third)
    if not (
        is_finite_point(first) and is_finite_point(vertex) and is_finite_point(third)
    ):
        raise DegenerateGeometryError("joint coordinates must be finite")
    if (
        float(np.linalg.norm(incoming)) <= min_segment_length
        or float(np.linalg.norm(outgoing)) <= min_segment_length
    ):
        raise DegenerateGeometryError("joint segment collapsed to a point")
    return angle_between_vectors(incoming, outgoing, min_norm=min_segment_length)


def interior_knee_angle(
    hip: Sequence[float] | np.ndarray,
    knee: Sequence[float] | np.ndarray,
    ankle: Sequence[float] | np.ndarray,
    *,
    min_segment_length: float = DEFAULT_MIN_SEGMENT_PX,
) -> float:
    """Anatomical knee flexion expressed as the interior hip-knee-ankle angle.

    A fully extended leg measures about ``180`` degrees.
    """

    return angle_at_vertex(
        hip, knee, ankle, min_segment_length=min_segment_length
    )


def knee_flex_degrees(
    hip: Sequence[float] | np.ndarray,
    knee: Sequence[float] | np.ndarray,
    ankle: Sequence[float] | np.ndarray,
    *,
    min_segment_length: float = DEFAULT_MIN_SEGMENT_PX,
) -> float:
    """Knee flexion in degrees, reported as ``180 - interior angle``.

    A straight leg is ``0``; a right-angled knee is ``90``.
    """

    interior = interior_knee_angle(
        hip, knee, ankle, min_segment_length=min_segment_length
    )
    return 180.0 - interior


def torso_inclination_degrees(
    hip_midpoint: Sequence[float] | np.ndarray,
    shoulder_midpoint: Sequence[float] | np.ndarray,
    *,
    min_segment_length: float = DEFAULT_MIN_SEGMENT_PX,
) -> float:
    """Angle of the hip-to-shoulder vector away from image vertical.

    ``0`` means the shoulder midpoint sits directly above the hip midpoint in
    the image; ``45`` means the torso leans 45 degrees in the image plane.
    Because this is a projected single-camera angle, a golfer bent toward or
    away from the camera shows less change than they actually made.
    """

    torso = vector(hip_midpoint, shoulder_midpoint)
    if not np.all(np.isfinite(torso)):
        raise DegenerateGeometryError("torso vector must be finite")
    if float(np.linalg.norm(torso)) <= min_segment_length:
        raise DegenerateGeometryError("torso vector collapsed to a point")
    return angle_between_vectors(torso, IMAGE_UP, min_norm=min_segment_length)


def image_diagonal(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    return math.hypot(float(width), float(height))


def percent_of_reference(value: float, reference: float) -> float:
    """Express ``value`` as a percentage of ``reference``.

    Returns a plain percentage (``50.0`` means half of the reference) and
    raises for a non-positive reference rather than dividing by zero.
    """

    if not math.isfinite(value) or not math.isfinite(reference):
        raise DegenerateGeometryError("percentage inputs must be finite")
    if reference <= 0:
        raise DegenerateGeometryError("percentage reference must be positive")
    return float(value) / float(reference) * 100.0


def displacement_px(
    first: Sequence[float] | np.ndarray, second: Sequence[float] | np.ndarray
) -> float:
    """Euclidean distance between two aspect-corrected pixel points."""

    return distance(first, second)


def signed_angle_difference(current: float, reference: float) -> float:
    """``current - reference`` in degrees, kept as a plain difference."""

    if not math.isfinite(current) or not math.isfinite(reference):
        raise DegenerateGeometryError("angle inputs must be finite")
    return float(current) - float(reference)
