"""Club / shaft detection and tracking (Engine v0.3, experimental).

The first tracker is classical computer vision constrained by the body pose:

1. **Hands anchor.** The shaft leaves the golfer's hands, so candidates are
   only searched on rays that start at the tracked hands (a small grid of
   origins absorbs the gap between wrist landmarks and the grip). A strong
   fence, roof or horizon line that does not come out of the hands is never a
   candidate.
2. **Ridge evidence.** A shaft is a *thin line*: strongly curved across its
   width and flat along its length. Second derivatives of the image (a
   Hessian at two scales) are sampled along each ray; step edges such as a
   silhouette or the horizon have little cross-curvature and are rejected.
3. **Second opinion.** OpenCV's LSD line-segment detector runs independently
   near the hands. A candidate with an LSD segment lying along it is flagged
   as corroborated.
4. **Body rejection.** Candidates running back along a forearm are rejected.
5. **Temporal selection.** A Viterbi pass over every frame's candidates plus
   an explicit "no club" state picks the most coherent sequence; the allowed
   rotation between frames grows with how fast the hands move.
6. **Honest confidence.** Each frame gets a confidence from ridge support,
   visible length, the margin over the next-best direction, corroboration and
   agreement with its neighbours. Low evidence means *unavailable*, not a
   guessed line. Only gaps of at most ``max_interpolated_frames`` between two
   confident frames are interpolated, and those frames are marked.

The :class:`ClubTracker` protocol keeps this replaceable by a trained model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Mapping, Protocol, Sequence, runtime_checkable

import cv2
import numpy as np

from .geometry import (
    allowed_rotation_deg,
    angle_difference,
    direction_angle_deg,
    orient_from,
    overlaps_forearm,
    point_line_distance,
    unit_from_angle,
    wrap_deg,
)
from .windows import FrameWindow

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .cache import CandidateCache

LEVEL_HIGH = "HIGH"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_LOW = "LOW"
LEVEL_UNAVAILABLE = "UNAVAILABLE"

STATUS_DETECTED = "detected"
STATUS_INTERPOLATED = "interpolated"
STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ClubConfig:
    """Engineering thresholds. Lengths are in torso lengths (hip-to-shoulder)."""

    angle_step_deg: float = 1.0
    origin_offset: float = 0.08
    sample_step_px: float = 1.5
    min_ray_start: float = 0.10
    max_ray_start: float = 0.65
    max_ray_length: float = 2.8
    min_visible_length: float = 0.45
    gap_tolerance_px: float = 8.0
    hessian_sigmas: tuple[float, ...] = (1.0, 2.0)
    ridge_floor: float = 4.0
    ridge_relative: float = 0.22
    along_ratio: float = 0.6
    candidates_per_frame: int = 6
    nms_deg: float = 6.0
    forearm_tolerance_deg: float = 22.0
    lsd_lateral: float = 0.12
    lsd_angle_deg: float = 4.0
    lsd_min_length: float = 0.3
    # Rotation allowance between frames: a base rate per second (the wrists
    # can hinge the club quickly while the hands barely move), plus a term for
    # how far the hands travelled, capped per second. Scaled by each frame's
    # real time step, so the same numbers hold at 30, 60 or 240 fps.
    rotation_base_deg_per_s: float = 900.0
    rotation_per_torso_deg: float = 150.0
    rotation_cap_deg_per_s: float = 3000.0
    lsd_max_miss: float = 0.6
    lsd_max_near: float = 0.9
    lsd_miss_penalty: float = 0.5
    none_cost: float = 0.30
    gap_cost: float = 0.15
    transition_weight: float = 0.35
    max_interpolated_frames: int = 2
    high_confidence: float = 0.70
    medium_confidence: float = 0.50
    low_confidence: float = 0.33
    weak_hand_visibility: float = 0.25
    body_tolerance_deg: float = 20.0
    body_distance: float = 0.12
    body_overlap: float = 0.5
    reference_frames: int = 16
    reference_hand_distance: float = 0.5
    min_reference_frames: int = 4
    static_presence: float = 0.75
    static_fraction: float = 0.5
    raw_candidates: int = 12
    symmetry_offset_px: float = 4.0
    symmetry_ratio: float = 0.55


@dataclass(frozen=True)
class HandsContext:
    """Where the hands are and which body points constrain the search."""

    origin: np.ndarray | None
    torso_px: float
    forearms: tuple[tuple[np.ndarray, np.ndarray], ...] = ()
    source: str = "unavailable"  # "landmarks", "weak" or "unavailable"
    reason: str = ""
    body_segments: tuple[tuple[str, np.ndarray, np.ndarray], ...] = ()


@dataclass(frozen=True)
class ShaftCandidate:
    """One possible shaft line leaving the hands."""

    grip: np.ndarray
    tip: np.ndarray
    angle_deg: float
    visible_start_px: float
    length_px: float
    support: float
    strength: float
    score: float
    method: str = "ridge"
    corroborated: bool = False
    rejected: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "grip": [round(float(v), 2) for v in self.grip],
            "tip": [round(float(v), 2) for v in self.tip],
            "angle_deg": round(self.angle_deg, 2),
            "length_px": round(self.length_px, 1),
            "support": round(self.support, 3),
            "score": round(self.score, 4),
            "method": self.method,
            "corroborated": self.corroborated,
            "rejected": self.rejected,
        }


@dataclass(frozen=True)
class FrameDetection:
    """Per-frame output of :meth:`ClubTracker.detect_frame` (before tracking)."""

    frame_index: int
    timestamp_ms: float
    hands: HandsContext
    candidates: tuple[ShaftCandidate, ...]
    rejected: tuple[ShaftCandidate, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FrameShaft:
    """The tracked shaft for one frame, or why there is none."""

    frame_index: int
    timestamp_ms: float
    status: str
    level: str
    confidence: float
    angle_deg: float | None = None
    grip: np.ndarray | None = None
    tip: np.ndarray | None = None
    method: str = ""
    temporal_consistency: float | None = None
    interpolated: bool = False
    reason: str = ""
    components: Mapping[str, float] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.status != STATUS_UNAVAILABLE and self.angle_deg is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "frame_index": self.frame_index,
            "timestamp_ms": round(self.timestamp_ms, 3),
            "status": self.status,
            "level": self.level,
            "confidence": round(self.confidence, 3),
            "angle_deg": None if self.angle_deg is None else round(self.angle_deg, 2),
            "grip": None if self.grip is None else [round(float(v), 2) for v in self.grip],
            "tip": None if self.tip is None else [round(float(v), 2) for v in self.tip],
            "method": self.method,
            "temporal_consistency": None
            if self.temporal_consistency is None
            else round(self.temporal_consistency, 3),
            "interpolated": self.interpolated,
            "reason": self.reason,
            "components": {key: round(float(value), 3) for key, value in self.components.items()},
        }


@dataclass(frozen=True)
class ClubTrack:
    """A tracked sequence plus the raw per-frame detections behind it."""

    frames: tuple[FrameShaft, ...]
    detections: tuple[FrameDetection, ...]
    tracker: str
    config: ClubConfig

    def at(self, frame_index: int) -> FrameShaft | None:
        for item in self.frames:
            if item.frame_index == frame_index:
                return item
        return None

    def coverage(self, minimum_level: str = LEVEL_MEDIUM) -> float:
        order = [LEVEL_UNAVAILABLE, LEVEL_LOW, LEVEL_MEDIUM, LEVEL_HIGH]
        floor = order.index(minimum_level)
        if not self.frames:
            return 0.0
        hits = sum(1 for item in self.frames if order.index(item.level) >= floor)
        return hits / len(self.frames)


@runtime_checkable
class ClubTracker(Protocol):
    """Interface shared by this classical tracker and any future model."""

    name: str

    def detect_frame(
        self, frame_bgr: np.ndarray, record: object, *, torso_px: float
    ) -> FrameDetection:
        ...

    def track_sequence(
        self,
        frames: Iterable[np.ndarray],
        records: Sequence[object],
    ) -> ClubTrack:
        ...


# ---------------------------------------------------------------------------
# Hands context
# ---------------------------------------------------------------------------

_HAND_POINTS = (
    "left_wrist",
    "right_wrist",
    "left_index",
    "right_index",
    "left_pinky",
    "right_pinky",
    "left_thumb",
    "right_thumb",
)


def torso_length_px(records: Sequence[object]) -> float:
    """Median hip-to-shoulder length over the clip, in pixels."""

    lengths = []
    for record in records:
        points = getattr(record, "points", None)
        if points is None:
            continue
        hip = points.pixels.get("hip_mid")
        shoulder = points.pixels.get("shoulder_mid")
        if hip is None or shoulder is None:
            continue
        length = float(np.linalg.norm(np.asarray(shoulder, float) - np.asarray(hip, float)))
        if math.isfinite(length) and length > 1.0:
            lengths.append(length)
    return float(np.median(lengths)) if lengths else 0.0


def hands_context(record: object, *, torso_px: float, config: ClubConfig) -> HandsContext:
    """Hands position and forearms from the pose, failing closed."""

    points = getattr(record, "points", None)
    if points is None or torso_px <= 1.0:
        return HandsContext(None, torso_px, reason="no body pose in this frame")
    usable = [
        np.asarray(points.pixels[name], float)
        for name in _HAND_POINTS
        if name in points.pixels and np.all(np.isfinite(points.pixels[name]))
    ]
    source = "landmarks"
    if not usable:
        width = float(getattr(record, "width", 0) or 0)
        height = float(getattr(record, "height", 0) or 0)
        weak = []
        for name in ("left_wrist", "right_wrist"):
            landmark = points.landmarks.get(name)
            if landmark is None or width <= 0 or height <= 0:
                continue
            visibility = getattr(landmark, "visibility", None)
            if visibility is None or not math.isfinite(float(visibility)):
                continue
            if float(visibility) >= config.weak_hand_visibility and landmark.finite:
                weak.append(np.array([landmark.x * width, landmark.y * height], float))
        if not weak:
            return HandsContext(None, torso_px, reason="hands were not tracked in this frame")
        usable = weak
        source = "weak"
    origin = np.mean(np.stack(usable), axis=0)
    forearms = []
    for side in ("left", "right"):
        wrist = points.pixels.get(f"{side}_wrist")
        elbow = points.pixels.get(f"{side}_elbow")
        if wrist is not None and elbow is not None:
            forearms.append((np.asarray(wrist, float), np.asarray(elbow, float)))
    segments = []
    for side in ("left", "right"):
        for label, a, b in (
            ("thigh", f"{side}_hip", f"{side}_knee"),
            ("shin", f"{side}_knee", f"{side}_ankle"),
            ("torso side", f"{side}_hip", f"{side}_shoulder"),
            ("upper arm", f"{side}_shoulder", f"{side}_elbow"),
        ):
            start = points.pixels.get(a)
            end = points.pixels.get(b)
            if start is not None and end is not None:
                segments.append((label, np.asarray(start, float), np.asarray(end, float)))
        # The arms always join the hands to the shoulders. When an elbow or
        # wrist is not tracked (often the lead arm, turned away from the
        # camera) this straight hands-to-shoulder line still marks where an
        # arm edge will be.
        shoulder = points.pixels.get(f"{side}_shoulder")
        if shoulder is not None:
            segments.append(("arm", origin.copy(), np.asarray(shoulder, float)))
    return HandsContext(origin, torso_px, tuple(forearms), source, body_segments=tuple(segments))


def along_body_segment(
    grip: np.ndarray,
    tip: np.ndarray,
    visible_start_px: float,
    segments: Sequence[tuple[str, np.ndarray, np.ndarray]],
    *,
    torso_px: float,
    tolerance_deg: float,
    distance: float,
    overlap: float,
) -> str:
    """Name of a body segment the candidate lies along, or ``""``.

    A line counts as the body's own edge when it is nearly parallel to a
    thigh, shin, torso side or upper arm *and* most of its visible length lies
    within ``distance`` torso lengths of that segment.
    """

    direction = np.asarray(tip, float) - np.asarray(grip, float)
    length = float(np.linalg.norm(direction))
    if length <= 1e-6:
        return ""
    unit = direction / length
    start = max(0.0, min(visible_start_px, length))
    samples = np.linspace(start, length, 16)
    points = np.asarray(grip, float)[None, :] + unit[None, :] * samples[:, None]
    angle = direction_angle_deg(unit)
    for label, a, b in segments:
        segment = b - a
        seg_length = float(np.linalg.norm(segment))
        if seg_length <= 1e-6:
            continue
        seg_angle = direction_angle_deg(segment)
        difference = abs(angle_difference(angle, seg_angle))
        difference = min(difference, 180.0 - difference)  # undirected
        if difference > tolerance_deg:
            continue
        t = np.clip(((points - a) @ segment) / (seg_length**2), 0.0, 1.0)
        nearest = a[None, :] + t[:, None] * segment[None, :]
        close = np.linalg.norm(points - nearest, axis=1) <= distance * torso_px
        if float(np.mean(close)) >= overlap:
            return label
    return ""


# ---------------------------------------------------------------------------
# Image evidence
# ---------------------------------------------------------------------------


def hessian_maps(gray: np.ndarray, sigmas: Sequence[float]) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Scale-normalised second derivatives (Hxx, Hxy, Hyy) for each sigma."""

    image = gray.astype(np.float32)
    maps = []
    for sigma in sigmas:
        blurred = cv2.GaussianBlur(image, (0, 0), float(sigma))
        scale = float(sigma) ** 2
        hxx = cv2.Sobel(blurred, cv2.CV_32F, 2, 0, ksize=3) * scale
        hyy = cv2.Sobel(blurred, cv2.CV_32F, 0, 2, ksize=3) * scale
        hxy = cv2.Sobel(blurred, cv2.CV_32F, 1, 1, ksize=3) * scale
        maps.append((hxx, hxy, hyy))
    return maps


def line_symmetry(
    gray: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    direction: np.ndarray,
    *,
    offset_px: float,
    ratio: float,
) -> np.ndarray:
    """True where a sample looks like a thin line, not the side of an edge.

    A thin shaft differs from *both* sides, and its two sides look alike. A
    silhouette or horizon edge is dark on one side and bright on the other;
    second-derivative filters still respond beside such edges, so without this
    test a body outline can pass as a shaft. Requires
    ``|centre - mean(sides)| >= ratio * |left - right|``.
    """

    original_shape = np.shape(xs)
    columns = original_shape[-1] if len(original_shape) >= 2 else max(1, int(np.size(xs)))
    flat = (-1, columns)
    direction = np.broadcast_to(direction, tuple(original_shape) + (2,))
    nx = -direction[..., 1].reshape(flat)
    ny = direction[..., 0].reshape(flat)
    x = np.asarray(xs, np.float32).reshape(flat)
    y = np.asarray(ys, np.float32).reshape(flat)
    image = gray.astype(np.float32)

    def sample(px, py):
        return cv2.remap(
            image,
            np.ascontiguousarray(px, dtype=np.float32),
            np.ascontiguousarray(py, dtype=np.float32),
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )

    centre = sample(x, y)
    left = sample(x + nx * offset_px, y + ny * offset_px)
    right = sample(x - nx * offset_px, y - ny * offset_px)
    contrast = np.abs(centre - (left + right) / 2.0)
    asymmetry = np.abs(left - right)
    return (contrast >= ratio * asymmetry).reshape(original_shape)


def ridge_along(
    maps: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray]],
    xs: np.ndarray,
    ys: np.ndarray,
    direction: np.ndarray,
    *,
    along_ratio: float,
) -> np.ndarray:
    """Thin-line strength at sample points for lines running along ``direction``.

    ``direction`` has shape (..., 2) broadcastable to ``xs``. The response is
    the curvature across the line, kept only where the curvature along the
    line is small (``along_ratio``), maximised over scales. A step edge or a
    blob scores low; a thin dark or bright line scores high.
    """

    original_shape = np.shape(xs)
    columns = original_shape[-1] if len(original_shape) >= 2 else max(1, int(np.size(xs)))
    flat = (-1, columns)
    direction = np.broadcast_to(direction, tuple(original_shape) + (2,))
    dx = direction[..., 0].reshape(flat)
    dy = direction[..., 1].reshape(flat)
    nx, ny = -dy, dx
    map_x = np.ascontiguousarray(np.asarray(xs, np.float32).reshape(flat))
    map_y = np.ascontiguousarray(np.asarray(ys, np.float32).reshape(flat))
    best = np.zeros(map_x.shape, dtype=np.float32)
    for hxx, hxy, hyy in maps:
        sxx = cv2.remap(hxx, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        sxy = cv2.remap(hxy, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        syy = cv2.remap(hyy, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        across = nx * nx * sxx + 2 * nx * ny * sxy + ny * ny * syy
        along = dx * dx * sxx + 2 * dx * dy * sxy + dy * dy * syy
        response = np.abs(across)
        response = np.where(np.abs(along) <= along_ratio * response, response - np.abs(along), 0.0)
        best = np.maximum(best, response.astype(np.float32))
    return best.reshape(original_shape)


def longest_supported_run(
    hits: np.ndarray, *, gap_samples: int, max_start: int
) -> tuple[int, int, int] | None:
    """Longest run of hits (allowing short gaps) that starts by ``max_start``.

    Returns ``(start, end_inclusive, hit_count)`` or ``None``.
    """

    best: tuple[int, int, int] | None = None
    count = len(hits)
    index = 0
    while index < count:
        if not hits[index]:
            index += 1
            continue
        if index > max_start:
            break
        start = index
        last = index
        total = 0
        gap = 0
        position = index
        while position < count:
            if hits[position]:
                last = position
                total += 1
                gap = 0
            else:
                gap += 1
                if gap > gap_samples:
                    break
            position += 1
        if best is None or (last - start) > (best[1] - best[0]):
            best = (start, last, total)
        index = last + 1
    return best


def passes_elbow(candidate: "ShaftCandidate", elbow, margin_px: float) -> bool:
    """True when the candidate's supported line continues ``margin_px`` beyond the elbow."""

    direction = np.asarray(candidate.tip, float) - np.asarray(candidate.grip, float)
    length = float(np.linalg.norm(direction))
    if length <= 1e-6 or elbow is None:
        return False
    along = float(np.dot(np.asarray(elbow, float) - np.asarray(candidate.grip, float), direction / length))
    return candidate.length_px >= max(0.0, along) + margin_px


def confidence_level(confidence: float, config: ClubConfig) -> str:
    if confidence >= config.high_confidence:
        return LEVEL_HIGH
    if confidence >= config.medium_confidence:
        return LEVEL_MEDIUM
    if confidence >= config.low_confidence:
        return LEVEL_LOW
    return LEVEL_UNAVAILABLE


NOISE_FLOOR_SCORE = 0.3


def candidate_margin(best_score: float, alternative_scores: Sequence[float]) -> float:
    """How clearly the chosen line beats the next-best *different* direction.

    A lone candidate is compared with a noise floor rather than with nothing,
    so a single faint line never looks unambiguous.
    """

    if best_score <= 0:
        return 0.0
    rival = max([NOISE_FLOOR_SCORE, *alternative_scores])
    return max(0.0, min(1.0, 1.0 - rival / best_score))


def frame_confidence(
    *,
    support: float,
    length_torso: float,
    margin: float,
    temporal: float | None,
    corroborated: bool,
    score: float = 1.2,
) -> tuple[float, dict[str, float]]:
    """Blend the evidence into one number in [0, 1], with its parts."""

    parts = {
        "support": max(0.0, min(1.0, support)),
        "length": max(0.0, min(1.0, length_torso / 1.2)),
        "margin": max(0.0, min(1.0, margin)),
        "temporal": 0.5 if temporal is None else max(0.0, min(1.0, temporal)),
        "corroborated": 1.0 if corroborated else 0.0,
        "evidence": max(0.0, min(1.0, score / 1.2)),
    }
    confidence = (
        0.25 * parts["support"]
        + 0.15 * parts["length"]
        + 0.15 * parts["margin"]
        + 0.20 * parts["temporal"]
        + 0.10 * parts["corroborated"]
        + 0.15 * parts["evidence"]
    )
    return confidence, parts


# ---------------------------------------------------------------------------
# Classical tracker
# ---------------------------------------------------------------------------


class ClassicalClubTracker:
    """Hands-anchored ridge search + LSD corroboration + Viterbi tracking."""

    name = "classical-ridge-v1"

    def __init__(self, config: ClubConfig | None = None) -> None:
        self.config = config or ClubConfig()
        self._lsd = None
        if hasattr(cv2, "createLineSegmentDetector"):
            try:
                self._lsd = cv2.createLineSegmentDetector()
            except cv2.error:  # pragma: no cover - build without LSD
                self._lsd = None

    # -- per frame -----------------------------------------------------------

    def detect_frame(
        self,
        frame_bgr: np.ndarray,
        record: object,
        *,
        torso_px: float,
        references: Sequence["ReferenceFrame"] = (),
        shaft_over_forearm_torso: float | None = None,
    ) -> FrameDetection:
        """Candidates for one frame.

        ``references`` are other frames of the same clip (with their hands
        position). A line that is still there in frames where the hands - and
        therefore the club - are somewhere else is static background.

        ``shaft_over_forearm_torso`` (off by default) keeps a candidate that
        points along a forearm when its supported line runs at least that many
        torso lengths *past the elbow*: a forearm edge ends at the elbow, a
        shaft lying over the forearm in the image does not. Such candidates
        still face the body-segment and static-background tests.
        """

        config = self.config
        frame_index = int(getattr(record, "index", 0))
        timestamp_ms = float(getattr(record, "timestamp_ms", 0.0))
        hands = hands_context(record, torso_px=torso_px, config=config)
        if hands.origin is None:
            return FrameDetection(frame_index, timestamp_ms, hands, (), reason=hands.reason)
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return FrameDetection(frame_index, timestamp_ms, hands, (), reason="no image for this frame")

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        maps = hessian_maps(gray, config.hessian_sigmas)
        raw, threshold = self._ray_candidates(maps, gray, hands)
        segments = self._lsd_segments(gray, hands)
        raw = self._merge(raw + self._lsd_candidates(maps, gray, segments, hands, threshold))
        eligible = [
            reference
            for reference in references
            if reference.frame_index != frame_index
            and reference.hands is not None
            and float(np.linalg.norm(reference.hands - hands.origin))
            >= config.reference_hand_distance * torso_px
        ]

        accepted: list[ShaftCandidate] = []
        rejected: list[ShaftCandidate] = []
        for candidate in raw:
            reason = ""
            for wrist, elbow in hands.forearms:
                if overlaps_forearm(
                    candidate.angle_deg, wrist, elbow, tolerance_deg=config.forearm_tolerance_deg
                ):
                    if shaft_over_forearm_torso is not None and passes_elbow(
                        candidate, elbow, torso_px * shaft_over_forearm_torso
                    ):
                        continue
                    reason = "runs along a forearm"
                    break
            if not reason:
                body = along_body_segment(
                    candidate.grip,
                    candidate.tip,
                    candidate.visible_start_px,
                    hands.body_segments,
                    torso_px=torso_px,
                    tolerance_deg=config.body_tolerance_deg,
                    distance=config.body_distance,
                    overlap=config.body_overlap,
                )
                if body:
                    reason = f"runs along the {body}"
            if not reason and len(eligible) >= config.min_reference_frames:
                if static_fraction(candidate, eligible, threshold, config) >= config.static_fraction:
                    reason = "static background line (present when the club is elsewhere)"
            corroborated = self._corroborated(candidate, segments, hands)
            updated = ShaftCandidate(
                grip=candidate.grip,
                tip=candidate.tip,
                angle_deg=candidate.angle_deg,
                visible_start_px=candidate.visible_start_px,
                length_px=candidate.length_px,
                support=candidate.support,
                strength=candidate.strength,
                score=candidate.score * (1.15 if corroborated else 1.0),
                method="ridge+lsd" if corroborated else "ridge",
                corroborated=corroborated,
                rejected=reason,
            )
            (rejected if reason else accepted).append(updated)
        accepted.sort(key=lambda item: item.score, reverse=True)
        rejected.sort(key=lambda item: item.score, reverse=True)
        reason = "" if accepted else "no thin line leaves the hands"
        if not accepted and rejected:
            reason = "only rejected lines were found (" + rejected[0].rejected + ")"
        return FrameDetection(
            frame_index,
            timestamp_ms,
            hands,
            tuple(accepted[: config.candidates_per_frame]),
            tuple(rejected[: config.candidates_per_frame]),
            reason,
        )

    def _ray_candidates(
        self,
        maps,
        gray: np.ndarray,
        hands: HandsContext,
    ) -> tuple[list[ShaftCandidate], float]:
        config = self.config
        shape = gray.shape
        torso = hands.torso_px
        origin = np.asarray(hands.origin, float)
        offset = config.origin_offset * torso
        origins = np.array(
            [
                origin,
                origin + [offset, 0.0],
                origin - [offset, 0.0],
                origin + [0.0, offset],
                origin - [0.0, offset],
            ]
        )
        angles = np.arange(-180.0 + config.angle_step_deg, 180.0 + 1e-9, config.angle_step_deg)
        directions = np.stack([np.cos(np.radians(angles)), -np.sin(np.radians(angles))], axis=-1)
        radii = np.arange(
            config.min_ray_start * torso,
            config.max_ray_length * torso,
            config.sample_step_px,
        )
        # Shape: origins x angles x samples.
        xs = origins[:, None, None, 0] + directions[None, :, None, 0] * radii[None, None, :]
        ys = origins[:, None, None, 1] + directions[None, :, None, 1] * radii[None, None, :]
        inside = (xs >= 1) & (ys >= 1) & (xs <= shape[1] - 2) & (ys <= shape[0] - 2)
        direction_grid = np.broadcast_to(directions[None, :, None, :], xs.shape + (2,))
        response = ridge_along(
            maps, xs, ys, direction_grid, along_ratio=config.along_ratio
        )
        response = np.where(inside, response, 0.0)
        # Adaptive threshold: a fraction of the strongest thin-line evidence
        # near the hands, never below a floor, so a blank frame finds nothing.
        strong = float(np.percentile(response[inside], 99.5)) if np.any(inside) else 0.0
        threshold = max(config.ridge_floor, config.ridge_relative * strong)
        symmetric = line_symmetry(
            gray, xs, ys, direction_grid,
            offset_px=config.symmetry_offset_px, ratio=config.symmetry_ratio,
        )
        hits = (response >= threshold) & symmetric

        gap = max(1, int(round(config.gap_tolerance_px / config.sample_step_px)))
        max_start = int(
            max(0, (config.max_ray_start - config.min_ray_start) * torso / config.sample_step_px)
        )
        min_len = config.min_visible_length * torso

        best_by_angle: dict[int, ShaftCandidate] = {}
        for origin_index in range(origins.shape[0]):
            for angle_index, angle in enumerate(angles):
                run = longest_supported_run(
                    hits[origin_index, angle_index], gap_samples=gap, max_start=max_start
                )
                if run is None:
                    continue
                start, end, total = run
                length = (end - start) * config.sample_step_px
                if length < min_len:
                    continue
                samples = end - start + 1
                support = total / samples
                strength = float(
                    np.mean(response[origin_index, angle_index, start : end + 1])
                ) / max(threshold, 1e-6)
                score = (
                    min(2.0, length / torso)
                    * support
                    * (0.5 + 0.5 * min(1.0, strength / 2.0))
                )
                grip = origins[origin_index]
                tip = grip + directions[angle_index] * radii[end]
                candidate = ShaftCandidate(
                    grip=grip.copy(),
                    tip=tip,
                    angle_deg=float(wrap_deg(angle)),
                    visible_start_px=float(radii[start]),
                    length_px=float(radii[end]),
                    support=float(support),
                    strength=float(strength),
                    score=float(score),
                )
                existing = best_by_angle.get(angle_index)
                if existing is None or candidate.score > existing.score:
                    best_by_angle[angle_index] = candidate
        return self._suppress(list(best_by_angle.values())), threshold

    def _suppress(self, candidates: list[ShaftCandidate]) -> list[ShaftCandidate]:
        """Keep the best candidate per direction (angular non-max suppression)."""

        kept: list[ShaftCandidate] = []
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            if all(
                abs(angle_difference(candidate.angle_deg, other.angle_deg)) > self.config.nms_deg
                for other in kept
            ):
                kept.append(candidate)
            if len(kept) >= self.config.raw_candidates:
                break
        return kept

    def _lsd_candidates(
        self,
        maps,
        gray: np.ndarray,
        segments: Sequence[tuple[np.ndarray, np.ndarray]],
        hands: HandsContext,
        threshold: float,
    ) -> list[ShaftCandidate]:
        """Line segments near the hands whose line *misses* the pose's hands a little.

        Pose wrists can be off by half a torso when the arms cross (the lead
        wrist is often not tracked at all), so a segment is allowed to pass
        up to ``lsd_max_miss`` torso lengths from the hands, with a score
        penalty that grows with the miss. The grip is where the segment's
        line passes closest to the hands.
        """

        config = self.config
        torso = hands.torso_px
        origin = np.asarray(hands.origin, float)
        candidates = []
        for a, b in segments:
            near, far = orient_from(origin, a, b)
            if np.linalg.norm(near - origin) > config.lsd_max_near * torso:
                continue
            miss = point_line_distance(origin, a, b)
            if miss > config.lsd_max_miss * torso:
                continue
            direction = far - near
            length = float(np.linalg.norm(direction))
            if length <= 1e-6:
                continue
            unit = direction / length
            grip = near + unit * float(np.dot(origin - near, unit))
            if float(np.dot(far - grip, unit)) <= float(np.dot(near - grip, unit)):
                continue
            radii = np.arange(
                max(0.0, float(np.dot(near - grip, unit))),
                float(np.dot(far - grip, unit)),
                config.sample_step_px,
            )
            if radii.size < 4:
                continue
            xs = grip[0] + unit[0] * radii
            ys = grip[1] + unit[1] * radii
            response = ridge_along(maps, xs[None, :], ys[None, :], unit, along_ratio=config.along_ratio)[0]
            symmetric = line_symmetry(
                gray, xs[None, :], ys[None, :], unit,
                offset_px=config.symmetry_offset_px, ratio=config.symmetry_ratio,
            )[0]
            hits = (response >= threshold) & symmetric
            support = float(np.mean(hits))
            if support < 0.4:
                continue
            strength = float(np.mean(response)) / max(threshold, 1e-6)
            visible = float(radii[-1] - radii[0])
            if visible < config.min_visible_length * torso:
                continue
            penalty = 1.0 - config.lsd_miss_penalty * (miss / (config.lsd_max_miss * torso))
            score = min(2.0, visible / torso) * support * (0.5 + 0.5 * min(1.0, strength / 2.0)) * penalty
            candidates.append(
                ShaftCandidate(
                    grip=grip,
                    tip=grip + unit * radii[-1],
                    angle_deg=direction_angle_deg(unit),
                    visible_start_px=float(radii[0]),
                    length_px=float(radii[-1]),
                    support=support,
                    strength=strength,
                    score=float(score),
                    method="lsd",
                )
            )
        return candidates

    def _merge(self, candidates: list[ShaftCandidate]) -> list[ShaftCandidate]:
        """One candidate per (direction, grip) from both generators."""

        kept: list[ShaftCandidate] = []
        limit = 0.3
        for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
            duplicate = False
            for other in kept:
                same_angle = abs(angle_difference(candidate.angle_deg, other.angle_deg)) <= self.config.nms_deg
                torso = max(1.0, float(np.linalg.norm(other.tip - other.grip)) / 2.0)
                same_place = point_line_distance(candidate.grip, other.grip, other.tip) <= limit * torso
                if same_angle and same_place:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(candidate)
            if len(kept) >= self.config.raw_candidates:
                break
        return kept

    def _lsd_segments(self, gray: np.ndarray, hands: HandsContext) -> list[tuple[np.ndarray, np.ndarray]]:
        if self._lsd is None:
            return []
        torso = hands.torso_px
        origin = np.asarray(hands.origin, float)
        radius = self.config.max_ray_length * torso
        x0 = int(max(0, origin[0] - radius))
        y0 = int(max(0, origin[1] - radius))
        x1 = int(min(gray.shape[1], origin[0] + radius))
        y1 = int(min(gray.shape[0], origin[1] + radius))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return []
        try:
            lines = self._lsd.detect(gray[y0:y1, x0:x1])[0]
        except cv2.error:  # pragma: no cover - defensive
            return []
        if lines is None:
            return []
        segments = []
        for x_a, y_a, x_b, y_b in lines.reshape(-1, 4):
            a = np.array([x_a + x0, y_a + y0], float)
            b = np.array([x_b + x0, y_b + y0], float)
            if np.linalg.norm(b - a) >= self.config.lsd_min_length * torso:
                segments.append((a, b))
        return segments

    def _corroborated(
        self,
        candidate: ShaftCandidate,
        segments: Sequence[tuple[np.ndarray, np.ndarray]],
        hands: HandsContext,
    ) -> bool:
        torso = hands.torso_px
        for a, b in segments:
            near, far = orient_from(candidate.grip, a, b)
            try:
                angle = direction_angle_deg(far - near)
            except ValueError:
                continue
            if abs(angle_difference(angle, candidate.angle_deg)) > self.config.lsd_angle_deg:
                continue
            middle = (near + far) / 2.0
            lateral = point_line_distance(middle, candidate.grip, candidate.tip)
            if lateral <= self.config.lsd_lateral * torso:
                return True
        return False

    # -- sequence ------------------------------------------------------------

    def track_sequence(
        self,
        frames: Iterable[np.ndarray],
        records: Sequence[object],
    ) -> ClubTrack:
        records = list(records)
        frames = as_frame_source(frames, records)
        torso = torso_length_px(records)
        references = build_references(frames, records, torso_px=torso, config=self.config)
        detections = [
            self.detect_frame(frame, record, torso_px=torso, references=references)
            for frame, record in frame_pairs(frames, records)
        ]
        return select_track(detections, config=self.config, tracker=self.name)

    def track_windows(
        self,
        frames: Sequence[np.ndarray] | Mapping[int, np.ndarray],
        records: Sequence[object],
        windows: Sequence[FrameWindow],
        *,
        cache: "CandidateCache | None" = None,
    ) -> ClubTrack:
        """Track the club only inside ``windows``, selected one window at a time.

        Everything global is unchanged: the torso scale is measured over the
        whole clip, the background references are sampled across the whole
        clip, and each frame is detected with the same configuration as the
        full-sequence pass. Only the expensive per-frame detection is
        restricted to the required frames, and the Viterbi pass runs per
        window so two windows are never treated as neighbours.

        ``cache`` stores per-frame candidates under a full identity; see
        :mod:`swingguru.club.cache`.
        """

        records = list(records)
        frames = as_frame_source(frames, records)
        torso = torso_length_px(records)
        references = build_references(frames, records, torso_px=torso, config=self.config)
        accessor = frame_accessor(frames, records, limit=len(records))
        positions = {int(getattr(record, "index", position)): position for position, record in enumerate(records)}

        chosen_frames: list[FrameShaft] = []
        chosen_detections: list[FrameDetection] = []
        for window in windows:
            window_detections: list[FrameDetection] = []
            for frame_index in window.indices():
                position = positions.get(int(frame_index))
                if position is None:
                    continue
                detection = None if cache is None else cache.get(int(frame_index))
                if detection is None:
                    detection = self.detect_frame(
                        accessor(position), records[position], torso_px=torso, references=references
                    )
                    if cache is not None:
                        cache.put(detection)
                window_detections.append(detection)
            if not window_detections:
                continue
            selected = select_track(window_detections, config=self.config, tracker=self.name)
            chosen_frames.extend(selected.frames)
            chosen_detections.extend(window_detections)
        return ClubTrack(tuple(chosen_frames), tuple(chosen_detections), self.name, self.config)


@dataclass(frozen=True)
class ReferenceFrame:
    """Another frame of the clip, kept to recognise static background lines."""

    frame_index: int
    hands: np.ndarray | None
    maps: Sequence[tuple[np.ndarray, np.ndarray, np.ndarray]]


def as_frame_source(
    frames: Iterable[np.ndarray] | Mapping[int, np.ndarray],
    records: Sequence[object],
) -> Sequence[np.ndarray] | Mapping[int, np.ndarray]:
    """Materialise a non-mapping frame source once, trimmed to ``records``.

    ``frames`` is either a mapping keyed by frame index or an iterable aligned
    with ``records`` (list, tuple or generator). Generators are consumed here,
    once, because several helpers need to length-check and re-read the source.
    """

    if isinstance(frames, MappingABC):
        return frames
    return list(frames)[: len(records)]


def frame_accessor(
    frames: Sequence[np.ndarray] | Mapping[int, np.ndarray],
    records: Sequence[object],
    *,
    limit: int | None = None,
):
    """A ``position -> frame image`` lookup for either frame container.

    ``frames`` is either a sequence aligned with ``records`` (the original
    API) or a mapping keyed by frame index, which lets a caller decode only
    the frames it needs.
    """

    frames = as_frame_source(frames, records)
    limit = len(records) if limit is None else limit
    if isinstance(frames, MappingABC):
        keys = [int(getattr(record, "index", position)) for position, record in enumerate(records)]

        def lookup(position: int):
            if not 0 <= position < len(keys):
                return None
            return frames.get(keys[position])

        return lookup

    def lookup(position: int):
        if not 0 <= position < limit or position >= len(frames):  # type: ignore[arg-type]
            return None
        return frames[position]

    return lookup


def frame_pairs(
    frames: Sequence[np.ndarray] | Mapping[int, np.ndarray],
    records: Sequence[object],
) -> list[tuple[np.ndarray | None, object]]:
    """``(frame, record)`` pairs, preserving the original zip semantics."""

    frames = as_frame_source(frames, records)
    if isinstance(frames, MappingABC):
        accessor = frame_accessor(frames, records)
        return [(accessor(position), record) for position, record in enumerate(records)]
    trimmed = list(frames)[: len(records)]
    return list(zip(trimmed, records))


def reference_picks(count: int, *, reference_frames: int) -> tuple[int, ...]:
    """Positions of the evenly spaced background-reference frames."""

    if count <= 0 or reference_frames <= 0:
        return ()
    picks = np.linspace(0, count - 1, min(count, reference_frames))
    return tuple(sorted({int(round(value)) for value in picks}))


def reference_hands(
    records: Sequence[object],
    *,
    torso_px: float,
    config: ClubConfig,
) -> tuple[tuple[int, np.ndarray | None], ...]:
    """Frame index and hand position of each background reference.

    Computed from the pose records alone, so callers can know which frames the
    static-line test will use before any image is decoded.
    """

    found = []
    for position in reference_picks(len(records), reference_frames=config.reference_frames):
        record = records[position]
        hands = hands_context(record, torso_px=torso_px, config=config).origin
        found.append(
            (
                int(getattr(record, "index", position)),
                None if hands is None else np.asarray(hands, float),
            )
        )
    return tuple(found)


def build_references(
    frames: Sequence[np.ndarray] | Mapping[int, np.ndarray],
    records: Sequence[object],
    *,
    torso_px: float,
    config: ClubConfig,
) -> list[ReferenceFrame]:
    """Evenly spaced frames across the clip with their Hessian maps."""

    frames = as_frame_source(frames, records)
    count = len(records) if isinstance(frames, MappingABC) else min(len(frames), len(records))
    if count == 0:
        return []
    positions = {int(getattr(record, "index", position)): position for position, record in enumerate(records)}
    accessor = frame_accessor(frames, records, limit=count)
    references = []
    for frame_index, hands in reference_hands(records[:count], torso_px=torso_px, config=config):
        position = positions.get(frame_index)
        if position is None:
            continue
        frame = accessor(position)
        if frame is None or getattr(frame, "size", 0) == 0:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        references.append(
            ReferenceFrame(
                frame_index,
                None if hands is None else np.asarray(hands, float),
                hessian_maps(gray, config.hessian_sigmas),
            )
        )
    return references


def static_fraction(
    candidate: ShaftCandidate,
    references: Sequence[ReferenceFrame],
    threshold: float,
    config: ClubConfig,
) -> float:
    """Share of a candidate's visible pixels that stay a line in other frames.

    Only frames where the hands are far away are passed in, so the club
    itself cannot be there. A pixel counts as static when the same thin line
    is present in at least ``static_presence`` of those frames.
    """

    direction = np.asarray(candidate.tip, float) - np.asarray(candidate.grip, float)
    length = float(np.linalg.norm(direction))
    if length <= 1e-6 or not references:
        return 0.0
    unit = direction / length
    radii = np.arange(candidate.visible_start_px, length, config.sample_step_px * 2)
    if radii.size == 0:
        return 0.0
    xs = candidate.grip[0] + unit[0] * radii
    ys = candidate.grip[1] + unit[1] * radii
    present = np.zeros(radii.shape, dtype=float)
    for reference in references:
        response = ridge_along(
            reference.maps, xs[None, :], ys[None, :], unit, along_ratio=config.along_ratio
        )[0]
        present += (response >= threshold).astype(float)
    presence = present / len(references)
    return float(np.mean(presence >= config.static_presence))


def _time_steps(detections: Sequence[FrameDetection]) -> list[float]:
    steps = [1.0 / 30.0]
    for previous, current in zip(detections, detections[1:]):
        dt = (current.timestamp_ms - previous.timestamp_ms) / 1000.0
        steps.append(dt if math.isfinite(dt) and dt > 0 else 1.0 / 30.0)
    return steps


def _hand_steps(detections: Sequence[FrameDetection]) -> list[float | None]:
    steps: list[float | None] = [None]
    for previous, current in zip(detections, detections[1:]):
        a = previous.hands.origin
        b = current.hands.origin
        torso = current.hands.torso_px
        if a is None or b is None or torso <= 1.0:
            steps.append(None)
        else:
            steps.append(float(np.linalg.norm(np.asarray(b) - np.asarray(a)) / torso))
    return steps


def select_track(
    detections: Sequence[FrameDetection],
    *,
    config: ClubConfig,
    tracker: str = "classical-ridge-v1",
) -> ClubTrack:
    """Viterbi selection over candidates plus a 'no club' state, then scoring."""

    count = len(detections)
    if count == 0:
        return ClubTrack((), (), tracker, config)
    steps = _hand_steps(detections)
    times = _time_steps(detections)
    options: list[list[ShaftCandidate | None]] = [
        list(detection.candidates) + [None] for detection in detections
    ]

    def emission(candidate: ShaftCandidate | None) -> float:
        return config.none_cost if candidate is None else -min(2.0, candidate.score)

    def allowed(index: int) -> float:
        return allowed_rotation_deg(
            steps[index],
            base_deg=config.rotation_base_deg_per_s * times[index],
            per_torso_deg=config.rotation_per_torso_deg,
            cap_deg=config.rotation_cap_deg_per_s * times[index],
        )

    def transition(previous: ShaftCandidate | None, current: ShaftCandidate | None, index: int) -> float:
        if previous is None or current is None:
            return config.gap_cost if (previous is None) != (current is None) else 0.0
        limit = allowed(index)
        delta = abs(angle_difference(current.angle_deg, previous.angle_deg))
        if delta > 2.5 * limit:
            return math.inf
        return config.transition_weight * (delta / limit) ** 2

    costs = [[emission(option) for option in options[0]]]
    back: list[list[int]] = [[-1] * len(options[0])]
    for index in range(1, count):
        row_cost = []
        row_back = []
        for option in options[index]:
            best_cost = math.inf
            best_from = len(options[index - 1]) - 1
            for previous_index, previous in enumerate(options[index - 1]):
                cost = costs[-1][previous_index] + transition(previous, option, index)
                if cost < best_cost:
                    best_cost, best_from = cost, previous_index
            row_cost.append(best_cost + emission(option))
            row_back.append(best_from)
        costs.append(row_cost)
        back.append(row_back)

    path = [int(np.argmin(costs[-1]))]
    for index in range(count - 1, 0, -1):
        path.append(back[index][path[-1]])
    path.reverse()
    chosen = [options[index][choice] for index, choice in enumerate(path)]

    frames: list[FrameShaft] = []
    for index, detection in enumerate(detections):
        candidate = chosen[index]
        if candidate is None:
            reason = detection.reason or "no candidate fitted the tracked sequence"
            if detection.candidates:
                reason = "candidates were inconsistent with the neighbouring frames"
            frames.append(
                FrameShaft(
                    detection.frame_index,
                    detection.timestamp_ms,
                    STATUS_UNAVAILABLE,
                    LEVEL_UNAVAILABLE,
                    0.0,
                    reason=reason,
                )
            )
            continue
        neighbours = []
        for other in (index - 1, index + 1):
            if 0 <= other < count and chosen[other] is not None:
                limit = allowed(max(index, other))
                delta = abs(angle_difference(candidate.angle_deg, chosen[other].angle_deg))
                neighbours.append(max(0.0, 1.0 - delta / (2.0 * limit)))
        temporal = float(np.mean(neighbours)) if neighbours else None
        alternatives = [
            other.score
            for other in detection.candidates
            if other is not candidate
            and abs(angle_difference(other.angle_deg, candidate.angle_deg)) > 15.0
        ]
        margin = candidate_margin(candidate.score, alternatives)
        confidence, parts = frame_confidence(
            support=candidate.support,
            length_torso=(candidate.length_px - candidate.visible_start_px) / max(detection.hands.torso_px, 1e-6),
            margin=margin,
            temporal=temporal,
            corroborated=candidate.corroborated,
            score=candidate.score,
        )
        if detection.hands.source == "weak":
            confidence *= 0.8
            parts["weak_hands"] = 1.0
        level = confidence_level(confidence, config)
        if level == LEVEL_UNAVAILABLE:
            frames.append(
                FrameShaft(
                    detection.frame_index,
                    detection.timestamp_ms,
                    STATUS_UNAVAILABLE,
                    LEVEL_UNAVAILABLE,
                    confidence,
                    reason="the best line's evidence was too weak to report",
                    components=parts,
                )
            )
            continue
        frames.append(
            FrameShaft(
                detection.frame_index,
                detection.timestamp_ms,
                STATUS_DETECTED,
                level,
                confidence,
                angle_deg=candidate.angle_deg,
                grip=candidate.grip,
                tip=candidate.tip,
                method=candidate.method,
                temporal_consistency=temporal,
                components=parts,
            )
        )

    frames = interpolate_short_gaps(frames, detections, config=config)
    return ClubTrack(tuple(frames), tuple(detections), tracker, config)


def interpolate_short_gaps(
    frames: Sequence[FrameShaft],
    detections: Sequence[FrameDetection],
    *,
    config: ClubConfig,
) -> list[FrameShaft]:
    """Fill gaps of at most ``max_interpolated_frames`` between confident frames.

    Only between two MEDIUM-or-better detections whose angles are close enough
    for the gap; the filled frames are marked interpolated and capped at LOW.
    Larger gaps stay unavailable.
    """

    result = list(frames)
    good = {LEVEL_HIGH, LEVEL_MEDIUM}
    index = 0
    while index < len(result):
        if result[index].available:
            index += 1
            continue
        start = index
        while index < len(result) and not result[index].available:
            index += 1
        end = index  # first available after the gap, or len
        gap = end - start
        if start == 0 or end >= len(result) or gap > config.max_interpolated_frames:
            continue
        before = result[start - 1]
        after = result[end]
        if before.level not in good or after.level not in good or before.status != STATUS_DETECTED:
            continue
        delta = angle_difference(after.angle_deg, before.angle_deg)  # type: ignore[arg-type]
        span_s = max(1e-6, (after.timestamp_ms - before.timestamp_ms) / 1000.0)
        if abs(delta) > config.rotation_cap_deg_per_s * span_s / 2:
            continue
        length = (
            float(np.linalg.norm(before.tip - before.grip)) + float(np.linalg.norm(after.tip - after.grip))
        ) / 2.0
        for offset, position in enumerate(range(start, end), start=1):
            fraction = offset / (gap + 1)
            angle = wrap_deg(before.angle_deg + delta * fraction)  # type: ignore[operator]
            # The grip is interpolated between the neighbouring detections,
            # not taken from the pose: when a frame is missed it is often
            # because the pose's hands were wrong in that frame.
            grip = np.asarray(before.grip, float) + (
                np.asarray(after.grip, float) - np.asarray(before.grip, float)
            ) * fraction
            tip = grip + unit_from_angle(angle) * length
            confidence = min(config.medium_confidence - 0.01, 0.6 * min(before.confidence, after.confidence))
            result[position] = FrameShaft(
                result[position].frame_index,
                result[position].timestamp_ms,
                STATUS_INTERPOLATED,
                LEVEL_LOW,
                confidence,
                angle_deg=angle,
                grip=grip,
                tip=tip,
                method="interpolated",
                temporal_consistency=None,
                interpolated=True,
                reason=f"interpolated across a {gap}-frame gap",
            )
    return result


__all__ = [
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "LEVEL_UNAVAILABLE",
    "STATUS_DETECTED",
    "STATUS_INTERPOLATED",
    "STATUS_UNAVAILABLE",
    "ClassicalClubTracker",
    "ClubConfig",
    "ClubTrack",
    "ClubTracker",
    "FrameDetection",
    "FrameShaft",
    "HandsContext",
    "ShaftCandidate",
    "confidence_level",
    "as_frame_source",
    "frame_accessor",
    "frame_pairs",
    "frame_confidence",
    "hands_context",
    "hessian_maps",
    "interpolate_short_gaps",
    "longest_supported_run",
    "reference_hands",
    "reference_picks",
    "ridge_along",
    "select_track",
    "torso_length_px",
]
