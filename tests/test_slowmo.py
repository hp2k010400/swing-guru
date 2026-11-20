"""Slow-motion clips (engine profile candidate, fix slowmo): the swing is searched on a rescaled clock."""

from __future__ import annotations

from types import SimpleNamespace

from swingguru.engine_profile import profile
from swingguru.phases import Phase
from swingguru.slowmo import NOTICE, ScaledRecord, estimate_with_slowmo, scaled, tempo


def records(n=300, fps=30.0):
    return [SimpleNamespace(index=i, timestamp_ms=1000.0 * i / fps, points="pose") for i in range(n)]


class Moments:
    def __init__(self, frames):
        self._f = frames

    def frames(self):
        return self._f


def swing(address, takeaway, top, impact):
    return Moments({Phase.ADDRESS: address, Phase.TAKEAWAY: takeaway, Phase.MID_BACK: (takeaway + top) // 2,
                    Phase.TOP: top, Phase.DOWNSWING: (top + impact) // 2, Phase.IMPACT: impact, Phase.FINISH: None})


NONE = Moments({p: None for p in Phase})


def test_slowmo_is_candidate_only() -> None:
    assert "slowmo" in profile("candidate").fixes and "slowmo" not in profile("next").fixes


def test_scaled_records_only_change_the_clock() -> None:
    r = records(10)
    s = scaled(r, 4.0)
    assert isinstance(s[3], ScaledRecord) and s[3].timestamp_ms == r[3].timestamp_ms / 4 and s[3].index == 3
    assert s[3].points == "pose" and scaled(r, 1.0) == r


def test_a_normal_speed_swing_is_left_alone() -> None:
    calls = []

    def estimate(recs):
        calls.append(recs[1].timestamp_ms)
        return swing(30, 45, 56, 64)          # 0.37 s back, 0.27 s down at 30 fps
    m, info = estimate_with_slowmo(records(), estimate)
    assert info["factor"] == 1.0 and "notice" not in info and len(calls) == 1


def test_a_4x_slow_motion_swing_is_found_on_the_rescaled_clock() -> None:
    real = records(400)

    def estimate(recs):
        scale = real[1].timestamp_ms / recs[1].timestamp_ms      # the factor this pass uses
        if scale < 2:
            return NONE                                          # at clip speed: no swing
        return swing(60, 120, 165, 197)                          # 1.5 s back, 1.07 s down in clip time
    m, info = estimate_with_slowmo(real, estimate)
    assert info["factor"] == 4.0 and info["notice"] == NOTICE
    t = tempo(m, real, info["factor"])
    assert abs(t["backswing_s"] - 0.375) < 0.01 and abs(t["downswing_s"] - 0.267) < 0.01


def test_no_plausible_speed_means_no_swing_is_invented() -> None:
    def estimate(recs):
        return Moments({Phase.ADDRESS: 10, Phase.TOP: 12, **{p: None for p in Phase if p not in (Phase.ADDRESS, Phase.TOP)}})
    m, info = estimate_with_slowmo(records(), estimate)
    assert info["factor"] == 1.0 and info["reason"] == "no plausible swing at any speed"


def test_window_finder_uses_the_rescaled_clock_only_when_asked() -> None:
    import inspect

    from swingguru import swing_window

    assert "slowmo" in inspect.signature(swing_window.locate_swing).parameters
    assert inspect.signature(swing_window.locate_swing).parameters["slowmo"].default is False
    assert swing_window.WindowDecision("NONE", 1.0, 0.0, 1.0, False, "NONE", "").time_scale == 1.0


def test_a_complete_normal_speed_result_is_never_retimed() -> None:
    """Speed-ramped broadcast clips (production P11): slow tempo, but every moment found at clip speed."""

    calls = []

    def estimate(recs):
        calls.append(1)
        return swing(39, 46, 59, 131)                            # 0.43 s back, 2.4 s down: a ramp
    m, info = estimate_with_slowmo(records(), estimate)
    assert info["factor"] == 1.0 and "notice" not in info and len(calls) == 1


def test_slowmo_step_keeps_at_least_double_the_normal_sampling() -> None:
    from swingguru.swing_window import slowmo_step

    assert [slowmo_step(f) for f in (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0)] == [1, 1, 1, 2, 2, 3, 4]
    assert all(f / slowmo_step(f) >= 2.0 for f in (4.0, 5.0, 6.0, 8.0))


def test_stepped_cut_keeps_every_nth_frame_at_the_same_rate(tmp_path) -> None:
    import cv2
    import numpy as np

    from swingguru.swing_window import cut_window

    src = tmp_path / "src.mp4"
    writer = cv2.VideoWriter(str(src), cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (64, 64))
    for i in range(40):
        writer.write(np.full((64, 64, 3), i * 6, np.uint8))
    writer.release()
    out = tmp_path / "out.mp4"
    cut_window(src, out, 0, 40, step=4, fps=30.0)
    cap = cv2.VideoCapture(str(out))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    assert n == 10 and abs(cap.get(cv2.CAP_PROP_FPS) - 30.0) < 0.1
