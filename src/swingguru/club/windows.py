"""Event-centred frame windows for the classical club tracker.

The tracker's Viterbi pass needs neighbouring frames for temporal continuity,
angular consistency and confidence, but it does not need the whole clip: only
the frames around the six analysis moments are ever read back. This module
turns the moments into merged, clipped, disjoint inclusive windows so each
needed frame is processed exactly once.

Rules kept deliberately simple:

* a window is inclusive ``[start, end]`` in *frame indices*, not positions;
* overlapping windows merge, and so do abutting ones - merging those adds no
  frame that is not already required, it only avoids running the tracker
  twice over a boundary;
* windows separated by at least one unprocessed frame stay separate, and the
  tracker runs one selection pass per window, so no frame is ever treated as
  the temporal neighbour of a frame it is not adjacent to in the video.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

CLUB_WINDOW_VERSION = "club-window-v1"


@dataclass(frozen=True)
class FrameWindow:
    """One inclusive, contiguous run of frame indices."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before its start {self.start}")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def indices(self) -> range:
        return range(self.start, self.end + 1)

    def contains(self, frame_index: int) -> bool:
        return self.start <= int(frame_index) <= self.end

    def to_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end, "frames": self.length}


def merge_windows(
    windows: Iterable[FrameWindow],
    *,
    lower: int | None = None,
    upper: int | None = None,
) -> tuple[FrameWindow, ...]:
    """Clip, sort, deduplicate and merge windows into disjoint ranges.

    ``lower``/``upper`` are inclusive bounds; a window that falls entirely
    outside them is dropped rather than clamped to an empty range.
    """

    clipped: list[FrameWindow] = []
    for window in windows:
        start = window.start if lower is None else max(window.start, lower)
        end = window.end if upper is None else min(window.end, upper)
        if end < start:
            continue
        clipped.append(FrameWindow(start, end))
    if not clipped:
        return ()

    clipped.sort(key=lambda item: (item.start, item.end))
    merged: list[FrameWindow] = [clipped[0]]
    for window in clipped[1:]:
        previous = merged[-1]
        if window.start <= previous.end + 1:
            merged[-1] = FrameWindow(previous.start, max(previous.end, window.end))
        else:
            merged.append(window)
    return tuple(merged)


def windows_around(
    centres: Iterable[int | None],
    radius: int,
    *,
    lower: int,
    upper: int,
) -> tuple[FrameWindow, ...]:
    """One ``centre +/- radius`` window per centre, merged and clipped."""

    if radius < 0:
        raise ValueError("radius must not be negative")
    if upper < lower:
        return ()
    windows = [
        FrameWindow(int(centre) - radius, int(centre) + radius)
        for centre in centres
        if centre is not None
    ]
    return merge_windows(windows, lower=lower, upper=upper)


def phase_windows(
    frames: Mapping[object, int | None] | Sequence[int | None],
    radius: int,
    *,
    lower: int,
    upper: int,
    phases: Sequence[object] | None = None,
) -> tuple[FrameWindow, ...]:
    """Event windows for a moment set's ``{phase: frame_index}`` mapping."""

    if isinstance(frames, Mapping):
        centres: Iterable[int | None]
        if phases is None:
            centres = frames.values()
        else:
            centres = [frames.get(phase) for phase in phases]
    else:
        centres = frames
    return windows_around(centres, radius, lower=lower, upper=upper)


def frames_in(windows: Sequence[FrameWindow]) -> tuple[int, ...]:
    """Every frame index covered by ``windows``, ascending and deduplicated."""

    seen: set[int] = set()
    for window in windows:
        seen.update(window.indices())
    return tuple(sorted(seen))


__all__ = [
    "CLUB_WINDOW_VERSION",
    "FrameWindow",
    "frames_in",
    "merge_windows",
    "phase_windows",
    "windows_around",
]
