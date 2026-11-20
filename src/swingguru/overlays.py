"""Visual debugging overlays drawn onto decoded frames.

The overlay is the primary accuracy-checking tool in v0.1: skeleton, joints,
measurement lines, values, frame/time, and quality state are all rendered onto
the frame so a human can compare what the engine believes against what the
golfer actually did.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

from .landmarks import LANDMARK_NAMES, POSE_CONNECTIONS, PoseObservation
from .measurements import (
    W_LOW_PRESENCE,
    W_LOW_VISIBILITY,
    FramePoints,
    MeasurementConfig,
    MeasurementResult,
    ReferenceState,
    landmark_rejection_reason,
)

# BGR colour plate.
COLOUR_GOOD = (80, 220, 80)
COLOUR_FAIR = (60, 190, 255)
COLOUR_POOR = (70, 70, 240)
COLOUR_JOINT = (255, 255, 255)
COLOUR_TORSO = (255, 210, 0)
COLOUR_LEAD = (120, 255, 120)
COLOUR_TRAIL = (255, 170, 90)
COLOUR_HEAD = (255, 80, 255)
COLOUR_PELVIS = (255, 200, 120)
COLOUR_TEXT = (255, 255, 255)
COLOUR_MUTED = (200, 200, 200)
COLOUR_ALERT = (60, 60, 255)


@runtime_checkable
class OverlayRecord(Protocol):
    """Everything the overlay needs from a processed frame."""

    index: int
    timestamp_ms: float
    observation: PoseObservation
    measurements: Mapping[str, MeasurementResult]
    points: FramePoints | None
    reference: ReferenceState | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class OverlayConfig:
    draw_skeleton: bool = True
    draw_measurements: bool = True
    draw_hud: bool = True
    draw_quality: bool = True
    show_all_landmarks: bool = False
    min_visibility: float = 0.5
    fps: float | None = None
    # When supplied, landmark colouring uses exactly the same quality rules the
    # measurements and the CSV export use, so the three cannot disagree.
    quality: MeasurementConfig | None = None


def _scale_for(width: int, height: int) -> tuple[float, float, int]:
    """Overlay scale factors derived from the frame size.

    A 480-pixel-tall frame is the reference size. Everything scales from there
    so a 4K clip does not render unreadable text and a small clip does not get
    a HUD that covers the golfer.
    """

    base = min(width, height)
    scale = max(0.5, min(2.0, base / 480.0))
    font_scale = 0.6 * scale
    thickness = max(1, int(round(1.4 * scale)))
    return scale, font_scale, thickness


def _quality_config(config: OverlayConfig) -> MeasurementConfig:
    return config.quality or MeasurementConfig(min_visibility=config.min_visibility)


def _quality_colour(landmark: object, config: OverlayConfig) -> tuple[int, int, int]:
    reason = landmark_rejection_reason(
        landmark,  # type: ignore[arg-type]
        getattr(landmark, "name", "landmark"),
        _quality_config(config),
    )
    if reason is None:
        return COLOUR_GOOD
    if reason.startswith(W_LOW_VISIBILITY) or reason.startswith(W_LOW_PRESENCE):
        return COLOUR_FAIR
    return COLOUR_POOR


def _text(
    image: np.ndarray,
    text: str,
    origin: tuple[int, int],
    *,
    scale: float,
    colour: tuple[int, int, int],
    thickness: int = 1,
    background: tuple[int, int, int] | None = None,
    pad: int = 3,
) -> None:
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_width, text_height), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = origin
    if background is not None:
        top_left = (max(0, x - pad), max(0, y - text_height - pad))
        bottom_right = (
            min(image.shape[1] - 1, x + text_width + pad),
            min(image.shape[0] - 1, y + baseline + pad),
        )
        if bottom_right[0] > top_left[0] and bottom_right[1] > top_left[1]:
            region = image[top_left[1] : bottom_right[1], top_left[0] : bottom_right[0]]
            overlay = np.full_like(region, background)
            cv2.addWeighted(overlay, 0.55, region, 0.45, 0, region)
    cv2.putText(
        image,
        text,
        (x, y),
        font,
        scale,
        colour,
        thickness,
        cv2.LINE_AA,
    )


def _fit_text(
    text: str,
    *,
    scale: float,
    thickness: int,
    max_width: int,
) -> str:
    font = cv2.FONT_HERSHEY_SIMPLEX
    candidate = text
    while candidate:
        (width, _), _ = cv2.getTextSize(candidate, font, scale, thickness)
        if width <= max_width:
            return candidate
        candidate = candidate[:-2]
    return ""


def _label(
    image: np.ndarray,
    text: str,
    x: int,
    y: int,
    *,
    scale: float,
    colour: tuple[int, int, int],
    thickness: int,
    align: str = "left",
) -> None:
    """Draw a label, clamped so it never runs off the frame."""

    if not text:
        return
    font = cv2.FONT_HERSHEY_SIMPLEX
    (text_width, text_height), _ = cv2.getTextSize(text, font, scale, thickness)
    max_x = max(2, image.shape[1] - text_width - 4)
    max_y = max(text_height + 4, image.shape[0] - 6)
    draw_x = x - text_width if align == "right" else x
    draw_x = int(max(4, min(draw_x, max_x)))
    draw_y = int(max(text_height + 4, min(y, max_y)))
    _text(
        image,
        text,
        (draw_x, draw_y),
        scale=scale,
        colour=colour,
        thickness=thickness,
        background=(0, 0, 0),
    )


def _point(points: FramePoints | None, name: str) -> np.ndarray | None:
    if points is None:
        return None
    return points.get(name)


def _draw_landmark_quality(
    image: np.ndarray,
    observation: PoseObservation,
    config: OverlayConfig,
    radius: int,
) -> None:
    by_index = {landmark.index: landmark for landmark in observation.landmarks}
    for start, end in POSE_CONNECTIONS:
        first = by_index.get(start)
        second = by_index.get(end)
        if first is None or second is None:
            continue
        if not (first.finite and second.finite):
            continue
        start_point = (int(round(first.x * image.shape[1])), int(round(first.y * image.shape[0])))
        end_point = (int(round(second.x * image.shape[1])), int(round(second.y * image.shape[0])))
        weaker = first if (first.visibility or 0) <= (second.visibility or 0) else second
        colour = _quality_colour(weaker, config)
        cv2.line(image, start_point, end_point, colour, max(1, radius // 2), cv2.LINE_AA)

    for landmark in observation.landmarks:
        if not landmark.finite:
            continue
        if not config.show_all_landmarks and landmark.index > 10 and not (
            11 <= landmark.index <= 32
        ):
            continue
        centre = (
            int(round(landmark.x * image.shape[1])),
            int(round(landmark.y * image.shape[0])),
        )
        cv2.circle(
            image,
            centre,
            radius,
            _quality_colour(landmark, config),
            -1,
            cv2.LINE_AA,
        )
        if config.show_all_landmarks:
            _text(
                image,
                LANDMARK_NAMES[landmark.index] if landmark.index < len(LANDMARK_NAMES) else "?",
                (centre[0] + radius + 2, centre[1] - radius - 2),
                scale=max(0.3, radius / 22.0),
                colour=COLOUR_MUTED,
            )


def _draw_measurement_lines(
    image: np.ndarray,
    record: OverlayRecord,
    config: OverlayConfig,
    thickness: int,
    text_scale: float,
) -> None:
    points = record.points
    if points is None:
        return

    hip_mid = _point(points, "hip_mid")
    shoulder_mid = _point(points, "shoulder_mid")
    if hip_mid is not None and shoulder_mid is not None:
        start = (int(hip_mid[0]), int(hip_mid[1]))
        end = (int(shoulder_mid[0]), int(shoulder_mid[1]))
        cv2.line(image, start, end, COLOUR_TORSO, thickness + 1, cv2.LINE_AA)
        result = record.measurements.get("torso_inclination_deg")
        label = (
            f"torso {result.value:.1f} deg"
            if result is not None and result.valid and result.value is not None
            else "torso n/a"
        )
        text_colour = COLOUR_TORSO if label != "torso n/a" else COLOUR_ALERT
        # Offset the label perpendicular to the torso line so it does not sit
        # on top of the skeleton.
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
        offset = int(max(12.0, 18.0 * text_scale / 0.6))
        perp_x = int(-dy / length * offset)
        perp_y = int(dx / length * offset)
        _label(
            image,
            label,
            (start[0] + end[0]) // 2 + perp_x,
            (start[1] + end[1]) // 2 + perp_y,
            scale=text_scale,
            colour=text_colour,
            thickness=thickness,
        )

    for limb, colour in (("lead", COLOUR_LEAD), ("trail", COLOUR_TRAIL)):
        hip = _point(points, f"{limb}_hip")
        knee = _point(points, f"{limb}_knee")
        ankle = _point(points, f"{limb}_ankle")
        if hip is None or knee is None:
            continue
        knee_point = (int(knee[0]), int(knee[1]))
        cv2.line(image, (int(hip[0]), int(hip[1])), knee_point, colour, thickness, cv2.LINE_AA)
        if ankle is not None:
            cv2.line(image, knee_point, (int(ankle[0]), int(ankle[1])), colour, thickness, cv2.LINE_AA)
        result = record.measurements.get(f"{limb}_knee_flex_deg")
        label = (
            f"{limb} knee {result.value:.1f} deg"
            if result is not None and result.valid and result.value is not None
            else f"{limb} knee n/a"
        )
        side_offset = int(max(8.0, 14.0 * text_scale / 0.6))
        _label(
            image,
            label,
            knee_point[0] + (side_offset if limb == "lead" else -side_offset),
            knee_point[1] + (0 if limb == "lead" else 14),
            scale=text_scale,
            colour=colour if "n/a" not in label else COLOUR_ALERT,
            thickness=thickness,
            align="left" if limb == "lead" else "right",
        )

    nose = _point(points, "nose")
    if nose is not None:
        cv2.circle(image, (int(nose[0]), int(nose[1])), max(3, thickness * 2), COLOUR_HEAD, -1, cv2.LINE_AA)
        _label(
            image,
            "nose",
            int(nose[0]) + 10,
            int(nose[1]) - 8,
            scale=text_scale * 0.9,
            colour=COLOUR_HEAD,
            thickness=thickness,
        )
    if hip_mid is not None:
        cv2.circle(
            image,
            (int(hip_mid[0]), int(hip_mid[1])),
            max(3, thickness * 2),
            COLOUR_PELVIS,
            -1,
            cv2.LINE_AA,
        )
        _label(
            image,
            "hip mid",
            int(hip_mid[0]) - 10,
            int(hip_mid[1]) + 18,
            scale=text_scale * 0.9,
            colour=COLOUR_PELVIS,
            thickness=thickness,
            align="right",
        )

    reference = record.reference
    if reference is not None and reference.selected:
        for point_name, reference_point, colour, key in (
            ("nose", reference.head_point, COLOUR_HEAD, "head_displacement_px"),
            ("hip_mid", reference.pelvis_point, COLOUR_PELVIS, "pelvis_displacement_px"),
        ):
            current = _point(points, point_name)
            if current is None or reference_point is None:
                continue
            start = (int(reference_point[0]), int(reference_point[1]))
            end = (int(current[0]), int(current[1]))
            cv2.line(image, start, end, colour, 1, cv2.LINE_4)
            cv2.circle(image, start, max(2, thickness), colour, 1, cv2.LINE_AA)
            result = record.measurements.get(key)
            if result is not None and result.valid and result.value is not None:
                _label(
                    image,
                    f"{result.value:.1f} px",
                    (start[0] + end[0]) // 2 + 6,
                    (start[1] + end[1]) // 2 - 6,
                    scale=text_scale * 0.85,
                    colour=colour,
                    thickness=thickness,
                )


# Warnings that hold for every frame of a run. They belong in the exports and
# metadata, not in a per-frame HUD that has to stay readable.
_HUD_CONTEXT_PREFIXES = (
    "face-on camera view",
    "down-the-line camera view",
    "camera view not recorded",
    "single-camera 2D projection",
)

_SHORT_REASONS = (
    ("no pose detected in this frame", "no pose"),
    ("landmark missing from pose output", "missing landmark"),
    ("landmark outside frame bounds", "out of frame"),
    ("landmark coordinate is not finite", "non-finite"),
    ("landmark visibility below threshold", "low visibility"),
    ("landmark presence below threshold", "low presence"),
    ("degenerate geometry", "degenerate geometry"),
    ("no reference frame selected", "no reference"),
    ("reference frame", "reference unusable"),
    ("current frame torso inclination is unavailable", "no torso value"),
)


def _short_reason(warning: str) -> str | None:
    """Compact, HUD-sized reason, or ``None`` for run-level context warnings."""

    for prefix in _HUD_CONTEXT_PREFIXES:
        if warning.startswith(prefix):
            return None
    for prefix, short in _SHORT_REASONS:
        if warning.startswith(prefix):
            return short
    return warning.split(":")[0].strip()[:24]


def _draw_hud(
    image: np.ndarray,
    record: OverlayRecord,
    config: OverlayConfig,
    text_scale: float,
    thickness: int,
) -> None:
    height, width = image.shape[:2]
    lines: list[tuple[str, tuple[int, int, int]]] = []

    stamp = f"frame {record.index}   t {record.timestamp_ms:.0f} ms"
    if config.fps:
        stamp += f"   {config.fps:.1f} fps"
    lines.append((stamp, COLOUR_TEXT))

    detected = record.observation.detected
    lines.append(
        (
            "pose: detected" if detected else "pose: NOT DETECTED",
            COLOUR_GOOD if detected else COLOUR_ALERT,
        )
    )

    if config.draw_quality:
        valid = [r for r in record.measurements.values() if r.valid]
        rejected = [r for r in record.measurements.values() if not r.valid]
        lines.append(
            (
                f"measurements valid: {len(valid)}/{len(record.measurements)}",
                COLOUR_GOOD if not rejected else COLOUR_FAIR,
            )
        )
        shown = 0
        for result in rejected:
            if shown >= 2:
                break
            reason = next(
                (
                    short
                    for warning in result.warnings
                    if (short := _short_reason(warning)) is not None
                ),
                None,
            )
            if reason is None:
                continue
            lines.append((f"  {result.key}: {reason}", COLOUR_ALERT))
            shown += 1
        if len(rejected) > shown:
            lines.append((f"  +{len(rejected) - shown} more rejected", COLOUR_MUTED))
        for note in record.notes[:1]:
            lines.append((f"  {note}", COLOUR_FAIR))

    y = int(round(text_scale * 22)) + 6
    line_height = int(round(max(12.0, text_scale * 26)))
    max_width = width - 24
    for text, colour in lines:
        fitted = _fit_text(
            text, scale=text_scale, thickness=thickness, max_width=max_width
        )
        if not fitted:
            continue
        _text(
            image,
            fitted,
            (10, y),
            scale=text_scale,
            colour=colour,
            thickness=thickness,
            background=(0, 0, 0),
        )
        y += line_height


def render_overlay(
    frame_bgr: np.ndarray,
    record: OverlayRecord,
    config: OverlayConfig | None = None,
) -> np.ndarray:
    """Return a copy of ``frame_bgr`` annotated with pose and measurements."""

    settings = config or OverlayConfig()
    if frame_bgr is None or frame_bgr.size == 0:
        raise ValueError("cannot render an overlay onto an empty frame")
    image = frame_bgr.copy()
    height, width = image.shape[:2]
    _, text_scale, thickness = _scale_for(width, height)
    radius = max(2, thickness)

    if settings.draw_skeleton and record.observation.detected:
        _draw_landmark_quality(image, record.observation, settings, radius)
    if settings.draw_measurements:
        _draw_measurement_lines(image, record, settings, thickness, text_scale)
    if settings.draw_hud:
        _draw_hud(image, record, settings, text_scale, thickness)
    return image
