"""Offline replay of the Address-consensus fix on stored production analyses (read-only).

    python scripts/replay_address_consensus.py <folder of analysis.json files> [--out report.json]

Production deletes the original upload after analysis, so the exact production input cannot be
re-run. But every analysis made with the ``next`` engine stores the club readings it used around
Address (``engine.plane_frames.address``: Address +-2 frames, level and shaft-line angle). This
replays ``swingguru.club.address_reference`` on exactly those readings and, where it would replace
the Address reference, recomputes the two plane checks with the engine's own functions (change =
shaft line angle minus the new Address line angle, verdict via ``verdicts.attach_verdicts``) and the
Swing Score with ``scoring.score_swing`` (v3, unchanged).

Limits (stated in the output): the stored angles are line angles folded to 0-90 deg, which is enough
for agreement between frames at a still Address; the hands-still gate cannot be applied (pose is not
stored), so every Address +-2 frame is treated as still; the plane-check confidence levels and the
camera alignment of the reference drawing are kept as stored.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.club.address_reference import address_reference  # noqa: E402
from swingguru.club.tracker import FrameShaft  # noqa: E402
from swingguru.scoring import score_swing  # noqa: E402
from swingguru.verdicts import attach_verdicts  # noqa: E402


class StoredTrack:
    def __init__(self, rows):
        self.frames = {}
        for r in rows:
            if r["source"] != "track":
                continue
            if r["line_deg"] is None:
                self.frames[r["frame"]] = FrameShaft(r["frame"], 0.0, "unavailable", r["level"], 0.0)
            else:
                self.frames[r["frame"]] = FrameShaft(r["frame"], 0.0, "detected", r["level"], 0.8,
                                                     angle_deg=float(r["line_deg"]), grip=np.zeros(2), tip=np.ones(2),
                                                     interpolated=not r["usable"] and r["level"] in ("HIGH", "MEDIUM"))

    def at(self, f):
        return self.frames.get(f)


def still_records(frames):
    return [SimpleNamespace(index=f, points=SimpleNamespace(pixels={"left_wrist": np.zeros(2)})) for f in frames]


def replay(analysis: dict) -> dict | None:
    block = ((analysis.get("engine") or {}).get("plane_frames") or {}).get("address")
    if not block:
        return None
    centre = int(block["frame"])
    track = StoredTrack(block["frames"])
    overrides = {r["frame"]: track.frames.get(r["frame"]) for r in block["frames"] if r["source"] == "override"}
    ref = address_reference(track, centre, still_records(range(centre - 2, centre + 3)), torso_px=1.0,
                            overrides=None if not overrides else None)
    out = {"status": ref.status, "address_line": None if track.at(centre) is None else track.at(centre).angle_deg,
           "consensus": ref.diagnostics.get("consensus_line_deg"), "agreeing": ref.diagnostics.get("agreeing_frames")}
    before = score_swing(analysis, "swing_score_v3")
    out["before"] = {"total": before["total"], "measured": before["measured"]}
    if not ref.changed:
        out["after"] = out["before"]
        return out
    new_line = float(ref.shaft.angle_deg)
    changed = copy.deepcopy(analysis)
    planes = {}
    for phase in changed["phases"]:
        for check in phase["checks"]:
            if check["kind"] == "shaft" and phase["phase"] in ("mid_back", "downswing"):
                old = check["raw"].get("change_vs_address_deg")
                check["raw"]["change_vs_address_deg"] = round(float(check["raw"]["shaft_from_horizontal_deg"]) - new_line, 3)
                check.pop("verdict", None)
                planes[phase["phase"]] = {"before": old, "after": check["raw"]["change_vs_address_deg"]}
    attach_verdicts(changed)
    for phase in changed["phases"]:
        for check in phase["checks"]:
            if phase["phase"] in planes and check["kind"] == "shaft":
                planes[phase["phase"]]["verdict"] = check["verdict"]["label"]
                planes[phase["phase"]]["scored"] = bool(check["guided"])
    after = score_swing(changed, "swing_score_v3")
    out.update({"planes": planes, "after": {"total": after["total"], "measured": after["measured"]}})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    results = {}
    for f in sorted(Path(args.folder).glob("*.json")):
        try:
            analysis = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if not isinstance(analysis, dict) or "phases" not in analysis:
            continue
        r = replay(analysis)
        if r is not None:
            results[f.stem[:8]] = r
    summary = {}
    for r in results.values():
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    text = json.dumps({"analyses": len(results), "decisions": summary, "swings": results}, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
