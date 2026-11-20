"""Rear-of-pelvis ("tush line") measurement for down-the-line video.

Instruction definition (see docs/research/golf-interpretation-rules.md): at
Address a vertical line is drawn down from the golfer's rear; early extension
is described as the rear moving forward off that line toward the ball. This
module is Swing Guru's own 2D approximation of that line - not a copy of any
vendor metric:

1. A person silhouette mask (MediaPipe pose segmentation, run only on the key
   frames) gives the golfer's outline.
2. A pelvis band is defined from the RTMW hip joints: from ``band_top`` to
   ``band_bottom`` torso lengths around the hip midpoint (the glutes sit at
   and just below the hip joints).
3. On every row of the band the silhouette is scanned from the hip midpoint
   *away from the ball* to where the mask falls below 0.5; the crossing is
   refined to sub-pixel. The rear point is a high percentile of those
   crossings (the rearmost part of the pelvis outline, robust to a single
   ragged row).
4. The Address rear point defines the tush line (a vertical line at its x).
   Later phases report the horizontal displacement of the rear point from that
   line: positive = toward the ball, normalised by body height.

Because this follows the silhouette, it measures whichever glute forms the
rear outline - which is what the tush line does on video - rather than the
hip-joint midpoint, which moves differently when the pelvis rotates or the
golfer squats.

It fails closed: no hips, hip midpoint outside the silhouette, too few valid
rows, the outline touching the frame edge, or a soft/ambiguous mask edge all
make the rear point unavailable with a reason. Reliability thresholds are
engineering thresholds on mask quality, not golf thresholds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

LEVEL_HIGH = "HIGH"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RearPelvisConfig:
    band_top: float = -0.05  # torso lengths above the hip midpoint (negative = up)
    band_bottom: float = 0.30  # torso lengths below the hip midpoint
    mask_threshold: float = 0.5
    rear_percentile: float = 90.0
    min_row_fraction: float = 0.70
    high_row_fraction: float = 0.85
    min_edge_margin_px: float = 3.0
    high_edge_margin_px: float = 8.0
    contrast_probe_px: int = 4
    min_contrast: float = 0.55
    high_contrast: float = 0.80
    max_rear_depth: float = 0.9  # torso lengths from hip midpoint to the outline
    min_rear_depth: float = 0.05
    max_row_spread: float = 0.35  # torso lengths between rearmost and median row
    min_hip_score: float = 0.45


@dataclass(frozen=True)
class RearPelvis:
    frame_index: int
    available: bool
    level: str
    point: tuple[float, float] | None = None  # rear point (x, y) in pixels
    band: tuple[float, float] | None = None  # y range scanned
    rows_valid: float = 0.0
    edge_margin_px: float | None = None
    contrast: float | None = None
    depth_torso: float | None = None
    reason: str = ""
    boundary: tuple[tuple[float, float], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "available": self.available,
            "level": self.level,
            "point": None if self.point is None else [round(v, 2) for v in self.point],
            "band": None if self.band is None else [round(v, 1) for v in self.band],
            "rows_valid": round(self.rows_valid, 3),
            "edge_margin_px": None if self.edge_margin_px is None else round(self.edge_margin_px, 1),
            "contrast": None if self.contrast is None else round(self.contrast, 3),
            "depth_torso": None if self.depth_torso is None else round(self.depth_torso, 3),
            "reason": self.reason,
        }


def _unavailable(frame_index: int, reason: str, **extra) -> RearPelvis:
    return RearPelvis(frame_index, False, LEVEL_UNAVAILABLE, reason=reason, **extra)


def _crossing(row: np.ndarray, start: int, step: int, threshold: float) -> tuple[float | None, bool]:
    """Sub-pixel x where the mask drops below ``threshold`` walking from ``start``.

    Returns ``(x, clipped)``; ``clipped`` is True when the frame edge is reached
    while still inside the silhouette.
    """

    x = start
    width = row.shape[0]
    while 0 <= x < width:
        if row[x] < threshold:
            inside = x - step
            a, b = float(row[inside]), float(row[x])
            fraction = 0.5 if a == b else (a - threshold) / (a - b)
            return inside + step * fraction, False
        x += step
    return None, True


def rear_pelvis(
    mask: np.ndarray,
    hip_mid: np.ndarray | None,
    *,
    toward_ball: float,
    torso_px: float,
    frame_index: int = 0,
    hip_score: float | None = None,
    config: RearPelvisConfig | None = None,
) -> RearPelvis:
    """Rear-of-pelvis point on one frame's silhouette, or unavailable."""

    settings = config or RearPelvisConfig()
    if mask is None or mask.ndim != 2 or mask.size == 0:
        return _unavailable(frame_index, "no silhouette for this frame")
    if hip_mid is None or not np.all(np.isfinite(hip_mid)) or torso_px <= 1:
        return _unavailable(frame_index, "hips not tracked")
    if hip_score is not None and hip_score < settings.min_hip_score:
        return _unavailable(frame_index, f"hip tracking confidence {hip_score:.2f} too low")
    if toward_ball not in (1, -1, 1.0, -1.0):
        return _unavailable(frame_index, "direction toward the ball unknown")

    height, width = mask.shape
    hx, hy = float(hip_mid[0]), float(hip_mid[1])
    x0 = int(round(hx))
    if not (0 <= x0 < width and 0 <= int(round(hy)) < height):
        return _unavailable(frame_index, "hip midpoint outside the frame")
    top = int(math.floor(hy + settings.band_top * torso_px))
    bottom = int(math.ceil(hy + settings.band_bottom * torso_px))
    top, bottom = max(0, top), min(height - 1, bottom)
    rows = list(range(top, bottom + 1))
    if len(rows) < 3:
        return _unavailable(frame_index, "pelvis band too small")

    step = -1 if toward_ball > 0 else 1  # walk away from the ball
    crossings: list[tuple[float, float]] = []
    clipped_rows = 0
    for y in rows:
        row = mask[y]
        if row[x0] < settings.mask_threshold:
            continue  # hip midpoint not inside the silhouette on this row
        x, clipped = _crossing(row, x0, step, settings.mask_threshold)
        if clipped:
            clipped_rows += 1
            continue
        if x is not None:
            crossings.append((x, float(y)))
    fraction = len(crossings) / len(rows)
    boundary = tuple(crossings)
    if clipped_rows:
        return _unavailable(frame_index, "the pelvis outline touches the frame edge",
                            band=(float(top), float(bottom)), rows_valid=fraction, boundary=boundary)
    if fraction < settings.min_row_fraction:
        return _unavailable(frame_index, f"only {fraction:.0%} of the pelvis band has a clear outline",
                            band=(float(top), float(bottom)), rows_valid=fraction, boundary=boundary)

    # Rearmost outline = most negative (toward_ball > 0) or most positive x.
    rear_extent = np.array([(hx - x) * toward_ball for x, _ in crossings])
    target = float(np.percentile(rear_extent, settings.rear_percentile))
    index = int(np.argmin(np.abs(rear_extent - target)))
    rear_x, rear_y = crossings[index]
    depth = target / torso_px
    spread = (target - float(np.median(rear_extent))) / torso_px
    if not settings.min_rear_depth <= depth <= settings.max_rear_depth:
        return _unavailable(frame_index, f"rear outline {depth:.2f} torso lengths from the hips is implausible",
                            band=(float(top), float(bottom)), rows_valid=fraction, boundary=boundary)
    if spread > settings.max_row_spread:
        return _unavailable(frame_index, "the rear outline is ragged (something else may overlap the pelvis)",
                            band=(float(top), float(bottom)), rows_valid=fraction, boundary=boundary)

    margin = rear_x if step < 0 else (width - 1 - rear_x)
    probe = settings.contrast_probe_px
    contrasts = []
    for x, y in crossings:
        yi = int(y)
        inside = int(round(x)) - step * probe
        outside = int(round(x)) + step * probe
        if 0 <= inside < width and 0 <= outside < width:
            contrasts.append(float(mask[yi, inside]) - float(mask[yi, outside]))
    contrast = float(np.median(contrasts)) if contrasts else 0.0
    if margin < settings.min_edge_margin_px:
        return _unavailable(frame_index, f"the rear outline is {margin:.0f}px from the frame edge",
                            band=(float(top), float(bottom)), rows_valid=fraction, edge_margin_px=margin,
                            boundary=boundary)
    if contrast < settings.min_contrast:
        return _unavailable(frame_index, f"the silhouette edge is soft (contrast {contrast:.2f})",
                            band=(float(top), float(bottom)), rows_valid=fraction, edge_margin_px=margin,
                            contrast=contrast, boundary=boundary)
    high = (
        fraction >= settings.high_row_fraction
        and margin >= settings.high_edge_margin_px
        and contrast >= settings.high_contrast
    )
    return RearPelvis(
        frame_index, True, LEVEL_HIGH if high else LEVEL_MEDIUM,
        point=(rear_x, rear_y), band=(float(top), float(bottom)), rows_valid=fraction,
        edge_margin_px=margin, contrast=contrast, depth_torso=depth, boundary=boundary,
    )


@dataclass(frozen=True)
class PelvisDepthResult:
    """Rear-pelvis displacement at one phase relative to the Address tush line."""

    phase: str
    available: bool
    level: str
    displacement_px: float | None = None  # + toward the ball
    displacement_pct_height: float | None = None
    direction: str = ""
    reason: str = ""
    rear: RearPelvis | None = None

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "available": self.available,
            "level": self.level,
            "displacement_px": None if self.displacement_px is None else round(self.displacement_px, 2),
            "displacement_pct_height": None if self.displacement_pct_height is None else round(self.displacement_pct_height, 3),
            "direction": self.direction,
            "reason": self.reason,
            "rear": None if self.rear is None else self.rear.to_dict(),
        }


NOISE_FLOOR_PCT = 0.5


def displacement_from_line(
    phase: str,
    address: RearPelvis,
    current: RearPelvis,
    *,
    toward_ball: float,
    body_height_px: float | None,
) -> PelvisDepthResult:
    """Horizontal movement of the rear point relative to the Address tush line."""

    if not address.available:
        return PelvisDepthResult(phase, False, LEVEL_UNAVAILABLE, reason=f"no Address tush line: {address.reason}")
    if not current.available:
        return PelvisDepthResult(phase, False, LEVEL_UNAVAILABLE, reason=current.reason, rear=current)
    if not body_height_px or body_height_px <= 1:
        return PelvisDepthResult(phase, False, LEVEL_UNAVAILABLE, reason="no body height to normalise by", rear=current)
    delta = (current.point[0] - address.point[0]) * float(toward_ball)  # type: ignore[index]
    pct = 100.0 * delta / body_height_px
    if abs(pct) < NOISE_FLOOR_PCT:
        direction = "on the Address line"
    else:
        direction = "toward the ball" if pct > 0 else "away from the ball"
    level = LEVEL_HIGH if address.level == LEVEL_HIGH and current.level == LEVEL_HIGH else LEVEL_MEDIUM
    return PelvisDepthResult(phase, True, level, displacement_px=delta, displacement_pct_height=pct,
                             direction=direction, rear=current)


class PersonSegmenter:
    """MediaPipe pose segmentation on single frames (lazy; key frames only)."""

    def __init__(self, model_path: str | None = None) -> None:
        self._model_path = model_path
        self._landmarker = None

    def _ensure(self):
        if self._landmarker is None:
            from mediapipe.tasks.python import BaseOptions, vision

            from .pose import default_model_path

            options = vision.PoseLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(self._model_path or default_model_path())),
                running_mode=vision.RunningMode.IMAGE,
                output_segmentation_masks=True,
            )
            self._landmarker = vision.PoseLandmarker.create_from_options(options)
        return self._landmarker

    def mask(self, frame_bgr: np.ndarray) -> np.ndarray | None:
        import cv2
        import mediapipe as mp

        # MediaPipe's float segmentation mask crashes the whole process (native abort in
        # MpImageDataFloat32) when the frame width is not a multiple of 4 - common for clips saved
        # from social apps (478, 606, 866 px wide). Pad 1-3 black columns on the right only for such
        # widths and crop the mask back: no pixel moves, and aligned widths are untouched.
        width = frame_bgr.shape[1]
        pad = (-width) % 4
        if pad:
            frame_bgr = cv2.copyMakeBorder(frame_bgr, 0, 0, 0, pad, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        result = self._ensure().detect(
            mp.Image(image_format=mp.ImageFormat.SRGB,
                     data=np.ascontiguousarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)))
        )
        if not result.segmentation_masks:
            return None
        # numpy_view() points into memory owned by the MediaPipe result; copy it
        # before the result is released, or later reads touch freed memory.
        mask = np.array(result.segmentation_masks[0].numpy_view(), dtype=np.float32, copy=True).squeeze()
        return mask[:, :width] if pad else mask

    def close(self) -> None:
        if self._landmarker is not None:
            self._landmarker.close()
            self._landmarker = None


def measure_pelvis_depth(
    frames: Mapping[str, tuple[int, np.ndarray | None, object | None]],
    masks: Mapping[str, np.ndarray | None],
    *,
    toward_ball: float | None,
    torso_px: float,
    body_height_px: float | None,
    hip_scores: Mapping[str, float | None] | None = None,
    phases: Sequence[str] = ("top", "downswing", "impact"),
) -> dict:
    """Rear-pelvis reference at Address and displacement at later phases.

    ``frames`` maps phase -> (frame_index, frame, record); ``masks`` maps phase
    -> silhouette mask. Returns a JSON-ready dict.
    """

    scores = hip_scores or {}

    def rear_for(phase: str) -> RearPelvis:
        index, _, record = frames.get(phase, (0, None, None))
        hip = None
        if record is not None and getattr(record, "points", None) is not None:
            value = record.points.pixels.get("hip_mid")
            hip = None if value is None else np.asarray(value, float)
        if toward_ball is None:
            return _unavailable(int(index), "direction toward the ball unknown")
        return rear_pelvis(masks.get(phase), hip, toward_ball=toward_ball, torso_px=torso_px,
                           frame_index=int(index), hip_score=scores.get(phase))

    address = rear_for("address")
    results = {}
    for phase in phases:
        if phase not in frames:
            results[phase] = PelvisDepthResult(phase, False, LEVEL_UNAVAILABLE, reason="moment not found").to_dict()
            continue
        results[phase] = displacement_from_line(
            phase, address, rear_for(phase), toward_ball=toward_ball or 0.0, body_height_px=body_height_px
        ).to_dict()
    return {
        "method": "silhouette-rear-of-pelvis v1",
        "address": address.to_dict(),
        "tush_line_x": None if address.point is None else round(address.point[0], 2),
        "phases": results,
    }


__all__ = [
    "PelvisDepthResult",
    "PersonSegmenter",
    "RearPelvis",
    "RearPelvisConfig",
    "displacement_from_line",
    "measure_pelvis_depth",
    "rear_pelvis",
]
