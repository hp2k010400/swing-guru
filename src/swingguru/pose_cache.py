"""Identity-keyed cache of pose records.

The analysis build caches the per-frame pose records so a rebuild does not pay
for RTMW again. The cache must not be able to stand in for a different video,
model or configuration: an unkeyed pickle from an earlier run would silently
return the old landmarks for a new experiment and make the experiment look
exactly like the baseline.

Every cache written from here on carries its full identity - cache version,
video content digest, provider, model, mode, detector cadence, tracker, the
digests of the ONNX assets that were actually loaded, the installed runtime
versions, the measurement settings the stored records were derived with, and
the options of any pose variant in use - and is only read back when that
identity matches exactly. A legacy bare pickle has no identity at all, so it is
reported as a miss unless a caller explicitly asks for it with ``allow_legacy``
(which is recorded in the run's status).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

POSE_CACHE_VERSION = 1


def _digest(payload: object) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def file_digest(path: str | Path, *, block: int = 1 << 20) -> str:
    """Content digest of one file, or a stable marker when it is unreadable."""

    target = Path(path)
    try:
        if not target.is_file():
            return "missing"
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(block), b""):
                digest.update(chunk)
        return digest.hexdigest()[:16]
    except OSError:
        return "unreadable"


def asset_fingerprint(paths: Sequence[str | Path]) -> str:
    """Cheap identity of the model assets that were actually loaded.

    Name, size and ``mtime_ns`` rather than a content hash: a replaced or
    re-exported ONNX file changes at least one of them, and hashing ~330 MB on
    every build would more than double the cost of a warm rebuild. Use
    :func:`file_digest` when a content hash is worth that.
    """

    entries = []
    for item in paths:
        text = str(item)
        target = Path(item)
        try:
            stat = target.stat() if target.is_file() else None
        except OSError:
            stat = None
        entries.append(
            [
                Path(text).name,
                None if stat is None else stat.st_size,
                None if stat is None else stat.st_mtime_ns,
            ]
        )
    return _digest(entries)


def settings_fingerprint(settings: object) -> str:
    """Digest of the settings the stored per-frame records were derived with."""

    if dataclasses.is_dataclass(settings) and not isinstance(settings, type):
        payload = {item.name: getattr(settings, item.name) for item in dataclasses.fields(settings)}
    elif isinstance(settings, Mapping):
        payload = dict(settings)
    else:  # pragma: no cover - defensive
        payload = repr(settings)
    return _digest(payload)


def runtime_fingerprint() -> str:
    """Installed pose runtime versions, so a dependency change invalidates."""

    import numpy

    versions: dict[str, str] = {"numpy": numpy.__version__}
    try:
        import onnxruntime

        versions["onnxruntime"] = onnxruntime.__version__
    except Exception:  # noqa: BLE001 - optional dependency
        versions["onnxruntime"] = "absent"
    try:
        from rtmlib import version as rtmlib_version

        versions["rtmlib"] = str(getattr(rtmlib_version, "__version__", "unknown"))
    except Exception:  # noqa: BLE001 - optional dependency
        versions["rtmlib"] = "absent"
    return _digest(versions)


@dataclass(frozen=True)
class PoseCacheIdentity:
    """Everything a cached pose record set depends on."""

    video: str
    provider: str
    model: str = ""
    mode: str = ""
    det_frequency: int = 0
    tracker: str = ""
    model_digest: str = ""
    runtime: str = ""
    measurement: str = ""
    options: str = "{}"
    version: int = POSE_CACHE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "video": self.video,
            "provider": self.provider,
            "model": self.model,
            "mode": self.mode,
            "det_frequency": self.det_frequency,
            "tracker": self.tracker,
            "model_digest": self.model_digest,
            "runtime": self.runtime,
            "measurement": self.measurement,
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PoseCacheIdentity":
        if not isinstance(payload, Mapping):
            raise ValueError("pose cache identity must be an object")
        try:
            version = int(payload["version"])
            det_frequency = int(payload.get("det_frequency", 0))
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError("pose cache identity has a malformed version") from error
        if not math.isfinite(float(version)) or not math.isfinite(float(det_frequency)):
            # int(float('nan')) and int(float('inf')) must never slip through as
            # a usable identity (inf raises OverflowError, nan a ValueError).
            raise ValueError("pose cache identity has a non-finite version")
        options = payload.get("options", "{}")
        if not isinstance(options, str):
            raise ValueError("pose cache identity options must be a string")
        return cls(
            video=str(payload.get("video", "")),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            mode=str(payload.get("mode", "")),
            det_frequency=det_frequency,
            tracker=str(payload.get("tracker", "")),
            model_digest=str(payload.get("model_digest", "")),
            runtime=str(payload.get("runtime", "")),
            measurement=str(payload.get("measurement", "")),
            options=options,
            version=version,
        )

    def key(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:16]

    @property
    def description(self) -> str:
        parts = [self.provider or "unknown", self.model or "-", self.mode or "-"]
        if self.det_frequency:
            parts.append(f"cadence{self.det_frequency}")
        return " ".join(parts)


def legacy_identity(provider: str = "rtmw", model: str = "wholebody", mode: str = "balanced", det_frequency: int = 5) -> PoseCacheIdentity:
    """The identity a pre-keying bare pickle is assumed to have had."""

    return PoseCacheIdentity(
        video="",
        provider=provider,
        model=model,
        mode=mode,
        det_frequency=det_frequency,
        tracker="rtmlib-pose-tracker",
        version=0,
    )


@dataclass(frozen=True)
class PoseCacheResult:
    records: list | None
    status: str
    identity: PoseCacheIdentity | None = None

    @property
    def hit(self) -> bool:
        return self.status in {"hit", "hit-legacy"}


def load_pose_cache(
    path: Path | None,
    identity: PoseCacheIdentity,
    *,
    allow_legacy: bool = False,
) -> PoseCacheResult:
    """Read the cache only when it identifies itself as this exact run."""

    if path is None:
        return PoseCacheResult(None, "miss-no-path")
    target = Path(path)
    if not target.exists():
        return PoseCacheResult(None, "miss-missing")
    try:
        payload = pickle.loads(target.read_bytes())
    except Exception:  # noqa: BLE001 - any unreadable pickle is a miss
        return PoseCacheResult(None, "miss-corrupt")

    if isinstance(payload, Mapping):
        if payload.get("version") != POSE_CACHE_VERSION:
            return PoseCacheResult(None, "miss-version")
        try:
            stored = PoseCacheIdentity.from_dict(payload["identity"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return PoseCacheResult(None, "miss-corrupt")
        if stored != identity:
            return PoseCacheResult(None, "miss-identity", stored)
        records = payload.get("records")
        if not isinstance(records, (list, tuple)):
            return PoseCacheResult(None, "miss-corrupt", stored)
        return PoseCacheResult(list(records), "hit", stored)

    # A bare list/tuple predates keying: we cannot check the video, model or
    # settings it came from, so it is only used when explicitly allowed.
    if isinstance(payload, (list, tuple)):
        if not allow_legacy:
            return PoseCacheResult(None, "miss-legacy")
        return PoseCacheResult(list(payload), "hit-legacy", legacy_identity())
    return PoseCacheResult(None, "miss-corrupt")


def save_pose_cache(path: Path, records: Sequence[object], identity: PoseCacheIdentity) -> None:
    """Write the records with their identity, beside the target, then rename."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": POSE_CACHE_VERSION,
        "identity": identity.to_dict(),
        "records": list(records),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(pickle.dumps(payload))
    os.replace(temporary, target)


def load_records_any(path: Path) -> list:
    """Read pose records from either cache format (diagnostic tools only)."""

    payload = pickle.loads(Path(path).read_bytes())
    if isinstance(payload, Mapping):
        records = payload.get("records")
        if not isinstance(records, (list, tuple)):
            raise ValueError(f"{path} does not contain a keyed record list")
        return list(records)
    if isinstance(payload, (list, tuple)):
        return list(payload)
    raise ValueError(f"{path} is not a pose record cache")


__all__ = [
    "POSE_CACHE_VERSION",
    "PoseCacheIdentity",
    "PoseCacheResult",
    "asset_fingerprint",
    "file_digest",
    "legacy_identity",
    "load_pose_cache",
    "load_records_any",
    "runtime_fingerprint",
    "save_pose_cache",
    "settings_fingerprint",
]
