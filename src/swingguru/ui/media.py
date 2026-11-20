"""Prepare a browser-playable local preview of the analysed frames.

The guided player needs a video whose frame numbering matches the analysed
records one-to-one, otherwise a held overlay would sit on the wrong picture.
This module transcodes a bounded, local, H.264 preview of exactly the frames
the engine processed and then *verifies* that the first preview frame matches
the first analysed frame before the player is allowed to use it.

Rules kept deliberately tight:

* everything stays on this machine; the preview is written inside the
  component's own static directory, so the browser reaches it through the same
  loopback Streamlit server that serves the page - no file server, no user
  directory exposure, no remote upload;
* the preview is downscaled (720p then 480p) and size-capped, and older
  previews are pruned;
* if the transcode fails, is too large, or cannot be aligned, the result says so
  and the caller falls back to stills instead of pretending playback works.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..video import VideoStream

DEFAULT_MAX_HEIGHT = 1080
DEFAULT_CAP_BYTES = 32 * 1024 * 1024
KEEP_PREVIEWS = 3
# Bumped whenever the encode settings change, so stale previews are rebuilt.
ENCODER_VERSION = "2-gop-crf20"
# Seconds between keyframes. A seek decodes from the previous keyframe, so a
# short interval keeps every hold and scrub close to instant.
KEYFRAME_INTERVAL_S = 0.2


@dataclass(frozen=True)
class PreviewResult:
    """Outcome of preparing the guided playback preview."""

    ok: bool
    src: str | None
    path: Path | None
    message: str
    fps: float
    first_index: int
    frame_count: int
    width: int
    height: int
    size_bytes: int = 0
    cache_key: str = ""


def preview_cache_key(
    source: Path | str,
    *,
    first_index: int,
    frame_count: int,
    fps: float,
    settings_key: str = "",
    max_height: int = DEFAULT_MAX_HEIGHT,
) -> str:
    """Identity of a preview: the source bytes plus the exact frame range."""

    path = Path(source)
    try:
        stat = path.stat()
        version = f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        version = "missing"
    payload = "|".join(
        str(value)
        for value in (
            str(path),
            version,
            first_index,
            frame_count,
            round(float(fps), 5),
            settings_key,
            max_height,
            ENCODER_VERSION,
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _preview_name(cache_key: str) -> str:
    return f"preview_{cache_key}.mp4"


def prune_previews(directory: Path, *, keep: int = KEEP_PREVIEWS) -> None:
    """Keep only the newest previews so the local cache stays bounded."""

    if not directory.exists():
        return
    previews = sorted(
        directory.glob("preview_*.mp4"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in previews[keep:]:
        try:
            stale.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass


def _run_ffmpeg(args: list[str], *, timeout_s: float) -> tuple[bool, str]:
    try:
        done = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"{type(error).__name__}: {error}"
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        return False, (detail[-1] if detail else f"ffmpeg exited {done.returncode}")
    return True, ""


def _encode(
    source: Path,
    target: Path,
    *,
    first_index: int,
    frame_count: int,
    fps: float,
    max_height: int,
    crf: int,
    exact: bool,
    timeout_s: float,
) -> tuple[bool, str]:
    start_s = max(0.0, first_index / fps)
    scale = f"scale=-2:'min({max_height},ih)'"
    base = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if exact:
        # Decode from the start and select the analysed frames by number: slow
        # but frame-accurate, and only needed when the fast path misaligns.
        select = (
            f"select=between(n\\,{first_index}\\,{first_index + frame_count - 1})"
        )
        filters = f"{select},{scale},setpts=N/FRAME_RATE/TB"
        args = [
            *base,
            "-i",
            str(source),
            "-vf",
            filters,
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(frame_count),
        ]
    else:
        args = [
            *base,
            "-ss",
            f"{start_s:.6f}",
            "-i",
            str(source),
            "-vf",
            scale,
            "-frames:v",
            str(frame_count),
        ]
    args.extend(
        [
            "-r",
            f"{fps:.6f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-g",
            str(max(1, int(round(fps * KEYFRAME_INTERVAL_S)))),
            "-keyint_min",
            "1",
            "-sc_threshold",
            "0",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    return _run_ffmpeg(args, timeout_s=timeout_s)


def preview_frame_matches(
    source: Path,
    source_index: int,
    preview: Path,
    *,
    tolerance: float = 12.0,
    blocks: int = 6,
) -> tuple[bool, str]:
    """Compare a preview frame with the analysed source frame.

    A preview that is two frames out would put every overlay on the wrong
    picture, so the alignment is verified rather than assumed. The comparison
    is tolerant of encoding blur and scaling but not of a different pose, so it
    compares the worst *block* of the frame rather than the frame average: a
    moving golfer only changes part of the picture, and an average over a
    mostly static background would hide a wrong frame.
    """

    try:
        with VideoStream.open(source) as source_stream:
            source_frame = source_stream.read_at(source_index)
        with VideoStream.open(preview) as preview_stream:
            preview_frame = preview_stream.read_at(0)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return False, f"could not read frames for the alignment check: {error}"
    if source_frame is None or preview_frame is None:
        return False, "a frame was missing during the alignment check"

    height, width = preview_frame.shape[:2]
    resized = cv2.resize(source_frame, (width, height), interpolation=cv2.INTER_AREA)
    difference = np.abs(
        resized.astype(np.float32) - preview_frame.astype(np.float32)
    ).mean(axis=2)
    rows = max(1, min(blocks, difference.shape[0]))
    columns = max(1, min(blocks, difference.shape[1]))
    block_rows = np.array_split(difference, rows, axis=0)
    worst = 0.0
    for row in block_rows:
        for block in np.array_split(row, columns, axis=1):
            if block.size:
                worst = max(worst, float(block.mean()))
    if worst <= tolerance:
        return True, f"preview frame 0 matches source frame {source_index}"
    return False, (
        f"preview frame 0 differs from source frame {source_index} "
        f"(worst block difference {worst:.1f})"
    )


def prepare_preview(
    source: Path | str,
    *,
    output_dir: Path | str,
    first_index: int,
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    cache_key: str | None = None,
    settings_key: str = "",
    max_height: int = DEFAULT_MAX_HEIGHT,
    cap_bytes: int = DEFAULT_CAP_BYTES,
    timeout_s: float = 240.0,
) -> PreviewResult:
    """Transcode and verify a preview of the analysed frames."""

    path = Path(source)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    frame_count = max(1, int(frame_count))
    fps = float(fps) if fps and fps > 0 else 30.0
    key = cache_key or preview_cache_key(
        path,
        first_index=first_index,
        frame_count=frame_count,
        fps=fps,
        settings_key=settings_key,
        max_height=max_height,
    )
    target = directory / _preview_name(key)

    def result(ok: bool, message: str, *, preview: Path | None = None) -> PreviewResult:
        return PreviewResult(
            ok=ok,
            src=None if preview is None else f"generated/{preview.name}",
            path=preview,
            message=message,
            fps=fps,
            first_index=int(first_index),
            frame_count=frame_count,
            width=int(width),
            height=int(height),
            size_bytes=0 if preview is None else preview.stat().st_size,
            cache_key=key,
        )

    if not path.exists():
        return result(False, "the analysed video is no longer on disk")
    if not ffmpeg_available():
        return result(
            False,
            "FFmpeg is not available, so a browser-playable preview could not be "
            "created. Stills are shown instead.",
        )

    if target.exists() and target.stat().st_size > 0:
        matches, _ = preview_frame_matches(path, int(first_index), target)
        if matches:
            prune_previews(directory)
            return result(True, "reused the cached preview", preview=target)

    attempts = (
        {"max_height": max_height, "crf": 20, "exact": False},
        {"max_height": max_height, "crf": 20, "exact": True},
        {"max_height": min(720, max_height), "crf": 26, "exact": True},
        {"max_height": min(480, max_height), "crf": 30, "exact": True},
    )
    messages: list[str] = []

    def preferred_message() -> str:
        for message in messages:
            if "size cap" in message or "aligned" in message:
                return message
        return messages[-1] if messages else "the preview could not be created"

    for attempt in attempts:
        temp = directory / f"tmp_{key}.mp4"
        ok, message = _encode(
            path,
            temp,
            first_index=int(first_index),
            frame_count=frame_count,
            fps=fps,
            max_height=attempt["max_height"],
            crf=attempt["crf"],
            exact=attempt["exact"],
            timeout_s=timeout_s,
        )
        if not ok:
            messages.append(f"FFmpeg could not build the preview: {message}")
            temp.unlink(missing_ok=True)
            continue
        matches, detail = preview_frame_matches(path, int(first_index), temp)
        if not matches:
            messages.append(
                "the preview could not be aligned to the analysed frames; "
                f"stills are shown instead ({detail})"
            )
            temp.unlink(missing_ok=True)
            continue
        size = temp.stat().st_size
        if size > cap_bytes:
            messages.append(
                "the preview would be larger than the local size cap "
                f"({size // (1024 * 1024)} MB); stills are shown instead"
            )
            temp.unlink(missing_ok=True)
            continue
        temp.replace(target)
        prune_previews(directory)
        return result(
            True,
            f"preview ready ({size // 1024} KiB, {frame_count} frames)",
            preview=target,
        )

    return result(False, preferred_message())


def poster_data_url(frame_bgr: np.ndarray | None, *, max_width: int = 720) -> str | None:
    """Small inline poster so the stage is never a black box."""

    if frame_bgr is None or getattr(frame_bgr, "size", 0) == 0:
        return None
    height, width = frame_bgr.shape[:2]
    scale = min(1.0, max_width / float(width)) if width else 1.0
    if scale < 1.0:
        frame_bgr = cv2.resize(
            frame_bgr,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buffer = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    if not ok:
        return None
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(buffer.tobytes()).decode("ascii")


__all__ = [
    "DEFAULT_CAP_BYTES",
    "DEFAULT_MAX_HEIGHT",
    "PreviewResult",
    "ffmpeg_available",
    "poster_data_url",
    "prepare_preview",
    "preview_cache_key",
    "preview_frame_matches",
    "prune_previews",
]
