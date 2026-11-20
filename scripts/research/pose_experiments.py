"""RTMW pose variants that are not on the product path (research harnesses).

Two families live here, both driven by ``scripts/benchmark_pose.py``:

* :class:`ScheduledPoseTracker` - the same box-tracking flow rtmlib's
  ``PoseTracker`` uses, but with an explicit detection schedule and an
  optional quality/boundary/intervallic re-acquisition trigger. With
  ``cadence=5`` and no trigger it reproduces the production path exactly; with
  a longer interval it is the "persistent ROI, detector only on demand" variant.
* :func:`run_adaptive_dense` - a sparse pass over the whole clip proposes the
  Address-setup + active-swing span from the tracker's *own* coarse poses, then
  a dense pass infers every frame inside that span and the remaining frames are
  interpolated from their inferred neighbours and marked as such. The frame
  count, indices and timestamps are untouched, so the viewer timebase and the
  preview length do not change.

Neither variant may read the accepted baseline landmarks or any hardcoded
frame/coordinate: the span proposal uses only poses this run produced.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from swingguru.landmarks import PoseObservation
from swingguru.measurements import MeasurementConfig
from swingguru.pipeline import FrameRecord, build_record
from swingguru.pose_rtm import select_golfer, to_observation

# rtmlib's own defaults, repeated here so the variant is explicit.
KEYPOINT_FLOOR = 0.3
MIN_CONFIDENT_KEYPOINTS = 5
POSE_MODE = "balanced"
POSE_MODEL = "wholebody"


@dataclass
class TrackerStats:
    """Everything the harness needs to explain a variant's cost."""

    detector_calls: int = 0
    pose_calls: int = 0
    reacquisitions: int = 0
    trigger_reasons: list[str] = field(default_factory=list)
    lost_frames: int = 0
    empty_detections: int = 0

    def note(self, reason: str) -> None:
        self.reacquisitions += 1
        self.trigger_reasons.append(reason)

    def summary(self) -> dict:
        counted: dict[str, int] = {}
        for reason in self.trigger_reasons:
            counted[reason] = counted.get(reason, 0) + 1
        return {
            "detector_calls": self.detector_calls,
            "pose_calls": self.pose_calls,
            "reacquisitions": self.reacquisitions,
            "trigger_reasons": counted,
            "lost_frames": self.lost_frames,
            "empty_detections": self.empty_detections,
        }


class ScheduledPoseTracker:
    """rtmlib-equivalent box tracking with an explicit detection schedule.

    Mirrors ``rtmlib.tools.solution.pose_tracker.PoseTracker`` for
    ``tracking=False``: on a detection frame the person detector runs on the
    full frame and every detected box is posed; between detections the previous
    frame's ``pose_to_bbox`` boxes are reused. The detection *decision* is made
    from the frame index and a trigger set instead of rtmlib's call counter, so
    a sparse or dense pass keeps the same global grid.
    """

    def __init__(
        self,
        *,
        model: str = POSE_MODEL,
        mode: str = POSE_MODE,
        cadence: int = 5,
        interval: int | None = None,
        area_jump: float = 0.35,
        min_keypoints: int = MIN_CONFIDENT_KEYPOINTS,
        score_floor: float = KEYPOINT_FLOOR,
        stats: TrackerStats | None = None,
    ) -> None:
        self.model = model
        self.mode = mode
        self.cadence = max(1, int(cadence))
        # ``interval`` is the safety bound on how long a tracked box may go
        # without re-anchoring; None means "only the quality triggers".
        self.interval = interval
        self.area_jump = float(area_jump)
        self.min_keypoints = int(min_keypoints)
        self.score_floor = float(score_floor)
        self.stats = stats or TrackerStats()
        self.detector_due = True  # the first frame always acquires
        self.last_detection_index = -1
        self._boxes: list[np.ndarray] = []
        self.box_history: list[tuple[int, list[float], bool]] = []
        self._tracker = None

    # -- rtmlib plumbing ---------------------------------------------------

    def _ensure(self):
        if self._tracker is None:
            from rtmlib import PoseTracker, Wholebody

            solution = Wholebody if self.model == "wholebody" else None
            if solution is None:  # pragma: no cover - only wholebody is used
                raise ValueError("only the wholebody model is supported here")
            self._tracker = PoseTracker(
                solution,
                det_frequency=1,
                tracking=False,
                mode=self.mode,
                backend="onnxruntime",
                device="cpu",
                to_openpose=False,
            )
        return self._tracker

    @property
    def detector(self):
        return self._ensure().det_model

    @property
    def pose_model(self):
        return self._ensure().pose_model

    def close(self) -> None:
        self._tracker = None

    # -- schedule ----------------------------------------------------------

    def schedule(
        self,
        frame_index: int,
        *,
        force_detection: bool = False,
        allow_tracking: bool = True,
    ) -> str:
        """Decide whether this frame re-anchors, and why.

        ``allow_tracking=False`` disables the between-detection box reuse (used
        by the sparse pass, which only ever wants scheduled detections).
        """

        if force_detection:
            return "forced"
        if frame_index % self.cadence == 0:
            return "schedule"
        if not allow_tracking:
            return "no-tracking"
        if not self._boxes:
            return "no-box"
        if self.interval is not None and frame_index - self.last_detection_index >= self.interval:
            return "interval"
        return ""

    def reset(self) -> None:
        self.detector_due = True
        self.last_detection_index = -1
        self._boxes = []
        self.box_history = []
        self.stats = TrackerStats()

    # -- one frame ---------------------------------------------------------

    def detect(
        self,
        frame_bgr: np.ndarray,
        frame_index: int,
        *,
        force_detection: bool = False,
        allow_tracking: bool = True,
    ) -> PoseObservation:
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return PoseObservation.missing("empty frame")
        reason = self.schedule(frame_index, force_detection=force_detection, allow_tracking=allow_tracking)
        if reason:
            boxes = list(self.detector(frame_bgr))
            self.stats.detector_calls += 1
            if reason != "schedule":
                self.stats.note(reason)
            if not boxes:
                self.stats.empty_detections += 1
                self._boxes = []
                self.last_detection_index = frame_index
                return PoseObservation.missing("the person detector found nobody")
        else:
            boxes = [box.copy() for box in self._boxes]
            if not boxes:
                return PoseObservation.missing("no tracked box to reuse")

        keypoints, scores = self.pose_model(frame_bgr, bboxes=boxes)
        self.stats.pose_calls += 1
        height, width = frame_bgr.shape[:2]
        person = select_golfer(keypoints, scores)
        if person is None:
            self.stats.lost_frames += 1
            self._boxes = []
            return PoseObservation.missing("no confidently tracked body in the crop")
        if reason:
            self.last_detection_index = frame_index
        self._boxes = [pose_to_bbox(kpts) for kpts in keypoints]
        self.box_history.append(
            (frame_index, [float(value) for value in self._boxes[0]], bool(reason))
        )
        return to_observation(keypoints[person], scores[person], width=width, height=height)

    # -- diagnostics -------------------------------------------------------

    def roi_state(self) -> dict:
        if not self._boxes:
            return {"tracked_boxes": 0, "box": None, "area": None}
        box = self._boxes[0]
        return {
            "tracked_boxes": len(self._boxes),
            "box": [float(value) for value in box],
            "area": float((box[2] - box[0]) * (box[3] - box[1])),
        }

    def crop_stability(self) -> dict:
        """How much the tracked crop moved and changed size over the run."""

        if len(self.box_history) < 2:
            return {"frames": len(self.box_history), "max_area_jump": None, "max_centre_jump_px": None}
        areas: list[float] = []
        centres: list[np.ndarray] = []
        for _, box, _ in self.box_history:
            x0, y0, x1, y1 = box
            areas.append(float((x1 - x0) * (y1 - y0)))
            centres.append(np.array([(x0 + x1) / 2.0, (y0 + y1) / 2.0]))
        area_jumps = [
            abs(areas[position] / areas[position - 1] - 1.0)
            for position in range(1, len(areas))
            if areas[position - 1] > 0
        ]
        centre_jumps = [
            float(np.linalg.norm(centres[position] - centres[position - 1]))
            for position in range(1, len(centres))
        ]
        return {
            "frames": len(self.box_history),
            "max_area_jump": round(max(area_jumps), 4) if area_jumps else None,
            "max_centre_jump_px": round(max(centre_jumps), 2) if centre_jumps else None,
            "median_area": round(float(np.median(areas)), 1),
        }


def pose_to_bbox(keypoints: np.ndarray, expansion: float = 1.25) -> np.ndarray:
    """The same box rtmlib derives from a pose, kept byte-compatible."""

    keypoints = np.asarray(keypoints, float)
    if keypoints.size == 0:
        return np.zeros(4, float)
    x = keypoints[:, 0]
    y = keypoints[:, 1]
    box = np.array([x.min(), y.min(), x.max(), y.max()])
    centre = np.array([box[0] + box[2], box[1] + box[3]]) / 2
    return np.concatenate(
        [
            centre - (centre - box[:2]) * expansion,
            centre + (box[2:] - centre) * expansion,
        ]
    )


# ---------------------------------------------------------------------------
# Adaptive dense refinement
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveResult:
    records: list[FrameRecord]
    stats: dict


def hand_point(observation: PoseObservation, *, width: int, height: int) -> np.ndarray | None:
    """Mean of the tracked wrists in pixels, or ``None``."""

    found = []
    for landmark in observation.landmarks:
        score = landmark.visibility if landmark.visibility is not None else landmark.presence
        if (
            landmark.name in {"left_wrist", "right_wrist"}
            and landmark.finite
            and score is not None
            and float(score) >= KEYPOINT_FLOOR
        ):
            found.append(np.array([landmark.x * width, landmark.y * height], float))
    if not found:
        return None
    return np.mean(np.stack(found, axis=0), axis=0)


def propose_dense_span(
    series: Sequence[tuple[int, float | None]],
    *,
    fps: float,
    frame_count: int,
    margin_before_s: float = 1.5,
    margin_after_s: float = 1.5,
    threshold_mad: float = 6.0,
    min_run: int = 3,
    floor: float | None = None,
) -> dict:
    """Span of frames worth inferring densely, from this run's own coarse poses.

    Motion is the hand displacement between coarse samples in torso lengths per
    second, thresholded at ``max(floor, median + threshold_mad * MAD)``, and a
    run of at least ``min_run`` consecutive moving samples is required at each
    end so a single noisy sample cannot define the span. ``floor`` defaults to
    the moment estimator's own "the golfer is still" speed
    (``MomentConfig.stability_speed``), so the span starts where the engine
    itself would call the golfer still rather than from a tuned number. No
    baseline frame number, coordinate or phase index is consulted.
    """

    if floor is None:
        from swingguru.auto_moments import MomentConfig

        floor = MomentConfig().stability_speed
    values = np.asarray([value for _, value in series if value is not None], dtype=float)
    if values.size == 0:
        return {"available": False, "reason": "no coarse hand positions"}
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) or 1e-6
    threshold = max(floor, median + threshold_mad * mad)
    flags = [value is not None and value > threshold for _, value in series]
    indices = [index for index, _ in series]
    moving = _sustained_indices(indices, flags, min_run=min_run)
    if not moving:
        return {
            "available": False,
            "reason": f"no run of {min_run} moving coarse samples",
            "median": round(median, 4),
            "mad": round(mad, 4),
            "threshold": round(threshold, 4),
            "min_run": min_run,
        }
    first, last = min(moving), max(moving)
    before = int(round(margin_before_s * fps))
    after = int(round(margin_after_s * fps))
    return {
        "available": True,
        "median": round(median, 4),
        "mad": round(mad, 4),
        "threshold": round(threshold, 4),
        "min_run": min_run,
        "motion_first": first,
        "motion_last": last,
        "start": max(0, first - before),
        "end": min(frame_count - 1, last + after),
        "margin_before_frames": before,
        "margin_after_frames": after,
    }


def _sustained_indices(indices: Sequence[int], flags: Sequence[bool], *, min_run: int) -> list[int]:
    """Indices belonging to a run of at least ``min_run`` consecutive flags."""

    kept: list[int] = []
    position = 0
    while position < len(flags):
        if not flags[position]:
            position += 1
            continue
        end = position
        while end + 1 < len(flags) and flags[end + 1]:
            end += 1
        if end - position + 1 >= min_run:
            kept.extend(indices[position : end + 1])
        position = end + 1
    return kept


def _coarse_series(
    sampled: Mapping[int, PoseObservation],
    *,
    width: int,
    height: int,
    fps: float,
) -> list[tuple[int, float | None]]:
    indices = sorted(sampled)
    points = {index: hand_point(sampled[index], width=width, height=height) for index in indices}
    torso = torso_length_px(sampled, width=width, height=height)
    series: list[tuple[int, float | None]] = []
    for position in range(1, len(indices)):
        previous, current = indices[position - 1], indices[position]
        first, second = points[previous], points[current]
        if first is None or second is None or torso is None:
            series.append((current, None))
            continue
        seconds = max(1e-6, (current - previous) / fps)
        series.append((current, float(np.linalg.norm(second - first)) / torso / seconds))
    return series


def torso_length_px(
    sampled: Mapping[int, PoseObservation],
    *,
    width: int,
    height: int,
) -> float | None:
    """Median hip-to-shoulder length over the coarse samples, in pixels."""

    lengths: list[float] = []
    for observation in sampled.values():
        named = {landmark.name: landmark for landmark in observation.landmarks}
        hip = _midpoint(named, ("left_hip", "right_hip"), width=width, height=height)
        shoulder = _midpoint(named, ("left_shoulder", "right_shoulder"), width=width, height=height)
        if hip is None or shoulder is None:
            continue
        length = float(np.linalg.norm(shoulder - hip))
        if math.isfinite(length) and length > 1.0:
            lengths.append(length)
    if not lengths:
        return None
    return float(np.median(lengths))


def _midpoint(named: Mapping[str, object], names: Sequence[str], *, width: int, height: int) -> np.ndarray | None:
    found = []
    for name in names:
        landmark = named.get(name)
        if landmark is None or not getattr(landmark, "finite", False):
            continue
        found.append(np.array([landmark.x * width, landmark.y * height], float))
    if not found:
        return None
    return np.mean(np.stack(found, axis=0), axis=0)


def run_adaptive_dense(
    video: Path,
    *,
    sparse_step: int = 15,
    cadence: int = 5,
    margin_before_s: float = 1.5,
    margin_after_s: float = 1.5,
    threshold_mad: float = 6.0,
    min_run: int = 3,
    config: MeasurementConfig | None = None,
) -> AdaptiveResult:
    """Sparse proposal pass, dense span pass, interpolated remainder.

    The detector grid is global (``index % cadence == 0``), and the dense pass
    starts on a grid frame, so every frame inside the span is inferred with the
    same crop chain the production path would use. Frames outside keep the
    original index, timestamp, size and frame count: they are interpolated
    between their inferred neighbours and carry an explicit note.
    """

    from swingguru.video import open_video

    settings = config or MeasurementConfig()
    tracker = ScheduledPoseTracker(cadence=cadence)
    observations: dict[int, PoseObservation] = {}
    dense: set[int] = set()
    frames: dict[int, tuple[float, int, int]] = {}
    span: dict = {}
    try:
        with open_video(video) as stream:
            info = stream.info
            fps = float(info.fps) if info.fps else 30.0
            width = int(info.width) or 1
            height = int(info.height) or 1
            # 1. sparse proposal pass over the whole clip (streamed, so no
            # frame list is retained; the dense pass decodes again).
            for item in stream.frames():
                frames[item.index] = (item.timestamp_ms, item.image.shape[1], item.image.shape[0])
                if item.index % sparse_step == 0:
                    observations[item.index] = tracker.detect(item.image, item.index, allow_tracking=False)
            frame_count = info.frame_count or len(frames)
            series = _coarse_series(observations, width=width, height=height, fps=fps)
            span = propose_dense_span(
                series,
                fps=fps,
                frame_count=frame_count,
                margin_before_s=margin_before_s,
                margin_after_s=margin_after_s,
                threshold_mad=threshold_mad,
                min_run=min_run,
            )
            sparse_stats = tracker.stats.summary()

            # 2. dense pass over the proposed span, starting on a grid frame
            dense_stats = TrackerStats()
            if not span.get("available"):
                # No proposal means no evidence for a dense span; fall back to
                # inferring the whole clip rather than interpolating all of it.
                span = {
                    **span,
                    "start": 0,
                    "end": frame_count - 1,
                    "fallback": "dense over the whole clip",
                }
            start = span["start"] - (span["start"] % cadence)
            end = span["end"]
            tracker.reset()
            with open_video(video) as dense_stream:
                for item in dense_stream.frames(start=max(0, start), max_frames=end - start + 1):
                    observations[item.index] = tracker.detect(
                        item.image, item.index, force_detection=item.index == start
                    )
                    dense.add(item.index)
            dense_stats = tracker.stats.summary()
            span["dense_start"] = start
            span["dense_frames"] = len(dense)

        records = _assemble_records(frames, observations, dense, settings)
    finally:
        tracker.close()
    return AdaptiveResult(
        records=records,
        stats={
            "span": span,
            "sparse": sparse_stats,
            "dense": dense_stats,
            "inferred_frames": len(observations),
            "dense_frames": len(dense),
            "interpolated_frames": sum(1 for record in records if "interpolated" in " ".join(record.notes)),
            "missing_frames": sum(1 for record in records if not record.pose_detected),
            "frames": len(records),
        },
    )


def _assemble_records(
    frames: Mapping[int, tuple[float, int, int]],
    observations: Mapping[int, PoseObservation],
    dense: set[int],
    config: MeasurementConfig,
) -> list[FrameRecord]:
    indices = sorted(frames)
    inferred = sorted(observations)
    records: list[FrameRecord] = []
    for index in indices:
        timestamp_ms, width, height = frames[index]
        observation = observations.get(index)
        notes: tuple[str, ...] = ()
        if observation is None:
            before = max((item for item in inferred if item < index), default=None)
            after = min((item for item in inferred if item > index), default=None)
            if before is None or after is None:
                observation = PoseObservation.missing("no pose was inferred on this frame")
                notes = ("pose was not inferred and had no neighbour to interpolate from",)
            else:
                blend = (index - before) / (after - before)
                observation = interpolate_observation(observations[before], observations[after], blend)
                notes = ("pose interpolated between inferred frames",)
        blank = np.zeros((height, width, 3), np.uint8)
        record = _build(index, timestamp_ms, blank, observation, config, notes)
        records.append(record)
    return records


def _build(index, timestamp_ms, image, observation, config, notes) -> FrameRecord:
    return build_record(
        index,
        timestamp_ms,
        image,
        observation,
        config=config,
        notes=notes,
    )


def interpolate_observation(first: PoseObservation, second: PoseObservation, blend: float) -> PoseObservation:
    """Linear landmark interpolation keeping the lower neighbour confidence."""

    by_name = {landmark.name: landmark for landmark in second.landmarks}
    landmarks = []
    for landmark in first.landmarks:
        other = by_name.get(landmark.name)
        if other is None:
            continue
        landmarks.append(
            type(landmark)(
                index=landmark.index,
                name=landmark.name,
                x=landmark.x + (other.x - landmark.x) * blend,
                y=landmark.y + (other.y - landmark.y) * blend,
                z=landmark.z + (other.z - landmark.z) * blend,
                visibility=_lower(landmark.visibility, other.visibility),
                presence=_lower(landmark.presence, other.presence),
            )
        )
    return PoseObservation(detected=bool(landmarks), landmarks=tuple(landmarks))


def _lower(first: float | None, second: float | None) -> float | None:
    if first is None or second is None:
        return first if second is None else second
    return min(float(first), float(second))


__all__ = [
    "AdaptiveResult",
    "KEYPOINT_FLOOR",
    "MIN_CONFIDENT_KEYPOINTS",
    "ScheduledPoseTracker",
    "TrackerStats",
    "hand_point",
    "interpolate_observation",
    "pose_to_bbox",
    "propose_dense_span",
    "run_adaptive_dense",
    "torso_length_px",
]
