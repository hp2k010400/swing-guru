"""Projected swing measurements derived from pose landmarks.

Every measurement is reported with an explicit validity flag, the landmark
indices it used, and the reasons it was rejected. A measurement is only valid
when all of its required landmarks were detected, finite, inside the frame, and
above the configured visibility and presence thresholds. Nothing is carried
across a frame where tracking was lost.

Units are degrees or pixels. Pixel values are never converted to centimetres
because a single uncalibrated camera cannot supply that scale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from . import geometry
from .landmarks import (
    LANDMARK_NAMES,
    Landmark,
    NAME_TO_INDEX,
    PoseObservation,
    lead_side,
    normalize_handedness,
    side_landmark,
    trail_side,
)

# ---------------------------------------------------------------------------
# Warning catalogue
#
# These strings are part of the machine-readable output, so tests assert on the
# prefixes rather than on free text.
# ---------------------------------------------------------------------------

W_NO_POSE = "no pose detected in this frame"
W_LANDMARK_MISSING = "landmark missing from pose output"
W_VISIBILITY_MISSING = "landmark visibility is missing"
W_VISIBILITY_INVALID = "landmark visibility is not finite or outside 0..1"
W_LOW_VISIBILITY = "landmark visibility below threshold"
W_PRESENCE_MISSING = "landmark presence is missing (required)"
W_PRESENCE_INVALID = "landmark presence is not finite or outside 0..1"
W_LOW_PRESENCE = "landmark presence below threshold"
W_OUT_OF_FRAME = "landmark outside frame bounds"
W_NONFINITE = "landmark coordinate is not finite"
W_NO_REFERENCE = "no reference frame selected; relative measurement unavailable"
W_DEGENERATE = "degenerate geometry"
W_VIEWPOINT_FACE_ON = (
    "face-on camera view: angles are in-image projections and cannot separate "
    "forward bend from side bend"
)
W_VIEWPOINT_UNKNOWN = (
    "camera view not recorded: in-image angles only, no 3D interpretation"
)
W_VIEWPOINT_DOWN_THE_LINE = (
    "down-the-line camera view: in-image torso angle is dominated by the "
    "camera-to-golfer perspective"
)
W_SINGLE_FRAME = (
    "single-camera 2D projection: absolute body angles are not measured"
)

CAMERA_VIEWS: tuple[str, ...] = ("face-on", "down-the-line", "unknown")

_CAMERA_VIEW_ALIASES: Mapping[str, str] = {
    "face-on": "face-on",
    "faceon": "face-on",
    "face_on": "face-on",
    "front": "face-on",
    "frontal": "face-on",
    "down-the-line": "down-the-line",
    "down_the_line": "down-the-line",
    "down the line": "down-the-line",
    "dtl": "down-the-line",
    "side": "down-the-line",
    "unknown": "unknown",
    "unspecified": "unknown",
}


def normalize_camera_view(value: str | None) -> str:
    """Map loose view labels onto the three recorded conventions."""

    if value is None:
        return "unknown"
    cleaned = str(value).strip().lower()
    if cleaned in _CAMERA_VIEW_ALIASES:
        return _CAMERA_VIEW_ALIASES[cleaned]
    raise ValueError(f"unknown camera view: {value!r}")


@dataclass(frozen=True)
class MeasurementConfig:
    """Quality thresholds and camera/handedness conventions for one run."""

    min_visibility: float = 0.5
    min_presence: float = 0.5
    frame_margin: float = 0.0
    handedness: str = "right"
    camera_view: str = "unknown"
    require_presence: bool = True
    mirrored_input: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "handedness", normalize_handedness(self.handedness))
        object.__setattr__(self, "camera_view", normalize_camera_view(self.camera_view))
        if not 0.0 <= self.min_visibility <= 1.0:
            raise ValueError("min_visibility must be between 0 and 1")
        if not 0.0 <= self.min_presence <= 1.0:
            raise ValueError("min_presence must be between 0 and 1")
        if self.frame_margin < 0:
            raise ValueError("frame_margin must not be negative")

    @property
    def lead(self) -> str:
        return lead_side(self.handedness)

    @property
    def trail(self) -> str:
        return trail_side(self.handedness)

    def viewpoint_warning(self) -> str:
        if self.camera_view == "face-on":
            return W_VIEWPOINT_FACE_ON
        if self.camera_view == "down-the-line":
            return W_VIEWPOINT_DOWN_THE_LINE
        return W_VIEWPOINT_UNKNOWN

    def to_dict(self) -> dict[str, object]:
        return {
            "min_visibility": self.min_visibility,
            "min_presence": self.min_presence,
            "frame_margin": self.frame_margin,
            "handedness": self.handedness,
            "lead_side": self.lead,
            "trail_side": self.trail,
            "camera_view": self.camera_view,
            "require_presence": self.require_presence,
            "mirrored_input": self.mirrored_input,
        }


@dataclass(frozen=True)
class MeasurementSpec:
    """Static description of a measurement, used for docs and exports."""

    key: str
    label: str
    unit: str
    landmarks: tuple[str, ...]
    method: str
    limitations: str
    relative: bool = False
    side_landmarks: tuple[tuple[str, str], ...] = ()

    def resolved_landmarks(
        self, config: "MeasurementConfig | None" = None
    ) -> tuple[str, ...]:
        """Landmark names for this measurement under a handedness config.

        Specs whose anatomy depends on handedness declare ``side_landmarks``
        as ``(role, part)`` pairs; the names are then resolved from the
        config's lead/trail sides instead of assuming a right-handed golfer.
        """

        if config is None or not self.side_landmarks:
            return self.landmarks
        resolved: list[str] = []
        for role, part in self.side_landmarks:
            side = config.lead if role == "lead" else config.trail
            resolved.append(side_landmark(side, part))
        return tuple(resolved)

    def resolved_indices(
        self, config: "MeasurementConfig | None" = None
    ) -> tuple[int, ...]:
        return tuple(NAME_TO_INDEX[name] for name in self.resolved_landmarks(config))

    def to_dict(self, config: "MeasurementConfig | None" = None) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "landmarks": list(self.resolved_landmarks(config)),
            "landmark_indices": list(self.resolved_indices(config)),
            "landmarks_right_handed_template": list(self.landmarks),
            "method": self.method,
            "limitations": self.limitations,
            "relative_to_reference": self.relative,
            "resolved_side_rules": [f"{role}_{part}" for role, part in self.side_landmarks],
        }


MEASUREMENT_SPECS: tuple[MeasurementSpec, ...] = (
    MeasurementSpec(
        key="torso_inclination_deg",
        label="Torso inclination",
        unit="degrees",
        landmarks=("left_shoulder", "right_shoulder", "left_hip", "right_hip"),
        method=(
            "Angle between the hip midpoint to shoulder midpoint vector and "
            "image vertical, computed in aspect-corrected pixel coordinates. "
            "0 degrees is shoulders directly above hips in the image."
        ),
        limitations=(
            "Single-camera projection. A torso bending toward or away from the "
            "lens barely changes this value, so a face-on view cannot separate "
            "forward bend from side bend and 'forward bend' is not a valid "
            "label for this number."
        ),
    ),
    MeasurementSpec(
        key="lead_knee_flex_deg",
        label="Lead knee flex",
        unit="degrees",
        landmarks=("left_hip", "left_knee", "left_ankle"),
        method=(
            "180 minus the interior hip-knee-ankle angle at the knee, in "
            "aspect-corrected pixels. Straight leg is 0 degrees; a right-angled "
            "knee is 90 degrees. Landmark indices follow the selected "
            "handedness: left side for a right-handed golfer."
        ),
        limitations=(
            "Measures in-image flexion only. Rotation of the leg out of the "
            "image plane, plus clothing or occlusion of the knee, is invisible "
            "to this estimate."
        ),
        side_landmarks=(("lead", "hip"), ("lead", "knee"), ("lead", "ankle")),
    ),
    MeasurementSpec(
        key="trail_knee_flex_deg",
        label="Trail knee flex",
        unit="degrees",
        landmarks=("right_hip", "right_knee", "right_ankle"),
        method=(
            "Same interior-angle definition as the lead knee, applied to the "
            "trail side (right side for a right-handed golfer)."
        ),
        limitations=(
            "The trail leg is frequently occluded by the lead leg during the "
            "downswing, so expect more rejected frames than the lead side."
        ),
        side_landmarks=(("trail", "hip"), ("trail", "knee"), ("trail", "ankle")),
    ),
    MeasurementSpec(
        key="head_displacement_px",
        label="Head displacement",
        unit="pixels",
        landmarks=("nose",),
        method=(
            "Euclidean distance between the nose position in this frame and the "
            "nose position in the selected reference frame, in aspect-corrected "
            "pixels. The nose is an explicit head proxy, not a head-centre "
            "estimate."
        ),
        limitations=(
            "Pixel distances depend on camera distance and resolution and are "
            "not comparable between videos. Rotation of the head is not "
            "captured by a single point."
        ),
        relative=True,
    ),
    MeasurementSpec(
        key="head_displacement_pct_height",
        label="Head displacement (frame height)",
        unit="percent of frame height",
        landmarks=("nose",),
        method=(
            "Head proxy displacement divided by the frame height and expressed "
            "as a percentage, to make videos of different resolutions roughly "
            "comparable."
        ),
        limitations=(
            "Still a camera-relative measure: a golfer closer to the lens shows "
            "a larger percentage for the same real movement."
        ),
        relative=True,
    ),
    MeasurementSpec(
        key="pelvis_displacement_px",
        label="Pelvis displacement",
        unit="pixels",
        landmarks=("left_hip", "right_hip"),
        method=(
            "Euclidean distance between the hip midpoint in this frame and the "
            "hip midpoint in the reference frame, in aspect-corrected pixels."
        ),
        limitations=(
            "The hip midpoint is a 2D proxy for pelvis location. Pelvis "
            "rotation and hip tilt are not recovered."
        ),
        relative=True,
    ),
    MeasurementSpec(
        key="pelvis_displacement_pct_height",
        label="Pelvis displacement (frame height)",
        unit="percent of frame height",
        landmarks=("left_hip", "right_hip"),
        method=(
            "Pelvis displacement divided by the frame height and expressed as "
            "a percentage."
        ),
        limitations=(
            "Camera-relative. Depth (toward or away from the lens) does not "
            "appear in this number."
        ),
        relative=True,
    ),
    MeasurementSpec(
        key="torso_angle_change_deg",
        label="Torso angle change",
        unit="degrees",
        landmarks=("left_shoulder", "right_shoulder", "left_hip", "right_hip"),
        method=(
            "Current torso inclination minus the torso inclination of the "
            "reference frame. Unavailable when either frame fails the torso "
            "measurement's quality checks."
        ),
        limitations=(
            "Inherits every limitation of the torso inclination measurement. A "
            "missing or invalid reference frame yields no value at all; the "
            "engine never invents an address reference."
        ),
        relative=True,
    ),
)

MEASUREMENT_KEYS: tuple[str, ...] = tuple(spec.key for spec in MEASUREMENT_SPECS)
SPECS_BY_KEY: Mapping[str, MeasurementSpec] = {spec.key: spec for spec in MEASUREMENT_SPECS}


@dataclass(frozen=True)
class MeasurementResult:
    """One measurement for one frame, valid or explicitly rejected."""

    key: str
    label: str
    value: float | None
    unit: str
    valid: bool
    warnings: tuple[str, ...] = ()
    landmark_indices: tuple[int, ...] = ()
    method: str = ""
    reference_frame: int | None = None
    components: Mapping[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "valid": self.valid,
            "warnings": list(self.warnings),
            "landmark_indices": list(self.landmark_indices),
            "reference_frame": self.reference_frame,
            "components": dict(self.components),
        }

    def warning_text(self) -> str:
        return "; ".join(self.warnings)


def _rejected(
    spec: MeasurementSpec,
    warnings: Sequence[str],
    *,
    config: "MeasurementConfig | None" = None,
    reference_frame: int | None = None,
    landmark_indices: Sequence[int] | None = None,
) -> MeasurementResult:
    return MeasurementResult(
        key=spec.key,
        label=spec.label,
        value=None,
        unit=spec.unit,
        valid=False,
        warnings=_dedupe(warnings),
        landmark_indices=tuple(
            landmark_indices
            if landmark_indices is not None
            else spec.resolved_indices(config)
        ),
        method=spec.method,
        reference_frame=reference_frame,
    )


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    """Keep warning order but drop repeats, so a lost frame reads cleanly."""

    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def landmark_index(name: str) -> int:
    return NAME_TO_INDEX[name]


# ---------------------------------------------------------------------------
# Per-frame usability
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FramePoints:
    """Pixel-space points derived from one frame's pose observation."""

    pixels: Mapping[str, np.ndarray]
    landmarks: Mapping[str, Landmark]
    warnings: tuple[str, ...] = ()
    usable: tuple[str, ...] = ()
    rejected: Mapping[str, str] = field(default_factory=dict)

    def get(self, name: str) -> np.ndarray | None:
        return self.pixels.get(name)

    def has(self, *names: str) -> bool:
        return all(name in self.pixels for name in names)

    def reasons_for(self, *names: str) -> tuple[str, ...]:
        return tuple(self.rejected[name] for name in names if name in self.rejected)


# Points derived from other landmarks rather than read straight off the model.
DERIVED_POINT_NAMES: tuple[str, ...] = (
    "hip_mid",
    "shoulder_mid",
    "lead_shoulder",
    "trail_shoulder",
    "lead_hip",
    "trail_hip",
    "lead_knee",
    "trail_knee",
    "lead_ankle",
    "trail_ankle",
)


def landmark_rejection_reason(
    landmark: Landmark | None, name: str, config: MeasurementConfig
) -> str | None:
    """Why a landmark cannot be trusted, or ``None`` when it is usable.

    This is the single quality predicate for the engine: measurements, the
    landmark CSV's ``usable`` flag and the overlay colouring all call it, so
    they cannot disagree. It fails closed - a missing quality value, a
    non-finite value, or a value outside ``0..1`` rejects the landmark instead
    of being treated as good.
    """

    if landmark is None:
        return f"{W_LANDMARK_MISSING}: {name}"
    if not landmark.finite:
        return f"{W_NONFINITE}: {name}"
    if not landmark.in_frame(config.frame_margin):
        return f"{W_OUT_OF_FRAME}: {name}"

    visibility = landmark.visibility
    if visibility is None:
        return f"{W_VISIBILITY_MISSING}: {name}"
    if not math.isfinite(visibility) or not 0.0 <= visibility <= 1.0:
        return f"{W_VISIBILITY_INVALID}: {name}"
    if visibility < config.min_visibility:
        return (
            f"{W_LOW_VISIBILITY} ({visibility:.2f} < {config.min_visibility:.2f}):"
            f" {name}"
        )

    if config.require_presence:
        presence = landmark.presence
        if presence is None:
            return f"{W_PRESENCE_MISSING}: {name}"
        if not math.isfinite(presence) or not 0.0 <= presence <= 1.0:
            return f"{W_PRESENCE_INVALID}: {name}"
        if presence < config.min_presence:
            return (
                f"{W_LOW_PRESENCE} ({presence:.2f} < {config.min_presence:.2f}):"
                f" {name}"
            )
    return None


# Backwards-compatible private alias used inside this module.
_landmark_rejection_reason = landmark_rejection_reason


def build_frame_points(
    observation: PoseObservation,
    *,
    width: int,
    height: int,
    config: MeasurementConfig,
) -> FramePoints:
    """Convert usable landmarks into aspect-corrected pixel points.

    Derived points (hip midpoint, shoulder midpoint, lead/trail limb aliases)
    are added when their inputs survive the quality checks.
    """

    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")

    if not observation.detected:
        # A lost frame yields no points at all, even if a provider left stale
        # landmark values attached to the observation. Every required point
        # reports the tracking loss itself rather than 33 separate misses.
        rejected = {name: W_NO_POSE for name in LANDMARK_NAMES}
        rejected.update({name: W_NO_POSE for name in DERIVED_POINT_NAMES})
        return FramePoints(
            pixels={},
            landmarks={},
            warnings=(W_NO_POSE,),
            usable=(),
            rejected=rejected,
        )

    pixels: dict[str, np.ndarray] = {}
    kept: dict[str, Landmark] = {}
    rejected: dict[str, str] = {}

    for landmark in observation.landmarks:
        reason = _landmark_rejection_reason(landmark, landmark.name, config)
        if reason is None:
            pixels[landmark.name] = geometry.to_pixel(
                (landmark.x, landmark.y), width, height
            )
            kept[landmark.name] = landmark
        else:
            rejected[landmark.name] = reason

    # A landmark the model never reported is a rejection reason in its own
    # right, so a lookup for it explains itself instead of finding nothing.
    for name in LANDMARK_NAMES:
        if name not in pixels and name not in rejected:
            rejected[name] = f"{W_LANDMARK_MISSING}: {name}"

    warnings: list[str] = []

    def add_derived(name: str, sources: Sequence[str]) -> None:
        missing = [source for source in sources if source not in pixels]
        if missing:
            for source in missing:
                reason = rejected.get(source, f"{W_LANDMARK_MISSING}: {source}")
                rejected[name] = reason
            return
        pixels[name] = geometry.midpoint(pixels[sources[0]], pixels[sources[1]])

    add_derived("hip_mid", ("left_hip", "right_hip"))
    add_derived("shoulder_mid", ("left_shoulder", "right_shoulder"))

    lead = config.lead
    trail = config.trail
    alias_pairs = (
        ("lead_shoulder", side_landmark(lead, "shoulder")),
        ("trail_shoulder", side_landmark(trail, "shoulder")),
        ("lead_hip", side_landmark(lead, "hip")),
        ("trail_hip", side_landmark(trail, "hip")),
        ("lead_knee", side_landmark(lead, "knee")),
        ("trail_knee", side_landmark(trail, "knee")),
        ("lead_ankle", side_landmark(lead, "ankle")),
        ("trail_ankle", side_landmark(trail, "ankle")),
    )
    for alias, source in alias_pairs:
        if source in pixels:
            pixels[alias] = pixels[source]
            kept[alias] = kept[source]
        else:
            rejected[alias] = rejected.get(
                source, f"{W_LANDMARK_MISSING}: {source}"
            )

    return FramePoints(
        pixels=pixels,
        landmarks=kept,
        warnings=tuple(warnings),
        usable=tuple(sorted(pixels)),
        rejected=rejected,
    )


# ---------------------------------------------------------------------------
# Reference frame (relative measurements)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceState:
    """The manually selected address frame used as the origin for displacement.

    ``valid=False`` means no reference was selected, or the selected frame did
    not produce a usable point for a given measurement. Relative measurements
    are unavailable in that case. The engine never substitutes an automatic
    guess for a missing reference.
    """

    frame_index: int | None
    width: int
    height: int
    head_point: np.ndarray | None = None
    pelvis_point: np.ndarray | None = None
    torso_inclination_deg: float | None = None
    torso_spec_valid: bool = False
    notes: tuple[str, ...] = ()

    @property
    def selected(self) -> bool:
        return self.frame_index is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "width": self.width,
            "height": self.height,
            "head_point_px": None
            if self.head_point is None
            else [float(v) for v in self.head_point],
            "pelvis_point_px": None
            if self.pelvis_point is None
            else [float(v) for v in self.pelvis_point],
            "torso_inclination_deg": self.torso_inclination_deg,
            "notes": list(self.notes),
        }


def build_reference(
    frame_index: int | None,
    points: FramePoints | None,
    *,
    width: int,
    height: int,
    config: MeasurementConfig,
) -> ReferenceState:
    """Capture the reference-frame state used by relative measurements."""

    if frame_index is None:
        return ReferenceState(
            frame_index=None,
            width=width,
            height=height,
            notes=(W_NO_REFERENCE,),
        )
    if points is None:
        return ReferenceState(
            frame_index=frame_index,
            width=width,
            height=height,
            notes=(f"reference frame {frame_index} has no pose data",),
        )

    notes: list[str] = []
    head = points.get("nose")
    if head is None:
        notes.append(f"reference frame {frame_index} has no usable nose landmark")
    pelvis = points.get("hip_mid")
    if pelvis is None:
        notes.append(f"reference frame {frame_index} has no usable hip midpoint")

    inclination: float | None = None
    torso_valid = points.has("hip_mid", "shoulder_mid")
    if torso_valid:
        try:
            inclination = geometry.torso_inclination_degrees(
                points.pixels["hip_mid"], points.pixels["shoulder_mid"]
            )
        except geometry.DegenerateGeometryError:
            torso_valid = False
            inclination = None
            notes.append(
                f"reference frame {frame_index} torso geometry is degenerate"
            )

    return ReferenceState(
        frame_index=frame_index,
        width=width,
        height=height,
        head_point=None if head is None else head.copy(),
        pelvis_point=None if pelvis is None else pelvis.copy(),
        torso_inclination_deg=inclination,
        torso_spec_valid=torso_valid,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Individual measurements
# ---------------------------------------------------------------------------


def _require(points: FramePoints, *names: str) -> tuple[np.ndarray, ...] | None:
    if not points.has(*names):
        return None
    return tuple(points.pixels[name] for name in names)


def _viewpoint_notes(config: MeasurementConfig) -> tuple[str, ...]:
    return (config.viewpoint_warning(), W_SINGLE_FRAME)


def measure_torso_inclination(
    points: FramePoints, config: MeasurementConfig
) -> MeasurementResult:
    spec = SPECS_BY_KEY["torso_inclination_deg"]
    required = ("hip_mid", "shoulder_mid")
    resolved = _require(points, *required)
    if resolved is None:
        return _rejected(
            spec,
            (*points.reasons_for(*required), *_viewpoint_notes(config)),
        )
    hip_mid, shoulder_mid = resolved
    try:
        value = geometry.torso_inclination_degrees(hip_mid, shoulder_mid)
    except geometry.DegenerateGeometryError as error:
        return _rejected(spec, (f"{W_DEGENERATE}: {error}", *_viewpoint_notes(config)))
    return MeasurementResult(
        key=spec.key,
        label=spec.label,
        value=value,
        unit=spec.unit,
        valid=True,
        warnings=_viewpoint_notes(config),
        landmark_indices=tuple(NAME_TO_INDEX[name] for name in spec.landmarks),
        method=spec.method,
    )


def _measure_knee_flex(
    points: FramePoints, config: MeasurementConfig, limb: str
) -> MeasurementResult:
    """``limb`` is ``"lead"`` or ``"trail"``; handedness picks the anatomy."""

    if limb not in ("lead", "trail"):
        raise ValueError(f"limb must be 'lead' or 'trail', got {limb!r}")
    key = f"{limb}_knee_flex_deg"
    spec = SPECS_BY_KEY[key]
    required = (f"{limb}_hip", f"{limb}_knee", f"{limb}_ankle")
    resolved_indices = spec.resolved_indices(config)
    resolved = _require(points, *required)
    if resolved is None:
        return _rejected(
            spec,
            (*points.reasons_for(*required), *_viewpoint_notes(config)),
            config=config,
            landmark_indices=resolved_indices,
        )
    hip, knee, ankle = resolved
    try:
        value = geometry.knee_flex_degrees(hip, knee, ankle)
    except geometry.DegenerateGeometryError as error:
        return _rejected(
            spec,
            (f"{W_DEGENERATE}: {error}", *_viewpoint_notes(config)),
            config=config,
            landmark_indices=resolved_indices,
        )
    return MeasurementResult(
        key=spec.key,
        label=spec.label,
        value=value,
        unit=spec.unit,
        valid=True,
        warnings=_viewpoint_notes(config),
        landmark_indices=resolved_indices,
        method=spec.method,
    )


def measure_lead_knee_flex(
    points: FramePoints, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_knee_flex(points, config, "lead")


def measure_trail_knee_flex(
    points: FramePoints, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_knee_flex(points, config, "trail")


def _measure_displacement(
    points: FramePoints,
    reference: ReferenceState | None,
    *,
    key: str,
    point_name: str,
    reference_point: np.ndarray | None,
    config: MeasurementConfig,
    as_percent: bool,
) -> MeasurementResult:
    spec = SPECS_BY_KEY[key]
    current = points.get(point_name)
    if current is None:
        return _rejected(
            spec,
            (*points.reasons_for(point_name), *_viewpoint_notes(config)),
            reference_frame=None if reference is None else reference.frame_index,
        )
    if reference is None or not reference.selected:
        return _rejected(
            spec, (W_NO_REFERENCE, *_viewpoint_notes(config)), reference_frame=None
        )
    if reference_point is None:
        return _rejected(
            spec,
            (
                f"reference frame {reference.frame_index} has no usable "
                f"{point_name.replace('_', ' ')} landmark",
                *_viewpoint_notes(config),
            ),
            reference_frame=reference.frame_index,
        )

    distance = geometry.displacement_px(reference_point, current)
    components: dict[str, float] = {"displacement_px": distance}

    if as_percent:
        try:
            value = geometry.percent_of_reference(distance, float(reference.height))
        except geometry.DegenerateGeometryError as error:
            return _rejected(
                spec,
                (f"{W_DEGENERATE}: {error}", *_viewpoint_notes(config)),
                reference_frame=reference.frame_index,
            )
        components["frame_height_px"] = float(reference.height)
    else:
        value = distance
        components["frame_height_px"] = float(reference.height)

    return MeasurementResult(
        key=spec.key,
        label=spec.label,
        value=value,
        unit=spec.unit,
        valid=True,
        warnings=_viewpoint_notes(config),
        landmark_indices=tuple(NAME_TO_INDEX[name] for name in spec.landmarks),
        method=spec.method,
        reference_frame=reference.frame_index,
        components=components,
    )


def measure_head_displacement_px(
    points: FramePoints, reference: ReferenceState | None, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_displacement(
        points,
        reference,
        key="head_displacement_px",
        point_name="nose",
        reference_point=None if reference is None else reference.head_point,
        config=config,
        as_percent=False,
    )


def measure_head_displacement_pct(
    points: FramePoints, reference: ReferenceState | None, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_displacement(
        points,
        reference,
        key="head_displacement_pct_height",
        point_name="nose",
        reference_point=None if reference is None else reference.head_point,
        config=config,
        as_percent=True,
    )


def measure_pelvis_displacement_px(
    points: FramePoints, reference: ReferenceState | None, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_displacement(
        points,
        reference,
        key="pelvis_displacement_px",
        point_name="hip_mid",
        reference_point=None if reference is None else reference.pelvis_point,
        config=config,
        as_percent=False,
    )


def measure_pelvis_displacement_pct(
    points: FramePoints, reference: ReferenceState | None, config: MeasurementConfig
) -> MeasurementResult:
    return _measure_displacement(
        points,
        reference,
        key="pelvis_displacement_pct_height",
        point_name="hip_mid",
        reference_point=None if reference is None else reference.pelvis_point,
        config=config,
        as_percent=True,
    )


def measure_torso_angle_change(
    current: MeasurementResult | None,
    reference: ReferenceState | None,
    config: MeasurementConfig,
) -> MeasurementResult:
    spec = SPECS_BY_KEY["torso_angle_change_deg"]
    reference_frame = None if reference is None else reference.frame_index

    if reference is None or not reference.selected:
        return _rejected(
            spec, (W_NO_REFERENCE, *_viewpoint_notes(config)), reference_frame=None
        )
    if current is None or not current.valid or current.value is None:
        return _rejected(
            spec,
            (
                "current frame torso inclination is unavailable",
                *_viewpoint_notes(config),
            ),
            reference_frame=reference_frame,
        )
    if not reference.torso_spec_valid or reference.torso_inclination_deg is None:
        return _rejected(
            spec,
            (
                f"reference frame {reference.frame_index} torso inclination is "
                "unavailable",
                *_viewpoint_notes(config),
            ),
            reference_frame=reference_frame,
        )
    value = geometry.signed_angle_difference(
        current.value, reference.torso_inclination_deg
    )
    return MeasurementResult(
        key=spec.key,
        label=spec.label,
        value=value,
        unit=spec.unit,
        valid=True,
        warnings=_viewpoint_notes(config),
        landmark_indices=tuple(NAME_TO_INDEX[name] for name in spec.landmarks),
        method=spec.method,
        reference_frame=reference_frame,
        components={
            "current_deg": float(current.value),
            "reference_deg": float(reference.torso_inclination_deg),
        },
    )


def compute_frame_measurements(
    points: FramePoints,
    reference: ReferenceState | None,
    config: MeasurementConfig,
) -> dict[str, MeasurementResult]:
    """All v0.1 measurements for a single frame, keyed by measurement key."""

    torso = measure_torso_inclination(points, config)
    results: dict[str, MeasurementResult] = {
        torso.key: torso,
        "lead_knee_flex_deg": measure_lead_knee_flex(points, config),
        "trail_knee_flex_deg": measure_trail_knee_flex(points, config),
        "head_displacement_px": measure_head_displacement_px(points, reference, config),
        "head_displacement_pct_height": measure_head_displacement_pct(
            points, reference, config
        ),
        "pelvis_displacement_px": measure_pelvis_displacement_px(
            points, reference, config
        ),
        "pelvis_displacement_pct_height": measure_pelvis_displacement_pct(
            points, reference, config
        ),
        "torso_angle_change_deg": measure_torso_angle_change(torso, reference, config),
    }
    return {key: results[key] for key in MEASUREMENT_KEYS}


# ---------------------------------------------------------------------------
# Series summaries (stability)
# ---------------------------------------------------------------------------


def summarize_series(values: Iterable[float | None]) -> dict[str, float | int | None]:
    """Descriptive statistics over the valid values of a measurement series.

    Stability measurements (head and pelvis) are derived from these ranges and
    standard deviations rather than from an ideal range, because v0.1 has no
    validated normative data.
    """

    clean = [
        float(value)
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    if not clean:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "std": None,
            "range": None,
        }
    array = np.asarray(clean, dtype=float)
    return {
        "count": int(array.size),
        "min": float(array.min()),
        "max": float(array.max()),
        "mean": float(array.mean()),
        "std": float(array.std(ddof=0)),
        "range": float(array.max() - array.min()),
    }
