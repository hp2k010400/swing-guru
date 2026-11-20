"""Experimental club / shaft detection (Engine v0.3).

Kept separate from the body engine and the UI. :class:`ClubTracker` is the
seam: the first implementation is classical computer vision constrained by
the body pose; a trained model can replace it later behind the same interface.
"""

from .tracker import (
    LEVEL_HIGH,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_UNAVAILABLE,
    ClassicalClubTracker,
    ClubConfig,
    ClubTrack,
    ClubTracker,
    FrameDetection,
    FrameShaft,
    ShaftCandidate,
)

__all__ = [
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_MEDIUM",
    "LEVEL_UNAVAILABLE",
    "ClassicalClubTracker",
    "ClubConfig",
    "ClubTrack",
    "ClubTracker",
    "FrameDetection",
    "FrameShaft",
    "ShaftCandidate",
]
