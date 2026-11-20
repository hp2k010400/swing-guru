"""Extra replay observations: measured, shown, never scored (engine profile ``candidate`` only).

The engine already measures more than the eight Swing Score checkpoints. A few of those extra
readings are reliable enough to show during the replay as *observations*. They:

* are never part of the Swing Score (``swingguru.scoring`` reads only the eight checkpoints; it never
  looks at this block);
* carry **no verdict**: no good / attention / problem colour and no target band, because none of
  these has a validated threshold at this moment (the Top posture and Impact head bands were set for
  those moments only). The replay shows the measured value and a plain description;
* are only marked at HIGH confidence, where the measurement survives camera movement (posture is an
  angle; head movement is already withheld when the camera moved and the correction could not be
  confirmed), and only when the reading is **stable on this swing**: re-measured on the frame either
  side of the moment, it moves by no more than ``MAX_FRAME_NOISE`` (half-range). A reading that
  depends on which exact frame was picked is not shown.

Evidence (65 swings, docs/agent-work/overlay-v3/PHASE_REPORT.md), frame noise median / 90th pct:
Mid-Back posture 0.63 / 1.29 deg, Top head 0.34 / 0.95 %, Mid-Down posture 1.9 / 3.2 deg (hence the
per-swing gate), Impact posture 2.0 / 4.4 deg (not offered: the noisiest, and standing up through
impact is normal, so a bare number would invite misreading).
"""

from __future__ import annotations

from typing import Callable, Mapping

OBSERVATION_VERSION = 1
NOTE = "Observation. Not part of your Swing Score."

# (phase, kind) -> golfer-facing name. Order = order within a moment.
OBSERVATIONS: Mapping[tuple[str, str], str] = {
    ("mid_back", "posture"): "Posture",
    ("top", "head"): "Head position",
    ("downswing", "posture"): "Posture",
}
LEVELS = ("HIGH",)
# Largest frame-to-frame half-range (check units) a reading may have on this swing to be shown.
MAX_FRAME_NOISE: Mapping[str, float] = {"posture": 3.0, "head": 1.0}

Stability = Callable[[str, str], "float | None"]


def mark_observations(analysis: dict, stability: Stability | None = None) -> dict:
    """Mark the reliable extra readings as observations (additive; never touches a value or verdict).

    ``stability(phase, kind)`` returns the reading's half-range over the moment frame +-1 (None when it
    cannot be measured, which withholds the observation). Without it nothing is marked.
    """

    marked, withheld = [], []
    for phase in analysis.get("phases", []):
        for check in phase.get("checks", []):
            name = OBSERVATIONS.get((phase["phase"], check.get("kind")))
            if name is None or check.get("guided") or check.get("level") not in LEVELS:
                continue
            noise = stability(phase["phase"], check["kind"]) if stability is not None else None
            if noise is None or noise > MAX_FRAME_NOISE[check["kind"]]:
                withheld.append({"id": check["id"], "frame_noise": None if noise is None else round(float(noise), 3)})
                continue
            check["observation"] = {"version": OBSERVATION_VERSION, "name": name, "scored": False,
                                    "label": check.get("callout") or check.get("value"), "note": NOTE,
                                    "frame_noise": round(float(noise), 3)}
            marked.append(check["id"])
    analysis["observations"] = {"version": OBSERVATION_VERSION, "checks": marked, "withheld_unstable": withheld,
                                "max_frame_noise": dict(MAX_FRAME_NOISE),
                                "note": "Shown in the replay only; never scored and never given a verdict."}
    return analysis


__all__ = ["LEVELS", "MAX_FRAME_NOISE", "NOTE", "OBSERVATIONS", "OBSERVATION_VERSION", "mark_observations"]
