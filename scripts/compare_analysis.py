"""Compare two analysis bundles and report anything the viewer can see.

    .venv\\Scripts\\python.exe scripts\\compare_analysis.py BEFORE.json AFTER.json

This is the acceptance guard for a performance change: the six phase frames,
every shown and hidden check, the wording, the confidence levels, the guided
flags, the shaft source and the overlay geometry. It fails closed - a missing
phase, an extra phase, a reordered phase, a different overlay element kind or
role, a missing anchor, an unlisted key or a changed confidence is a
difference, not something to skip.

Numeric tolerance is granted in exactly two places, the ones agreed for the
bundle:

* shaft angles (``shaft_direction_deg``, ``shaft_from_horizontal_deg``,
  ``change_vs_address_deg``): 1 degree;
* normalised overlay coordinates and anchors: 2 original-image pixels.

Everything else must be identical. ``engine`` is excluded there because it
holds the timings that are supposed to change; the result also states whether
the remaining consumer payload is byte-for-byte identical.

Exit code is 0 when the two bundles are equivalent within tolerance.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ANGLE_TOLERANCE_DEG = 1.0
GEOMETRY_TOLERANCE_PX = 2.0

ANGLE_RAW_KEYS = ("shaft_direction_deg", "shaft_from_horizontal_deg", "change_vs_address_deg")

SCALAR_KEYS = (
    "version",
    "source",
    "camera_view",
    "video",
    "fps",
    "width",
    "height",
    "duration_s",
    "segment_start_s",
    "segment_end_s",
)
PHASE_KEYS = ("phase", "label", "frame_index", "time_s", "source")
CHECK_KEYS = (
    "id",
    "kind",
    "phase",
    "frame_index",
    "title",
    "value",
    "qualifier",
    "summary",
    "callout",
    "detail",
    "explain",
    "level",
    "estimated",
    "guided",
    "source",
    "confidence",
    "time_s",
)
GEOMETRY_KEYS = ("kind", "role", "radius", "start_deg", "end_deg")

# Interpretation output (added with the golf interpretation layer) is
# golfer-visible, so it is compared exactly - but older bundles have neither
# key, and two bundles that both lack it are still equivalent.
OPTIONAL_COMPARED_KEYS = ("geometry_context", "interpretations")
TOP_LEVEL_KEYS = set(SCALAR_KEYS) | {"summary", "focus", "phases", "hidden", "engine", *OPTIONAL_COMPARED_KEYS}
PHASE_OBJECT_KEYS = set(PHASE_KEYS) | {"checks"}
CHECK_OBJECT_KEYS = set(CHECK_KEYS) | {"raw", "geometry", "anchor"}
GEOMETRY_OBJECT_KEYS = set(GEOMETRY_KEYS) | {"points"}


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        point = float(value[0]), float(value[1])
        return point if all(math.isfinite(item) for item in point) else None
    except (TypeError, ValueError):
        return None


def _point_delta_px(before: Any, after: Any, width: int, height: int) -> float | None:
    first, second = _point(before), _point(after)
    if first is None or second is None:
        return None
    return max(abs(first[0] - second[0]) * width, abs(first[1] - second[1]) * height)


def _scalar_differences(before: Mapping, after: Mapping, keys, where: str) -> list[str]:
    """Presence-aware, exact comparison of a fixed key list."""

    out: list[str] = []
    for key in keys:
        present_before, present_after = key in before, key in after
        if not present_before and not present_after:
            continue
        if present_before != present_after:
            out.append(f"{where}.{key} present in only one bundle")
        elif before[key] != after[key]:
            out.append(f"{where}.{key} {before[key]!r} -> {after[key]!r}")
    return out


def _unknown_key_differences(before: Mapping, after: Mapping, known: set, where: str) -> list[str]:
    """Fail closed on any key this guard does not know about."""

    out: list[str] = []
    for key in sorted(set(before) | set(after)):
        if key in known:
            continue
        present_before, present_after = key in before, key in after
        if present_before != present_after:
            out.append(f"{where}.{key} unexpected key in only one bundle")
        elif before[key] != after[key]:
            out.append(f"{where}.{key} unexpected key {before[key]!r} -> {after[key]!r}")
    return out


def _payload_without_engine(payload: Mapping) -> str:
    stripped = {key: value for key, value in payload.items() if key != "engine"}
    return json.dumps(stripped, sort_keys=False, default=str)


def _check_differences(
    check_before: Any,
    check_after: Any,
    width: int,
    height: int,
    result: dict,
    where: str,
) -> list[str]:
    if not isinstance(check_before, Mapping) or not isinstance(check_after, Mapping):
        return [f"{where} is not an object"]

    out = _scalar_differences(check_before, check_after, CHECK_KEYS, where)
    out.extend(_unknown_key_differences(check_before, check_after, CHECK_OBJECT_KEYS, where))

    raw_before = check_before.get("raw")
    raw_after = check_after.get("raw")
    if not isinstance(raw_before, Mapping) or not isinstance(raw_after, Mapping):
        out.append(f"{where}.raw is missing or not an object")
    else:
        for key in sorted(set(raw_before) | set(raw_after)):
            if key not in raw_before or key not in raw_after:
                out.append(f"{where}.raw.{key} present in only one bundle")
                continue
            first, second = raw_before[key], raw_after[key]
            if key in ANGLE_RAW_KEYS:
                try:
                    delta = abs(float(second) - float(first))
                    if not math.isfinite(delta):
                        raise ValueError("non-finite angle")
                except (TypeError, ValueError):
                    out.append(f"{where}.raw.{key} is not a number")
                    continue
                if key == "shaft_direction_deg":
                    result["max_angle_delta_deg"] = max(result["max_angle_delta_deg"], delta)
                if delta > ANGLE_TOLERANCE_DEG:
                    out.append(f"{where}.raw.{key} {delta:.3f} deg")
            elif first != second:
                out.append(f"{where}.raw.{key} {first!r} -> {second!r}")

    geometry_before = check_before.get("geometry")
    geometry_after = check_after.get("geometry")
    if not isinstance(geometry_before, list) or not isinstance(geometry_after, list):
        out.append(f"{where}.geometry is missing or not a list")
    elif len(geometry_before) != len(geometry_after):
        out.append(f"{where}.geometry count {len(geometry_before)} -> {len(geometry_after)}")
    else:
        for index, (item_before, item_after) in enumerate(zip(geometry_before, geometry_after)):
            here = f"{where}.geometry[{index}]"
            if not isinstance(item_before, Mapping) or not isinstance(item_after, Mapping):
                out.append(f"{here} is not an object")
                continue
            out.extend(_scalar_differences(item_before, item_after, GEOMETRY_KEYS, here))
            out.extend(_unknown_key_differences(item_before, item_after, GEOMETRY_OBJECT_KEYS, here))
            points_before, points_after = item_before.get("points"), item_after.get("points")
            if not isinstance(points_before, list) or not isinstance(points_after, list):
                if points_before is not None or points_after is not None:
                    out.append(f"{here}.points is missing or not a list")
                continue
            if len(points_before) != len(points_after):
                out.append(f"{here}.points count {len(points_before)} -> {len(points_after)}")
                continue
            for point_index, (point_before, point_after) in enumerate(zip(points_before, points_after)):
                delta = _point_delta_px(point_before, point_after, width, height)
                if delta is None:
                    out.append(f"{here}.points[{point_index}] is not a point")
                    continue
                result["max_geometry_delta_px"] = max(result["max_geometry_delta_px"], delta)
                if delta > GEOMETRY_TOLERANCE_PX:
                    out.append(f"{here}.points[{point_index}] {delta:.2f} px")

    anchor_before, anchor_after = check_before.get("anchor"), check_after.get("anchor")
    if (anchor_before is None) != (anchor_after is None):
        out.append(f"{where}.anchor present in only one bundle")
    elif anchor_before is not None:
        delta = _point_delta_px(anchor_before, anchor_after, width, height)
        if delta is None:
            out.append(f"{where}.anchor is not a point")
        else:
            result["max_geometry_delta_px"] = max(result["max_geometry_delta_px"], delta)
            if delta > GEOMETRY_TOLERANCE_PX:
                out.append(f"{where}.anchor {delta:.2f} px")
    return out


def compare(before: Mapping, after: Mapping) -> dict:
    """Structural, fail-closed comparison of two ``analysis.json`` payloads."""

    result: dict[str, Any] = {
        "scalars": {},
        "phases": {},
        "max_angle_delta_deg": 0.0,
        "max_geometry_delta_px": 0.0,
        "differences": [],
    }
    differences: list[str] = result["differences"]

    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        differences.append("one of the payloads is not an analysis object")
        result["equivalent"] = False
        result["exact_payload_except_engine"] = False
        return result

    width = int(before.get("width") or 0)
    height = int(before.get("height") or 0)

    differences.extend(_scalar_differences(before, after, SCALAR_KEYS, "analysis"))
    differences.extend(_unknown_key_differences(before, after, TOP_LEVEL_KEYS, "analysis"))
    for key in ("summary", "focus"):
        same = (
            key in before
            and key in after
            and json.dumps(before[key], sort_keys=True) == json.dumps(after[key], sort_keys=True)
        )
        result["scalars"][key] = {"same": same}
        if not same:
            differences.append(f"analysis.{key} changed")
    for key in OPTIONAL_COMPARED_KEYS:
        same = json.dumps(before.get(key), sort_keys=True) == json.dumps(after.get(key), sort_keys=True)
        result["scalars"][key] = {"same": same}
        if not same:
            differences.append(f"analysis.{key} changed")

    before_phases, after_phases = before.get("phases"), after.get("phases")
    compared_phases: list = []
    if not isinstance(before_phases, list) or not isinstance(after_phases, list):
        differences.append("analysis.phases is missing or not a list")
        compared_phases = []
    else:
        if len(before_phases) != len(after_phases):
            differences.append(f"phase count {len(before_phases)} -> {len(after_phases)}")
        compared_phases = list(zip(before_phases, after_phases))

    for index, (phase_before, phase_after) in enumerate(compared_phases):
        label = f"phase[{index}]"
        if not isinstance(phase_before, Mapping) or not isinstance(phase_after, Mapping):
            differences.append(f"{label} is not an object")
            continue
        name = phase_before.get("phase", label)
        if phase_before.get("phase") != phase_after.get("phase"):
            differences.append(
                f"{label} identity {phase_before.get('phase')!r} -> {phase_after.get('phase')!r}"
            )
        entry = _scalar_differences(phase_before, phase_after, PHASE_KEYS, label)
        entry.extend(_unknown_key_differences(phase_before, phase_after, PHASE_OBJECT_KEYS, label))
        differences.extend(entry)
        result["phases"][str(name)] = {"same": not entry}

        checks_before = phase_before.get("checks")
        checks_after = phase_after.get("checks")
        if not isinstance(checks_before, list) or not isinstance(checks_after, list):
            differences.append(f"{label}.checks is missing or not a list")
            continue
        if len(checks_before) != len(checks_after):
            differences.append(f"{label}.checks count {len(checks_before)} -> {len(checks_after)}")
        for check_index, (check_before, check_after) in enumerate(zip(checks_before, checks_after)):
            differences.extend(
                _check_differences(
                    check_before,
                    check_after,
                    width,
                    height,
                    result,
                    f"{label}.checks[{check_index}]",
                )
            )

    if isinstance(before_phases, list) and isinstance(after_phases, list):
        for index in range(len(after_phases) - len(before_phases)):
            phase_after = after_phases[len(before_phases) + index]
            name = phase_after.get("phase") if isinstance(phase_after, Mapping) else f"[{index}]"
            differences.append(f"phase[{len(before_phases) + index}] {name!r} is present only in the second bundle")

    hidden_same = (
        "hidden" in before
        and "hidden" in after
        and json.dumps(before["hidden"], sort_keys=True) == json.dumps(after["hidden"], sort_keys=True)
    )
    result["scalars"]["hidden"] = {"same": hidden_same}
    if not hidden_same:
        differences.append("hidden checks changed")

    result["max_angle_delta_deg"] = round(result["max_angle_delta_deg"], 4)
    result["max_geometry_delta_px"] = round(result["max_geometry_delta_px"], 3)
    result["equivalent"] = not differences
    result["exact_payload_except_engine"] = _payload_without_engine(before) == _payload_without_engine(after)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--out", default=None, help="write the full comparison JSON here")
    parser.add_argument(
        "--require-exact-payload",
        action="store_true",
        help="also require the consumer payload (everything but engine) to be identical",
    )
    args = parser.parse_args(argv)

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    result = compare(before, after)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({
        "equivalent": result["equivalent"],
        "exact_payload_except_engine": result["exact_payload_except_engine"],
        "max_angle_delta_deg": result["max_angle_delta_deg"],
        "max_geometry_delta_px": result["max_geometry_delta_px"],
        "phases": result["phases"],
        "differences": result["differences"],
    }, indent=2))
    if not result["equivalent"]:
        return 1
    if args.require_exact_payload and not result["exact_payload_except_engine"]:
        print("consumer payload (everything except engine) is not identical")
        return 1
    return 0


__all__ = ["ANGLE_TOLERANCE_DEG", "GEOMETRY_TOLERANCE_PX", "compare", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
