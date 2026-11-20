"""Address shaft reference by neighbouring-frame consensus (engine profile ``candidate`` only).

Every plane check is measured against the shaft at Address. Today that reference is the club
detection on the single Address frame, so one false line there (for example a horizontal edge at
hand height, found at HIGH confidence on production swing bce2ca87) throws both plane readings
outside the plausible range and they are refused.

Address is a still moment, so the shaft cannot genuinely change between neighbouring frames while
the hands stay put. This module looks at the directly detected shafts on the Address frame +-2 and:

* keeps the Address frame's own detection whenever it agrees with them (the usual case: nothing
  changes);
* replaces it only when it is an isolated outlier against a clear majority of agreeing
  neighbours, using the real detection from the nearest agreeing frame (nothing is averaged or
  synthesised);
* recovers a missing or unusable Address detection the same way, only from such a majority;
* otherwise keeps today's behaviour exactly.

Only directly detected HIGH / MEDIUM frames vote (never interpolated ones), and only frames whose
hands stayed within ``STILL_TORSO`` torso lengths of the Address hands, so a neighbour that has
already started the takeaway cannot outvote the real reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .tracker import LEVEL_HIGH, LEVEL_MEDIUM

RADIUS = 2               # frames either side of Address that may vote
TOLERANCE_DEG = 5.0      # two lines within this (orientation, mod 180) agree
MIN_AGREE = 3            # at least this many agreeing frames (Address included when it agrees)
STILL_TORSO = 0.15       # a voting frame's hands within this share of the torso length of Address


@dataclass(frozen=True)
class AddressReference:
    shaft: object | None       # the FrameShaft to use as the Address reference (None = none)
    status: str                # kept | centre_agrees | outlier_replaced | missing_recovered | no_consensus
    diagnostics: dict

    @property
    def changed(self) -> bool:
        return self.status in ("outlier_replaced", "missing_recovered")


def _orientation(angle_deg: float) -> float:
    return float(angle_deg) % 180.0


def _diff(a: float, b: float) -> float:
    """Smallest difference between two line orientations (degrees, 0..90)."""

    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def _usable(shaft) -> bool:
    return (shaft is not None and shaft.available and not shaft.interpolated
            and shaft.level in (LEVEL_HIGH, LEVEL_MEDIUM))


def _hands(record) -> np.ndarray | None:
    points = getattr(record, "points", None)
    pixels = getattr(points, "pixels", None) or {}
    wrists = [np.asarray(pixels[name], float) for name in ("left_wrist", "right_wrist") if pixels.get(name) is not None]
    return np.mean(wrists, axis=0) if wrists else None


def address_reference(track, address_frame: int | None, records: Sequence[object], torso_px: float,
                      overrides: Mapping[int, object] | None = None) -> AddressReference:
    """The Address shaft reference (see the module docstring for the rules)."""

    centre_shaft = None
    if address_frame is not None:
        centre_shaft = (overrides or {}).get(int(address_frame)) or (track.at(int(address_frame)) if track else None)
    if track is None or address_frame is None:
        return AddressReference(centre_shaft, "kept", {"reason": "no track or no Address moment"})
    centre = int(address_frame)
    by_index = {int(getattr(r, "index")): r for r in records}
    centre_hands = _hands(by_index.get(centre))
    rows, votes = [], []
    for f in range(centre - RADIUS, centre + RADIUS + 1):
        shaft = centre_shaft if f == centre else track.at(f)
        row = {"frame": f, "level": None if shaft is None else shaft.level,
               "line_deg": None if shaft is None or shaft.angle_deg is None else round(_orientation(shaft.angle_deg), 2)}
        usable = _usable(shaft)
        still = True
        if f != centre:
            hands = _hands(by_index.get(f))
            still = (centre_hands is not None and hands is not None and torso_px > 0
                     and float(np.linalg.norm(hands - centre_hands)) <= STILL_TORSO * torso_px)
        row.update({"usable": usable, "still": still})
        rows.append(row)
        if usable and still:
            votes.append((f, shaft, _orientation(shaft.angle_deg)))
    diagnostics = {"frames": rows, "tolerance_deg": TOLERANCE_DEG, "min_agree": MIN_AGREE, "radius": RADIUS}
    if not votes:
        return AddressReference(centre_shaft, "no_consensus", {**diagnostics, "reason": "no usable still frames"})

    # The largest group of mutually agreeing lines (seeded at each vote; ties prefer the group that
    # contains Address, then the nearer seed).
    best = None
    for f, _, line in votes:
        group = [v for v in votes if _diff(v[2], line) <= TOLERANCE_DEG]
        # Every member must agree with every other (no chaining across the tolerance).
        group = [v for v in group if all(_diff(v[2], w[2]) <= TOLERANCE_DEG for w in group)]
        key = (len(group), any(v[0] == centre for v in group), -abs(f - centre))
        if best is None or key > best[0]:
            best = (key, group)
    group = best[1]
    majority = len(group) >= MIN_AGREE and len(group) * 2 > len(votes)
    lines = [v[2] for v in group]
    ref = lines[0]
    median = (ref + float(np.median([((x - ref + 90.0) % 180.0) - 90.0 for x in lines]))) % 180.0
    diagnostics.update({"agreeing_frames": [v[0] for v in group], "consensus_line_deg": round(median, 2),
                        "votes": len(votes)})
    centre_usable = _usable(centre_shaft)
    if not majority:
        return AddressReference(centre_shaft, "no_consensus", {**diagnostics, "reason": "no clear majority"})
    if centre_usable and _diff(_orientation(centre_shaft.angle_deg), median) <= TOLERANCE_DEG:
        return AddressReference(centre_shaft, "centre_agrees", diagnostics)
    # Address is an outlier (or missing): the real detection of the nearest agreeing frame.
    chosen = min(group, key=lambda v: (abs(v[0] - centre), -float(v[1].confidence)))
    status = "outlier_replaced" if centre_usable else "missing_recovered"
    diagnostics.update({"chosen_frame": chosen[0],
                        "address_line_deg": None if centre_shaft is None or centre_shaft.angle_deg is None
                        else round(_orientation(centre_shaft.angle_deg), 2)})
    return AddressReference(chosen[1], status, diagnostics)


__all__ = ["AddressReference", "MIN_AGREE", "RADIUS", "STILL_TORSO", "TOLERANCE_DEG", "address_reference"]
