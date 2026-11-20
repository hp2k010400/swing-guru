"""Video probing, streaming, seeking and writing."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from swingguru.video import (
    AnnotatedVideoWriter,
    VideoOpenError,
    VideoWriteError,
    ffmpeg_path,
    is_readable_video,
    open_video,
    probe_video,
    transcode_to_h264,
)


def write_clip(
    path: Path,
    *,
    width: int = 160,
    height: int = 90,
    fps: float = 24.0,
    frames: int = 12,
    fourcc: str = "mp4v",
) -> Path:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*fourcc), fps, (width, height)
    )
    assert writer.isOpened(), f"could not open writer for {path}"
    for index in range(frames):
        frame = np.full((height, width, 3), 30, dtype=np.uint8)
        cv2.rectangle(
            frame,
            (5 + index * 3, 20),
            (20 + index * 3, 60),
            (0, 200, 255),
            -1,
        )
        writer.write(frame)
    writer.release()
    return path


@pytest.mark.parametrize(
    ("width", "height", "orientation"),
    [(320, 180, "landscape"), (180, 320, "portrait"), (200, 200, "square")],
)
def test_probe_reports_geometry_and_orientation(
    tmp_path: Path, width: int, height: int, orientation: str
) -> None:
    path = write_clip(tmp_path / "clip.mp4", width=width, height=height, frames=8)
    info = probe_video(path)
    assert (info.width, info.height) == (width, height)
    assert info.orientation == orientation
    assert info.frame_count == 8
    assert info.fps == pytest.approx(24.0, rel=1e-3)
    assert not info.fps_estimated
    assert info.duration_s == pytest.approx(8 / 24.0, rel=1e-3)


@pytest.mark.parametrize("fps", [15.0, 24.0, 30.0, 60.0])
def test_probe_reports_frame_rate(tmp_path: Path, fps: float) -> None:
    path = write_clip(tmp_path / f"clip_{fps}.mp4", fps=fps, frames=6)
    info = probe_video(path)
    assert info.fps == pytest.approx(fps, rel=1e-2)
    assert info.timestamp_ms(5) == pytest.approx(5000.0 / info.fps, rel=1e-6)


def test_stream_yields_every_frame_with_monotonic_timestamps(tmp_path: Path) -> None:
    path = write_clip(tmp_path / "clip.mp4", frames=10)
    with open_video(path) as stream:
        frames = list(stream.frames())
    assert [frame.index for frame in frames] == list(range(10))
    timestamps = [frame.timestamp_ms for frame in frames]
    assert timestamps == sorted(timestamps)
    assert all(frame.image.shape == (90, 160, 3) for frame in frames)


def test_stream_supports_start_and_limit(tmp_path: Path) -> None:
    path = write_clip(tmp_path / "clip.mp4", frames=10)
    with open_video(path) as stream:
        frames = list(stream.frames(start=4, max_frames=3))
    assert [frame.index for frame in frames] == [4, 5, 6]


def test_stream_random_access_reads_the_requested_frame(tmp_path: Path) -> None:
    path = write_clip(tmp_path / "clip.mp4", frames=12)
    with open_video(path) as stream:
        first = stream.read_at(0)
        later = stream.read_at(7)
        again = stream.read_at(7)
    assert first is not None and later is not None and again is not None
    assert np.array_equal(later, again)
    assert not np.array_equal(first, later)


def test_open_video_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VideoOpenError):
        probe_video(tmp_path / "missing.mp4")


def test_open_video_rejects_a_directory(tmp_path: Path) -> None:
    with pytest.raises(VideoOpenError):
        probe_video(tmp_path)


def test_open_video_rejects_empty_and_corrupt_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(VideoOpenError):
        probe_video(empty)

    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"this is not a video stream" * 40)
    with pytest.raises(VideoOpenError):
        probe_video(corrupt)
    assert not is_readable_video(corrupt)


def test_writer_produces_a_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "out.mp4"
    with AnnotatedVideoWriter(path, fps=20.0, size=(64, 48)) as writer:
        for index in range(6):
            writer.write(np.full((48, 64, 3), 20 * index, dtype=np.uint8))
    assert writer.frames_written == 6
    info = probe_video(path)
    assert info.frame_count == 6
    assert (info.width, info.height) == (64, 48)


def test_writer_rejects_mismatched_frame_size(tmp_path: Path) -> None:
    path = tmp_path / "out.mp4"
    writer = AnnotatedVideoWriter(path, fps=20.0, size=(64, 48))
    with pytest.raises(VideoWriteError):
        writer.write(np.zeros((50, 70, 3), dtype=np.uint8))
    # Nothing was accepted, so closing reports the empty output as well.
    with pytest.raises(VideoWriteError):
        writer.close()


def test_writer_reports_an_empty_output(tmp_path: Path) -> None:
    path = tmp_path / "unused.mp4"
    writer = AnnotatedVideoWriter(path, fps=20.0, size=(64, 48))
    with pytest.raises(VideoWriteError):
        writer.close()


@pytest.mark.skipif(ffmpeg_path() is None, reason="ffmpeg is not on PATH")
def test_transcode_produces_h264(tmp_path: Path) -> None:
    source = write_clip(tmp_path / "source.mp4", frames=8)
    destination = tmp_path / "encoded.mp4"
    result = transcode_to_h264(source, destination)
    assert result.ok, result.message
    assert destination.exists()
    assert result.codec == "h264"
    assert is_readable_video(destination)


def test_transcode_reports_a_missing_source(tmp_path: Path) -> None:
    result = transcode_to_h264(tmp_path / "nope.mp4", tmp_path / "out.mp4")
    assert not result.ok
    assert "missing" in result.message
