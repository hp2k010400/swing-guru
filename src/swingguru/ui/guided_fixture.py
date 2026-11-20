"""Deterministic local swing fixture for the guided playback workflow.

This is QA and demo scaffolding, not evidence about real golf. It draws a
simple figure whose hands follow a scripted swing timetable - a still set-up, a
takeaway, a top, a return through the ball and a settled finish - and the
matching scripted provider returns the same joint positions, so the analysed
records and the drawn figure agree by construction.

The clip exists so the guided playback mechanics (moments, holds, checks,
transport controls) can be exercised end to end without a real swing video, and
so the browser QA pass has moments to play between. Nothing here claims that
the estimator finds real swing events accurately; the interface labels every
automatic moment as an estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..landmarks import LANDMARK_NAMES, POSE_CONNECTIONS, PoseObservation
from ..phases import Phase
from ..synthetic import Point, joints_to_observation, synthetic_joints

# Hands, as offsets from the pelvis in torso lengths (x lateral, y downward).
# This is a timetable for the fixture, not a model of an ideal swing.
_HAND_TIMETABLE: tuple[tuple[float, tuple[float, float]], ...] = (
    (0.00, (-0.10, 0.55)),
    (0.20, (-0.10, 0.55)),
    (0.30, (-0.40, 0.45)),
    (0.46, (0.60, -0.60)),
    (0.62, (0.15, 0.05)),
    (0.72, (-0.05, 0.55)),
    (0.84, (0.35, -0.30)),
    (0.88, (0.25, -0.45)),
    (1.00, (0.25, -0.45)),
)

_CUE_FRACTIONS: dict[Phase, float] = {
    Phase.ADDRESS: 0.20,
    Phase.TAKEAWAY: 0.30,
    # Where the scripted hands are half way (by distance) from set-up to top.
    Phase.MID_BACK: 0.39,
    Phase.TOP: 0.46,
    Phase.DOWNSWING: 0.62,
    Phase.IMPACT: 0.72,
    Phase.FINISH: 0.88,
}

# The body model only moves while the swing is happening, so the set-up and the
# finish windows are genuinely still.
_BODY_START = 0.20
_BODY_SETTLED = 0.88
# The body sweeps part of the analytic model's range rather than its full
# cycle, so the finish posture is visibly different from the set-up posture.
_BODY_PHASE_START = 0.15
_BODY_PHASE_END = 0.70

BACKGROUND_TOP = (58, 52, 46)
BACKGROUND_BOTTOM = (28, 26, 24)
FIGURE_COLOUR = (240, 240, 240)
JOINT_COLOUR = (120, 220, 255)
GROUND_COLOUR = (90, 150, 90)


def _smoothstep(value: float) -> float:
    clamped = min(1.0, max(0.0, value))
    return clamped * clamped * (3.0 - 2.0 * clamped)


@dataclass(frozen=True)
class GuidedSwingClip:
    """Description of the scripted fixture clip."""

    width: int = 640
    height: int = 360
    fps: float = 30.0
    frame_count: int = 90
    handedness: str = "right"
    torso_units: float = 0.20

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.fps

    def frame_index_for_timestamp(self, timestamp_ms: float) -> int:
        if self.fps <= 0:
            return 0
        index = int(round(float(timestamp_ms) * self.fps / 1000.0))
        return max(0, min(self.frame_count - 1, index))

    def cue_frame(self, phase: Phase | str) -> int:
        """Scripted frame for a cue; used for fixture descriptions only."""

        fraction = _CUE_FRACTIONS[Phase(phase)]
        return int(round(fraction * (self.frame_count - 1)))


def _hand_offsets(t: float) -> tuple[float, float]:
    """Interpolated hand offset from the pelvis, in torso lengths."""

    points = _HAND_TIMETABLE
    if t <= points[0][0]:
        return points[0][1]
    for (start_t, start_v), (end_t, end_v) in zip(points, points[1:]):
        if t <= end_t:
            span = max(end_t - start_t, 1e-9)
            weight = _smoothstep((t - start_t) / span)
            return (
                start_v[0] + (end_v[0] - start_v[0]) * weight,
                start_v[1] + (end_v[1] - start_v[1]) * weight,
            )
    return points[-1][1]


def guided_joints(
    frame_index: int, *, clip: GuidedSwingClip | None = None
) -> dict[str, Point]:
    """Joint positions for one fixture frame, in normalised image space."""

    settings = clip or GuidedSwingClip()
    t = 0.0 if settings.frame_count <= 1 else frame_index / (settings.frame_count - 1)
    body_t = (t - _BODY_START) / (_BODY_SETTLED - _BODY_START)
    body_phase = _BODY_PHASE_START + (_BODY_PHASE_END - _BODY_PHASE_START) * _smoothstep(
        body_t
    )
    body_index = int(round(body_phase * (settings.frame_count - 1)))
    joints = dict(synthetic_joints(body_index, clip=_body_clip(settings)))

    hands_x, hands_y = _hand_offsets(t)
    pelvis_x = (joints["left_hip"][0] + joints["right_hip"][0]) / 2.0
    pelvis_y = (joints["left_hip"][1] + joints["right_hip"][1]) / 2.0
    target_x = pelvis_x + hands_x * settings.torso_units
    target_y = pelvis_y + hands_y * settings.torso_units

    for name in ("left_wrist", "right_wrist"):
        _, _, z, visibility, presence = joints[name]
        offset = -0.012 if name.startswith("left") else 0.012
        joints[name] = (target_x + offset, target_y, z, visibility, presence)
    for name in ("left_elbow", "right_elbow"):
        _, _, z, visibility, presence = joints[name]
        shoulder = (
            joints["left_shoulder"]
            if name.startswith("left")
            else joints["right_shoulder"]
        )
        joints[name] = (
            shoulder[0] + (target_x - shoulder[0]) * 0.55,
            shoulder[1] + (target_y - shoulder[1]) * 0.55,
            z,
            visibility,
            presence,
        )
    for name in ("left_pinky", "right_pinky", "left_index", "right_index"):
        _, _, z, visibility, presence = joints[name]
        offset = -0.02 if name.startswith("left") else 0.02
        joints[name] = (target_x + offset, target_y + 0.008, z, visibility, presence)
    for name in ("left_thumb", "right_thumb"):
        _, _, z, visibility, presence = joints[name]
        offset = -0.014 if name.startswith("left") else 0.014
        joints[name] = (target_x + offset, target_y - 0.01, z, visibility, presence)
    return joints


def _body_clip(settings: GuidedSwingClip):
    from ..synthetic import SyntheticClip

    return SyntheticClip(
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
        frame_count=settings.frame_count,
        handedness=settings.handedness,
    )


@dataclass
class GuidedPoseProvider:
    """Deterministic pose provider for the fixture clip."""

    clip: GuidedSwingClip = field(default_factory=GuidedSwingClip)
    name: str = "scripted-guided-swing"

    def detect(self, frame_bgr: np.ndarray, timestamp_ms: float) -> PoseObservation:
        index = self.clip.frame_index_for_timestamp(timestamp_ms)
        return joints_to_observation(guided_joints(index, clip=self.clip))

    def close(self) -> None:  # pragma: no cover - nothing to release
        return None


def draw_guided_frame(
    frame_index: int, *, clip: GuidedSwingClip | None = None
) -> np.ndarray:
    """Render one fixture frame with the scripted figure drawn on it."""

    settings = clip or GuidedSwingClip()
    width, height = settings.width, settings.height
    joints = guided_joints(frame_index, clip=settings)

    image = np.zeros((height, width, 3), dtype=np.uint8)
    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    for channel in range(3):
        image[:, :, channel] = np.clip(
            BACKGROUND_TOP[channel] * (1.0 - ramp)
            + BACKGROUND_BOTTOM[channel] * ramp,
            0,
            255,
        ).astype(np.uint8)

    cv2.rectangle(image, (0, int(height * 0.92)), (width, height), GROUND_COLOUR, -1)

    def to_px(name: str) -> tuple[int, int]:
        x, y, *_ = joints[name]
        return int(round(x * width)), int(round(y * height))

    thickness = max(3, int(round(min(width, height) / 150.0)))
    for start, end in POSE_CONNECTIONS:
        first = LANDMARK_NAMES[start]
        second = LANDMARK_NAMES[end]
        if first not in joints or second not in joints:
            continue
        cv2.line(
            image, to_px(first), to_px(second), FIGURE_COLOUR, thickness, cv2.LINE_AA
        )

    for name in (
        "left_shoulder",
        "right_shoulder",
        "left_hip",
        "right_hip",
        "left_knee",
        "right_knee",
        "left_ankle",
        "right_ankle",
        "left_elbow",
        "right_elbow",
        "left_wrist",
        "right_wrist",
    ):
        cv2.circle(image, to_px(name), max(3, thickness), JOINT_COLOUR, -1, cv2.LINE_AA)

    head_x, head_y, *_ = joints["nose"]
    cv2.circle(
        image,
        (int(round(head_x * width)), int(round((head_y - 0.012) * height))),
        max(6, int(round(height * 0.055))),
        FIGURE_COLOUR,
        -1,
        cv2.LINE_AA,
    )
    return image


def make_guided_clip(
    path: Path | str,
    *,
    clip: GuidedSwingClip | None = None,
    fourcc: str = "mp4v",
) -> Path:
    """Write the fixture clip and return its path."""

    from ..video import AnnotatedVideoWriter

    settings = clip or GuidedSwingClip()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with AnnotatedVideoWriter(
        target,
        fps=settings.fps,
        size=(settings.width, settings.height),
        fourcc=fourcc,
    ) as writer:
        for frame_index in range(settings.frame_count):
            writer.write(draw_guided_frame(frame_index, clip=settings))
    return target


__all__ = [
    "GuidedPoseProvider",
    "GuidedSwingClip",
    "draw_guided_frame",
    "guided_joints",
    "make_guided_clip",
]
