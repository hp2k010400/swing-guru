"""4K clips are shrunk to 1080p before analysis (production 6-14 Nov 2026: 18 of 21 generic failures were 4K)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swingguru import analysis_runner as runner

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def _clip(path: Path, width: int, height: int, frames: int = 30, fps: int = 60) -> Path:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size={width}x{height}:rate={fps}", "-frames:v", str(frames),
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@needs_ffmpeg
def test_portrait_4k_is_shrunk_to_1080p_keeping_every_frame(tmp_path):
    source = _clip(tmp_path / "4k.mp4", 2160, 3840)
    info = runner.probe(source)
    clip, shrunk, did = runner.shrink_if_large(source, tmp_path, info)
    assert did and clip != source
    assert (shrunk["width"], shrunk["height"]) == (1080, 1920)
    assert shrunk["frames"] == info["frames"]            # frame-exact: phase timing is unchanged
    assert shrunk["fps"] == pytest.approx(info["fps"], rel=0.01)


@needs_ffmpeg
def test_landscape_4k_keeps_its_orientation(tmp_path):
    source = _clip(tmp_path / "4k_land.mp4", 3840, 2160, frames=10)
    _, shrunk, did = runner.shrink_if_large(source, tmp_path, runner.probe(source))
    assert did and (shrunk["width"], shrunk["height"]) == (1920, 1080)


@needs_ffmpeg
def test_1080p_and_smaller_are_untouched(tmp_path):
    source = _clip(tmp_path / "hd.mp4", 1080, 1920, frames=5)
    info = runner.probe(source)
    clip, same, did = runner.shrink_if_large(source, tmp_path, info)
    assert (clip, same, did) == (source, info, False)
    assert not (tmp_path / "shrunk.mp4").exists()


def test_run_analysis_hands_the_engine_the_shrunk_clip(tmp_path, monkeypatch):
    """The window finder and engine both see the 1080p copy, never the 4K original."""

    big = {"fps": 60.0, "frames": 300, "duration_s": 5.0, "width": 2160, "height": 3840}
    small = {**big, "width": 1080, "height": 1920}
    shrunk_path = tmp_path / "work" / "shrunk.mp4"
    monkeypatch.setattr(runner, "probe", lambda video: big)
    monkeypatch.setattr(runner, "shrink_if_large", lambda upload, work, info: (shrunk_path, small, True))
    seen = {}

    def fake_locate(video, **_):
        seen["window"] = Path(video)
        raise RuntimeError("stop here")

    import swingguru.swing_window as sw
    monkeypatch.setattr(sw, "locate_swing", fake_locate)
    result = runner.run_analysis(tmp_path / "upload.mov", tmp_path / "work")
    assert seen["window"] == shrunk_path
    assert result.video_info["shrunk_from_long_side"] == 3840
    assert "shrink_s" in result.timings


def test_a_failed_shrink_is_an_honest_failure(tmp_path, monkeypatch):
    big = {"fps": 60.0, "frames": 300, "duration_s": 5.0, "width": 2160, "height": 3840}
    monkeypatch.setattr(runner, "probe", lambda video: big)

    def boom(*_):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(runner, "shrink_if_large", boom)
    result = runner.run_analysis(tmp_path / "upload.mov", tmp_path / "work")
    assert not result.ok and result.error == "failed" and result.detail.startswith("shrink failed")
