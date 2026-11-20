"""Context-safe cache for the classical tracker's per-frame candidates.

The expensive half of the tracker is :meth:`ClassicalClubTracker.detect_frame`:
it builds the Hessian maps, searches the hand-anchored rays and runs the LSD
corroboration. The cheap half is the Viterbi selection, which depends on the
frames being processed *together*.

So this cache stores **candidates**, never a temporally selected track, and
every entry is keyed by a full identity:

* the tracker version and its settings (``ClubConfig``),
* the video content digest,
* the pose input digest (the landmark records the candidates were anchored to),
* the global background-reference set the static-line test used,
* the frame index.

If any of those change, the identity changes and the entry is not reused. The
Viterbi pass is re-run for the current windows on every build, so a cached
candidate can never leak a temporal choice made for a different context.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .tracker import FrameDetection, HandsContext, ShaftCandidate

CANDIDATE_CACHE_VERSION = 1


def _float(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        return number
    return number


def _finite(value: Any, *, where: str) -> float:
    """A finite float, or ``ValueError`` - used when reading cached payloads."""

    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{where} is not a number") from error
    if not math.isfinite(number):
        raise ValueError(f"{where} is not finite")
    return number


def _decode_point(value: Any, *, where: str) -> np.ndarray:
    """A two-element finite point, or ``ValueError``."""

    try:
        array = np.asarray(value, float).ravel()
    except (TypeError, ValueError) as error:
        raise ValueError(f"{where} is not a point") from error
    if array.size != 2 or not np.all(np.isfinite(array)):
        raise ValueError(f"{where} must be two finite numbers")
    return array


def _point_list(point: Any) -> list[float] | None:
    if point is None:
        return None
    return [_float(v) for v in np.asarray(point, float).ravel()]


def encode_candidate(candidate: ShaftCandidate) -> dict[str, Any]:
    return {
        "grip": [_float(v) for v in candidate.grip],
        "tip": [_float(v) for v in candidate.tip],
        "angle_deg": _float(candidate.angle_deg),
        "visible_start_px": _float(candidate.visible_start_px),
        "length_px": _float(candidate.length_px),
        "support": _float(candidate.support),
        "strength": _float(candidate.strength),
        "score": _float(candidate.score),
        "method": candidate.method,
        "corroborated": bool(candidate.corroborated),
        "rejected": candidate.rejected,
    }


def decode_candidate(payload: Mapping[str, Any]) -> ShaftCandidate:
    if not isinstance(payload, Mapping):
        raise ValueError("a cached candidate must be an object")
    return ShaftCandidate(
        grip=_decode_point(payload["grip"], where="candidate.grip"),
        tip=_decode_point(payload["tip"], where="candidate.tip"),
        angle_deg=_finite(payload["angle_deg"], where="candidate.angle_deg"),
        visible_start_px=_finite(payload["visible_start_px"], where="candidate.visible_start_px"),
        length_px=_finite(payload["length_px"], where="candidate.length_px"),
        support=_finite(payload["support"], where="candidate.support"),
        strength=_finite(payload["strength"], where="candidate.strength"),
        score=_finite(payload["score"], where="candidate.score"),
        method=str(payload.get("method", "ridge")),
        corroborated=bool(payload.get("corroborated", False)),
        rejected=str(payload.get("rejected", "")),
    )


def encode_hands(hands: HandsContext) -> dict[str, Any]:
    return {
        "origin": _point_list(hands.origin),
        "torso_px": _float(hands.torso_px),
        "forearms": [[_point_list(a), _point_list(b)] for a, b in hands.forearms],
        "source": hands.source,
        "reason": hands.reason,
        "body_segments": [
            [label, _point_list(a), _point_list(b)]
            for label, a, b in hands.body_segments
        ],
    }


def decode_hands(payload: Mapping[str, Any]) -> HandsContext:
    if not isinstance(payload, Mapping):
        raise ValueError("cached hands must be an object")
    origin = payload.get("origin")
    forearms = payload.get("forearms") or []
    segments = payload.get("body_segments") or []
    if not isinstance(forearms, (list, tuple)) or not isinstance(segments, (list, tuple)):
        raise ValueError("cached hands must list forearms and body segments")
    return HandsContext(
        None if origin is None else _decode_point(origin, where="hands.origin"),
        _finite(payload.get("torso_px", 0.0), where="hands.torso_px"),
        tuple(
            (
                _decode_point(pair[0], where="hands.forearms[][0]"),
                _decode_point(pair[1], where="hands.forearms[][1]"),
            )
            for pair in forearms
        ),
        str(payload.get("source", "unavailable")),
        str(payload.get("reason", "")),
        tuple(
            (
                str(item[0]),
                _decode_point(item[1], where="hands.body_segments[][1]"),
                _decode_point(item[2], where="hands.body_segments[][2]"),
            )
            for item in segments
        ),
    )


def encode_detection(detection: FrameDetection) -> dict[str, Any]:
    return {
        "frame_index": int(detection.frame_index),
        "timestamp_ms": _float(detection.timestamp_ms),
        "hands": encode_hands(detection.hands),
        "candidates": [encode_candidate(item) for item in detection.candidates],
        "rejected": [encode_candidate(item) for item in detection.rejected],
        "reason": detection.reason,
    }


def decode_detection(payload: Mapping[str, Any]) -> FrameDetection:
    if not isinstance(payload, Mapping):
        raise ValueError("a cached detection must be an object")
    frame_index = payload["frame_index"]
    if isinstance(frame_index, bool) or not isinstance(frame_index, (int, float)):
        raise ValueError("detection.frame_index must be a number")
    candidates = payload.get("candidates", [])
    rejected = payload.get("rejected", [])
    if not isinstance(candidates, (list, tuple)) or not isinstance(rejected, (list, tuple)):
        raise ValueError("detection candidates must be lists")
    return FrameDetection(
        int(frame_index),
        _finite(payload["timestamp_ms"], where="detection.timestamp_ms"),
        decode_hands(payload["hands"]),
        tuple(decode_candidate(item) for item in candidates),
        tuple(decode_candidate(item) for item in rejected),
        str(payload.get("reason", "")),
    )


def _digest(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def config_fingerprint(config: object) -> str:
    """Stable digest of one tracker configuration."""

    if is_dataclass(config):
        payload = {item.name: getattr(config, item.name) for item in fields(config)}
    elif isinstance(config, Mapping):
        payload = dict(config)
    else:  # pragma: no cover - defensive
        payload = repr(config)
    return _digest(payload)


def pose_fingerprint(records: Sequence[object]) -> str:
    """Digest of every pose input the per-frame candidates are derived from.

    The hand anchor is not the only input: when the usable pixel points are
    missing, ``hands_context`` falls back to the raw normalised landmark
    ``x``/``y`` (scaled by the record's width and height) gated on landmark
    ``visibility`` and ``finite``. All of those are hashed here, unrounded, so
    a change to a weak-fallback coordinate cannot reuse a stale candidate.
    """

    parts: list[dict[str, object]] = []
    for record in records:
        points = getattr(record, "points", None)
        pixels = getattr(points, "pixels", {}) if points is not None else {}
        landmarks = getattr(points, "landmarks", {}) if points is not None else {}
        parts.append(
            {
                "index": int(getattr(record, "index", -1)),
                # Unrounded: a shifted timestamp is a different input.
                "timestamp_ms": repr(float(getattr(record, "timestamp_ms", 0.0))),
                "width": repr(float(getattr(record, "width", 0) or 0.0)),
                "height": repr(float(getattr(record, "height", 0) or 0.0)),
                "pixels": {
                    name: [repr(float(v)) for v in np.asarray(value, float).ravel()]
                    for name, value in sorted(pixels.items())
                },
                "landmarks": {
                    name: _landmark_fingerprint(landmark)
                    for name, landmark in sorted(landmarks.items())
                },
            }
        )
    return _digest(parts)


def _landmark_fingerprint(landmark: object) -> list[object]:
    """Raw fields ``hands_context`` and its weak fallback can read."""

    def value(name: str) -> object:
        raw = getattr(landmark, name, None)
        if raw is None:
            return None
        try:
            return repr(float(raw))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return repr(raw)

    try:
        finite = bool(landmark.finite)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - defensive: any broken property is an input
        finite = repr(landmark)
    return [value("x"), value("y"), value("z"), value("visibility"), value("presence"), finite]


def reference_fingerprint(references: Sequence[object]) -> str:
    """Digest of the background-reference set (frames and their hands).

    Accepts either :class:`~swingguru.club.tracker.ReferenceFrame` objects or
    plain ``(frame_index, hands)`` pairs, so the identity can be built from the
    pose records before any frame is decoded.
    """

    parts = []
    for reference in references:
        if isinstance(reference, (tuple, list)) and len(reference) == 2:
            frame_index, hands = reference
            parts.append(
                [
                    int(frame_index),
                    None if hands is None else [repr(float(v)) for v in np.asarray(hands, float).ravel()],
                ]
            )
            continue
        hands = getattr(reference, "hands", None)
        parts.append(
            [
                int(getattr(reference, "frame_index", -1)),
                None if hands is None else [repr(float(v)) for v in np.asarray(hands, float).ravel()],
            ]
        )
    return _digest(parts)


@dataclass(frozen=True)
class CandidateIdentity:
    """Everything a cached candidate depends on."""

    tracker: str
    video: str
    pose: str
    config: str
    references: str
    version: int = CANDIDATE_CACHE_VERSION

    def key(self, frame_index: int) -> str:
        return (
            f"{self.tracker}:{self.version}:{self.video}:{self.pose}:"
            f"{self.config}:{self.references}:{int(frame_index)}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tracker": self.tracker,
            "version": self.version,
            "video": self.video,
            "pose": self.pose,
            "config": self.config,
            "references": self.references,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CandidateIdentity":
        return cls(
            tracker=str(payload.get("tracker", "")),
            video=str(payload.get("video", "")),
            pose=str(payload.get("pose", "")),
            config=str(payload.get("config", "")),
            references=str(payload.get("references", "")),
            version=int(payload.get("version", 0)),
        )


class CandidateCache:
    """In-memory frame-candidate cache with optional JSON persistence.

    ``path`` is optional: without it the cache only deduplicates work inside
    one run. With it, a later build reuses candidates only when the whole
    identity matches, which is what makes a warm rebuild cheap without ever
    reusing a temporally selected result from another context.
    """

    def __init__(
        self,
        identity: CandidateIdentity,
        *,
        path: Path | None = None,
        entries: Mapping[int, FrameDetection] | None = None,
    ) -> None:
        self.identity = identity
        self.path = None if path is None else Path(path)
        self.entries: dict[int, FrameDetection] = dict(entries or {})
        self.hits = 0
        self.misses = 0
        self.stale = False

    # -- lookup ------------------------------------------------------------

    def get(self, frame_index: int) -> FrameDetection | None:
        found = self.entries.get(int(frame_index))
        if found is None:
            self.misses += 1
        else:
            self.hits += 1
        return found

    def put(self, detection: FrameDetection) -> None:
        self.entries[int(detection.frame_index)] = detection

    def __contains__(self, frame_index: object) -> bool:
        return int(frame_index) in self.entries  # type: ignore[arg-type]

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def stats(self) -> dict[str, object]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self.entries),
            "stale_identity": self.stale,
        }

    # -- persistence -------------------------------------------------------

    def to_payload(self) -> dict[str, object]:
        return {
            "version": CANDIDATE_CACHE_VERSION,
            "identity": self.identity.to_dict(),
            "frames": {
                str(index): encode_detection(detection)
                for index, detection in sorted(self.entries.items())
            },
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so an interrupted save cannot
        # leave a half-written cache behind.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.to_payload()), encoding="utf-8")
        os.replace(temporary, self.path)

    @classmethod
    def load(
        cls,
        identity: CandidateIdentity,
        path: Path | None,
    ) -> "CandidateCache":
        if path is None or not Path(path).exists():
            return cls(identity, path=path)
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("candidate cache must be an object")
            if int(payload.get("version", 0)) != CANDIDATE_CACHE_VERSION:
                raise ValueError("candidate cache version does not match")
            raw_identity = payload.get("identity")
            if not isinstance(raw_identity, Mapping):
                raise ValueError("candidate cache identity is missing or malformed")
            if CandidateIdentity.from_dict(raw_identity) != identity:
                raise ValueError("candidate cache identity does not match")
            frames = payload.get("frames")
            if not isinstance(frames, Mapping):
                raise ValueError("candidate cache frames are missing or malformed")
        except (OSError, TypeError, ValueError, OverflowError):
            cache = cls(identity, path=path)
            cache.stale = True
            return cache

        entries: dict[int, FrameDetection] = {}
        for key, item in frames.items():
            try:
                detection = decode_detection(item)
            except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError):
                # A single unreadable frame is a cache miss for that frame, not
                # a reason to throw the rest of the cache away.
                continue
            if str(detection.frame_index) != str(key):
                continue
            entries[detection.frame_index] = detection
        return cls(identity, path=path, entries=entries)


__all__ = [
    "CANDIDATE_CACHE_VERSION",
    "CandidateCache",
    "CandidateIdentity",
    "config_fingerprint",
    "decode_candidate",
    "decode_detection",
    "decode_hands",
    "encode_candidate",
    "encode_detection",
    "encode_hands",
    "pose_fingerprint",
    "reference_fingerprint",
]
