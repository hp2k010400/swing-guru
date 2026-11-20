"""Swing Guru Engine v0.1.

A local experimental golf swing pose and geometry engine. It decodes video,
runs MediaPipe pose landmarking, derives deterministic geometry measurements
from the landmarks, and writes annotated video plus raw data exports.

See ``docs/measurements.md`` for the measurement definitions and
``docs/agent-work/engine-v01/REPORT.md`` for the v0.1 evidence trail.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
