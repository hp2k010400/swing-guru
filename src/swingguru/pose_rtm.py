"""RTMPose / RTMW pose provider (Apache-2.0 models via ``rtmlib`` + ONNX Runtime).

Why this exists: on real down-the-line footage MediaPipe BlazePose lost the
lead (left) arm entirely - 0 % of lead wrist and elbow frames between Address
and Impact on the test swing - and put the only tracked wrist on the chest at
Mid-Back. RTMW tracked both arms on every one of those frames and placed the
wrists on the grip (see ``docs/research/fastest-sensei-path.md``).

The adapter maps COCO-WholeBody's 133 keypoints onto the engine's 33-point
MediaPipe vocabulary, so measurements, moments, playback and the club tracker
work unchanged. Each keypoint's model score is used as both ``visibility`` and
``presence`` (clipped to 0..1), so the engine's existing fail-closed quality
predicate still decides what is usable.

The ONNX models are downloaded by ``rtmlib`` on first use into its cache.
RTMPose/RTMW export to ONNX natively and have CoreML ports, which keeps a
mobile path open.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

from .landmarks import LANDMARK_NAMES, Landmark, PoseObservation

# COCO-WholeBody index for each MediaPipe landmark name (body 0-16, feet
# 17-22, face 23-90, left hand 91-111, right hand 112-132).
_LEFT_HAND = 91
_RIGHT_HAND = 112
WHOLEBODY_TO_MEDIAPIPE: dict[str, int] = {
    "nose": 0,
    "left_eye_inner": 1,
    "left_eye": 1,
    "left_eye_outer": 1,
    "right_eye_inner": 2,
    "right_eye": 2,
    "right_eye_outer": 2,
    "left_ear": 3,
    "right_ear": 4,
    "mouth_left": 23 + 54,  # face landmark 54: mouth corner on the subject's left
    "mouth_right": 23 + 48,
    "left_shoulder": 5,
    "right_shoulder": 6,
    "left_elbow": 7,
    "right_elbow": 8,
    "left_wrist": 9,
    "right_wrist": 10,
    # MediaPipe's pinky/index/thumb are knuckles: use the hand model's MCPs.
    "left_pinky": _LEFT_HAND + 17,
    "right_pinky": _RIGHT_HAND + 17,
    "left_index": _LEFT_HAND + 5,
    "right_index": _RIGHT_HAND + 5,
    "left_thumb": _LEFT_HAND + 2,
    "right_thumb": _RIGHT_HAND + 2,
    "left_hip": 11,
    "right_hip": 12,
    "left_knee": 13,
    "right_knee": 14,
    "left_ankle": 15,
    "right_ankle": 16,
    "left_heel": 19,
    "right_heel": 22,
    "left_foot_index": 17,  # big toe
    "right_foot_index": 20,
}
BODY17_NAMES = {name: index for name, index in WHOLEBODY_TO_MEDIAPIPE.items() if index < 17}


def rtm_available() -> bool:
    """True when rtmlib and ONNX Runtime can be imported."""

    import importlib.util

    return all(importlib.util.find_spec(name) is not None for name in ("rtmlib", "onnxruntime"))


def local_model_paths(model: str = "wholebody", mode: str = "balanced") -> tuple[str, ...]:
    """Local ONNX files this configuration loads, resolved without downloading.

    rtmlib names a checkpoint after its URL basename (and after the zip's
    contents, ``<name>.onnx``, once a distributed zip has been extracted). We
    only look in that cache directory; a model that is not present is reported
    as ``missing:<name>`` rather than fetched, so the pose cache identity can be
    computed before anything is loaded.
    """

    from rtmlib import Body, Wholebody
    from rtmlib.tools.file import _get_rtmhub_dir

    directory = Path(_get_rtmhub_dir()) / "checkpoints"
    solution = Wholebody if model == "wholebody" else Body
    settings = solution.MODE[mode]
    found: list[str] = []
    for url in (settings["det"], settings["pose"]):
        name = Path(urlparse(url).path).name
        candidates = (directory / name, directory / (name.split(".")[0] + ".onnx"))
        located = next((candidate for candidate in candidates if candidate.is_file()), None)
        found.append(str(located) if located is not None else f"missing:{name}")
    return tuple(found)


def to_observation(
    keypoints: np.ndarray,
    scores: np.ndarray,
    *,
    width: int,
    height: int,
) -> PoseObservation:
    """Map one person's keypoints (pixels) and scores onto a PoseObservation.

    Works for 133-point whole-body and 17-point body output; names without a
    source keypoint are simply absent, which the engine already treats as
    missing landmarks.
    """

    count = int(np.shape(keypoints)[0])
    mapping = WHOLEBODY_TO_MEDIAPIPE if count >= 133 else BODY17_NAMES
    landmarks = []
    for index, name in enumerate(LANDMARK_NAMES):
        source = mapping.get(name)
        if source is None or source >= count:
            continue
        x, y = (float(v) for v in keypoints[source][:2])
        score = float(np.clip(scores[source], 0.0, 1.0))
        landmarks.append(
            Landmark(
                index=index,
                name=name,
                x=x / width,
                y=y / height,
                z=0.0,
                visibility=score,
                presence=score,
            )
        )
    return PoseObservation(detected=True, landmarks=tuple(landmarks))


def select_golfer(keypoints: np.ndarray, scores: np.ndarray) -> int | None:
    """The person to analyse: the largest confidently tracked body in frame."""

    if keypoints is None or len(keypoints) == 0:
        return None
    best, best_value = None, -1.0
    for person in range(len(keypoints)):
        body = np.asarray(keypoints[person][:17], float)
        body_scores = np.asarray(scores[person][:17], float)
        confident = body[body_scores >= 0.3]
        if len(confident) < 5:
            continue
        extent = float(np.ptp(confident[:, 1]) * max(1.0, np.ptp(confident[:, 0])))
        value = extent * float(np.mean(body_scores))
        if value > best_value:
            best, best_value = person, value
    return best


@contextlib.contextmanager
def _ort_threads(value: str | None):
    """Pin ONNX Runtime's intra-op thread count for sessions rtmlib creates inside this block.

    Unset (local default): ORT picks the physical core count it sees. In a
    container that is the *host's* count, not the CPU quota, which
    oversubscribes the quota. The thread count also changes float reduction
    order, so a fixed value keeps production results reproducible. rtmlib
    exposes no session options, hence the scoped wrapper.
    """

    threads = int(value) if value and value.strip().isdigit() and int(value) > 0 else 0
    if not threads:
        yield
        return
    import onnxruntime as ort

    original = ort.InferenceSession

    def session(*args, **kwargs):
        options = kwargs.get("sess_options") or ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        kwargs["sess_options"] = options
        return original(*args, **kwargs)

    ort.InferenceSession = session
    try:
        yield
    finally:
        ort.InferenceSession = original


@dataclass
class RTMPoseAdapter:
    """Pose provider backed by rtmlib's Wholebody (RTMW) or Body (RTMPose).

    ``mode`` is rtmlib's ``lightweight`` / ``balanced`` / ``performance``.
    ``det_frequency`` re-runs the person detector every N frames and tracks the
    box in between, which is most of the speed-up on a single-golfer clip.

    ``det_frequency=5`` is the retained default. Relaxing it was measured on
    the real down-the-line swing and rejected: 6 moved the Address moment by
    23 frames and shifted the body measurements by several degrees, 7 moved it
    by 3 frames, and 10 moved it by one frame with a 0.6-1.3 degree body shift
    and one lost lead-wrist frame. The tracked box drifts between re-anchors
    and the set-up phase detection depends on that hand path, so the cadence is
    load-bearing. See ``docs/agent-work/pose-performance/REPORT.md``.
    """

    model: str = "wholebody"
    mode: str = "balanced"
    det_frequency: int = 5
    name: str = field(init=False)
    _tracker: object = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.model not in ("wholebody", "body"):
            raise ValueError("model must be 'wholebody' or 'body'")
        self.name = f"rtm-{self.model}-{self.mode}"

    def _ensure(self):
        if self._tracker is None:
            try:
                from rtmlib import Body, PoseTracker, Wholebody
            except ImportError as error:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "rtmlib and onnxruntime are needed for the RTMPose provider: "
                    "pip install onnxruntime && pip install --no-deps rtmlib"
                ) from error
            solution = Wholebody if self.model == "wholebody" else Body
            with _ort_threads(os.environ.get("SG_ORT_THREADS")):
                self._tracker = PoseTracker(
                    solution,
                    det_frequency=max(1, int(self.det_frequency)),
                    tracking=False,
                    mode=self.mode,
                    backend="onnxruntime",
                    device="cpu",
                    to_openpose=False,
                )
        return self._tracker

    def detect(self, frame_bgr: np.ndarray, timestamp_ms: float) -> PoseObservation:
        if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
            return PoseObservation.missing("empty frame")
        keypoints, scores = self._ensure()(frame_bgr)
        person = select_golfer(keypoints, scores)
        if person is None:
            return PoseObservation.missing("no person detected")
        height, width = frame_bgr.shape[:2]
        return to_observation(keypoints[person], scores[person], width=width, height=height)

    def close(self) -> None:
        self._tracker = None


__all__ = [
    "BODY17_NAMES",
    "RTMPoseAdapter",
    "WHOLEBODY_TO_MEDIAPIPE",
    "local_model_paths",
    "rtm_available",
    "select_golfer",
    "to_observation",
]
