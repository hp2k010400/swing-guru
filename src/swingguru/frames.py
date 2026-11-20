"""Selective, single-pass video decoding for the analysis build.

The full analysis needs two disjoint sets of frames: the frames around the six
moments (expensive per-frame club detection) and the evenly spaced background
reference frames (cheap Hessian maps only). Decoding every frame of the clip
and keeping it costs memory and time for frames nobody reads.

:func:`decode_selected_frames` pulls the decoder forward once and only
``retrieve``s the frames that were asked for; everything else is ``grab``bed,
which still advances the decoder without paying for the colour conversion.
The counters it returns separate reference-only frames from detection frames
so the run report can show what was actually decoded and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

from .video import _capture


@dataclass
class SelectedFrames:
    """Frames retained from one decode pass, plus what that pass cost."""

    frames: dict[int, np.ndarray] = field(default_factory=dict)
    scanned: int = 0
    kept: int = 0
    skipped: int = 0
    missing: tuple[int, ...] = ()

    def get(self, index: int) -> np.ndarray | None:
        return self.frames.get(int(index))

    def has(self, index: int) -> bool:
        return int(index) in self.frames

    def __len__(self) -> int:
        return len(self.frames)

    def counters(self) -> dict[str, int]:
        return {
            "frames_scanned": self.scanned,
            "frames_kept": self.kept,
            "frames_skipped": self.skipped,
            "frames_missing": len(self.missing),
        }


def decode_selected_frames(
    video: Path | str,
    wanted: Iterable[int],
    *,
    max_index: int | None = None,
) -> SelectedFrames:
    """Decode ``wanted`` frame indices from ``video`` in one forward pass.

    The decoder stops after the highest requested index, so unused footage at
    the end of a clip is never read. Indices that the container cannot deliver
    are reported in ``missing`` instead of being silently dropped.
    """

    requested = {int(index) for index in wanted if int(index) >= 0}
    if max_index is not None:
        requested = {index for index in requested if index <= int(max_index)}
    if not requested:
        return SelectedFrames()

    last = max(requested)
    result = SelectedFrames()
    available: list[int] = []
    capture = _capture(Path(video))
    try:
        index = 0
        while index <= last:
            if index in requested:
                ok, frame = capture.read()
                if not ok or frame is None:
                    break
                result.frames[index] = frame
                result.kept += 1
                available.append(index)
            else:
                if not capture.grab():
                    break
                result.skipped += 1
            result.scanned += 1
            index += 1
    finally:
        capture.release()

    delivered = set(available)
    result.missing = tuple(sorted(requested - delivered))
    return result


def wanted_frames(
    detection_frames: Iterable[int],
    reference_frames: Iterable[int],
) -> Mapping[int, tuple[str, ...]]:
    """Map each needed frame index to the roles that need it.

    A frame can be both a detection frame and a reference frame; it is decoded
    once and counted once, with both roles recorded.
    """

    roles: dict[int, list[str]] = {}
    for role, indices in (("detection", detection_frames), ("reference", reference_frames)):
        for index in indices:
            key = int(index)
            bucket = roles.setdefault(key, [])
            if role not in bucket:
                bucket.append(role)
    return {index: tuple(items) for index, items in roles.items()}


__all__ = ["SelectedFrames", "decode_selected_frames", "wanted_frames"]
