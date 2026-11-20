"""Conservative down-the-line camera validity check.

Catches footage that clearly cannot support pelvis-depth interpretation. It
does not judge professional camera placement (hand line, height), which needs
more than one clip to calibrate.

Signals (each VALID / UNCERTAIN / INVALID):

* ``orientation_width`` - shoulder and hip widths in the image, relative to
  torso length, at Address. Seen down the line the two shoulders (and the two
  hips) nearly overlap; face-on they are a full body-width apart.
* ``hands_in_front`` - horizontal offset of the hands from the hips at
  Address, relative to torso length. Down the line the hands hang clearly in
  front of the body toward the ball; face-on they sit in line with it.
* ``full_body`` - head and both ankles inside the frame at Address and Impact.
* ``golfer_size`` - body height in pixels (resolution for a pixel-level
  boundary). The rear-pelvis noise floor is 0.5 % of body height; it must span
  at least ~1 px, which is ~7x the jitter measured on still frames of the real
  swing (0.13 px), so 200 px is VALID and under 120 px INVALID.
* ``camera_motion`` - background motion from Address to each later key frame
  (Top, Mid-Down, Impact) as a share of frame height, from ORB features on
  pixels that are background in both frames and a RANSAC similarity fit. Drift
  is graded against the position noise floor (0.5 % of body height; frame-height
  bounds only when body height is unknown). Any reliable comparison over it
  makes the signal INVALID; any comparison that cannot be measured reliably
  makes it UNCERTAIN (fail closed).

The overall status is the worst signal. Thresholds are engineering bounds for
"clearly not a usable down-the-line view", set wide on purpose; they are not
golf thresholds and are recorded with every result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np

VALID = "VALID"
UNCERTAIN = "UNCERTAIN"
INVALID = "INVALID"
_ORDER = {VALID: 0, UNCERTAIN: 1, INVALID: 2}


@dataclass(frozen=True)
class CameraCheckConfig:
    width_valid: float = 0.45  # shoulder/hip width per torso length
    width_invalid: float = 0.75
    hands_valid: float = 0.30  # hands ahead of hips per torso length
    hands_invalid: float = 0.12
    frame_margin: float = 0.01  # share of frame size
    size_valid_px: float = 200.0  # body height in pixels
    size_invalid_px: float = 120.0
    motion_valid: float = 0.01  # background shift / frame height (only when body height is unknown)
    motion_invalid: float = 0.03
    # Camera drift moves every Address-relative position (tush line, hip and
    # head movement) by the same amount, so with a known body height the drift
    # must stay inside those measurements' own noise floor (0.5 % of body
    # height, the interpretation noise floor). A reliably measured drift above
    # it is INVALID. Robustness batch: tripod clips 0.0-0.2 px; handheld clips
    # 6-12 px = 2-4 % of body height, which is as large as the pelvis
    # movements being reported.
    motion_valid_body: float = 0.005


def _grade(value: float | None, valid: float, invalid: float, *, higher_is_better: bool) -> str:
    if value is None or not math.isfinite(value):
        return UNCERTAIN
    if higher_is_better:
        return VALID if value >= valid else INVALID if value < invalid else UNCERTAIN
    return VALID if value <= valid else INVALID if value > invalid else UNCERTAIN


def _px(record, name):
    points = getattr(record, "points", None)
    if points is None:
        return None
    value = points.pixels.get(name)
    return None if value is None else np.asarray(value, float)


@dataclass(frozen=True)
class MotionConfig:
    """Background registration settings (engineering values, not golf ones).

    The first version filled the golfer with one flat patch in both frames and
    phase-correlated the whole image; the patch edges were identical in both
    frames and dominated the correlation, so a panning phone read as ~0 px
    (swing 08: 54 px real, 0.3 px measured). Registration now only ever looks
    at pixels that are background in *both* frames and never synthesises
    pixels.
    """

    mask_threshold: float = 0.3
    dilate_fraction: float = 0.025  # golfer mask grown by this share of frame height
    border_px: int = 8  # frame edges excluded (compression/rolling-shutter noise)
    min_background: float = 0.10  # share of the frame that is background in both
    max_features: int = 2000
    ratio: float = 0.75  # Lowe ratio test for descriptor matches
    ransac_px: float = 3.0
    min_inliers: int = 25
    min_inlier_ratio: float = 0.35
    min_spread: float = 0.25  # inlier bounding-box diagonal / frame diagonal


@dataclass(frozen=True)
class MotionEstimate:
    """Background motion between two frames, or why it could not be trusted."""

    reliable: bool
    shift_px: float | None = None  # median displacement of background inliers
    dx_px: float | None = None
    dy_px: float | None = None
    scale: float | None = None
    rotation_deg: float | None = None
    inliers: int = 0
    matches: int = 0
    background_share: float | None = None
    spread: float | None = None
    reason: str = ""
    # 2x3 similarity A -> B (maps an Address-frame point to the later frame) and
    # the median reprojection error of the inliers under it, in pixels.
    matrix: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None
    residual_px: float | None = None

    def to_dict(self) -> dict:
        def r(value, digits=3):
            return None if value is None else round(float(value), digits)

        return {"reliable": self.reliable, "shift_px": r(self.shift_px, 2), "dx_px": r(self.dx_px, 2),
                "dy_px": r(self.dy_px, 2), "scale": r(self.scale, 4), "rotation_deg": r(self.rotation_deg, 2),
                "inliers": self.inliers, "matches": self.matches, "background_share": r(self.background_share),
                "spread": r(self.spread), "reason": self.reason,
                "matrix": None if self.matrix is None else [[round(float(v), 6) for v in row] for row in self.matrix],
                "residual_px": r(self.residual_px, 3)}


def _background_mask(shape: tuple[int, int], masks, config: MotionConfig):
    """Pixels that are background in every given frame (golfer in either excluded)."""

    import cv2

    height, width = shape
    person = np.zeros(shape, np.uint8)
    for mask in masks:
        if mask is not None and mask.shape == shape:
            person |= (mask > config.mask_threshold).astype(np.uint8)
    size = max(3, int(round(config.dilate_fraction * height)) | 1)
    person = cv2.dilate(person, np.ones((size, size), np.uint8))
    valid = (person == 0).astype(np.uint8) * 255
    b = config.border_px
    valid[:b, :] = valid[-b:, :] = 0
    valid[:, :b] = valid[:, -b:] = 0
    return valid


def background_motion(frame_a: np.ndarray | None, frame_b: np.ndarray | None,
                      person_a: np.ndarray | None, person_b: np.ndarray | None,
                      config: MotionConfig | None = None) -> MotionEstimate:
    """Robust global background motion from frame A to frame B.

    ORB features are detected only where both frames are background, matched
    with a ratio test, and a similarity transform (pan, zoom, roll) is fitted
    with RANSAC. The reported shift is the median displacement of the inlier
    background points under that transform. Too little background, too few
    or too clustered inliers, or a weak consensus make the estimate
    unreliable, which the caller must treat as UNCERTAIN - never as still.
    """

    import cv2

    config = config or MotionConfig()
    if frame_a is None or frame_b is None:
        return MotionEstimate(False, reason="frame missing")
    if frame_a.shape != frame_b.shape:
        return MotionEstimate(False, reason="frame sizes differ")
    gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY) if frame_a.ndim == 3 else frame_a
    gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY) if frame_b.ndim == 3 else frame_b
    height, width = gray_a.shape
    valid = _background_mask((height, width), (person_a, person_b), config)
    share = float(np.count_nonzero(valid)) / valid.size
    if share < config.min_background:
        return MotionEstimate(False, background_share=share, reason=f"only {share:.0%} of the frame is background in both")

    orb = cv2.ORB_create(nfeatures=config.max_features, fastThreshold=10)
    keys_a, desc_a = orb.detectAndCompute(gray_a, valid)
    keys_b, desc_b = orb.detectAndCompute(gray_b, valid)
    if desc_a is None or desc_b is None or len(keys_a) < config.min_inliers or len(keys_b) < config.min_inliers:
        return MotionEstimate(False, background_share=share, reason="too little background texture")
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc_a, desc_b, k=2)
    good = [m for pair in pairs if len(pair) == 2 for m, n in [pair] if m.distance < config.ratio * n.distance]
    if len(good) < config.min_inliers:
        return MotionEstimate(False, matches=len(good), background_share=share,
                              reason=f"only {len(good)} background matches")
    src = np.float32([keys_a[m.queryIdx].pt for m in good])
    dst = np.float32([keys_b[m.trainIdx].pt for m in good])
    model, inlier_mask = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC,
                                                     ransacReprojThreshold=config.ransac_px,
                                                     maxIters=4000, confidence=0.999)
    if model is None or inlier_mask is None:
        return MotionEstimate(False, matches=len(good), background_share=share, reason="no consistent motion")
    keep = inlier_mask.ravel().astype(bool)
    inliers = int(keep.sum())
    points = src[keep]
    spread = 0.0
    if inliers:
        extent = points.max(axis=0) - points.min(axis=0)
        spread = float(math.hypot(*extent) / math.hypot(width, height))
    moved = points @ model[:, :2].T + model[:, 2] - points
    shift = float(np.median(np.linalg.norm(moved, axis=1))) if inliers else None
    scale = float(math.hypot(model[0, 0], model[1, 0]))
    rotation = float(math.degrees(math.atan2(model[1, 0], model[0, 0])))
    residual = None
    if inliers:
        projected = points @ model[:, :2].T + model[:, 2]
        residual = float(np.median(np.linalg.norm(projected - dst[keep], axis=1)))
    estimate = dict(shift_px=shift, dx_px=float(np.median(moved[:, 0])) if inliers else None,
                    dy_px=float(np.median(moved[:, 1])) if inliers else None, scale=scale,
                    rotation_deg=rotation, inliers=inliers, matches=len(good), background_share=share, spread=spread,
                    matrix=tuple(tuple(float(v) for v in row) for row in model), residual_px=residual)
    if inliers < config.min_inliers:
        return MotionEstimate(False, reason=f"only {inliers} consistent background points", **estimate)
    if inliers / len(good) < config.min_inlier_ratio:
        return MotionEstimate(False, reason=f"weak consensus ({inliers}/{len(good)} matches agree)", **estimate)
    if spread < config.min_spread:
        return MotionEstimate(False, reason="background points cover too small an area", **estimate)
    return MotionEstimate(True, **estimate)


def background_shift(frame_a: np.ndarray, frame_b: np.ndarray, person_a: np.ndarray | None,
                     person_b: np.ndarray | None) -> float | None:
    """Background shift in px, or ``None`` when it cannot be measured reliably."""

    estimate = background_motion(frame_a, frame_b, person_a, person_b)
    return estimate.shift_px if estimate.reliable else None


def motion_signal(address: tuple[np.ndarray | None, np.ndarray | None],
                  others: Mapping[str, tuple[np.ndarray | None, np.ndarray | None]],
                  height: int, settings: "CameraCheckConfig", *, body_height_px: float | None = None) -> dict:
    """Address -> each later key frame. Fails closed.

    With a body height, each reliable comparison is VALID only while the
    drift stays within ``motion_valid_body`` of body height (the position
    measurements' noise floor) and INVALID above it. Without one, the
    frame-height bounds apply (VALID / UNCERTAIN band / INVALID). A comparison
    that cannot be measured reliably is UNCERTAIN; any INVALID comparison makes
    the signal INVALID; VALID needs every comparison measured and within bounds.
    """

    body = float(body_height_px) if body_height_px and body_height_px > 0 else None
    comparisons = {}
    for name, (frame, mask) in others.items():
        estimate = background_motion(address[0], frame, address[1], mask)
        shift = estimate.shift_px if estimate.reliable else None
        share = None if shift is None else shift / height
        of_body = None if shift is None or body is None else shift / body
        if shift is None:
            status = UNCERTAIN
        elif of_body is not None:
            status = VALID if of_body <= settings.motion_valid_body else INVALID
        else:
            status = _grade(share, settings.motion_valid, settings.motion_invalid, higher_is_better=False)
        comparisons[f"address->{name}"] = {**estimate.to_dict(),
                                           "share_of_height": None if share is None else round(share, 4),
                                           "share_of_body": None if of_body is None else round(of_body, 4),
                                           "status": status}
    if not comparisons:
        return {"value": None, "status": UNCERTAIN, "comparisons": {},
                "note": "no later key frame to compare with Address, so camera motion is unmeasured"}
    statuses = [c["status"] for c in comparisons.values()]
    status = INVALID if INVALID in statuses else UNCERTAIN if UNCERTAIN in statuses else VALID
    key = "share_of_body" if body is not None else "share_of_height"
    measured = [c[key] for c in comparisons.values() if c[key] is not None]
    return {
        "value": round(max(measured), 4) if measured else None,
        "unit": "share of body height" if body is not None else "share of frame height",
        "status": status,
        "comparisons": comparisons,
        "note": "largest reliable background shift from Address; background only where no golfer is in "
                "either frame; must stay within the position noise floor"
                + (f" ({settings.motion_valid_body:.1%} of body height)" if body is not None else ""),
    }


def check_camera(
    address_record,
    impact_record,
    *,
    frame_size: tuple[int, int],
    body_height_px: float | None,
    torso_px: float,
    address_frame: np.ndarray | None = None,
    impact_frame: np.ndarray | None = None,
    address_mask: np.ndarray | None = None,
    impact_mask: np.ndarray | None = None,
    config: CameraCheckConfig | None = None,
    motion_frames: Mapping[str, tuple[np.ndarray | None, np.ndarray | None]] | None = None,
) -> dict:
    """VALID / UNCERTAIN / INVALID with every signal that produced it.

    ``motion_frames`` adds later key frames (e.g. Top, Mid-Down) to the
    Address -> Impact background comparison as ``{name: (frame, mask)}``.
    """

    settings = config or CameraCheckConfig()
    width, height = frame_size
    signals: dict[str, dict] = {}

    if address_record is None or torso_px <= 1:
        return {"status": INVALID, "signals": {"address": {"status": INVALID, "note": "no Address pose"}},
                "config": settings.__dict__}

    shoulder_l, shoulder_r = _px(address_record, "left_shoulder"), _px(address_record, "right_shoulder")
    hip_l, hip_r = _px(address_record, "left_hip"), _px(address_record, "right_hip")
    widths = [float(np.linalg.norm(a - b)) / torso_px for a, b in ((shoulder_l, shoulder_r), (hip_l, hip_r))
              if a is not None and b is not None]
    width_ratio = max(widths) if widths else None
    signals["orientation_width"] = {
        "value": None if width_ratio is None else round(width_ratio, 3),
        "status": _grade(width_ratio, settings.width_valid, settings.width_invalid, higher_is_better=False),
        "note": "shoulder/hip width per torso length (small when seen down the line)",
    }

    hands = [p for p in (_px(address_record, "left_wrist"), _px(address_record, "right_wrist")) if p is not None]
    hip = _px(address_record, "hip_mid")
    offset = None if not hands or hip is None else abs(float(np.mean(hands, axis=0)[0] - hip[0])) / torso_px
    signals["hands_in_front"] = {
        "value": None if offset is None else round(offset, 3),
        "status": _grade(offset, settings.hands_valid, settings.hands_invalid, higher_is_better=True),
        "note": "hands ahead of the hips per torso length (clear when seen down the line)",
    }

    def inside(record) -> bool | None:
        if record is None:
            return None
        points = [_px(record, name) for name in ("nose", "left_ankle", "right_ankle")]
        if any(p is None for p in points):
            return False
        mx, my = settings.frame_margin * width, settings.frame_margin * height
        return all(mx <= p[0] <= width - mx and my <= p[1] <= height - my for p in points)

    visible = [inside(address_record), inside(impact_record)]
    signals["full_body"] = {
        "value": visible,
        "status": VALID if all(v is True for v in visible) else INVALID if any(v is False for v in visible) else UNCERTAIN,
        "note": "head and both ankles inside the frame at Address and Impact",
    }

    size = None if not body_height_px else float(body_height_px)
    signals["golfer_size"] = {
        "value": None if size is None else round(size, 1),
        "status": _grade(size, settings.size_valid_px, settings.size_invalid_px, higher_is_better=True),
        "note": "body height in pixels (0.5 % of it must span ~1 px)",
    }

    later = {name: pair for name, pair in (motion_frames or {}).items() if pair[0] is not None}
    if impact_frame is not None:
        later["impact"] = (impact_frame, impact_mask)
    signals["camera_motion"] = motion_signal((address_frame, address_mask), later, height, settings,
                                             body_height_px=body_height_px)

    status = max((signal["status"] for signal in signals.values()), key=lambda s: _ORDER[s])
    return {"status": status, "signals": signals, "config": dict(settings.__dict__)}


__all__ = [
    "INVALID", "UNCERTAIN", "VALID", "CameraCheckConfig", "MotionConfig", "MotionEstimate",
    "background_motion", "background_shift", "check_camera", "motion_signal",
]
