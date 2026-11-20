"""Mid-Back plane study (read-only): today's +-5 deg rule vs the shaft-to-shoulder zone, and frame sensitivity.

    .venv\\Scripts\\python.exe scripts\\plane_zone_study.py [--run candidate] [--out docs/agent-work/analysis-v2/PLANE_ZONE.md]

Changes nothing in the engine or the score. For every swing with a measured Mid-Back plane:

* **today**: the shaft line at Mid-Back vs the Address shaft line; on plane within +-5 deg
  (slightly / clearly steep or flat beyond that: 5-12 / 12+);
* **zone**: on plane anywhere from ZONE_MARGIN below the Address shaft line to ZONE_MARGIN above the
  Address shoulder line (ball to base of the neck); steeper is steep, flatter is flat.

Both are read at the Mid-Back moment and at +-1 / +-2 frames (the engine's own per-frame club
readings, plane_frames), to show how much the verdict depends on the exact frame. Ground truth does not
exist yet, so "plausible" is judged by proxies stated in the output, not by accuracy.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.pose_cache import load_records_any  # noqa: E402

ZONE_MARGIN = 3.0
SETS = (("P", "var/qa/validation_prod"), ("N", "var/qa/validation_new"), ("R", "var/qa/validation_1_0_1"))
# Tour / elite clips, classified by context in the head-movement study (var/qa/head_components.py).
TOUR = {"P04", "P05", "P06", "P09", "P14", "P15", "P16", "P17", "P22", "P23", "P24", "P28", "P29", "P39", "P43",
        "P44", "P45", "P46", "P47", "N02", "N03"}


def today(change: float) -> str:
    a = abs(change)
    if a <= 5:
        return "on"
    side = "steep" if change > 0 else "flat"
    return ("slightly " if a <= 12 else "") + side


def zone(line: float, shaft_ref: float, shoulder: float) -> str:
    if line > shoulder + ZONE_MARGIN:
        return "steep"
    if line < shaft_ref - ZONE_MARGIN:
        return "flat"
    return "on"


def coarse(v: str) -> str:
    return v.split()[-1]


def address_lines(a: dict, recs: dict) -> tuple[float, float] | None:
    """Address shaft line and shoulder line (ball to base of neck), degrees from horizontal."""

    addr = next((p for p in a["phases"] if p["phase"] == "address"), None)
    if addr is None or addr.get("frame_index") is None:
        return None
    shaft = next((c for c in addr["checks"] if c["kind"] == "shaft"), None)
    if shaft is None:
        return None
    seg = next((g for g in shaft["geometry"] if g.get("role") == "shaft"), None)
    rec = recs.get(int(addr["frame_index"]))
    if seg is None or rec is None:
        return None
    w, h = a["width"], a["height"]
    p0, p1 = (np.array([x * w, y * h]) for x, y in seg["points"])
    ball = p0 if p0[1] > p1[1] else p1
    px = rec.points.pixels
    ls, rs = px.get("left_shoulder"), px.get("right_shoulder")
    if ls is None or rs is None:
        return None
    neck = (np.asarray(ls, float) + np.asarray(rs, float)) / 2
    v = neck - ball
    shoulder = float(np.degrees(np.arctan2(-v[1], abs(v[0]))))
    return float(shaft["raw"]["shaft_from_horizontal_deg"]), shoulder


def readings(a: dict) -> dict[int, float]:
    """Mid-Back shaft line by frame offset, from the engine's plane_frames (override preferred, as measured)."""

    block = ((a.get("engine") or {}).get("plane_frames") or {}).get("mid_back") or {}
    centre = block.get("frame")
    out = {}
    if centre is None:
        return out
    for off in (-2, -1, 0, 1, 2):
        rows = [r for r in block.get("frames", []) if r["frame"] == centre + off and r.get("usable")]
        pick = next((r for r in rows if r["source"] == "override"), rows[0] if rows else None)
        if pick is not None:
            out[off] = float(pick["line_deg"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", default="candidate")
    parser.add_argument("--out", default=str(ROOT / "docs" / "agent-work" / "analysis-v2" / "PLANE_ZONE.md"))
    args = parser.parse_args(argv)

    rows = []
    for tag, root in SETS:
        for d in sorted((ROOT / root / args.run).glob("*")):
            if not (d / "analysis.json").exists() or not (d / "pose_records.pkl").exists():
                continue
            a = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
            mid = next((c for p in a["phases"] if p["phase"] == "mid_back" for c in p["checks"] if c["kind"] == "shaft"), None)
            if mid is None or "change_vs_address_deg" not in mid["raw"]:
                continue
            recs = {int(r.index): r for r in load_records_any(d / "pose_records.pkl")}
            lines = address_lines(a, recs)
            if lines is None:
                continue
            line = float(mid["raw"]["shaft_from_horizontal_deg"])
            ref = line - float(mid["raw"]["change_vs_address_deg"])   # the reference the check used
            shoulder = lines[1]
            by_off = readings(a)
            rows.append({
                "swing": tag + d.name, "tour": tag + d.name in TOUR, "line": line, "ref": ref, "shoulder": shoulder,
                "gap": shoulder - ref, "today": today(line - ref), "zone": zone(line, ref, shoulder),
                "level": mid.get("level"),
                "today_by_off": {o: today(v - ref) for o, v in by_off.items()},
                "zone_by_off": {o: zone(v, ref, shoulder) for o, v in by_off.items()},
                "lines_by_off": by_off,
            })

    n = len(rows)
    out = [f"# Mid-Back plane: today's ±5° rule vs the shaft-to-shoulder zone\n",
           f"Run `{args.run}`, {n} swings with a measured Mid-Back plane and an Address shoulder line. "
           f"Zone = from {ZONE_MARGIN:.0f}° below the Address shaft line to {ZONE_MARGIN:.0f}° above the Address "
           "shoulder line (ball to base of the neck). Nothing in the engine or score was changed.\n"]

    def dist(key):
        c = {}
        for r in rows:
            c[coarse(r[key])] = c.get(coarse(r[key]), 0) + 1
        return c

    gaps = [r["gap"] for r in rows]
    out.append(f"Shoulder line sits a median {st.median(gaps):.1f}° above the Address shaft line "
               f"(p10 {sorted(gaps)[n // 10]:.1f}°, p90 {sorted(gaps)[9 * n // 10]:.1f}°).\n")
    out.append("## Verdicts\n")
    out.append("| | On plane | Steep | Flat |\n|---|---|---|---|")
    for key, name in (("today", "Today (±5° of the shaft line)"), ("zone", "Shaft-to-shoulder zone")):
        c = dist(key)
        out.append(f"| {name} | {c.get('on', 0)} | {c.get('steep', 0)} | {c.get('flat', 0)} |")
    changed = [r for r in rows if coarse(r["today"]) != coarse(r["zone"])]
    out.append(f"\n**{len(changed)} of {n} swings change verdict** (on / steep / flat).\n")
    pairs = {}
    for r in changed:
        k = f"{coarse(r['today'])} → {coarse(r['zone'])}"
        pairs[k] = pairs.get(k, 0) + 1
    for k, v in sorted(pairs.items(), key=lambda x: -x[1]):
        out.append(f"* {k}: {v}")

    out.append("\n## Frame sensitivity (verdict at ±1 / ±2 frames vs the moment frame)\n")
    out.append("| | Same verdict at ±1 frame | Same at ±2 frames |\n|---|---|---|")
    for key, name in (("today", "Today"), ("zone", "Zone")):
        same1 = tot1 = same2 = tot2 = 0
        for r in rows:
            by = r[f"{key}_by_off"]
            if 0 not in by:
                continue
            for o in (-1, 1):
                if o in by:
                    tot1 += 1
                    same1 += coarse(by[o]) == coarse(by[0])
            for o in (-2, 2):
                if o in by:
                    tot2 += 1
                    same2 += coarse(by[o]) == coarse(by[0])
        out.append(f"| {name} | {same1}/{tot1} ({100 * same1 / max(tot1, 1):.0f}%) | {same2}/{tot2} ({100 * same2 / max(tot2, 1):.0f}%) |")
    steps = [abs(r["lines_by_off"][1] - r["lines_by_off"][0]) for r in rows if 0 in r["lines_by_off"] and 1 in r["lines_by_off"]]
    if steps:
        out.append(f"\nThe shaft line changes a median {st.median(steps):.1f}° per frame at Mid-Back "
                   f"(p90 {sorted(steps)[int(0.9 * (len(steps) - 1))]:.1f}°).\n")

    out.append("## Plausibility proxies (no ground truth yet)\n")
    for group, pick in (("Tour / elite clips", lambda r: r["tour"]), ("Other clips", lambda r: not r["tour"])):
        g = [r for r in rows if pick(r)]
        if not g:
            continue
        t = sum(coarse(r["today"]) == "on" for r in g)
        z = sum(coarse(r["zone"]) == "on" for r in g)
        out.append(f"* {group} ({len(g)}): on plane today {t} ({100 * t / len(g):.0f}%), zone {z} ({100 * z / len(g):.0f}%)")
    # Is it the golfer's own shoulder line that helps, or just a wider / steeper-leaning band?
    gap = st.median(gaps)
    width = (gap + 2 * ZONE_MARGIN) / 2
    controls = (
        (f"Symmetric ±{width:.0f}° (the median zone's width)", lambda r: abs(r["line"] - r["ref"]) <= width),
        (f"Fixed −{ZONE_MARGIN:.0f}° to +{gap + ZONE_MARGIN:.0f}° (the median zone, same for everyone)",
         lambda r: -ZONE_MARGIN <= r["line"] - r["ref"] <= gap + ZONE_MARGIN),
        ("Personal zone (each golfer's own shoulder line)", lambda r: coarse(r["zone"]) == "on"),
    )
    out.append("\nWidth controls (on plane, tour vs other):\n")
    for name, pick in controls:
        t = [r for r in rows if r["tour"]]
        o = [r for r in rows if not r["tour"]]
        out.append(f"* {name}: tour {sum(map(pick, t))}/{len(t)}, other {sum(map(pick, o))}/{len(o)}")
    out.append("")
    out.append("## Per swing\n")
    out.append("| Swing | Tour | Shaft ref | Shoulder | Mid-Back line | Today | Zone | Level |\n|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: r["swing"]):
        out.append(f"| {r['swing']} | {'yes' if r['tour'] else ''} | {r['ref']:.0f}° | {r['shoulder']:.0f}° | {r['line']:.0f}° | "
                   f"{r['today']} | {r['zone']} | {r['level']} |")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    (Path(args.out).with_suffix(".json")).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print("\n".join(out[:40]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
