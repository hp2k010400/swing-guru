"""Swing phase assignment with a clean seam for future automatic detection.

v0.1 ships manual phase assignment only. Automatic detection is represented by
the :class:`PhaseProvider` protocol and by
:class:`UnavailableAutoPhaseProvider`, which fails loudly rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable


class Phase(str, Enum):
    ADDRESS = "address"
    TAKEAWAY = "takeaway"
    MID_BACK = "mid_back"
    TOP = "top"
    # The value stays "downswing" so existing CLI arguments and exports keep
    # working; the golfer-facing name is "Mid-Down".
    DOWNSWING = "downswing"
    IMPACT = "impact"
    FINISH = "finish"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.ADDRESS,
    Phase.TAKEAWAY,
    Phase.MID_BACK,
    Phase.TOP,
    Phase.DOWNSWING,
    Phase.IMPACT,
    Phase.FINISH,
)

PHASE_LABELS: Mapping[Phase, str] = {
    Phase.ADDRESS: "Address",
    Phase.TAKEAWAY: "Takeaway",
    Phase.MID_BACK: "Mid-Back",
    Phase.TOP: "Top",
    Phase.DOWNSWING: "Mid-Down",
    Phase.IMPACT: "Impact",
    Phase.FINISH: "Finish",
}

PHASE_DESCRIPTIONS: Mapping[Phase, str] = {
    Phase.ADDRESS: "Setup position immediately before the swing starts.",
    Phase.TAKEAWAY: "Club and hands first move away from the ball.",
    Phase.MID_BACK: "Part way through the backswing, between takeaway and the top.",
    Phase.TOP: "End of the backswing, before the downswing begins.",
    Phase.DOWNSWING: "Part way down, between the top and the ball.",
    Phase.IMPACT: "Club face meets the ball.",
    Phase.FINISH: "End of the swing, body settled in the follow-through.",
}

_PHASE_ALIASES: Mapping[str, Phase] = {
    "mid-back": Phase.MID_BACK,
    "midback": Phase.MID_BACK,
    "mid_down": Phase.DOWNSWING,
    "mid-down": Phase.DOWNSWING,
    "middown": Phase.DOWNSWING,
}

SOURCE_MANUAL = "manual"
SOURCE_PROVIDER = "automatic"


def coerce_phase(value: str | Phase) -> Phase:
    if isinstance(value, Phase):
        return value
    cleaned = str(value).strip().lower()
    for phase in Phase:
        if phase.value == cleaned:
            return phase
    if cleaned in _PHASE_ALIASES:
        return _PHASE_ALIASES[cleaned]
    raise ValueError(f"unknown phase: {value!r}")


def parse_phase_assignments(items: Iterable[str]) -> dict[Phase, int]:
    """Parse ``["address=12", "top=40"]`` into a phase-to-frame mapping."""

    assignments: dict[Phase, int] = {}
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if "=" not in text:
            raise ValueError(f"expected PHASE=FRAME, got {item!r}")
        name, _, raw_index = text.partition("=")
        phase = coerce_phase(name)
        try:
            frame_index = int(raw_index.strip())
        except ValueError as error:
            raise ValueError(f"frame index must be an integer: {item!r}") from error
        if frame_index < 0:
            raise ValueError(f"frame index must not be negative: {item!r}")
        assignments[phase] = frame_index
    return assignments


@dataclass(frozen=True)
class PhaseSelection:
    """Phase frames plus the reference frame used by relative measurements."""

    frames: Mapping[Phase, int | None] = field(default_factory=dict)
    reference_frame: int | None = None
    source: str = SOURCE_MANUAL
    warnings: tuple[str, ...] = ()

    def phase_frame(self, phase: Phase | str) -> int | None:
        return self.frames.get(coerce_phase(phase))

    def assigned(self) -> dict[Phase, int]:
        return {
            phase: int(index)
            for phase, index in self.frames.items()
            if index is not None
        }

    def ordered_frames(self) -> list[tuple[Phase, int]]:
        return [
            (phase, self.assigned()[phase])
            for phase in PHASE_ORDER
            if phase in self.assigned()
        ]

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "reference_frame": self.reference_frame,
            "frames": {
                phase.value: self.frames.get(phase) for phase in PHASE_ORDER
            },
            "assigned_order": [
                {"phase": phase.value, "frame": index}
                for phase, index in self.ordered_frames()
            ],
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PhaseSelection":
        raw_frames = payload.get("frames") or {}
        frames: dict[Phase, int | None] = {}
        for phase in PHASE_ORDER:
            value = raw_frames.get(phase.value) if isinstance(raw_frames, Mapping) else None
            frames[phase] = None if value is None else int(value)  # type: ignore[arg-type]
        reference = payload.get("reference_frame")
        warnings = payload.get("warnings") or ()
        return cls(
            frames=frames,
            reference_frame=None if reference is None else int(reference),  # type: ignore[arg-type]
            source=str(payload.get("source") or SOURCE_MANUAL),
            warnings=tuple(str(item) for item in warnings),  # type: ignore[union-attr]
        )

    def validate(self, available_frames: Sequence[int] | None = None) -> tuple[str, ...]:
        """Warnings about negative, unavailable, duplicate or out-of-order frames.

        ``available_frames`` are the frame indices the run actually holds, not
        ``range(frame_count)``: a run that starts part-way through a video has
        records at, say, 5..12, and a phase at frame 3 is unavailable even
        though it is inside the source video.
        """

        issues: list[str] = []
        assigned = self.assigned()
        available = (
            None if available_frames is None else {int(index) for index in available_frames}
        )
        span = None if not available else (min(available), max(available))

        def check(label: str, index: int | None) -> None:
            if index is None:
                return
            if index < 0:
                issues.append(f"{label} frame {index} is negative")
                return
            if available is not None and index not in available:
                if span is not None:
                    issues.append(
                        f"{label} frame {index} is outside the processed frames "
                        f"({span[0]}..{span[1]})"
                    )
                else:
                    issues.append(
                        f"{label} frame {index} is outside the processed frames"
                    )

        for phase in PHASE_ORDER:
            check(phase.value, assigned.get(phase))
        check("reference", self.reference_frame)

        seen: dict[int, Phase] = {}
        for phase in PHASE_ORDER:
            index = assigned.get(phase)
            if index is None:
                continue
            if index in seen:
                issues.append(
                    f"{phase.value} and {seen[index].value} share frame {index}"
                )
            else:
                seen[index] = phase

        ordered = self.ordered_frames()
        for (previous_phase, previous), (phase, index) in zip(ordered, ordered[1:]):
            if index < previous:
                issues.append(
                    f"{phase.value} ({index}) is before {previous_phase.value} "
                    f"({previous})"
                )

        return tuple(issues)

    def with_reference(self, frame_index: int | None) -> "PhaseSelection":
        return PhaseSelection(
            frames=dict(self.frames),
            reference_frame=frame_index,
            source=self.source,
            warnings=self.warnings,
        )


@runtime_checkable
class PhaseProvider(Protocol):
    """Interface implemented by manual selection and future detectors."""

    name: str
    requires_records: bool

    def select(self, records: Sequence[object] | None = None) -> PhaseSelection:
        ...


@dataclass
class ManualPhaseProvider:
    """Development mechanism for assigning phases by frame index.

    ``derive_reference_from_address`` keeps the common case convenient: when
    the user has explicitly assigned an address frame, that frame becomes the
    reference for relative measurements and the derivation is recorded in the
    selection warnings. With the flag off, and whenever no address frame is
    assigned, relative measurements stay unavailable rather than being
    silently anchored to an invented frame.
    """

    frames: Mapping[Phase | str, int | None] = field(default_factory=dict)
    reference_frame: int | None = None
    derive_reference_from_address: bool = True
    name: str = "manual"
    requires_records: bool = False

    def resolved_frames(self) -> dict[Phase, int | None]:
        return {
            phase: (None if self.frames.get(phase) is None else int(self.frames[phase]))  # type: ignore[arg-type]
            for phase in PHASE_ORDER
        }

    def select(self, records: Sequence[object] | None = None) -> PhaseSelection:
        frames = {
            coerce_phase(phase): (
                None if index is None else int(index)  # type: ignore[arg-type]
            )
            for phase, index in self.frames.items()
        }
        warnings: list[str] = []
        reference = self.reference_frame
        if reference is None and self.derive_reference_from_address:
            address = frames.get(Phase.ADDRESS)
            if address is not None:
                reference = address
                warnings.append(
                    "reference frame derived from the manually assigned address "
                    f"phase (frame {address})"
                )
        if reference is None:
            warnings.append(
                "no reference frame selected; relative measurements unavailable"
            )
        return PhaseSelection(
            frames=frames,
            reference_frame=reference,
            source=SOURCE_MANUAL,
            warnings=tuple(warnings),
        )


class UnavailableAutoPhaseProvider:
    """Placeholder for future automatic phase detection.

    It exists so the interface is exercised and so calling it produces a clear
    error instead of a plausible-looking guess.
    """

    name = "automatic-unavailable"
    requires_records = True

    def select(self, records: Sequence[object] | None = None) -> PhaseSelection:
        raise NotImplementedError(
            "automatic swing phase detection is not implemented in Engine v0.1; "
            "assign phases manually by frame index"
        )
