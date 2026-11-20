"""Engine profile ``candidate`` (1.0.1 development): Address shaft consensus and replay observations.

``next`` (production since 2026-10-07) must be unaffected: it never gets either fix.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.club.address_reference import address_reference
from swingguru.club.tracker import FrameShaft
from swingguru.engine_profile import CANDIDATE_FIXES, FIXES, profile
from swingguru.observations import OBSERVATIONS, mark_observations
from swingguru.scoring import score_swing


def shaft(frame: int, angle: float | None, level: str = "HIGH", *, interpolated: bool = False, confidence: float = 0.8):
    if angle is None:
        return FrameShaft(frame, frame * 33.0, "unavailable", "UNAVAILABLE", 0.0)
    rad = np.radians(angle)
    grip = np.array([300.0, 500.0])
    return FrameShaft(frame, frame * 33.0, "interpolated" if interpolated else "detected", level, confidence,
                      angle_deg=float(angle), grip=grip, tip=grip + 200 * np.array([np.cos(rad), np.sin(rad)]),
                      method="test", interpolated=interpolated)


class Track:
    def __init__(self, angles: dict, **kw):
        self.frames = {f: (a if isinstance(a, FrameShaft) else shaft(f, a)) for f, a in angles.items()}

    def at(self, f):
        return self.frames.get(f)


def records(frames, moving: dict | None = None):
    out = []
    for f in frames:
        dx = (moving or {}).get(f, 0.0)
        out.append(SimpleNamespace(index=f, points=SimpleNamespace(
            pixels={"left_wrist": np.array([300.0 + dx, 500.0]), "right_wrist": np.array([305.0 + dx, 502.0])})))
    return out


FRAMES = range(80, 91)
TORSO = 200.0


def test_profiles_next_is_unchanged_and_candidate_adds_the_new_fixes(monkeypatch) -> None:
    monkeypatch.delenv("SG_ENGINE_PROFILE", raising=False)
    assert profile("next").fixes == frozenset(FIXES)
    assert not ({"address_consensus", "observations"} & profile("next").fixes)
    assert profile("candidate").fixes == frozenset(CANDIDATE_FIXES)
    assert profile("candidate", only=["address_consensus"]).fixes == {"address_consensus"}
    with pytest.raises(ValueError):
        profile("next", only=["address_consensus"])          # never available under next


def test_production_swing_bce2ca87_false_flat_address_line_is_replaced() -> None:
    # The real readings: Address 85/86 at 2-3 deg (a horizontal edge at hand height), 83/84/87 at 38-40.
    track = Track({83: 40.0, 84: 38.0, 85: 2.0, 86: 3.0, 87: 38.0})
    ref = address_reference(track, 85, records(FRAMES), TORSO)
    assert ref.status == "outlier_replaced" and ref.changed
    assert ref.shaft.frame_index == 84 and ref.shaft.angle_deg == 38.0      # a real neighbouring detection
    assert ref.diagnostics["agreeing_frames"] == [83, 84, 87]


def test_a_genuine_address_reading_is_kept() -> None:
    track = Track({83: 40.0, 84: 38.0, 85: 39.0, 86: 41.0, 87: 38.0})
    ref = address_reference(track, 85, records(FRAMES), TORSO)
    assert ref.status == "centre_agrees" and not ref.changed and ref.shaft is track.at(85)


def test_no_clear_majority_keeps_todays_behaviour() -> None:
    # Two against two (plus the Address frame on its own): nothing is replaced.
    track = Track({83: 40.0, 84: 38.0, 85: 2.0, 86: 3.0, 87: None})
    ref = address_reference(track, 85, records(FRAMES), TORSO)
    assert ref.status == "no_consensus" and ref.shaft is track.at(85) and not ref.changed
    # Neighbours that disagree among themselves never outvote Address.
    track = Track({83: 40.0, 84: 20.0, 85: 2.0, 86: 60.0, 87: 80.0})
    assert address_reference(track, 85, records(FRAMES), TORSO).status == "no_consensus"


def test_low_or_interpolated_neighbours_never_vote() -> None:
    track = Track({83: shaft(83, 40.0, "LOW"), 84: shaft(84, 38.0, interpolated=True), 85: 2.0, 86: 39.0, 87: 38.0})
    ref = address_reference(track, 85, records(FRAMES), TORSO)
    assert ref.status == "no_consensus"           # only 86 and 87 vote for 38-39: below the three needed


def test_neighbours_whose_hands_moved_never_vote() -> None:
    # 86 and 87 are already in the takeaway (hands moved 40 % of the torso): they cannot outvote Address.
    track = Track({83: 2.0, 84: 3.0, 85: 2.0, 86: 38.0, 87: 38.0})
    moving = {86: 0.4 * TORSO, 87: 0.5 * TORSO}
    ref = address_reference(track, 85, records(FRAMES, moving), TORSO)
    assert ref.status == "centre_agrees" and ref.shaft is track.at(85)
    rows = {r["frame"]: r for r in ref.diagnostics["frames"]}
    assert not rows[86]["still"] and not rows[87]["still"]


def test_a_missing_address_detection_is_recovered_only_from_a_clear_majority() -> None:
    track = Track({83: 40.0, 84: 38.0, 85: None, 86: 39.0, 87: 38.0})
    ref = address_reference(track, 85, records(FRAMES), TORSO)
    assert ref.status == "missing_recovered" and ref.shaft.frame_index in (84, 86)
    sparse = Track({83: None, 84: 38.0, 85: None, 86: 39.0, 87: None})
    assert address_reference(sparse, 85, records(FRAMES), TORSO).status == "no_consensus"


def test_line_orientation_wraps_so_opposite_directions_are_the_same_line() -> None:
    track = Track({83: -142.0, 84: 38.0, 85: 2.0, 86: 218.0, 87: 38.5})
    assert address_reference(track, 85, records(FRAMES), TORSO).status == "outlier_replaced"


# ---------------------------------------------------------------- observations


def _analysis():
    def check(phase, kind, level="HIGH", guided=False, value="4°", callout="4° more upright"):
        return {"id": f"{phase}:{kind}", "phase": phase, "kind": kind, "level": level, "guided": guided,
                "value": value, "callout": callout, "raw": {"torso_angle_change_deg": -4.0},
                "verdict": {"status": "good", "label": "x", "mapping": {}}}

    return {"phases": [
        {"phase": "mid_back", "checks": [check("mid_back", "posture")]},
        {"phase": "top", "checks": [check("top", "posture", guided=True), check("top", "head", callout="2.1% of height"),
                                    check("top", "hand_depth")]},
        {"phase": "downswing", "checks": [check("downswing", "posture", level="MEDIUM")]},
        {"phase": "impact", "checks": [check("impact", "posture")]},
    ]}


def test_observations_are_marked_only_for_listed_high_confidence_stable_readings() -> None:
    steady = lambda phase, kind: 0.5
    a = mark_observations(_analysis(), steady)
    # MEDIUM Mid-Down posture left out; Impact posture is not offered (noisiest, easily misread).
    assert a["observations"]["checks"] == ["mid_back:posture", "top:head"]
    obs = {c["id"]: c.get("observation") for p in a["phases"] for c in p["checks"]}
    assert obs["top:posture"] is None                       # a scored checkpoint is never an observation
    assert obs["top:hand_depth"] is None                    # not listed (no defensible meaning yet)
    assert obs["impact:posture"] is None
    assert obs["mid_back:posture"] == {"version": 1, "name": "Posture", "scored": False, "label": "4° more upright",
                                       "note": "Observation. Not part of your Swing Score.", "frame_noise": 0.5}
    assert set(OBSERVATIONS) == {("mid_back", "posture"), ("top", "head"), ("downswing", "posture")}


def test_an_unstable_or_unmeasurable_reading_is_never_shown() -> None:
    from swingguru.observations import MAX_FRAME_NOISE

    shaky = {"mid_back": MAX_FRAME_NOISE["posture"] + 0.1, "top": None}
    a = mark_observations(_analysis(), lambda phase, kind: shaky.get(phase, 0.2))
    assert a["observations"]["checks"] == []
    assert {w["id"] for w in a["observations"]["withheld_unstable"]} == {"mid_back:posture", "top:head"}
    assert mark_observations(_analysis())["observations"]["checks"] == []      # no stability -> nothing (fail closed)


def test_observations_never_change_values_verdicts_or_the_swing_score() -> None:
    from test_swing_score import analysis as scored_analysis

    base = scored_analysis()
    for phase in base["phases"]:
        if phase["phase"] == "mid_back":
            phase["checks"].append({"id": "mid_back:posture", "phase": "mid_back", "kind": "posture", "level": "HIGH",
                                    "guided": False, "value": "4°", "callout": "4° more upright",
                                    "raw": {"torso_angle_change_deg": -4.0}, "verdict": {"status": "info"}})
    before = copy.deepcopy(base)
    marked = mark_observations(copy.deepcopy(base), lambda phase, kind: 0.2)
    for version in ("swing_score_v2", "swing_score_v3"):
        assert score_swing(marked, version) == score_swing(before, version)
    strip = lambda a: [{k: v for k, v in c.items() if k != "observation"} for p in a["phases"] for c in p["checks"]]
    assert strip(marked) == strip(before)


# ---------------------------------------------------------------- plane from the nearest clear frame


from swingguru.club.plane_neighbour import nearest_clear_frame  # noqa: E402


def hands_at(frames, grip=(300.0, 500.0)):
    return [SimpleNamespace(index=f, points=SimpleNamespace(pixels={"left_wrist": np.array(grip), "right_wrist": np.array(grip)}))
            for f in frames]


def test_plane_neighbour_is_candidate_only() -> None:
    assert "plane_neighbour" in profile("candidate").fixes and "plane_neighbour" not in profile("next").fixes


def test_a_missing_moment_is_measured_on_a_clear_high_neighbour() -> None:
    track = Track({48: 70.0, 49: 72.0, 50: None, 51: None, 52: None})
    near, diag = nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)
    assert near is track.at(49) and diag["result"] == "recovered" and diag["offset"] == -1


def test_a_usable_moment_frame_is_never_replaced() -> None:
    track = Track({49: 72.0, 50: 40.0, 51: 70.0})
    near, diag = nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)
    assert near is None and diag["result"] == "moment frame usable"


def test_low_interpolated_or_unconfirmed_medium_neighbours_never_count() -> None:
    track = Track({48: None, 49: shaft(49, 72.0, "LOW"), 50: None, 51: shaft(51, 70.0, interpolated=True), 52: 70.0})
    assert nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)[0] is None
    track = Track({48: None, 49: shaft(49, 72.0, "MEDIUM"), 50: None, 51: None})
    assert nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)[0] is None   # no witness
    track = Track({48: None, 49: 72.0, 50: None, 51: None})
    assert nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)[0] is None   # HIGH needs one too
    track = Track({48: 74.0, 49: shaft(49, 72.0, "MEDIUM"), 50: None, 51: None})
    assert nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)[0] is track.at(49)  # confirmed


def test_a_jumping_line_or_a_line_away_from_the_hands_is_refused() -> None:
    track = Track({48: 10.0, 49: 72.0, 50: None, 51: None})
    near, diag = nearest_clear_frame(track, 50, "mid_back", hands_at(range(45, 56)), TORSO)
    assert near is None and "witness disagrees" in diag["candidates"][0]["rejected"]
    track = Track({48: 70.0, 49: 72.0, 50: None, 51: None})
    far = hands_at(range(45, 56), grip=(300.0 + 0.6 * TORSO, 500.0))
    assert nearest_clear_frame(track, 50, "mid_back", far, TORSO)[0] is None


def test_disagreeing_neighbours_recover_nothing() -> None:
    track = Track({48: 40.0, 49: 40.0, 50: None, 51: 60.0, 52: 60.0})
    near, diag = nearest_clear_frame(track, 50, "downswing", hands_at(range(45, 56)), TORSO)
    assert near is None and diag["result"] == "neighbours disagree"
    near, _ = nearest_clear_frame(Track({48: 40.0, 49: 40.0, 50: None, 51: 46.0, 52: 46.0}), 50, "downswing",
                                  hands_at(range(45, 56)), TORSO)
    assert near is not None


def test_a_line_through_the_golfers_head_is_refused() -> None:
    # Swing 46: a cap-brim edge from the hands through the head, two frames running (so the witness agreed).
    grip = np.array([300.0, 500.0])
    def edge(f):
        return FrameShaft(f, 0.0, "detected", "MEDIUM", 0.6, angle_deg=-20.0, grip=grip,
                          tip=grip + 200 * np.array([np.cos(np.radians(-20.0)), np.sin(np.radians(-20.0))]))
    track = Track({50: None, 51: edge(51), 52: edge(52)})
    head = grip + 120 * np.array([np.cos(np.radians(-20.0)), np.sin(np.radians(-20.0))])     # on the line
    recs = [SimpleNamespace(index=f, points=SimpleNamespace(pixels={"left_wrist": grip, "right_wrist": grip, "nose": head}))
            for f in range(45, 56)]
    near, diag = nearest_clear_frame(track, 50, "mid_back", recs, TORSO)
    assert near is None and "head" in diag["candidates"][1]["rejected"]
    clear = [SimpleNamespace(index=f, points=SimpleNamespace(pixels={"left_wrist": grip, "right_wrist": grip,
                                                                     "nose": head + np.array([0.0, -0.5 * TORSO])}))
             for f in range(45, 56)]
    assert nearest_clear_frame(track, 50, "mid_back", clear, TORSO)[0] is not None
