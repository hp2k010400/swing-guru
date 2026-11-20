"""Video decoding, frame streaming and annotated-video writing.

Frames are streamed from OpenCV one at a time. Nothing here keeps decoded
images in memory, so a long clip costs the same as a short one apart from the
per-frame data records the pipeline chooses to retain.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import cv2
import numpy as np

DEFAULT_FPS_FALLBACK = 30.0

# Practical guard rails. A 9000-frame clip is five minutes at 30 fps, far
# longer than any single golf swing, and it bounds the retained landmark record
# set to roughly a few tens of megabytes.
DEFAULT_MAX_FRAMES = 9000
LARGE_FRAME_PIXELS = 3840 * 2160


class VideoError(RuntimeError):
    """Base class for video failures."""


class VideoOpenError(VideoError):
    """Raised when a file cannot be opened or decoded as video."""


class VideoWriteError(VideoError):
    """Raised when an output video cannot be written."""


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    width: int
    height: int
    fps: float
    frame_count: int | None
    fps_estimated: bool = False

    @property
    def duration_s(self) -> float | None:
        if self.frame_count is None or self.fps <= 0:
            return None
        return self.frame_count / self.fps

    @property
    def orientation(self) -> str:
        if self.width > self.height:
            return "landscape"
        if self.height > self.width:
            return "portrait"
        return "square"

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height else float("nan")

    @property
    def pixels(self) -> int:
        return self.width * self.height

    def timestamp_ms(self, index: int) -> float:
        """Deterministic timestamp for a frame index, derived from the fps."""

        return index * 1000.0 / self.fps

    def to_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "fps_estimated": self.fps_estimated,
            "frame_count": self.frame_count,
            "duration_s": self.duration_s,
            "orientation": self.orientation,
            "aspect_ratio": self.aspect_ratio,
        }


@dataclass(frozen=True)
class DecodedFrame:
    index: int
    timestamp_ms: float
    image: np.ndarray


def _capture(path: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise VideoOpenError(f"could not open video for reading: {path}")
    return capture


def probe_video(
    path: Path | str, *, fallback_fps: float = DEFAULT_FPS_FALLBACK
) -> VideoInfo:
    """Open a video just long enough to report its geometry and timing."""

    target = Path(path)
    if not target.exists():
        raise VideoOpenError(f"video file does not exist: {target}")
    if target.is_dir():
        raise VideoOpenError(f"expected a video file but found a directory: {target}")

    capture = _capture(target)
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        raw_fps = float(capture.get(cv2.CAP_PROP_FPS))
        raw_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        decoded = capture.read()
        ok, _frame = decoded if isinstance(decoded, tuple) else (decoded, None)
    finally:
        capture.release()

    if width <= 0 or height <= 0 or not ok:
        raise VideoOpenError(f"no decodable video stream found in: {target}")

    fps_estimated = False
    fps = raw_fps
    if not math.isfinite(fps) or fps <= 0:
        fps = float(fallback_fps)
        fps_estimated = True

    frame_count: int | None
    if math.isfinite(raw_count) and raw_count > 0:
        frame_count = int(round(raw_count))
    else:
        frame_count = None

    return VideoInfo(
        path=target,
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        fps_estimated=fps_estimated,
    )


class VideoStream:
    """Sequential and random-access frame reader over one ``VideoCapture``."""

    def __init__(self, info: VideoInfo, capture: cv2.VideoCapture) -> None:
        self.info = info
        self._capture = capture
        self._closed = False

    @classmethod
    def open(
        cls, path: Path | str, *, fallback_fps: float = DEFAULT_FPS_FALLBACK
    ) -> "VideoStream":
        """Open a stream directly, for callers that manage the lifetime."""

        target = Path(path)
        if not target.exists():
            raise VideoOpenError(f"video file does not exist: {target}")
        info = probe_video(target, fallback_fps=fallback_fps)
        return cls(info, _capture(target))

    def frames(
        self,
        *,
        start: int = 0,
        max_frames: int | None = None,
    ) -> Iterator[DecodedFrame]:
        """Yield frames from ``start``, at most ``max_frames`` of them."""

        if start < 0:
            raise ValueError("start must not be negative")
        if max_frames is not None and max_frames <= 0:
            return
        if start:
            if not self._capture.set(cv2.CAP_PROP_POS_FRAMES, float(start)):
                raise VideoOpenError(
                    f"could not seek to frame {start} in {self.info.path}"
                )

        index = start
        emitted = 0
        while True:
            if max_frames is not None and emitted >= max_frames:
                break
            ok, frame = self._capture.read()
            if not ok or frame is None:
                break
            yield DecodedFrame(
                index=index,
                timestamp_ms=self.info.timestamp_ms(index),
                image=frame,
            )
            index += 1
            emitted += 1

    def read_at(self, index: int) -> np.ndarray | None:
        """Read a single frame by index, or ``None`` when unavailable.

        Container seeking is unreliable for some codecs, so a failed seek
        falls back to sequential grab from the start of the file.
        """

        if index < 0:
            raise ValueError("frame index must not be negative")
        if self._capture.set(cv2.CAP_PROP_POS_FRAMES, float(index)):
            ok, frame = self._capture.read()
            if ok and frame is not None:
                return frame
        if not self._capture.set(cv2.CAP_PROP_POS_FRAMES, 0.0):
            return None
        for _ in range(index):
            if not self._capture.grab():
                return None
        ok, frame = self._capture.read()
        if ok and frame is not None:
            return frame
        return None

    def close(self) -> None:
        if not self._closed:
            self._capture.release()
            self._closed = True

    def __enter__(self) -> "VideoStream":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@contextmanager
def open_video(
    path: Path | str, *, fallback_fps: float = DEFAULT_FPS_FALLBACK
) -> Iterator[VideoStream]:
    """Context manager yielding a :class:`VideoStream` that is always released."""

    target = Path(path)
    if not target.exists():
        raise VideoOpenError(f"video file does not exist: {target}")
    info = probe_video(target, fallback_fps=fallback_fps)
    capture = _capture(target)
    stream = VideoStream(info, capture)
    try:
        yield stream
    finally:
        stream.close()


class AnnotatedVideoWriter:
    """Thin wrapper around ``cv2.VideoWriter`` with explicit failure handling."""

    def __init__(
        self,
        path: Path | str,
        *,
        fps: float,
        size: tuple[int, int],
        fourcc: str = "mp4v",
    ) -> None:
        self.path = Path(path)
        self.size = (int(size[0]), int(size[1]))
        self.fps = float(fps) if fps and fps > 0 else DEFAULT_FPS_FALLBACK
        self.fourcc = fourcc
        self.frames_written = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        code = cv2.VideoWriter_fourcc(*fourcc)
        self._writer = cv2.VideoWriter(str(self.path), code, self.fps, self.size)
        if not self._writer.isOpened():
            raise VideoWriteError(
                f"could not open video writer for {self.path} "
                f"(size={self.size}, fps={self.fps}, fourcc={fourcc})"
            )

    def write(self, frame: np.ndarray) -> None:
        if frame.shape[1] != self.size[0] or frame.shape[0] != self.size[1]:
            raise VideoWriteError(
                "frame size "
                f"{frame.shape[1]}x{frame.shape[0]} does not match writer size "
                f"{self.size[0]}x{self.size[1]}"
            )
        self._writer.write(frame)
        self.frames_written += 1

    def close(self) -> Path:
        self._writer.release()
        if self.frames_written == 0:
            raise VideoWriteError(f"no frames were written to {self.path}")
        if not self.path.exists() or self.path.stat().st_size == 0:
            raise VideoWriteError(f"no video data was written to {self.path}")
        return self.path

    def __enter__(self) -> "AnnotatedVideoWriter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def ffmpeg_path() -> str | None:
    """Absolute path to ``ffmpeg`` when it is available on PATH."""

    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


@dataclass(frozen=True)
class TranscodeResult:
    ok: bool
    output_path: Path | None
    codec: str | None
    message: str


def transcode_to_h264(
    source: Path | str,
    destination: Path | str,
    *,
    crf: int = 20,
    preset: str = "medium",
) -> TranscodeResult:
    """Re-encode ``source`` as H.264/yuv420p for browser playback.

    Returns an explicit result instead of raising so callers can fall back to
    the raw OpenCV output and say so in the run metadata.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.exists():
        return TranscodeResult(
            ok=False,
            output_path=None,
            codec=None,
            message=f"source video missing: {source_path}",
        )

    binary = ffmpeg_path()
    if binary is None:
        return TranscodeResult(
            ok=False,
            output_path=None,
            codec=None,
            message="ffmpeg not found on PATH; keeping the raw MPEG-4 output",
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        binary,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(destination_path),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return TranscodeResult(
            ok=False,
            output_path=None,
            codec=None,
            message=f"ffmpeg could not be executed: {error}",
        )

    if completed.returncode != 0 or not destination_path.exists():
        tail = (completed.stderr or "").strip().splitlines()[-5:]
        return TranscodeResult(
            ok=False,
            output_path=None,
            codec=None,
            message="ffmpeg transcode failed: " + " | ".join(tail),
        )

    codec = probe_codec(destination_path)
    return TranscodeResult(
        ok=True,
        output_path=destination_path,
        codec=codec,
        message=f"transcoded to H.264 ({codec or 'unknown codec'})",
    )


def probe_codec(path: Path | str, *, timeout: int = 60) -> str | None:
    """Video codec name from ffprobe, or ``None`` when it is unavailable."""

    binary = ffprobe_path()
    target = Path(path)
    if binary is None or not target.exists():
        return None
    command = [
        binary,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams: Sequence[dict[str, object]] = payload.get("streams") or []
    if not streams:
        return None
    codec = streams[0].get("codec_name")
    return str(codec) if codec else None


def is_readable_video(path: Path | str) -> bool:
    """True when OpenCV can decode at least one frame from ``path``."""

    try:
        probe_video(path)
    except VideoError:
        return False
    return True
