"""Deterministic synthetic clips and scripted pose providers.

These exist so the whole plumbing path - decode, record, measure, overlay,
write, export - can be tested and demonstrated end to end without a golf
video. The same analytic joint model drives both the drawn figure and the
scripted provider, so the overlay and the measurements are consistent by
construction.

This is a plumbing fixture. It says nothing about whether MediaPipe tracks a
real golfer, which is exactly why the engine reports golf tracking accuracy as
unverified until real footage is supplied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import cv2
import numpy as np

from .landmarks import (
    LANDMARK_NAMES,
    POSE_CONNECTIONS,
    Landmark,
    PoseObservation,
    WorldLandmark,
    normalize_handedness,
)
from .video import AnnotatedVideoWriter

Point = tuple[float, float, float, float, float]  # x, y, z, visibility, presence


@dataclass(frozen=True)
class SyntheticClip:
    """Description of a synthetic clip produced by :func:`make_synthetic_clip`."""

    width: int = 640
    height: int = 360
    fps: float = 30.0
    frame_count: int = 48
    handedness: str = "right"
    mirrored: bool = False
    body_height_m: float = 1.75

    def __post_init__(self) -> None:
        normalize_handedness(self.handedness)

    @property
    def duration_s(self) -> float:
        return self.frame_count / self.fps

    def frame_index_for_timestamp(self, timestamp_ms: float) -> int:
        if self.fps <= 0:
            return 0
        return int(round(float(timestamp_ms) * self.fps / 1000.0))


def _motion_phase(frame_index: int, frame_count: int) -> float:
    if frame_count <= 1:
        return 0.0
    return min(1.0, max(0.0, frame_index / float(frame_count - 1)))


def synthetic_joints(
    frame_index: int,
    *,
    clip: SyntheticClip | None = None,
) -> dict[str, Point]:
    """Analytic joint positions for one frame, in normalised image space.

    The figure leans from a golf setup posture, sweeps the arms across the
    body, and settles into a finish. Values are chosen to stay comfortably
    inside the frame for every supported aspect ratio.
    """

    settings = clip or SyntheticClip()
    t = _motion_phase(frame_index, settings.frame_count)
    swing = math.sin(math.pi * t)

    pelvis_x = 0.50 + 0.012 * math.sin(2 * math.pi * t)
    pelvis_y = 0.60 - 0.01 * swing

    torso_length = 0.20
    inclination = math.radians(22.0 + 18.0 * swing)
    shoulder_x = pelvis_x + torso_length * math.sin(inclination)
    shoulder_y = pelvis_y - torso_length * math.cos(inclination)

    shoulder_half = 0.045 * math.cos(inclination)
    shoulder_tilt = 0.012 * math.sin(inclination)
    lead_shoulder_x = shoulder_x - shoulder_half
    lead_shoulder_y = shoulder_y + shoulder_tilt
    trail_shoulder_x = shoulder_x + shoulder_half
    trail_shoulder_y = shoulder_y - shoulder_tilt

    head_x = shoulder_x + 0.055 * math.sin(inclination)
    head_y = shoulder_y - 0.075 * math.cos(inclination)

    hip_half = 0.040
    lead_hip_x = pelvis_x - hip_half
    trail_hip_x = pelvis_x + hip_half
    lead_hip_y = pelvis_y + 0.004 * math.sin(2 * math.pi * t)
    trail_hip_y = pelvis_y - 0.004 * math.sin(2 * math.pi * t)

    lead_knee_x = lead_hip_x - 0.020 - 0.010 * swing
    trail_knee_x = trail_hip_x + 0.020
    lead_knee_y = pelvis_y + 0.165 - 0.012 * swing
    trail_knee_y = pelvis_y + 0.165 - 0.006 * swing

    lead_ankle_x = lead_knee_x - 0.008
    trail_ankle_x = trail_knee_x + 0.012
    lead_ankle_y = lead_knee_y + 0.155
    trail_ankle_y = trail_knee_y + 0.155

    hands_centre_x = (lead_shoulder_x + trail_shoulder_x) / 2.0
    hands_centre_y = (lead_shoulder_y + trail_shoulder_y) / 2.0
    hands_angle = math.radians(118.0 - 220.0 * t)
    hands_radius = 0.175 - 0.02 * swing
    hands_x = hands_centre_x + hands_radius * math.cos(hands_angle)
    hands_y = hands_centre_y + hands_radius * math.sin(hands_angle)

    lead_elbow_x = (lead_shoulder_x + hands_x) / 2.0 - 0.035 * swing
    lead_elbow_y = (lead_shoulder_y + hands_y) / 2.0 + 0.02
    trail_elbow_x = (trail_shoulder_x + hands_x) / 2.0 + 0.035 * swing
    trail_elbow_y = (trail_shoulder_y + hands_y) / 2.0 + 0.02

    raw: dict[str, Point] = {
        "nose": (head_x, head_y + 0.008, 0.0, 0.99, 0.99),
        "left_eye_inner": (head_x - 0.008, head_y - 0.004, 0.0, 0.95, 0.95),
        "left_eye": (head_x - 0.010, head_y - 0.004, 0.0, 0.95, 0.95),
        "left_eye_outer": (head_x - 0.013, head_y - 0.003, 0.0, 0.90, 0.90),
        "right_eye_inner": (head_x + 0.008, head_y - 0.004, 0.0, 0.95, 0.95),
        "right_eye": (head_x + 0.010, head_y - 0.004, 0.0, 0.95, 0.95),
        "right_eye_outer": (head_x + 0.013, head_y - 0.003, 0.0, 0.90, 0.90),
        "left_ear": (head_x - 0.016, head_y, 0.0, 0.88, 0.88),
        "right_ear": (head_x + 0.016, head_y, 0.0, 0.88, 0.88),
        "mouth_left": (head_x - 0.006, head_y + 0.016, 0.0, 0.86, 0.86),
        "mouth_right": (head_x + 0.006, head_y + 0.016, 0.0, 0.86, 0.86),
        "left_shoulder": (lead_shoulder_x, lead_shoulder_y, 0.0, 0.98, 0.98),
        "right_shoulder": (trail_shoulder_x, trail_shoulder_y, 0.0, 0.98, 0.98),
        "left_elbow": (lead_elbow_x, lead_elbow_y, 0.0, 0.95, 0.95),
        "right_elbow": (trail_elbow_x, trail_elbow_y, 0.0, 0.95, 0.95),
        "left_wrist": (hands_x - 0.012, hands_y, 0.0, 0.92, 0.92),
        "right_wrist": (hands_x + 0.012, hands_y, 0.0, 0.92, 0.92),
        "left_pinky": (hands_x - 0.020, hands_y + 0.008, 0.0, 0.85, 0.85),
        "right_pinky": (hands_x + 0.020, hands_y + 0.008, 0.0, 0.85, 0.85),
        "left_index": (hands_x - 0.022, hands_y, 0.0, 0.85, 0.85),
        "right_index": (hands_x + 0.022, hands_y, 0.0, 0.85, 0.85),
        "left_thumb": (hands_x - 0.014, hands_y - 0.010, 0.0, 0.85, 0.85),
        "right_thumb": (hands_x + 0.014, hands_y - 0.010, 0.0, 0.85, 0.85),
        "left_hip": (lead_hip_x, lead_hip_y, 0.0, 0.97, 0.97),
        "right_hip": (trail_hip_x, trail_hip_y, 0.0, 0.97, 0.97),
        "left_knee": (lead_knee_x, lead_knee_y, 0.0, 0.94, 0.94),
        "right_knee": (trail_knee_x, trail_knee_y, 0.0, 0.94, 0.94),
        "left_ankle": (lead_ankle_x, lead_ankle_y, 0.0, 0.90, 0.90),
        "right_ankle": (trail_ankle_x, trail_ankle_y, 0.0, 0.90, 0.90),
        "left_heel": (lead_ankle_x - 0.012, lead_ankle_y + 0.012, 0.0, 0.85, 0.85),
        "right_heel": (trail_ankle_x - 0.012, trail_ankle_y + 0.012, 0.0, 0.85, 0.85),
        "left_foot_index": (lead_ankle_x + 0.020, lead_ankle_y + 0.014, 0.0, 0.85, 0.85),
        "right_foot_index": (trail_ankle_x + 0.020, trail_ankle_y + 0.014, 0.0, 0.85, 0.85),
    }

    if settings.mirrored:
        flipped: dict[str, Point] = {}
        for name, (x, y, z, visibility, presence) in raw.items():
            target = name
            if name.startswith("left_"):
                target = "right_" + name[len("left_") :]
            elif name.startswith("right_"):
                target = "left_" + name[len("right_") :]
            flipped[target] = (1.0 - x, y, -z, visibility, presence)
        raw = flipped

    return raw


def joints_to_observation(
    joints: Mapping[str, Point],
    *,
    body_height_m: float = 1.75,
) -> PoseObservation:
    """Build a :class:`PoseObservation` from analytic joints."""

    index_by_name = {name: index for index, name in enumerate(LANDMARK_NAMES)}
    landmarks: list[Landmark] = []
    world: list[WorldLandmark] = []
    for name, index in index_by_name.items():
        point = joints.get(name)
        if point is None:
            continue
        x, y, z, visibility, presence = point
        landmarks.append(
            Landmark(
                index=index,
                name=name,
                x=float(x),
                y=float(y),
                z=float(z),
                visibility=float(visibility),
                presence=float(presence),
            )
        )
        world.append(
            WorldLandmark(
                index=index,
                name=name,
                x=float(x - 0.5) * body_height_m,
                y=float(y - 0.5) * body_height_m,
                z=float(z) * body_height_m,
                visibility=float(visibility),
                presence=float(presence),
            )
        )
    return PoseObservation(
        detected=True,
        landmarks=tuple(sorted(landmarks, key=lambda item: item.index)),
        world_landmarks=tuple(sorted(world, key=lambda item: item.index)),
    )


BACKGROUND_TOP = (58, 52, 46)
BACKGROUND_BOTTOM = (28, 26, 24)
FIGURE_COLOUR = (240, 240, 240)
JOINT_COLOUR = (120, 220, 255)
GROUND_COLOUR = (90, 150, 90)
BALL_COLOUR = (60, 240, 240)


def draw_synthetic_frame(
    frame_index: int,
    *,
    clip: SyntheticClip | None = None,
) -> np.ndarray:
    """Render one synthetic frame containing a drawn golfer figure."""

    settings = clip or SyntheticClip()
    width, height = settings.width, settings.height
    joints = synthetic_joints(frame_index, clip=settings)

    image = np.zeros((height, width, 3), dtype=np.uint8)
    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    for channel in range(3):
        image[:, :, channel] = np.clip(
            BACKGROUND_TOP[channel] * (1.0 - ramp)
            + BACKGROUND_BOTTOM[channel] * ramp,
            0,
            255,
        ).astype(np.uint8)

    ground_y = int(height * 0.92)
    cv2.rectangle(image, (0, ground_y), (width, height), GROUND_COLOUR, -1)

    def to_px(name: str) -> tuple[int, int]:
        x, y, *_ = joints[name]
        return int(round(x * width)), int(round(y * height))

    ball_x, ball_y = to_px("left_foot_index")
    cv2.circle(image, (ball_x + 24, ball_y - 4), max(3, height // 90), BALL_COLOUR, -1)

    thickness = max(3, int(round(min(width, height) / 150.0)))
    for start, end in POSE_CONNECTIONS:
        first = LANDMARK_NAMES[start]
        second = LANDMARK_NAMES[end]
        if first not in joints or second not in joints:
            continue
        cv2.line(image, to_px(first), to_px(second), FIGURE_COLOUR, thickness, cv2.LINE_AA)

    for name in ("left_shoulder", "right_shoulder", "left_hip", "right_hip",
                 "left_knee", "right_knee", "left_ankle", "right_ankle",
                 "left_elbow", "right_elbow", "left_wrist", "right_wrist"):
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


def make_synthetic_clip(
    path: Path | str,
    *,
    clip: SyntheticClip | None = None,
    fourcc: str = "mp4v",
    jitter: float = 0.0,
    seed: int = 20260928,
) -> Path:
    """Write a synthetic clip to ``path`` and return the path.

    ``jitter`` adds deterministic per-pixel brightness noise, which keeps the
    file from being trivially compressible without changing the figure.
    """

    settings = clip or SyntheticClip()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    with AnnotatedVideoWriter(
        target,
        fps=settings.fps,
        size=(settings.width, settings.height),
        fourcc=fourcc,
    ) as writer:
        for frame_index in range(settings.frame_count):
            frame = draw_synthetic_frame(frame_index, clip=settings)
            if jitter > 0:
                noise = rng.normal(0.0, jitter, frame.shape)
                frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(
                    np.uint8
                )
            writer.write(frame)
    return target


@dataclass
class ScriptedPoseProvider:
    """Deterministic pose provider driven by :func:`synthetic_joints`.

    Knobs cover the quality cases the engine must surface: dropped frames,
    occluded landmarks and deliberately out-of-frame landmarks.
    """

    clip: SyntheticClip = field(default_factory=SyntheticClip)
    drop_frames: frozenset[int] = frozenset()
    occlude: frozenset[str] = frozenset()
    occluded_visibility: float = 0.05
    off_frame: frozenset[str] = frozenset()
    mirrored: bool = False
    name: str = "scripted-synthetic"

    def detect(self, frame_bgr: np.ndarray, timestamp_ms: float) -> PoseObservation:
        index = self.clip.frame_index_for_timestamp(timestamp_ms)
        if index in self.drop_frames:
            return PoseObservation.missing("scripted tracking loss")

        clip = self.clip
        if self.mirrored != clip.mirrored:
            clip = SyntheticClip(
                width=clip.width,
                height=clip.height,
                fps=clip.fps,
                frame_count=clip.frame_count,
                handedness=clip.handedness,
                mirrored=self.mirrored,
                body_height_m=clip.body_height_m,
            )
        joints = synthetic_joints(index, clip=clip)
        adjusted: dict[str, Point] = {}
        for name, point in joints.items():
            x, y, z, visibility, presence = point
            if name in self.occlude:
                visibility = self.occluded_visibility
                presence = self.occluded_visibility
            if name in self.off_frame:
                x, y = -0.25, -0.25
            adjusted[name] = (x, y, z, visibility, presence)
        return joints_to_observation(adjusted, body_height_m=clip.body_height_m)

    def close(self) -> None:  # pragma: no cover - nothing to release
        return None


def synthetic_frame_count(
    clip: SyntheticClip, *, maximum: int | None = None
) -> int:
    if maximum is None:
        return clip.frame_count
    return min(clip.frame_count, maximum)


def iter_expected_frames(clip: SyntheticClip) -> Iterable[int]:
    return range(clip.frame_count)
