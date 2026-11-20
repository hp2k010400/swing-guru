"""Checks and geometry for guided playback.

One *moment* is a point in the video (an estimated or hand-picked frame). One
*check* is a single measurement at that moment. Several checks share a moment,
so the player holds the picture still and steps through the measurements at the
same video time.

This module owns three things:

* the mapping from a moment to its ordered checks, reusing the checkpoint
  table so labels and measurement keys cannot drift;
* the normalised geometry primitives for each check, so the browser never
  contains biomechanics: it only draws lines and dots that were validated in
  Python; and
* the payload the player component consumes.

Geometry is normalised to the video frame (0..1) and converted to pixels only
for the local fallback still, so portrait, landscape and letterboxed playback
all scale correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ..phases import Phase
from ..pipeline import apply_reference
from .presentation import (
    CHECKPOINTS,
    CHECKPOINT_BY_PHASE,
    MEASURED_AGAINST,
    Geometry,
    measurement_anchor,
    measurement_geometry,
    measurement_rows,
    tracking_confidence,
    tracking_level,
)

# The walkthrough starts a little before the first moment and ends a little
# after the last, so Play goes straight into the swing instead of the waggle.
LEAD_IN_S = 0.5
LEAD_OUT_S = 0.8


@dataclass(frozen=True)
class Check:
    """One measurement shown while the video is held at a moment."""

    id: str
    phase: Phase
    moment_source: str
    review_required: bool
    frame_index: int
    time_s: float
    measurement_key: str
    label: str
    value_text: str
    unit: str
    valid: bool
    reason: str | None
    technical: tuple[str, ...]
    geometry: tuple[Geometry, ...]
    anchor: tuple[float, float] | None = None
    measured_against: str = ""
    confidence: float | None = None
    tracking: str = "unknown"

    @property
    def callout(self) -> str:
        """Short on-video label: the value where it can be measured."""

        if not self.valid:
            return f"{self.label} · can't measure"
        short = {"°": "°", "% of frame height": "%", "px": " px"}.get(
            self.unit, f" {self.unit}" if self.unit else ""
        )
        return f"{self.label} · {self.value_text}{short}"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "anchor": None
            if self.anchor is None
            else [round(self.anchor[0], 5), round(self.anchor[1], 5)],
            "callout": self.callout,
            "measured_against": self.measured_against,
            "confidence": None if self.confidence is None else round(self.confidence, 3),
            "tracking": self.tracking,
            "phase": self.phase.value,
            "moment_source": self.moment_source,
            "review_required": self.review_required,
            "frame_index": self.frame_index,
            "time_s": round(self.time_s, 4),
            "measurement_key": self.measurement_key,
            "label": self.label,
            "value_text": self.value_text,
            "unit": self.unit,
            "valid": self.valid,
            "reason": self.reason,
            "technical": list(self.technical),
            "geometry": [item.to_dict() for item in self.geometry],
        }


@dataclass(frozen=True)
class MomentEvent:
    """All the checks that share one held video time."""

    phase: Phase
    frame_index: int
    time_s: float
    source: str
    review_required: bool
    label: str
    provenance: str
    checks: tuple[Check, ...]
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "frame_index": self.frame_index,
            "time_s": round(self.time_s, 4),
            "source": self.source,
            "review_required": self.review_required,
            "label": self.label,
            "provenance": self.provenance,
            "evidence": list(self.evidence),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class Timeline:
    """Ordered moments and checks for one analysed run."""

    events: tuple[MomentEvent, ...]
    fps: float
    first_index: int
    last_index: int
    reference_frame: int | None
    reference_source: str
    review_notes: tuple[str, ...] = ()

    @property
    def checks(self) -> tuple[Check, ...]:
        return tuple(check for event in self.events for check in event.checks)

    @property
    def empty(self) -> bool:
        return not self.checks

    def check_ids(self) -> tuple[str, ...]:
        return tuple(check.id for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "fps": self.fps,
            "first_index": self.first_index,
            "last_index": self.last_index,
            "reference_frame": self.reference_frame,
            "reference_source": self.reference_source,
            "review_notes": list(self.review_notes),
            "events": [event.to_dict() for event in self.events],
            "check_ids": list(self.check_ids()),
        }


def _record_at(records: Sequence[object], frame_index: int) -> object | None:
    for record in records:
        if int(getattr(record, "index")) == frame_index:
            return record
    return None


def build_timeline(
    records: Sequence[object],
    moments: object,
    *,
    fps: float,
    measurement_config,
    reference_frame: int | None = None,
) -> Timeline:
    """Turn moments into ordered checks, with geometry, for the player.

    The caller passes the moment set (estimated, manual or mixed). Records are
    re-derived against the comparison frame here, exactly as the engine does
    before export, so the values on screen and the exported CSV agree.
    """

    if not records:
        return Timeline(
            events=(),
            fps=float(fps),
            first_index=0,
            last_index=0,
            reference_frame=None,
            reference_source="unavailable",
        )

    first_index = int(getattr(records[0], "index"))
    last_index = int(getattr(records[-1], "index"))
    fps = float(fps) if fps and fps > 0 else 30.0

    reference_record = None
    if reference_frame is not None:
        reference_record = _record_at(records, int(reference_frame))
        if reference_record is not None:
            apply_reference(records, int(reference_frame), config=measurement_config)
        else:
            reference_frame = None
            apply_reference(records, None, config=measurement_config)
    else:
        apply_reference(records, None, config=measurement_config)

    reference_source = "unavailable"
    if reference_frame is not None and moments is not None:
        address = None
        for estimate in getattr(moments, "estimates", ()):
            if estimate.phase is Phase.ADDRESS:
                address = estimate
                break
        if address is not None and address.frame_index == reference_frame:
            reference_source = address.source
        elif reference_frame in {
            estimate.frame_index for estimate in getattr(moments, "estimates", ())
        }:
            reference_source = "mixed"
        else:
            reference_source = "custom"

    events: list[MomentEvent] = []
    notes: list[str] = []
    for estimate in getattr(moments, "estimates", ()):
        if estimate.frame_index is None:
            continue
        frame_index = int(estimate.frame_index)
        record = _record_at(records, frame_index)
        if record is None:
            notes.append(
                f"{estimate.label} points at frame {frame_index}, which is not in "
                "the analysed range; its checks were skipped"
            )
            continue
        checkpoint = CHECKPOINT_BY_PHASE[estimate.phase]
        rows = measurement_rows(record, checkpoint)
        time_s = max(0.0, (frame_index - first_index) / fps)
        checks: list[Check] = []
        for row in rows:
            geometry = measurement_geometry(
                record, row.key, reference_record=reference_record
            )
            confidence = tracking_confidence(record, row.key) if row.valid else None
            checks.append(
                Check(
                    id=f"{estimate.phase.value}:{row.key}",
                    phase=estimate.phase,
                    moment_source=estimate.source,
                    review_required=bool(estimate.review_required),
                    frame_index=frame_index,
                    time_s=time_s,
                    measurement_key=row.key,
                    label=row.label,
                    value_text=row.value_text,
                    unit=row.unit,
                    valid=row.valid,
                    reason=row.reason,
                    technical=row.technical,
                    geometry=geometry if row.valid else (),
                    anchor=measurement_anchor(record, row.key) if row.valid else None,
                    measured_against=MEASURED_AGAINST.get(row.key, ""),
                    confidence=confidence,
                    tracking=tracking_level(confidence) if row.valid else "unmeasured",
                )
            )
        events.append(
            MomentEvent(
                phase=estimate.phase,
                frame_index=frame_index,
                time_s=time_s,
                source=estimate.source,
                review_required=bool(estimate.review_required),
                label=estimate.label,
                provenance=estimate.provenance_label,
                checks=tuple(checks),
                evidence=tuple(estimate.evidence),
            )
        )

    events.sort(key=lambda event: (event.time_s, _phase_order(event.phase)))
    return Timeline(
        events=tuple(events),
        fps=fps,
        first_index=first_index,
        last_index=last_index,
        reference_frame=reference_frame,
        reference_source=reference_source,
        review_notes=tuple(notes),
    )


def _phase_order(phase: Phase) -> int:
    for index, checkpoint in enumerate(CHECKPOINTS):
        if checkpoint.phase == phase:
            return index
    return len(CHECKPOINTS)


def timeline_payload(
    timeline: Timeline,
    *,
    run_key: str,
    video_src: str,
    hold_seconds: float,
    poster: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """The JSON the player component receives."""

    duration_s = max(0.0, (timeline.last_index - timeline.first_index + 1) / timeline.fps)
    times = [event.time_s for event in timeline.events]
    segment_start = max(0.0, min(times) - LEAD_IN_S) if times else 0.0
    segment_end = min(duration_s, max(times) + LEAD_OUT_S) if times else duration_s
    return {
        "segment_start_s": round(segment_start, 4),
        "segment_end_s": round(segment_end, 4),
        "duration_s": round(duration_s, 4),
        "run_key": run_key,
        "video_src": video_src,
        "poster": poster,
        "fps": timeline.fps,
        "first_index": timeline.first_index,
        "last_index": timeline.last_index,
        "hold_seconds": float(hold_seconds),
        "reference_frame": timeline.reference_frame,
        "reference_source": timeline.reference_source,
        "review_notes": list(timeline.review_notes),
        "labels": dict(labels or {}),
        "events": [event.to_dict() for event in timeline.events],
    }


__all__ = [
    "LEAD_IN_S",
    "LEAD_OUT_S",
    "Check",
    "Geometry",
    "MomentEvent",
    "ROLE_PRIMARY",
    "ROLE_REFERENCE",
    "Timeline",
    "build_timeline",
    "measurement_geometry",
    "timeline_payload",
]
