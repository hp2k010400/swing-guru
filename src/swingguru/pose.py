"""MediaPipe Tasks pose adapter.

Only the public ``mediapipe.tasks.python`` surface is used. MediaPipe 1.0
deletes its internal submodules from the package namespace during import, so
dotted imports such as ``mediapipe.tasks.python.vision.pose_landmarker`` fail
after the package loads; the ``from ... import Name`` form used here is stable.
"""

from __future__ import annotations

import platform
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np

from .landmarks import Landmark, PoseObservation, WorldLandmark
from .runtime_env import ensure_runtime_env, repo_root

DEFAULT_MODEL_FILENAME = "pose_landmarker_full.task"
DEFAULT_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
)
DEFAULT_MODEL_DIR = repo_root() / "models"


class ModelNotFoundError(FileNotFoundError):
    """Raised when the pose model asset is missing."""


class PoseAdapterError(RuntimeError):
    """Raised when the pose runtime cannot be created or used."""


def default_model_path() -> Path:
    return DEFAULT_MODEL_DIR / DEFAULT_MODEL_FILENAME


def resolve_model_path(explicit: Path | str | None = None) -> Path:
    """Return an existing model path or explain exactly how to obtain one."""

    candidate = Path(explicit) if explicit is not None else default_model_path()
    if candidate.exists() and candidate.stat().st_size > 0:
        return candidate
    raise ModelNotFoundError(
        f"pose model not found at {candidate}. Download it with "
        f"'python -m swingguru.cli download-model' or fetch {DEFAULT_MODEL_URL} "
        f"and save it to {default_model_path()}."
    )


def download_model(
    destination: Path | str | None = None,
    *,
    url: str = DEFAULT_MODEL_URL,
    timeout: float = 120.0,
) -> Path:
    """Download the official float16 pose landmarker bundle.

    Requires network access. The file is written to a temporary name first so a
    partial download never looks like a valid model.
    """

    target = Path(destination) if destination is not None else default_model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = response.read()
    except OSError as error:
        raise PoseAdapterError(f"could not download {url}: {error}") from error
    if not payload:
        raise PoseAdapterError(f"downloaded an empty model file from {url}")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


@runtime_checkable
class PoseProvider(Protocol):
    """Anything that can turn a BGR frame into a pose observation."""

    name: str

    def detect(self, frame_bgr: np.ndarray, timestamp_ms: float) -> PoseObservation:
        ...

    def close(self) -> None:
        ...


@dataclass
class MediaPipePoseAdapter:
    """Synchronous VIDEO-mode pose landmarker.

    ``detect`` enforces strictly increasing timestamps, which the MediaPipe
    video API requires; when a caller supplies a non-monotonic timestamp the
    adapter advances by one millisecond instead of failing the frame.
    """

    model_path: Path | str
    num_poses: int = 1
    min_detection_confidence: float = 0.5
    min_presence_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    delegate: str = "cpu"
    name: str = "mediapipe-pose-landmarker"

    _landmarker: object = field(default=None, init=False, repr=False)
    _mp: object = field(default=None, init=False, repr=False)
    _last_timestamp_ms: float = field(default=-1.0, init=False, repr=False)
    _frames_seen: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.model_path = resolve_model_path(self.model_path)
        ensure_runtime_env()
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions
            from mediapipe.tasks.python.vision import (
                PoseLandmarker,
                PoseLandmarkerOptions,
                RunningMode,
            )
        except Exception as error:  # pragma: no cover - environment specific
            raise PoseAdapterError(
                f"could not import the MediaPipe Tasks pose API: {error}"
            ) from error

        self._mp = mp
        delegate = self._resolve_delegate(BaseOptions)
        options = PoseLandmarkerOptions(
            base_options=BaseOptions(
                model_asset_path=str(self.model_path),
                delegate=delegate,
            ),
            running_mode=RunningMode.VIDEO,
            num_poses=self.num_poses,
            min_pose_detection_confidence=self.min_detection_confidence,
            min_pose_presence_confidence=self.min_presence_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
            output_segmentation_masks=False,
        )
        try:
            self._landmarker = PoseLandmarker.create_from_options(options)
        except Exception as error:  # pragma: no cover - environment specific
            raise PoseAdapterError(
                f"could not create the pose landmarker from {self.model_path}: {error}"
            ) from error

    def _resolve_delegate(self, base_options: object) -> object:
        """MediaPipe 1.0 wants the nested ``Delegate`` enum, not a string."""

        raw = self.delegate
        enum_type = getattr(base_options, "Delegate", None)
        if enum_type is None:  # pragma: no cover - older MediaPipe layouts
            return raw
        if isinstance(raw, enum_type):
            return raw
        try:
            return enum_type[str(raw).upper()]
        except KeyError as error:
            raise PoseAdapterError(
                f"unknown delegate {raw!r}; expected one of "
                f"{[member.name for member in enum_type]}"
            ) from error

    # -- detection ---------------------------------------------------------

    def _next_timestamp(self, timestamp_ms: float) -> int:
        candidate = int(round(float(timestamp_ms)))
        if candidate <= self._last_timestamp_ms:
            candidate = int(self._last_timestamp_ms) + 1
        self._last_timestamp_ms = float(candidate)
        return candidate

    def detect(self, frame_bgr: np.ndarray, timestamp_ms: float) -> PoseObservation:
        if frame_bgr is None or frame_bgr.size == 0:
            raise PoseAdapterError("cannot detect pose on an empty frame")
        if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
            raise PoseAdapterError(
                f"expected a 3-channel BGR frame, got shape {frame_bgr.shape}"
            )

        mp = self._mp
        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp = self._next_timestamp(timestamp_ms)
        self._frames_seen += 1
        result = self._landmarker.detect_for_video(image, timestamp)
        return observation_from_result(result)

    @property
    def frames_seen(self) -> int:
        return self._frames_seen

    def close(self) -> None:
        landmarker = self._landmarker
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass
            self._landmarker = None

    def __enter__(self) -> "MediaPipePoseAdapter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def observation_from_result(result: object) -> PoseObservation:
    """Convert a ``PoseLandmarkerResult`` into a :class:`PoseObservation`."""

    pose_landmarks = getattr(result, "pose_landmarks", None) or []
    if not pose_landmarks:
        return PoseObservation.missing("pose model returned no person for this frame")

    first = pose_landmarks[0]
    landmarks = tuple(
        Landmark(
            index=index,
            name=landmark_name(index),
            x=float(getattr(item, "x", float("nan"))),
            y=float(getattr(item, "y", float("nan"))),
            z=float(getattr(item, "z", 0.0) or 0.0),
            visibility=_optional_float(getattr(item, "visibility", None)),
            presence=_optional_float(getattr(item, "presence", None)),
        )
        for index, item in enumerate(first)
    )

    world_landmarks: tuple[WorldLandmark, ...] = ()
    world = getattr(result, "pose_world_landmarks", None) or []
    if world:
        world_landmarks = tuple(
            WorldLandmark(
                index=index,
                name=landmark_name(index),
                x=float(getattr(item, "x", float("nan"))),
                y=float(getattr(item, "y", float("nan"))),
                z=float(getattr(item, "z", float("nan"))),
                visibility=_optional_float(getattr(item, "visibility", None)),
                presence=_optional_float(getattr(item, "presence", None)),
            )
            for index, item in enumerate(world[0])
        )

    return PoseObservation(
        detected=True,
        landmarks=landmarks,
        world_landmarks=world_landmarks,
    )


def landmark_name(index: int) -> str:
    from .landmarks import LANDMARK_NAMES

    if 0 <= index < len(LANDMARK_NAMES):
        return LANDMARK_NAMES[index]
    return f"landmark_{index}"


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number


def describe_runtime() -> dict[str, str]:
    """Version banner stored in every run's metadata."""

    # Must run before the MediaPipe import below: Matplotlib needs a writable
    # config directory, and on a first launch outside the test suite this is the
    # only thing standing between the sidebar/doctor and a PermissionError.
    ensure_runtime_env()

    versions: dict[str, str] = {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
    }
    try:
        import cv2

        versions["opencv"] = cv2.__version__
    except Exception:  # pragma: no cover - optional
        versions["opencv"] = "unavailable"
    try:
        import numpy

        versions["numpy"] = numpy.__version__
    except Exception:  # pragma: no cover - optional
        versions["numpy"] = "unavailable"
    try:
        import mediapipe

        versions["mediapipe"] = mediapipe.__version__
    except Exception:  # pragma: no cover - optional
        versions["mediapipe"] = "unavailable"
    return versions
