"""Landmark vocabulary for the 33-point MediaPipe pose topology.

This module is deliberately free of any MediaPipe import so that geometry,
measurement, overlay and export code can be exercised without loading the
native runtime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Sequence

# MediaPipe BlazePose topology, in the exact index order the model emits.
LANDMARK_NAMES: tuple[str, ...] = (
    "nose",
    "left_eye_inner",
    "left_eye",
    "left_eye_outer",
    "right_eye_inner",
    "right_eye",
    "right_eye_outer",
    "left_ear",
    "right_ear",
    "mouth_left",
    "mouth_right",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_pinky",
    "right_pinky",
    "left_index",
    "right_index",
    "left_thumb",
    "right_thumb",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
    "left_heel",
    "right_heel",
    "left_foot_index",
    "right_foot_index",
)

LANDMARK_COUNT = len(LANDMARK_NAMES)

NAME_TO_INDEX: Mapping[str, int] = {
    name: index for index, name in enumerate(LANDMARK_NAMES)
}


def index_of(name: str) -> int:
    """Index for a landmark name, raising ``KeyError`` for unknown names."""

    return NAME_TO_INDEX[name]


def name_of(index: int) -> str:
    """Name for a landmark index."""

    return LANDMARK_NAMES[index]


# Skeleton edges used by the overlay renderer. MediaPipe ships its own list at
# ``PoseLandmarksConnections.POSE_CONNECTIONS``; ``tests/test_landmarks.py``
# asserts this constant matches it exactly, and the overlay uses this local copy
# so rendering does not need the native runtime.
POSE_CONNECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 7),
    (0, 4),
    (4, 5),
    (5, 6),
    (6, 8),
    (9, 10),
    (11, 12),
    (11, 13),
    (13, 15),
    (15, 17),
    (15, 19),
    (15, 21),
    (17, 19),
    (12, 14),
    (14, 16),
    (16, 18),
    (16, 20),
    (16, 22),
    (18, 20),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (24, 26),
    (25, 27),
    (26, 28),
    (27, 29),
    (28, 30),
    (29, 31),
    (30, 32),
    (27, 31),
    (28, 32),
)

SIDES: tuple[str, str] = ("left", "right")
OPPOSITE_SIDE: Mapping[str, str] = {"left": "right", "right": "left"}

LEAD_SIDE_BY_HANDEDNESS: Mapping[str, str] = {
    # A right-handed golfer swings with the left arm leading, so the left
    # shoulder/hip/knee/ankle chain is the lead chain.
    "right": "left",
    # Mirror image for a left-handed golfer.
    "left": "right",
}


def normalize_handedness(value: str | None) -> str:
    """Return ``"right"`` or ``"left"``, defaulting to right-handed."""

    if value is None:
        return "right"
    cleaned = str(value).strip().lower()
    if cleaned in ("right", "rh", "right-handed", "right_handed"):
        return "right"
    if cleaned in ("left", "lh", "left-handed", "left_handed"):
        return "left"
    raise ValueError(f"unknown handedness: {value!r}")


def lead_side(handedness: str | None) -> str:
    """Anatomical side of the lead limb for the given handedness."""

    return LEAD_SIDE_BY_HANDEDNESS[normalize_handedness(handedness)]


def trail_side(handedness: str | None) -> str:
    """Anatomical side of the trail limb (the opposite of the lead side)."""

    return OPPOSITE_SIDE[lead_side(handedness)]


def side_landmark(side: str, part: str) -> str:
    """``side_landmark("left", "knee")`` -> ``"left_knee"``."""

    cleaned = str(side).strip().lower()
    if cleaned not in SIDES:
        raise ValueError(f"unknown side: {side!r}")
    name = f"{cleaned}_{part}"
    if name not in NAME_TO_INDEX:
        raise ValueError(f"unknown landmark part: {part!r}")
    return name


@dataclass(frozen=True)
class Landmark:
    """One normalised image-space landmark from a single frame.

    ``x``, ``y`` and ``z`` are MediaPipe's normalised output: ``x`` and ``y``
    are fractions of the frame width and height, ``z`` is a relative depth
    estimate scaled roughly like ``x``. None of them are calibrated
    millimetres, and ``x``/``y`` can fall outside ``[0, 1]`` when the model
    extrapolates a joint past the frame edge.
    """

    index: int
    name: str
    x: float
    y: float
    z: float
    visibility: float | None = None
    presence: float | None = None

    @property
    def finite(self) -> bool:
        return math.isfinite(self.x) and math.isfinite(self.y)

    def in_frame(self, margin: float = 0.0) -> bool:
        """True when the normalised point sits inside the frame."""

        if not self.finite:
            return False
        return (
            -margin <= self.x <= 1.0 + margin and -margin <= self.y <= 1.0 + margin
        )

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "index": self.index,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "visibility": self.visibility,
            "presence": self.presence,
        }


@dataclass(frozen=True)
class WorldLandmark:
    """One landmark in MediaPipe's metric world space.

    The origin is the midpoint between the hips and axes follow image space
    orientation, with ``z`` pointing roughly toward the camera. Values are
    model estimates from a single RGB frame, not calibrated measurements, so
    they are exported for inspection only and never used for scoring.
    """

    index: int
    name: str
    x: float
    y: float
    z: float
    visibility: float | None = None
    presence: float | None = None

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "index": self.index,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "visibility": self.visibility,
            "presence": self.presence,
        }


@dataclass(frozen=True)
class PoseObservation:
    """Pose output for a single frame.

    ``detected=False`` means the pose model returned no usable person for this
    frame. The record still exists so downstream code can distinguish "tracking
    lost" from "frame never decoded".
    """

    detected: bool = False
    landmarks: tuple[Landmark, ...] = ()
    world_landmarks: tuple[WorldLandmark, ...] = ()
    note: str | None = None
    error: str | None = None

    @classmethod
    def missing(cls, note: str | None = None) -> "PoseObservation":
        return cls(detected=False, note=note)

    def by_name(self, name: str) -> Landmark | None:
        target = NAME_TO_INDEX.get(name)
        if target is None:
            raise KeyError(f"unknown landmark name: {name!r}")
        return self.by_index(target)

    def by_index(self, index: int) -> Landmark | None:
        for landmark in self.landmarks:
            if landmark.index == index:
                return landmark
        return None

    def world_by_name(self, name: str) -> WorldLandmark | None:
        target = NAME_TO_INDEX.get(name)
        if target is None:
            raise KeyError(f"unknown landmark name: {name!r}")
        for landmark in self.world_landmarks:
            if landmark.index == target:
                return landmark
        return None

    def as_name_map(self) -> dict[str, Landmark]:
        return {landmark.name: landmark for landmark in self.landmarks}

    def missing_names(self, names: Iterable[str]) -> tuple[str, ...]:
        present = {landmark.name for landmark in self.landmarks}
        return tuple(name for name in names if name not in present)


def build_landmarks(
    rows: Sequence[Sequence[float | None]],
    *,
    start_index: int = 0,
) -> tuple[Landmark, ...]:
    """Build :class:`Landmark` records from raw ``(x, y, z, vis, pres)`` rows.

    Used by the MediaPipe adapter and by synthetic fixtures. Rows shorter than
    five entries leave the missing quality values as ``None``.
    """

    built: list[Landmark] = []
    for offset, row in enumerate(rows):
        index = start_index + offset
        values = list(row) + [None] * (5 - len(row))
        x, y, z, visibility, presence = values[:5]
        built.append(
            Landmark(
                index=index,
                name=name_of(index) if index < LANDMARK_COUNT else f"landmark_{index}",
                x=float(x) if x is not None else math.nan,
                y=float(y) if y is not None else math.nan,
                z=float(z) if z is not None else math.nan,
                visibility=float(visibility) if visibility is not None else None,
                presence=float(presence) if presence is not None else None,
            )
        )
    return tuple(built)


def iter_landmark_pairs(
    observation: PoseObservation,
    connections: Sequence[tuple[int, int]] = POSE_CONNECTIONS,
) -> Iterator[tuple[Landmark, Landmark]]:
    """Yield landmark pairs for every skeleton edge present in the frame."""

    by_index = {landmark.index: landmark for landmark in observation.landmarks}
    for start, end in connections:
        first = by_index.get(start)
        second = by_index.get(end)
        if first is not None and second is not None:
            yield first, second
