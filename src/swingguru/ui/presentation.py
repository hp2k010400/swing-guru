"""Golfer-facing presentation of the measured swing checkpoints.

The engine measures; this module decides how a golfer sees those measurements.
It owns three things and nothing else:

* :data:`CHECKPOINTS` - the seven ordered checkpoints and their fixed mapping to
  measurement keys that already exist in the engine. There is no new
  measurement, no phase detection and no scoring here.
* :func:`render_checkpoint_still` - a clean geometry renderer for the hero
  still, separate from the developer debug overlay in
  :mod:`swingguru.overlays`. Green geometry is drawn only for the current
  checkpoint's measurements that the engine marked valid, so a failed
  measurement leaves no geometry behind.
* :func:`measurement_rows` and the wording helpers - display values and plain
  language reasons, with the engine's raw warnings kept alongside for the
  Advanced analysis section.

Everything here is deterministic and local: no model, no network, no
inference, and no automatic checkpoint assignment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import cv2
import numpy as np

from ..measurements import (
    SPECS_BY_KEY,
    W_DEGENERATE,
    W_LANDMARK_MISSING,
    W_LOW_PRESENCE,
    W_LOW_VISIBILITY,
    W_NO_POSE,
    W_NO_REFERENCE,
    W_NONFINITE,
    W_OUT_OF_FRAME,
    W_PRESENCE_INVALID,
    W_PRESENCE_MISSING,
    W_SINGLE_FRAME,
    W_VIEWPOINT_DOWN_THE_LINE,
    W_VIEWPOINT_FACE_ON,
    W_VIEWPOINT_UNKNOWN,
    W_VISIBILITY_INVALID,
    W_VISIBILITY_MISSING,
    FramePoints,
    MeasurementConfig,
    MeasurementResult,
)
from ..phases import PHASE_ORDER, Phase

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

PALETTE: Mapping[str, str] = {
    "page": "#F7F7F2",
    "card": "#FFFFFF",
    "green": "#4F7F5F",
    "green_dark": "#365B43",
    "text": "#292929",
    "muted": "#7E7E7E",
    "border": "#E7E5DE",
    "reference": "#B8B8B2",
    "review": "#C49A45",
    "failure": "#B3261E",
}


def hex_to_bgr(value: str) -> tuple[int, int, int]:
    """``"#4F7F5F"`` -> the ``(blue, green, red)`` tuple OpenCV draws with."""

    cleaned = str(value).lstrip("#")
    if len(cleaned) != 6:
        raise ValueError(f"expected a 6-digit hex colour, got {value!r}")
    red, green, blue = (int(cleaned[index : index + 2], 16) for index in (0, 2, 4))
    return (blue, green, red)


COLOUR_MEASUREMENT = hex_to_bgr(PALETTE["green"])
COLOUR_MEASUREMENT_DARK = hex_to_bgr(PALETTE["green_dark"])
COLOUR_REFERENCE = hex_to_bgr(PALETTE["reference"])

EM_DASH = "\u2014"

UNIT_LABELS: Mapping[str, str] = {
    "degrees": "\u00b0",
    "pixels": "px",
    "percent of frame height": "% of frame height",
}

# One sentence that has to stay true for every number on screen.
CAMERA_NOTE = (
    "Numbers come from this camera view. Movement compares each still with the "
    "Address frame you chose."
)

# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointMeasurement:
    """One engine measurement as it is shown for a checkpoint."""

    key: str
    label: str
    unit: str


@dataclass(frozen=True)
class Checkpoint:
    """A single moment of the swing, its wording and its measurements."""

    phase: Phase
    title: str
    prompt: str
    description: str
    measurements: tuple[CheckpointMeasurement, ...]


def _measurement(key: str, label: str) -> CheckpointMeasurement:
    """Bind a golfer-facing label to an engine measurement key.

    The unit comes from the engine's own measurement spec, so a unit can never
    drift away from the value the engine actually reported. An unknown key
    fails at import time instead of showing an unmeasurable row.
    """

    return CheckpointMeasurement(key=key, label=label, unit=SPECS_BY_KEY[key].unit)


CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint(
        phase=Phase.ADDRESS,
        title="Address",
        prompt="Find the frame where you are set up over the ball.",
        description="Set-up posture and knee flex before the swing starts.",
        measurements=(
            _measurement("torso_inclination_deg", "Torso inclination"),
            _measurement("lead_knee_flex_deg", "Lead knee flex"),
            _measurement("trail_knee_flex_deg", "Trail knee flex"),
        ),
    ),
    Checkpoint(
        phase=Phase.TAKEAWAY,
        title="Takeaway",
        prompt="Find the frame where the club has just started back.",
        description="Early movement compared with the Address frame.",
        measurements=(
            _measurement("torso_inclination_deg", "Torso inclination"),
            _measurement("head_displacement_pct_height", "Head movement"),
        ),
    ),
    Checkpoint(
        phase=Phase.MID_BACK,
        title="Mid-Back",
        prompt="Find a frame part way through the backswing.",
        description="Posture and head position part way back.",
        measurements=(
            _measurement("torso_angle_change_deg", "Posture change"),
            _measurement("head_displacement_pct_height", "Head movement"),
        ),
    ),
    Checkpoint(
        phase=Phase.TOP,
        title="Top",
        prompt="Find the frame at the top of the backswing.",
        description="Top of the swing compared with the Address frame.",
        measurements=(
            _measurement("torso_angle_change_deg", "Posture change"),
            _measurement("head_displacement_pct_height", "Head movement"),
            _measurement("pelvis_displacement_pct_height", "Pelvis movement"),
        ),
    ),
    Checkpoint(
        phase=Phase.DOWNSWING,
        title="Mid-Down",
        prompt="Find a frame after the top, while the body is coming down.",
        description="Posture change and lower body movement on the way down.",
        measurements=(
            _measurement("torso_angle_change_deg", "Posture change"),
            _measurement("pelvis_displacement_pct_height", "Pelvis movement"),
        ),
    ),
    Checkpoint(
        phase=Phase.IMPACT,
        title="Impact",
        prompt="Find the frame where the club meets the ball.",
        description="Posture, head and pelvis at the ball.",
        measurements=(
            _measurement("torso_angle_change_deg", "Posture change"),
            _measurement("head_displacement_pct_height", "Head movement"),
            _measurement("pelvis_displacement_pct_height", "Pelvis movement"),
        ),
    ),
    Checkpoint(
        phase=Phase.FINISH,
        title="Finish",
        prompt="Find the frame where your swing settles at the end.",
        description="Where the body finishes relative to the Address frame.",
        measurements=(
            _measurement("torso_angle_change_deg", "Posture change"),
            _measurement("head_displacement_pct_height", "Head movement"),
            _measurement("pelvis_displacement_pct_height", "Pelvis movement"),
        ),
    ),
)

CHECKPOINT_BY_PHASE: Mapping[Phase, Checkpoint] = {
    checkpoint.phase: checkpoint for checkpoint in CHECKPOINTS
}

CHECKPOINT_INDEX_BY_PHASE: Mapping[Phase, int] = {
    checkpoint.phase: index for index, checkpoint in enumerate(CHECKPOINTS)
}


def checkpoint_index(phase: Phase | str) -> int:
    return CHECKPOINT_INDEX_BY_PHASE[Phase(phase)]


def clamp_checkpoint_index(value: object) -> int:
    """Clamp a stored or requested checkpoint position to the real range."""

    try:
        index = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(len(CHECKPOINTS) - 1, index))


def has_previous(index: int) -> bool:
    return clamp_checkpoint_index(index) > 0


def has_next(index: int) -> bool:
    return clamp_checkpoint_index(index) < len(CHECKPOINTS) - 1


# ---------------------------------------------------------------------------
# Display rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementRow:
    """One checkpoint measurement ready for display."""

    key: str
    label: str
    unit: str
    value_text: str
    needs_review: bool
    reason: str | None
    technical: tuple[str, ...]
    valid: bool


_FRIENDLY_BY_PREFIX: tuple[tuple[str, str], ...] = (
    (W_NO_POSE, "We could not find the golfer in this frame."),
    (W_NO_REFERENCE, "Choose Address to compare movement."),
    (W_LANDMARK_MISSING, "Part of the body was hidden in this frame."),
    (W_OUT_OF_FRAME, "The golfer is partly outside the frame."),
    (W_NONFINITE, "This frame's body position was not usable."),
    (W_VISIBILITY_MISSING, "This frame's body position was not reliable enough."),
    (W_VISIBILITY_INVALID, "This frame's body position was not reliable enough."),
    (W_LOW_VISIBILITY, "Too much of the body is hidden to measure this."),
    (W_PRESENCE_MISSING, "This frame's body position was not reliable enough."),
    (W_PRESENCE_INVALID, "This frame's body position was not reliable enough."),
    (W_LOW_PRESENCE, "Too much of the body is hidden to measure this."),
    (W_DEGENERATE, "The body position in this frame was too collapsed to measure."),
    ("reference frame", "The Address frame you chose has no usable position here."),
    (
        "current frame torso inclination is unavailable",
        "Torso angle cannot be measured in this frame.",
    ),
)

_CONTEXT_PREFIXES: tuple[str, ...] = (
    W_VIEWPOINT_FACE_ON,
    W_VIEWPOINT_DOWN_THE_LINE,
    W_VIEWPOINT_UNKNOWN,
    W_SINGLE_FRAME,
)

_FRIENDLY_FALLBACK = "This measurement could not be completed for this frame."


def is_context_warning(warning: str) -> bool:
    """True for run-wide viewpoint context that is not a failure."""

    return any(str(warning).startswith(prefix) for prefix in _CONTEXT_PREFIXES)


def measurement_warnings(result: MeasurementResult | None) -> tuple[str, ...]:
    """Failure warnings only; viewpoint context is not a per-frame failure."""

    if result is None:
        return ()
    return tuple(
        warning for warning in result.warnings if not is_context_warning(warning)
    )


def friendly_reason(result: MeasurementResult | None) -> str | None:
    """Plain-language reason a value is unavailable, or ``None`` when valid."""

    if result is not None and result.valid and result.value is not None:
        return None
    warnings = measurement_warnings(result)
    for warning in warnings:
        for prefix, message in _FRIENDLY_BY_PREFIX:
            if str(warning).startswith(prefix):
                return message
    return _FRIENDLY_FALLBACK


def technical_reasons(result: MeasurementResult | None) -> tuple[str, ...]:
    """The engine's own wording, for the Advanced analysis section."""

    if result is None:
        return ()
    return tuple(str(warning) for warning in result.warnings)


def unit_label(unit: str) -> str:
    return UNIT_LABELS.get(unit, unit)


def format_value(value: float | None, unit: str) -> str:
    """One decimal place for every unit, or an em dash when unavailable."""

    if value is None:
        return EM_DASH
    return f"{float(value):.1f}"


def measurement_rows(record: object | None, checkpoint: Checkpoint) -> tuple[MeasurementRow, ...]:
    """Display rows for one checkpoint, preserving engine validity exactly."""

    measurements: Mapping[str, MeasurementResult] = {}
    if record is not None:
        measurements = getattr(record, "measurements", {}) or {}

    rows: list[MeasurementRow] = []
    for measurement in checkpoint.measurements:
        result = measurements.get(measurement.key)
        valid = bool(result is not None and result.valid and result.value is not None)
        rows.append(
            MeasurementRow(
                key=measurement.key,
                label=measurement.label,
                unit=unit_label(measurement.unit),
                value_text=(
                    format_value(result.value, measurement.unit)
                    if valid and result is not None
                    else EM_DASH
                ),
                needs_review=not valid,
                reason=None if valid else friendly_reason(result),
                technical=technical_reasons(result),
                valid=valid,
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Wording for the manual checkpoint flow
# ---------------------------------------------------------------------------


def progress_caption(index: int) -> str:
    return f"Checkpoint {clamp_checkpoint_index(index) + 1} of {len(CHECKPOINTS)}"


def selection_caption(phase: Phase | str, frame_index: int | None) -> str:
    """Honest wording for a chosen or unchosen checkpoint moment."""

    checkpoint = CHECKPOINT_BY_PHASE[Phase(phase)]
    if frame_index is None:
        return (
            f"No {checkpoint.title} moment chosen yet. The still is a frame you "
            "are browsing; this app has not picked one for you."
        )
    return f"Chosen manually at frame {int(frame_index)}."


# ---------------------------------------------------------------------------
# Hero still rendering
# ---------------------------------------------------------------------------

_TORSO_KEYS: tuple[str, ...] = ("torso_inclination_deg", "torso_angle_change_deg")
_KNEE_KEYS: Mapping[str, str] = {
    "lead_knee_flex_deg": "lead",
    "trail_knee_flex_deg": "trail",
}
_HEAD_KEYS: tuple[str, ...] = (
    "head_displacement_px",
    "head_displacement_pct_height",
)
_PELVIS_KEYS: tuple[str, ...] = (
    "pelvis_displacement_px",
    "pelvis_displacement_pct_height",
)


def _geometry_style(width: int, height: int) -> tuple[int, int]:
    scale = max(0.6, min(2.0, min(width, height) / 640.0))
    return max(2, int(round(3 * scale))), max(4, int(round(6 * scale)))


def _point(points: FramePoints | None, name: str) -> np.ndarray | None:
    if points is None:
        return None
    return points.pixels.get(name)


def _anchor_point(record: object | None, name: str) -> np.ndarray | None:
    """Anchor position for a relative measurement.

    Accepts a :class:`~swingguru.pipeline.FrameRecord` (the comparison frame)
    or a :class:`~swingguru.measurements.ReferenceState`, so the same helper
    serves the analysed records and the player's geometry.
    """

    if record is None:
        return None
    points = getattr(record, "points", None)
    if points is not None and name in points.pixels:
        return _point(points, name)
    attribute = {"nose": "head_point", "hip_mid": "pelvis_point"}.get(name)
    if attribute is None:
        return None
    return getattr(record, attribute, None)


def _valid(record: object, key: str) -> bool:
    measurements: Mapping[str, MeasurementResult] = (
        getattr(record, "measurements", {}) or {}
    )
    result = measurements.get(key)
    return bool(result is not None and result.valid and result.value is not None)


@dataclass(frozen=True)
class Geometry:
    """One normalised drawing primitive for a measurement.

    ``points`` are fractions of the video frame (0..1), so the browser can
    scale them to whatever box the video is displayed in without knowing the
    source resolution - portrait, landscape and letterboxed playback included.
    ``radius`` (rings only) is a fraction of the frame *height*, so a ring stays
    round whatever the frame's aspect ratio.
    """

    kind: str  # "line", "polyline", "dot" or "ring"
    role: str  # "primary" or "reference"
    points: tuple[tuple[float, float], ...]
    filled: bool = True
    radius: float | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "role": self.role,
            "points": [[round(x, 5), round(y, 5)] for x, y in self.points],
            "filled": self.filled,
        }
        if self.radius is not None:
            payload["radius"] = round(self.radius, 5)
        return payload


ROLE_PRIMARY = "primary"
ROLE_REFERENCE = "reference"

# What each number is measured against, in a few words for the card.
MEASURED_AGAINST: Mapping[str, str] = {
    "torso_inclination_deg": "Hip-to-shoulder line vs vertical",
    "torso_angle_change_deg": "Torso angle vs Address",
    "lead_knee_flex_deg": "Bend from a straight leg",
    "trail_knee_flex_deg": "Bend from a straight leg",
    "head_displacement_pct_height": "Head position vs Address",
    "head_displacement_px": "Head position vs Address",
    "pelvis_displacement_pct_height": "Pelvis position vs Address",
    "pelvis_displacement_px": "Pelvis position vs Address",
}

# Landmark confidence at or above this reads as clear tracking. This is a
# statement about the pose model's own confidence in the points, not about the
# accuracy of the measurement.
CLEAR_TRACKING = 0.8


def _normalised(
    point: np.ndarray | None, width: int, height: int
) -> tuple[float, float] | None:
    if point is None or width <= 0 or height <= 0:
        return None
    return float(point[0]) / width, float(point[1]) / height


def _torso_length(points: FramePoints | None) -> float | None:
    hip = _point(points, "hip_mid")
    shoulder = _point(points, "shoulder_mid")
    if hip is None or shoulder is None:
        return None
    length = float(np.linalg.norm(np.asarray(shoulder, float) - np.asarray(hip, float)))
    return length if math.isfinite(length) and length > 1.0 else None


def _side_points(key: str, config: MeasurementConfig | None) -> tuple[str, str, str]:
    """Hip, knee and ankle names for a knee measurement.

    The engine resolves lead/trail from the configured handedness; the derived
    ``lead_*``/``trail_*`` points carry that resolution, so the overlay can
    never draw the other leg.
    """

    side = _KNEE_KEYS[key]
    return f"{side}_hip", f"{side}_knee", f"{side}_ankle"


def measurement_geometry(
    record: object,
    key: str,
    *,
    reference_record: object | None = None,
) -> tuple[Geometry, ...]:
    """Normalised primitives for one measurement, or nothing when it failed.

    Only this measurement's geometry is returned: no skeleton, no other
    measurement's lines, and nothing at all for a rejected measurement. Every
    measurement also draws *what it is measured against* as reference
    geometry: the vertical for torso inclination, a straight leg for knee flex
    and the Address position for posture and movement.
    """

    width = int(getattr(record, "width", 0))
    height = int(getattr(record, "height", 0))
    points: FramePoints | None = getattr(record, "points", None)
    if points is None or not _valid(record, key):
        return ()

    primitives: list[Geometry] = []

    def norm(point: np.ndarray | None) -> tuple[float, float] | None:
        return _normalised(point, width, height)

    def add(kind: str, role: str, raw: list[np.ndarray | None], **extra) -> None:
        pair = [norm(item) for item in raw]
        if not pair or any(item is None for item in pair):
            return
        primitives.append(
            Geometry(kind=kind, role=role, points=tuple(pair), **extra)  # type: ignore[arg-type]
        )

    if key in _TORSO_KEYS:
        hip = _point(points, "hip_mid")
        shoulder = _point(points, "shoulder_mid")
        if key == "torso_inclination_deg" and hip is not None:
            length = _torso_length(points)
            if length is not None:
                upright = np.asarray(hip, float) + np.array([0.0, -length])
                add("line", ROLE_REFERENCE, [hip, upright])
        if key == "torso_angle_change_deg" and reference_record is not None:
            reference_points = getattr(reference_record, "points", None)
            add(
                "line",
                ROLE_REFERENCE,
                [_point(reference_points, "hip_mid"), _point(reference_points, "shoulder_mid")],
            )
        add("line", ROLE_PRIMARY, [hip, shoulder])
        add("dot", ROLE_PRIMARY, [hip])
        add("dot", ROLE_PRIMARY, [shoulder])
        return tuple(primitives)

    if key in _KNEE_KEYS:
        hip_name, knee_name, ankle_name = _side_points(key, None)
        hip = _point(points, hip_name)
        knee = _point(points, knee_name)
        ankle = _point(points, ankle_name)
        if hip is not None and knee is not None and ankle is not None:
            thigh = np.asarray(knee, float) - np.asarray(hip, float)
            shin_length = float(np.linalg.norm(np.asarray(ankle, float) - np.asarray(knee, float)))
            thigh_length = float(np.linalg.norm(thigh))
            if thigh_length > 1e-6:
                straight = np.asarray(knee, float) + thigh / thigh_length * shin_length
                add("line", ROLE_REFERENCE, [knee, straight])
        add("polyline", ROLE_PRIMARY, [hip, knee, ankle])
        add("dot", ROLE_PRIMARY, [hip])
        add("dot", ROLE_PRIMARY, [knee])
        add("dot", ROLE_PRIMARY, [ankle])
        return tuple(primitives)

    if key in _HEAD_KEYS or key in _PELVIS_KEYS:
        name = "nose" if key in _HEAD_KEYS else "hip_mid"
        scale = 0.24 if key in _HEAD_KEYS else 0.18
        current = _point(points, name)
        anchor = _anchor_point(reference_record, name)
        length = _torso_length(points)
        radius = None if length is None or height <= 0 else scale * length / height
        if radius is not None:
            add("ring", ROLE_PRIMARY, [current], radius=radius, filled=False)
        if anchor is not None and current is not None:
            add("line", ROLE_PRIMARY, [anchor, current])
        add("dot", ROLE_PRIMARY, [current])
        # Drawn last so a small movement never hides where the head started.
        if radius is not None:
            add("ring", ROLE_REFERENCE, [anchor], radius=radius, filled=False)
        add("dot", ROLE_REFERENCE, [anchor], filled=False)
        return tuple(primitives)

    return ()


def measurement_anchor(record: object, key: str) -> tuple[float, float] | None:
    """Where the callout's leader line starts, on the measured geometry."""

    width = int(getattr(record, "width", 0))
    height = int(getattr(record, "height", 0))
    points: FramePoints | None = getattr(record, "points", None)
    if points is None:
        return None
    if key in _TORSO_KEYS:
        hip = _point(points, "hip_mid")
        shoulder = _point(points, "shoulder_mid")
        if hip is None or shoulder is None:
            return None
        return _normalised((np.asarray(hip, float) + np.asarray(shoulder, float)) / 2.0, width, height)
    if key in _KNEE_KEYS:
        return _normalised(_point(points, _side_points(key, None)[1]), width, height)
    if key in _HEAD_KEYS:
        return _normalised(_point(points, "nose"), width, height)
    if key in _PELVIS_KEYS:
        return _normalised(_point(points, "hip_mid"), width, height)
    return None


def tracking_confidence(record: object, key: str) -> float | None:
    """Lowest pose-model confidence among the landmarks a measurement used.

    Visibility and presence are the model's own estimates, so this says how
    sure the tracker was about the points, not whether the number is right.
    """

    points: FramePoints | None = getattr(record, "points", None)
    measurements: Mapping[str, MeasurementResult] = getattr(record, "measurements", {}) or {}
    result = measurements.get(key)
    if points is None or result is None:
        return None
    from ..landmarks import LANDMARK_NAMES

    values: list[float] = []
    for index in result.landmark_indices:
        if not 0 <= int(index) < len(LANDMARK_NAMES):
            continue
        landmark = points.landmarks.get(LANDMARK_NAMES[int(index)])
        if landmark is None:
            continue
        for value in (getattr(landmark, "visibility", None), getattr(landmark, "presence", None)):
            if value is not None and math.isfinite(float(value)):
                values.append(float(value))
    return min(values) if values else None


def tracking_level(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    return "clear" if confidence >= CLEAR_TRACKING else "uncertain"


def _draw_geometry(
    image: np.ndarray,
    primitives: tuple[Geometry, ...],
    thickness: int,
    radius: int,
) -> None:
    """Draw normalised primitives onto ``image`` in the presentation palette."""

    height, width = image.shape[:2]

    def to_pixel(point: tuple[float, float]) -> tuple[int, int]:
        return int(round(point[0] * width)), int(round(point[1] * height))

    for item in primitives:
        colour = (
            COLOUR_MEASUREMENT
            if item.role == ROLE_PRIMARY
            else COLOUR_REFERENCE
        )
        if item.kind in ("line", "polyline") and len(item.points) >= 2:
            for start, end in zip(item.points, item.points[1:]):
                cv2.line(
                    image,
                    to_pixel(start),
                    to_pixel(end),
                    colour,
                    thickness if item.role == ROLE_PRIMARY else max(1, thickness - 1),
                    cv2.LINE_AA,
                )
        elif item.kind == "ring" and item.points and item.radius:
            cv2.circle(
                image,
                to_pixel(item.points[0]),
                max(3, int(round(item.radius * height))),
                colour,
                max(1, thickness - 1),
                cv2.LINE_AA,
            )
        elif item.kind == "dot" and item.points:
            cv2.circle(
                image,
                to_pixel(item.points[0]),
                radius,
                colour,
                -1 if item.filled else max(2, radius // 2),
                cv2.LINE_AA,
            )


def render_checkpoint_still(
    frame_bgr: np.ndarray,
    record: object,
    checkpoint: Checkpoint,
    *,
    reference_record: object | None = None,
    config: MeasurementConfig | None = None,
) -> np.ndarray:
    """Clean geometry still for one checkpoint.

    Only the current checkpoint's measurements are drawn, and only when the
    engine marked them valid. There is no skeleton, no HUD, no identifier and
    no text on the image; values live below the still.

    ``config`` is accepted for call-site symmetry with the engine checks. The
    validity predicate used here is the engine's own measurement verdict, so
    the drawing cannot disagree with the number shown beneath it.
    """

    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        raise ValueError("cannot render a checkpoint still onto an empty frame")
    image = frame_bgr.copy()
    points = getattr(record, "points", None)
    if points is None or not getattr(record, "pose_detected", False):
        return image

    height, width = image.shape[:2]
    thickness, radius = _geometry_style(width, height)
    reference = reference_record
    if reference is None:
        reference = getattr(record, "reference", None)
    for measurement in checkpoint.measurements:
        primitives = measurement_geometry(
            record, measurement.key, reference_record=reference
        )
        _draw_geometry(image, primitives, thickness, radius)
    return image


__all__ = [
    "CAMERA_NOTE",
    "CHECKPOINTS",
    "CHECKPOINT_BY_PHASE",
    "CHECKPOINT_INDEX_BY_PHASE",
    "COLOUR_MEASUREMENT",
    "COLOUR_MEASUREMENT_DARK",
    "COLOUR_REFERENCE",
    "EM_DASH",
    "MEASURED_AGAINST",
    "measurement_anchor",
    "tracking_confidence",
    "tracking_level",
    "PALETTE",
    "Checkpoint",
    "CheckpointMeasurement",
    "MeasurementRow",
    "checkpoint_index",
    "clamp_checkpoint_index",
    "format_value",
    "friendly_reason",
    "has_next",
    "has_previous",
    "hex_to_bgr",
    "is_context_warning",
    "measurement_rows",
    "measurement_warnings",
    "progress_caption",
    "render_checkpoint_still",
    "selection_caption",
    "technical_reasons",
    "unit_label",
]
