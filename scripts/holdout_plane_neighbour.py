"""Hold-out accuracy of the nearest-clear-frame plane rule (read-only).

    python scripts/holdout_plane_neighbour.py <run dir or folder of analysis.json> [...]

For every Mid-Back / Mid-Down moment whose own frame *was* clearly detected (HIGH/MEDIUM, direct),
the moment reading is hidden and the plane_neighbour rule is applied to the stored neighbouring
detections (``engine.plane_frames``: moment +-2). The recovered reading is then compared with the
real one: that difference is the error the rule adds when it substitutes a neighbour.

Uses the engine's own rule (``swingguru.club.plane_neighbour``) on the stored rows; the grip-at-hands
check cannot be applied (pose is not stored), so it is counted as passed (conservative for error).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.club.plane_neighbour import nearest_clear_frame  # noqa: E402
from swingguru.club.tracker import FrameShaft  # noqa: E402


class Rows:
    def __init__(self, rows, hide=None):
        self.frames = {}
        for r in rows:
            if r["source"] != "track" or r["frame"] == hide:
                continue
            if r["line_deg"] is None:
                self.frames[r["frame"]] = FrameShaft(r["frame"], 0.0, "unavailable", r["level"], 0.0)
            else:
                self.frames[r["frame"]] = FrameShaft(r["frame"], 0.0, "detected", r["level"], 0.8,
                                                     angle_deg=float(r["line_deg"]), grip=np.zeros(2), tip=np.ones(2),
                                                     interpolated=not r["usable"] and r["level"] in ("HIGH", "MEDIUM"))

    def at(self, f):
        return self.frames.get(f)


def analyses(paths):
    for p in paths:
        p = Path(p)
        files = sorted(p.glob("*/analysis.json")) or sorted(p.glob("*.json"))
        for f in files:
            try:
                a = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(a, dict) and (a.get("engine") or {}).get("plane_frames"):
                yield f, a


def main(argv=None) -> int:
    paths = (argv or sys.argv[1:])
    out = {"mid_back": [], "downswing": []}
    tried = {"mid_back": 0, "downswing": 0}
    for f, a in analyses(paths):
        for phase in ("mid_back", "downswing"):
            block = a["engine"]["plane_frames"].get(phase)
            if not block:
                continue
            centre = int(block["frame"])
            real = [r for r in block["frames"] if r["frame"] == centre and r["source"] == "track" and r["usable"]]
            if not real:
                continue
            tried[phase] += 1
            track = Rows(block["frames"], hide=centre)
            still = [SimpleNamespace(index=i, points=SimpleNamespace(pixels={"left_wrist": np.zeros(2)}))
                     for i in range(centre - 3, centre + 4)]
            near, diag = nearest_clear_frame(track, centre, phase, still, torso_px=1.0)
            if near is None:
                continue
            d = abs(float(near.angle_deg) - float(real[0]["line_deg"]))
            out[phase].append(round(min(d, 180 - d), 2))
    summary = {}
    for phase, errs in out.items():
        e = np.asarray(errs) if errs else np.asarray([np.nan])
        summary[phase] = {"moments_with_a_real_reading": tried[phase], "rule_would_recover": len(errs),
                          "error_median_deg": round(float(np.nanmedian(e)), 2), "error_p75_deg": round(float(np.nanpercentile(e, 75)), 2),
                          "error_p90_deg": round(float(np.nanpercentile(e, 90)), 2), "error_max_deg": round(float(np.nanmax(e)), 2),
                          "within_noise_share": round(float(np.mean(e <= (7.0 if phase == 'mid_back' else 4.0))), 2) if errs else None}
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
