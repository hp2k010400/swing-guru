"""Tests for the guided playback preview preparation."""

from __future__ import annotations

from pathlib import Path

from swingguru.ui import media
from swingguru.video import VideoStream


def _first_frame(path: Path):
    with VideoStream.open(path) as stream:
        return stream.read_at(0)


def test_preview_is_prepared_and_aligned_to_the_analysed_frames(
    fixture_video: Path, tmp_path: Path
) -> None:
    preview = media.prepare_preview(
        fixture_video,
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=12,
        fps=24.0,
        width=320,
        height=180,
    )
    assert preview.ok, preview.message
    assert preview.path is not None and preview.path.exists()
    assert preview.src is not None and preview.src.startswith("generated/")
    assert preview.size_bytes > 0
    matched, detail = media.preview_frame_matches(
        fixture_video, 0, preview.path
    )
    assert matched, detail


def test_preview_alignment_check_rejects_a_different_frame(
    fixture_video: Path, tmp_path: Path
) -> None:
    preview = media.prepare_preview(
        fixture_video,
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=6,
        fps=24.0,
        width=320,
        height=180,
    )
    assert preview.ok, preview.message
    matched, detail = media.preview_frame_matches(
        fixture_video, 18, preview.path  # type: ignore[arg-type]
    )
    assert not matched, detail


def test_preview_is_reused_from_the_cache(fixture_video: Path, tmp_path: Path) -> None:
    first = media.prepare_preview(
        fixture_video,
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=8,
        fps=24.0,
        width=320,
        height=180,
    )
    second = media.prepare_preview(
        fixture_video,
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=8,
        fps=24.0,
        width=320,
        height=180,
    )
    assert second.ok
    assert second.path == first.path
    assert "reused" in second.message


def test_previews_are_pruned_to_a_bounded_cache(tmp_path: Path) -> None:
    directory = tmp_path / "generated"
    directory.mkdir(parents=True)
    for index in range(5):
        target = directory / f"preview_{index}.mp4"
        target.write_bytes(b"x")
        # Make the order deterministic: higher index is newer.
        import os

        os.utime(target, (1_600_000_000 + index, 1_600_000_000 + index))
    media.prune_previews(directory, keep=2)
    remaining = sorted(path.name for path in directory.glob("preview_*.mp4"))
    assert remaining == ["preview_3.mp4", "preview_4.mp4"]


def test_poster_is_small_and_inline(fixture_video: Path) -> None:
    frame = _first_frame(fixture_video)
    poster = media.poster_data_url(frame)
    assert poster is not None
    assert poster.startswith("data:image/jpeg;base64,")
    assert 1500 < len(poster) < 400_000


def test_missing_source_is_reported_not_faked(tmp_path: Path) -> None:
    preview = media.prepare_preview(
        tmp_path / "nope.mp4",
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=4,
        fps=24.0,
        width=320,
        height=180,
    )
    assert not preview.ok
    assert preview.path is None
    assert "no longer on disk" in preview.message


def test_size_cap_produces_a_readable_failure(fixture_video: Path, tmp_path: Path) -> None:
    preview = media.prepare_preview(
        fixture_video,
        output_dir=tmp_path / "generated",
        first_index=0,
        frame_count=12,
        fps=24.0,
        width=320,
        height=180,
        cap_bytes=512,
    )
    assert not preview.ok
    assert "size cap" in preview.message
    assert preview.path is None


def test_cache_key_changes_with_the_frame_range(fixture_video: Path) -> None:
    first = media.preview_cache_key(
        fixture_video, first_index=0, frame_count=8, fps=24.0
    )
    second = media.preview_cache_key(
        fixture_video, first_index=4, frame_count=8, fps=24.0
    )
    assert first != second
