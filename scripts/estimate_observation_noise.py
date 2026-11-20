"""Reliability of the extra replay observations (Phase 2), from swings the harness analysed.

    python scripts/estimate_observation_noise.py <validation run dir> [<run dir> ...] [--out file.json]

For every swing folder (``analysis.json`` + ``pose_records.pkl``) each candidate observation is
re-measured with the engine's own code at its moment frame and one frame either side, exactly as
``estimate_measurement_noise.py`` does for the scored checks. Reported per observation:

* availability (shown at all) and confidence levels;
* frame noise: the half-range of the three readings, median and 75th percentile over swings;
* the same figure for the scored Top posture check, as the reference a golfer already sees;
* the spread of the readings themselves (10th-90th percentile).

Read-only: nothing in the bundles changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.analysis_bundle import build_body_check, swing_context  # noqa: E402
from swingguru.engine_profile import profile, rebind_sides  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import Phase  # noqa: E402
from swingguru.pipeline import apply_reference  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402

CANDIDATES = {  # id -> (kind, phase, raw key)
    "mid_back.posture": ("posture", Phase.MID_BACK, "torso_angle_change_deg"),
    "top.posture (scored reference)": ("posture", Phase.TOP, "torso_angle_change_deg"),
    "top.head": ("head", Phase.TOP, "movement_pct_height"),
    "downswing.posture": ("posture", Phase.DOWNSWING, "torso_angle_change_deg"),
    "impact.posture": ("posture", Phase.IMPACT, "torso_angle_change_deg"),
}


def readings(folder: Path) -> dict:
    analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    records = load_records_any(folder / "pose_records.pkl")
    by_index = {int(r.index): r for r in records}
    frames = {p["phase"]: p.get("frame_index") for p in analysis["phases"]}
    shown = {(p["phase"], c["kind"]): c for p in analysis["phases"] for c in p["checks"]}
    hidden = {(h["phase"], h["kind"]) for h in analysis.get("hidden", [])}
    address = frames.get("address")
    name = (analysis.get("engine_profile") or {}).get("name", "current")
    prof = None if name == "current" else profile(name)
    handed = (analysis.get("handedness") or {}).get("value", "right")
    if handed != "right":
        rebind_sides(records, handed)
    apply_reference(records, address, config=MeasurementConfig(handedness=handed))
    context = swing_context(records, address, "rtmw", prof)
    out = {}
    for cid, (kind, phase, key) in CANDIDATES.items():
        check = shown.get((phase.value, kind))
        row = {"shown": check is not None, "level": None if check is None else check.get("level"),
               "observation": bool(check and check.get("observation")),
               "camera_hidden": (phase.value, kind) in hidden and check is None}
        frame = frames.get(phase.value)
        if check is not None and frame is not None:
            values = []
            for f in (frame - 1, frame, frame + 1):
                record = by_index.get(int(f))
                c = build_body_check(kind, phase, record, context)[0] if record is not None else None
                if c is not None and key in c["raw"]:
                    values.append(float(c["raw"][key]))
            row["value"] = float(check["raw"][key])
            if len(values) == 3:
                row["noise"] = (max(values) - min(values)) / 2
        out[cid] = row
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    rows = []
    for run in args.runs:
        for folder in sorted(Path(run).glob("*")):
            if (folder / "analysis.json").is_file() and (folder / "pose_records.pkl").is_file():
                rows.append((f"{Path(run).parent.name}/{folder.name}", readings(folder)))
    summary = {}
    for cid in CANDIDATES:
        items = [r[cid] for _, r in rows]
        noise = [i["noise"] for i in items if "noise" in i]
        values = [i["value"] for i in items if "value" in i]
        levels = {}
        for i in items:
            if i["shown"]:
                levels[i["level"]] = levels.get(i["level"], 0) + 1
        summary[cid] = {
            "swings": len(items), "shown": sum(i["shown"] for i in items), "levels": levels,
            "observations_marked": sum(i["observation"] for i in items),
            "camera_hidden": sum(i["camera_hidden"] for i in items),
            "noise_median": round(float(np.median(noise)), 2) if noise else None,
            "noise_p75": round(float(np.percentile(noise, 75)), 2) if noise else None,
            "noise_p90": round(float(np.percentile(noise, 90)), 2) if noise else None,
            "value_p10_p90": [round(float(np.percentile(values, 10)), 1), round(float(np.percentile(values, 90)), 1)]
            if values else None,
        }
    result = {"swings": len(rows), "summary": summary}
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
