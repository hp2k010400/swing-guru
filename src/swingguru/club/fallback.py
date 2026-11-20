"""OWLv2 shaft fallback for key moments the classical tracker cannot confirm.

Used ONLY on individual key-phase frames where the classical ClubTracker did
not reach a reportable level. Never run across a whole video.

How a fallback shaft is derived:

1. Crop around the tracked hands (RTMW) and ask OWLv2 (Apache-2.0,
   ``google/owlv2-base-patch16-ensemble``) for "a golf club shaft". Only the
   "shaft" prompt is used: in testing, "golf club" and "club head" fired on
   the golf bag and on the golfer's head.
2. Keep only boxes that touch the hands (the shaft leaves the grip): the box
   must lie within ``max_hand_gap`` torso lengths of the hands.
3. The shaft runs from the hands to the box corner farthest from them.
4. That direction is refined within +/-``refine_deg`` using the classical
   tracker's thin-line evidence (Hessian ridge + symmetry) from the hands.
5. Confidence blends detection score, hand proximity, visible length, ridge
   support, agreement with neighbouring classical frames and ambiguity with
   other boxes. It is capped at MEDIUM: a box alone never makes a HIGH shaft.
   Below ``min_confidence`` the result is unavailable.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Sequence

import cv2
import numpy as np

from .geometry import angle_difference, direction_angle_deg, overlaps_forearm, unit_from_angle
from .metrics import reportable, unwrap_angles
from .tracker import (
    LEVEL_MEDIUM,
    LEVEL_UNAVAILABLE,
    STATUS_DETECTED,
    STATUS_UNAVAILABLE,
    ClubTrack,
    FrameShaft,
    HandsContext,
    hessian_maps,
    line_symmetry,
    ridge_along,
)

METHOD = "owlv2-fallback"
MODEL_ID = "google/owlv2-base-patch16-ensemble"
PROMPT = "a golf club shaft"


@dataclass(frozen=True)
class FallbackConfig:
    min_box_score: float = 0.10
    good_box_score: float = 0.45
    crop_torso: float = 3.2
    max_hand_gap: float = 0.5
    min_length: float = 0.5
    good_length: float = 1.2
    refine_deg: float = 10.0
    forearm_tolerance_deg: float = 18.0
    temporal_window: int = 8
    temporal_tolerance_deg: float = 45.0
    ambiguity_deg: float = 20.0
    min_confidence: float = 0.45
    medium_confidence: float = 0.55


@dataclass(frozen=True)
class BoxCandidate:
    box: tuple[float, float, float, float]  # x0, y0, x1, y1 in frame pixels
    score: float


class ModelUnavailable(RuntimeError):
    """The optional OWLv2 club model could not be loaded (not installed, incomplete or corrupt).

    Distinct from programming errors (TypeError, AttributeError, ...), which still propagate.
    """


def _is_unavailable(error: BaseException) -> bool:
    """Missing / incomplete / corrupt model files, or the library itself missing."""

    if isinstance(error, (ImportError, OSError, ValueError)):  # OSError covers EnvironmentError + missing files
        return True
    # safetensors reports a corrupt weights file with its own exception type.
    return type(error).__name__ in ("SafetensorError", "HeaderTooLarge", "DecodeError")


@dataclass
class OwlShaftFallback:
    """Lazy OWLv2 wrapper; the model loads on the first fallback only.

    The model is optional: it only rescues club lines the classical tracker could not confirm.
    It is loaded from the locally installed copy only (``fetch_models.py`` is the explicit
    provisioning step; nothing is downloaded at analysis time). If it cannot be loaded, one
    attempt is made per run and every later call raises :class:`ModelUnavailable` at once.
    """

    config: FallbackConfig = field(default_factory=FallbackConfig)
    _model: object = field(default=None, init=False, repr=False)
    _processor: object = field(default=None, init=False, repr=False)
    load_seconds: float = field(default=0.0, init=False)
    unavailable: str | None = field(default=None, init=False)

    def _ensure(self) -> None:
        if self._model is not None:
            return
        if self.unavailable is not None:
            raise ModelUnavailable(self.unavailable)
        started = time.monotonic()
        try:
            from transformers import Owlv2ForObjectDetection, Owlv2Processor

            self._processor = Owlv2Processor.from_pretrained(MODEL_ID, local_files_only=True)
            self._model = Owlv2ForObjectDetection.from_pretrained(MODEL_ID, local_files_only=True).eval()
        except Exception as error:  # noqa: BLE001 - classified below; programming errors re-raise
            if not _is_unavailable(error):
                raise
            self._processor = self._model = None
            self.unavailable = f"{type(error).__name__}: the optional club model is not available locally"
            raise ModelUnavailable(self.unavailable) from error
        finally:
            self.load_seconds = time.monotonic() - started

    def boxes(self, frame_bgr: np.ndarray, hands: np.ndarray, torso_px: float) -> list[BoxCandidate]:
        """Shaft boxes on a crop around the hands, in frame coordinates."""

        import torch
        from PIL import Image

        self._ensure()
        height, width = frame_bgr.shape[:2]
        radius = self.config.crop_torso * torso_px
        x0 = int(max(0, hands[0] - radius))
        y0 = int(max(0, hands[1] - radius))
        x1 = int(min(width, hands[0] + radius))
        y1 = int(min(height, hands[1] + radius))
        crop = frame_bgr[y0:y1, x0:x1]
        image = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
        inputs = self._processor(text=[[PROMPT]], images=image, return_tensors="pt")  # type: ignore[operator]
        with torch.no_grad():
            outputs = self._model(**inputs)  # type: ignore[operator]
        side = max(image.size)
        result = self._processor.post_process_grounded_object_detection(  # type: ignore[union-attr]
            outputs, threshold=self.config.min_box_score, target_sizes=torch.tensor([[side, side]])
        )[0]
        found = []
        for box, score in zip(result["boxes"], result["scores"]):
            bx0, by0, bx1, by1 = (float(v) for v in box.tolist())
            found.append(BoxCandidate((bx0 + x0, by0 + y0, bx1 + x0, by1 + y0), float(score)))
        return sorted(found, key=lambda item: item.score, reverse=True)


def box_gap(box: Sequence[float], point: np.ndarray) -> float:
    """Distance from a point to an axis-aligned box (0 inside)."""

    x0, y0, x1, y1 = box
    dx = max(x0 - point[0], 0.0, point[0] - x1)
    dy = max(y0 - point[1], 0.0, point[1] - y1)
    return math.hypot(dx, dy)


def far_corner(box: Sequence[float], point: np.ndarray) -> np.ndarray:
    x0, y0, x1, y1 = box
    corners = np.array([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], float)
    return corners[int(np.argmax(np.linalg.norm(corners - point, axis=1)))]


def ridge_support(maps, gray: np.ndarray, grip: np.ndarray, angle: float, start: float, end: float,
                  threshold: float) -> float:
    """Share of samples from ``start`` to ``end`` px that look like a thin line."""

    if end <= start:
        return 0.0
    unit = unit_from_angle(angle)
    radii = np.arange(start, end, 1.5)
    xs = grip[0] + unit[0] * radii
    ys = grip[1] + unit[1] * radii
    inside = (xs >= 1) & (ys >= 1) & (xs <= gray.shape[1] - 2) & (ys <= gray.shape[0] - 2)
    if not np.any(inside):
        return 0.0
    response = ridge_along(maps, xs[None, :], ys[None, :], unit, along_ratio=0.6)[0]
    symmetric = line_symmetry(gray, xs[None, :], ys[None, :], unit, offset_px=4.0, ratio=0.55)[0]
    hits = (response >= threshold) & symmetric & inside
    return float(np.mean(hits[inside]))


def temporal_expectation(track: ClubTrack, frame_index: int, window: int) -> float | None:
    """Angle expected from confident classical frames either side, if any."""

    before = [s for s in track.frames if frame_index - window <= s.frame_index < frame_index and reportable(s)]
    after = [s for s in track.frames if frame_index < s.frame_index <= frame_index + window and reportable(s)]
    if before and after:
        a, b = before[-1], after[0]
        unwrapped = unwrap_angles([float(a.angle_deg), float(b.angle_deg)])  # type: ignore[arg-type]
        fraction = (frame_index - a.frame_index) / max(1, b.frame_index - a.frame_index)
        value = unwrapped[0] + (unwrapped[1] - unwrapped[0]) * fraction
        return ((value + 180.0) % 360.0) - 180.0
    return None


def fallback_shaft(
    frame_bgr: np.ndarray,
    frame_index: int,
    timestamp_ms: float,
    hands: HandsContext,
    track: ClubTrack,
    boxes: Sequence[BoxCandidate],
    *,
    config: FallbackConfig | None = None,
) -> tuple[FrameShaft, dict]:
    """Turn OWLv2 boxes into a shaft for one frame, or an honest unavailable.

    Pure given the boxes, so it is testable without the model.
    """

    settings = config or FallbackConfig()
    diagnostics: dict = {"method": METHOD, "boxes": [list(b.box) + [round(b.score, 3)] for b in boxes]}

    def unavailable(reason: str) -> tuple[FrameShaft, dict]:
        diagnostics["reason"] = reason
        return FrameShaft(frame_index, timestamp_ms, STATUS_UNAVAILABLE, LEVEL_UNAVAILABLE, 0.0,
                          method=METHOD, reason=reason), diagnostics

    if hands.origin is None or hands.torso_px <= 1:
        return unavailable("no hands to anchor the shaft")
    origin = np.asarray(hands.origin, float)
    torso = hands.torso_px
    touching = [b for b in boxes if box_gap(b.box, origin) <= settings.max_hand_gap * torso]
    if not touching:
        return unavailable("no shaft box leaves the hands")
    rejections = []
    best = None
    for box in touching:  # best score first
        tip = far_corner(box.box, origin)
        length = float(np.linalg.norm(tip - origin))
        if length < settings.min_length * torso:
            rejections.append("too short")
            continue
        angle = direction_angle_deg(tip - origin)
        along_forearm = False
        for wrist, elbow in hands.forearms:
            forearm_length = float(np.linalg.norm(np.asarray(elbow) - np.asarray(wrist)))
            # Only a box no longer than the forearm itself could *be* the
            # forearm; a foreshortened forearm gives no reliable direction
            # against a box reaching well past the hands.
            if length <= 1.5 * forearm_length and overlaps_forearm(
                angle, wrist, elbow, tolerance_deg=settings.forearm_tolerance_deg
            ):
                along_forearm = True
        if along_forearm:
            rejections.append("along a forearm")
            continue
        best = box
        break
    if best is None:
        return unavailable("no usable shaft box (" + ", ".join(rejections) + ")")

    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    maps = hessian_maps(gray, (1.0, 2.0))
    threshold = 4.0
    start = 0.15 * torso
    best_angle, best_support = angle, ridge_support(maps, gray, origin, angle, start, length, threshold)
    for delta in np.arange(-settings.refine_deg, settings.refine_deg + 0.1, 1.0):
        candidate = angle + float(delta)
        support = ridge_support(maps, gray, origin, candidate, start, length, threshold)
        if support > best_support + 0.02:
            best_angle, best_support = candidate, support
    best_angle = ((best_angle + 180.0) % 360.0) - 180.0

    expected = temporal_expectation(track, frame_index, settings.temporal_window)
    temporal = None
    if expected is not None:
        temporal = max(0.0, 1.0 - abs(angle_difference(best_angle, expected)) / settings.temporal_tolerance_deg)

    rivals = [
        b.score for b in touching[1:]
        if abs(angle_difference(direction_angle_deg(far_corner(b.box, origin) - origin), best_angle)) > settings.ambiguity_deg
    ]
    ambiguity = 1.0 - (max(rivals) / best.score if rivals else 0.0)

    parts = {
        "detection": min(1.0, best.score / settings.good_box_score),
        "hand_proximity": 1.0 - box_gap(best.box, origin) / (settings.max_hand_gap * torso),
        "length": min(1.0, length / (settings.good_length * torso)),
        "ridge_support": best_support,
        "temporal": 0.5 if temporal is None else temporal,
        "unambiguous": max(0.0, ambiguity),
    }
    confidence = (
        0.25 * parts["detection"] + 0.15 * parts["hand_proximity"] + 0.15 * parts["length"]
        + 0.15 * parts["ridge_support"] + 0.20 * parts["temporal"] + 0.10 * parts["unambiguous"]
    )
    diagnostics.update({"parts": {k: round(v, 3) for k, v in parts.items()}, "confidence": round(confidence, 3),
                        "box_angle": round(angle, 2), "refined_angle": round(best_angle, 2),
                        "expected_angle": None if expected is None else round(expected, 2)})
    if confidence < settings.min_confidence:
        return unavailable(f"fallback confidence {confidence:.2f} too low")
    level = LEVEL_MEDIUM if confidence >= settings.medium_confidence else "LOW"
    return FrameShaft(
        frame_index, timestamp_ms, STATUS_DETECTED, level, confidence,
        angle_deg=best_angle, grip=origin, tip=origin + unit_from_angle(best_angle) * length,
        method=METHOD, components=parts,
    ), diagnostics


__all__ = [
    "ModelUnavailable",
    "METHOD",
    "BoxCandidate",
    "FallbackConfig",
    "OwlShaftFallback",
    "box_gap",
    "fallback_shaft",
    "far_corner",
    "ridge_support",
    "temporal_expectation",
]
