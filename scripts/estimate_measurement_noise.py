"""Measurement noise per scored check, from the reference swings themselves.

    python scripts/estimate_measurement_noise.py [--run next]

For every swing analysed by ``scripts/validate_analysis.py`` (default: the ``next`` profile, i.e.
with the C6 stature normaliser), each scored check is re-measured with the engine's own code at its
moment frame and one frame either side (frame choice at 30 fps is uncertain by about one frame -
the mirrored-swing test moved Impact by one frame). A check's noise is the half-range of those three
readings; the per-check figure is the median over swings (with the 75th percentile reported).

Plane: the club at the plane moments +-1 frame, stored by the ``next`` engine profile in
``engine.plane_frames`` (both Mid-Back and Mid-Down are measured; nothing is borrowed).

Early extension is a silhouette measured on key frames only, so its frame sensitivity is estimated
from the hip-centre's movement toward the ball (same body point, same normaliser) - a proxy, flagged
as such in the output.

Writes ``docs/agent-work/analysis-quality/measurement_noise.json``; Swing Score v3 reads its numbers
from ``swingguru.scoring.config`` (copied there by hand, so a noise change is a reviewed, versioned
scoring change).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.analysis_bundle import build_body_check, shaft_line_angle, swing_context  # noqa: E402
from swingguru.engine_profile import profile, rebind_sides  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import Phase  # noqa: E402
from swingguru.pipeline import apply_reference  # noqa: E402
from swingguru.pose_cache import load_records_any  # noqa: E402

VALIDATION = ROOT / "var" / "qa" / "validation"
OUT = ROOT / "docs" / "agent-work" / "analysis-quality" / "measurement_noise.json"
BODY = {  # check id -> (kind, phase, raw key)
    "address.torso_angle": ("torso_angle", Phase.ADDRESS, "torso_inclination_deg"),
    "address.trail_knee": ("trail_knee", Phase.ADDRESS, "knee_flex_deg"),
    "address.arm_hang": ("arm_hang", Phase.ADDRESS, "arm_angle_deg"),
    "top.posture": ("posture", Phase.TOP, "torso_angle_change_deg"),
    "impact.head": ("head", Phase.IMPACT, "movement_pct_height"),
}


def readings(folder: Path) -> dict[str, list[float]]:
    analysis = json.loads((folder / "analysis.json").read_text(encoding="utf-8"))
    records = load_records_any(folder / "pose_records.pkl")
    by_index = {int(r.index): r for r in records}
    frames = {p["phase"]: p.get("frame_index") for p in analysis["phases"]}
    address = frames.get("address")
    prof = profile("next")
    handed = (analysis.get("handedness") or {}).get("value", "right")
    if handed != "right":
        rebind_sides(records, handed)
    apply_reference(records, address, config=MeasurementConfig(handedness=handed))
    context = swing_context(records, address, "rtmw", prof)
    out: dict[str, list[float]] = {}
    for check_id, (kind, phase, key) in BODY.items():
        frame = frames.get(phase.value)
        if frame is None:
            continue
        values = []
        for f in (frame - 1, frame, frame + 1):
            record = by_index.get(int(f))
            check = build_body_check(kind, phase, record, context)[0] if record is not None else None
            if check is not None and key in check["raw"]:
                values.append(float(check["raw"][key]))
        if len(values) == 3:
            out[check_id] = values
    # Extension proxy: hip-centre movement toward the ball, % of stature.
    impact = frames.get("impact")
    a_rec = by_index.get(int(address)) if address is not None else None
    if impact is not None and a_rec is not None and context.body_height_px and context.toward_ball_known:
        base = np.asarray(a_rec.points.pixels.get("hip_mid"), float)
        values = []
        for f in (impact - 1, impact, impact + 1):
            r = by_index.get(int(f))
            p = None if r is None else r.points.pixels.get("hip_mid")
            if p is not None:
                values.append(100.0 * float(np.asarray(p, float)[0] - base[0]) * context.toward_ball / context.body_height_px)
        if len(values) == 3:
            out["impact.pelvis"] = values
    # Plane: the club at each plane moment +-1 frame (engine.plane_frames, next profile). The Address
    # reference is the Address check's angle; within a frame the plane check's own source wins
    # (OWLv2 override, else the track), and neighbours use the same source when they have it.
    planes = (analysis.get("engine") or {}).get("plane_frames") or {}
    ref = None
    for phase_out in analysis["phases"]:
        if phase_out["phase"] == "address":
            for check in phase_out["checks"]:
                if check["kind"] == "shaft":
                    ref = shaft_line_angle(float(check["raw"]["shaft_direction_deg"]))
    for check_id, phase in (("mid_back.shaft", "mid_back"), ("downswing.shaft", "downswing")):
        block = planes.get(phase)
        if not block or ref is None:
            continue
        centre = block["frame"]
        usable = [r for r in block["frames"] if r["usable"] and r["line_deg"] is not None]
        # One-frame change at the moment (|angle(f) - angle(f-1)|, else f+1), from one source. Mid-Down
        # sits a frame before Impact, where the club is usually blurred, so f+1 is rarely readable;
        # for locally steady motion this equals the half-range of f-1..f+1 used for the other checks.
        for source in ("track", "override"):
            rows = {r["frame"]: r["line_deg"] for r in usable if r["source"] == source}
            if centre in rows:
                neighbour = centre - 1 if (centre - 1) in rows else (centre + 1 if (centre + 1) in rows else None)
                if neighbour is not None:
                    change = abs(rows[centre] - rows[neighbour])
                    out[check_id] = [rows[centre] - ref - change, rows[centre] - ref, rows[centre] - ref + change]
                    break
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="next")
    args = parser.parse_args(argv)
    per_check: dict[str, list[float]] = {}
    per_swing: dict[str, dict] = {}
    for folder in sorted((VALIDATION / args.run).glob("*")):
        if not (folder / "analysis.json").is_file():
            continue
        got = readings(folder)
        per_swing[folder.name] = {k: [round(v, 3) for v in vals] for k, vals in got.items()}
        for k, vals in got.items():
            per_check.setdefault(k, []).append((max(vals) - min(vals)) / 2.0)
    summary = {}
    for k, half_ranges in sorted(per_check.items()):
        arr = np.asarray(half_ranges)
        summary[k] = {"swings": len(arr), "sigma_median": round(float(np.median(arr)), 3),
                      "sigma_p75": round(float(np.percentile(arr, 75)), 3), "max": round(float(arr.max()), 3),
                      "proxy": k == "impact.pelvis"}
    OUT.write_text(json.dumps({"method": "half-range of the reading at frame-1, frame, frame+1 (next profile)",
                               "summary": summary, "per_swing": per_swing}, indent=2), encoding="utf-8")
    for k, v in summary.items():
        print(f"{k:22} n={v['swings']:2} sigma median {v['sigma_median']:6.2f}  p75 {v['sigma_p75']:6.2f}  max {v['max']:6.2f}"
              + ("  (proxy)" if v["proxy"] else ""))
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
