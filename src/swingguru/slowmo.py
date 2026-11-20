"""Slow-motion clips (engine profile ``candidate``, fix ``slowmo``).

The swing finder and the moment detector both judge a swing by its *timing*: hands still for a
moment, a backswing of at most ~1.5 s, a downswing of at most ~0.8 s. A slow-motion clip that was
shared or exported as normal-speed video (iPhone Slo-Mo at 120/240 fps baked into a 30 fps file,
or a slowed edit) stretches the swing 4-8x, so the fast downswing never looks fast and the clip
reads as "no swing found" - the cause reproduced for the two Driver failures of 2026-10-13 (a
known-good swing slowed 3x gives exactly their production signature).

Nothing about the *measurements* depends on time: every check is an angle or a share of body
height on one frame. So when no swing is found at the clip's own speed, the finder is retried on a
rescaled clock (the clip treated as slowed by ``factor``); the first factor at which a complete
swing with a human tempo appears is used. Real-time video is never altered: the replay plays the
clip as recorded, and every measurement is taken on the same frames as before.

Guards: it only runs after the normal-speed search found nothing; factors are limited to
``FACTORS``; the moment set must be complete enough and its rescaled tempo plausible
(``BACKSWING_S`` / ``DOWNSWING_S``), otherwise the clip is still refused.
"""

from __future__ import annotations

from typing import Callable, Sequence

FACTORS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
# Tempo as this engine times real, normal-speed swings (68 clips, 2026-10-28): Takeaway -> Top median
# 0.37 s (5-95 % 0.27-0.85), Top -> Impact median 0.27 s (0.20-1.21). A factor is chosen by how close
# its rescaled tempo is to these medians (log scale); only clearly non-human tempos are refused.
TYPICAL_BACKSWING_S = 0.37
TYPICAL_DOWNSWING_S = 0.27
BACKSWING_S = (0.18, 1.4)
DOWNSWING_S = (0.12, 1.3)
MIN_LOCATED = 5               # of the six analysed moments
NOTICE = "This looks like a slow-motion video, so we analysed the swing at its real speed."


class ScaledRecord:
    """A pose record seen on a rescaled clock (``timestamp_ms / factor``); everything else unchanged."""

    __slots__ = ("_record", "timestamp_ms")

    def __init__(self, record: object, factor: float) -> None:
        self._record = record
        self.timestamp_ms = float(getattr(record, "timestamp_ms")) / factor

    def __getattr__(self, name: str):
        return getattr(self._record, name)


def scaled(records: Sequence[object], factor: float) -> list:
    return list(records) if factor == 1.0 else [ScaledRecord(r, factor) for r in records]


def tempo(moments, records: Sequence[object], factor: float) -> dict | None:
    """Rescaled backswing / downswing durations (s) from a moment set, or None when not measurable."""

    from .phases import Phase

    frames = moments.frames()
    by_index = {int(getattr(r, "index")): float(getattr(r, "timestamp_ms")) / 1000.0 / factor for r in records}
    start = frames.get(Phase.TAKEAWAY) or frames.get(Phase.ADDRESS)
    top, impact = frames.get(Phase.TOP), frames.get(Phase.IMPACT)
    if start is None or top is None or impact is None:
        return None
    try:
        return {"backswing_s": round(by_index[int(top)] - by_index[int(start)], 3),
                "downswing_s": round(by_index[int(impact)] - by_index[int(top)], 3)}
    except KeyError:
        return None


def located(moments) -> int:
    from .analysis_bundle import ANALYSIS_PHASES

    frames = moments.frames()
    return sum(1 for p in ANALYSIS_PHASES if frames.get(p) is not None)


def plausible(t: dict | None) -> bool:
    return bool(t) and BACKSWING_S[0] <= t["backswing_s"] <= BACKSWING_S[1] and DOWNSWING_S[0] <= t["downswing_s"] <= DOWNSWING_S[1]


def tempo_distance(t: dict) -> float:
    import math

    return abs(math.log(t["backswing_s"] / TYPICAL_BACKSWING_S)) + abs(math.log(t["downswing_s"] / TYPICAL_DOWNSWING_S))


def estimate_with_slowmo(records: Sequence[object], estimate: Callable, *, hint: float | None = None):
    """``(moments, info)``. Normal speed first; rescaled clocks only when that finds no plausible swing.

    A normal-speed result is kept when it locates the moments and its tempo is human. Otherwise every
    factor in ``FACTORS`` (and the swing finder's ``hint``) is tried and the one whose rescaled tempo is
    closest to a typical swing wins; nothing is accepted without MIN_LOCATED moments and a plausible tempo.
    """

    first = estimate(list(records))
    t1 = tempo(first, records, 1.0)
    # A complete normal-speed result is kept whatever its tempo: one clock cannot fix a clip whose speed
    # changes part-way (a broadcast speed ramp), and re-timing such a clip moved Address into the
    # takeaway on production swing P11. Only a swing the clip's own clock cannot find is re-timed.
    if (hint in (None, 1.0)) and (located(first) >= MIN_LOCATED or
                                  (located(first) >= 4 and (t1 is None or plausible(t1)))):
        return first, {"factor": 1.0, "tried": [1.0], "tempo": t1}
    options = []
    if located(first) >= MIN_LOCATED and plausible(t1):
        options.append((tempo_distance(t1), 1.0, first, t1))
    for factor in sorted(set(FACTORS) | ({hint} if hint else set())):
        if factor == 1.0:
            continue
        moments = estimate(scaled(records, factor))
        t = tempo(moments, records, factor)
        if located(moments) >= MIN_LOCATED and plausible(t):
            options.append((tempo_distance(t), factor, moments, t))
    tried = [1.0] + sorted(set(FACTORS) | ({hint} if hint else set()))
    if not options:
        return first, {"factor": 1.0, "tried": tried, "tempo": t1, "reason": "no plausible swing at any speed"}
    _, factor, moments, t = min(options, key=lambda o: o[0])
    info = {"factor": factor, "tried": tried, "tempo": t, "source": "window" if factor == hint else "engine"}
    if factor != 1.0:
        info["notice"] = NOTICE
    return moments, info


__all__ = ["BACKSWING_S", "DOWNSWING_S", "FACTORS", "TYPICAL_BACKSWING_S", "TYPICAL_DOWNSWING_S", "tempo_distance", "MIN_LOCATED", "NOTICE", "ScaledRecord", "estimate_with_slowmo",
           "located", "plausible", "scaled", "tempo"]
