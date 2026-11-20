"""Candidate-cache identity, round trip and staleness (performance bundle).

The cache stores per-frame candidates, never a temporally selected track, and
only hands back an entry when the whole identity matches: tracker version and
settings, video content, pose input and the background-reference set.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest

from swingguru.club.cache import (
    CANDIDATE_CACHE_VERSION,
    CandidateCache,
    CandidateIdentity,
    config_fingerprint,
    decode_detection,
    encode_detection,
    pose_fingerprint,
    reference_fingerprint,
)
from swingguru.club.geometry import unit_from_angle
from swingguru.club.tracker import (
    ClubConfig,
    FrameDetection,
    HandsContext,
    ShaftCandidate,
)
from swingguru.landmarks import Landmark

HANDS = np.array([200.0, 300.0])


def _candidate(angle: float = 45.0, *, method: str = "ridge+lsd") -> ShaftCandidate:
    return ShaftCandidate(
        grip=HANDS.copy(),
        tip=HANDS + unit_from_angle(angle) * 200.0,
        angle_deg=angle,
        visible_start_px=17.5,
        length_px=200.0,
        support=0.91,
        strength=1.75,
        score=1.234,
        method=method,
        corroborated=True,
        rejected="",
    )


def _hands() -> HandsContext:
    return HandsContext(
        HANDS.copy(),
        118.25,
        ((np.array([1.0, 2.0]), np.array([3.0, 4.0])),),
        "landmarks",
        "",
        (("thigh", np.array([5.0, 6.0]), np.array([7.0, 8.0])),),
    )


def _detection(frame_index: int = 12) -> FrameDetection:
    return FrameDetection(
        frame_index,
        frame_index * 1000.0 / 30.0,
        _hands(),
        (_candidate(), _candidate(120.0, method="lsd")),
        (_candidate(-30.0),),
        "",
    )


def _record(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=400,
        height=600,
        points=SimpleNamespace(
            pixels={"right_wrist": np.array([200.0, 300.0]), "hip_mid": np.array([150.0, 330.0])},
            landmarks={
                "left_wrist": Landmark(
                    index=15,
                    name="left_wrist",
                    x=0.5,
                    y=0.5,
                    z=0.0,
                    visibility=0.8,
                    presence=0.8,
                ),
                "right_wrist": Landmark(
                    index=16,
                    name="right_wrist",
                    x=0.51,
                    y=0.49,
                    z=0.0,
                    visibility=0.9,
                    presence=0.9,
                ),
            },
        ),
    )


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_a_detection_survives_encode_and_decode() -> None:
    original = _detection()
    restored = decode_detection(json.loads(json.dumps(encode_detection(original))))

    assert restored.frame_index == original.frame_index
    assert restored.timestamp_ms == pytest.approx(original.timestamp_ms)
    assert restored.hands.origin.tolist() == original.hands.origin.tolist()
    assert restored.hands.torso_px == pytest.approx(original.hands.torso_px)
    assert restored.hands.source == original.hands.source
    assert len(restored.hands.forearms) == len(original.hands.forearms)
    assert [label for label, _, _ in restored.hands.body_segments] == ["thigh"]
    assert len(restored.candidates) == len(original.candidates)
    assert len(restored.rejected) == len(original.rejected)
    for before, after in zip(original.candidates, restored.candidates):
        assert after.angle_deg == pytest.approx(before.angle_deg)
        assert after.visible_start_px == pytest.approx(before.visible_start_px)
        assert after.score == pytest.approx(before.score)
        assert after.method == before.method
        assert after.corroborated == before.corroborated
        assert after.tip.tolist() == before.tip.tolist()


def test_a_detection_without_hands_round_trips() -> None:
    blank = FrameDetection(
        3, 100.0, HandsContext(None, 0.0, reason="hands were not tracked in this frame"), (), (), "hands were not tracked in this frame"
    )
    restored = decode_detection(json.loads(json.dumps(encode_detection(blank))))
    assert restored.hands.origin is None
    assert restored.reason == blank.reason
    assert restored.candidates == ()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def test_identity_key_changes_with_every_component() -> None:
    base = CandidateIdentity("tracker", "video", "pose", "config", "refs")
    assert base.key(12).endswith(":12")
    for field in ("tracker", "video", "pose", "config", "references"):
        changed = CandidateIdentity(
            **{**base.to_dict(), field: "different", "version": CANDIDATE_CACHE_VERSION}  # type: ignore[arg-type]
        )
        assert changed.key(12) != base.key(12)
    assert CandidateIdentity("tracker", "video", "pose", "config", "refs", version=99).key(12) != base.key(12)
    assert base.key(12) != base.key(13)


def test_config_fingerprint_tracks_settings() -> None:
    default = config_fingerprint(ClubConfig())
    assert config_fingerprint(ClubConfig()) == default
    assert config_fingerprint(ClubConfig(angle_step_deg=2.0)) != default
    assert config_fingerprint({"a": 1}) == config_fingerprint({"a": 1})
    assert config_fingerprint({"a": 1}) != config_fingerprint({"a": 2})


def test_pose_fingerprint_tracks_the_landmarks() -> None:
    first = pose_fingerprint([_record(0), _record(1)])
    assert pose_fingerprint([_record(0), _record(1)]) == first
    moved = _record(1)
    moved.points.pixels["hip_mid"] = np.array([151.0, 330.0])
    assert pose_fingerprint([_record(0), moved]) != first
    assert pose_fingerprint([_record(0)]) != first


def test_reference_fingerprint_accepts_frames_or_pairs() -> None:
    hands = np.array([10.0, 20.0])
    frame = SimpleNamespace(frame_index=7, hands=hands)
    assert reference_fingerprint([frame]) == reference_fingerprint([(7, hands)])
    assert reference_fingerprint([(7, hands)]) != reference_fingerprint([(8, hands)])
    assert reference_fingerprint([(7, None)]) != reference_fingerprint([(7, hands)])


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_cache_counts_hits_and_misses() -> None:
    cache = CandidateCache(CandidateIdentity("t", "v", "p", "c", "r"))
    assert cache.get(1) is None
    cache.put(_detection(1))
    assert cache.get(1) is not None
    assert cache.stats == {"hits": 1, "misses": 1, "entries": 1, "stale_identity": False}
    assert 1 in cache and 2 not in cache
    assert len(cache) == 1


def test_cache_round_trips_through_a_file(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "club_candidates.json"
    cache = CandidateCache(identity, path=path)
    cache.put(_detection(1))
    cache.put(_detection(2))
    cache.save()
    assert path.exists()

    reloaded = CandidateCache.load(identity, path)
    assert sorted(reloaded.entries) == [1, 2]
    assert reloaded.stale is False
    assert reloaded.get(2) is not None
    assert reloaded.hits == 1


def test_a_stale_identity_is_not_reused(tmp_path) -> None:
    path = tmp_path / "club_candidates.json"
    written = CandidateCache(CandidateIdentity("t", "v", "p", "c", "r"), path=path)
    written.put(_detection(1))
    written.save()

    different = CandidateIdentity("t", "v", "p", "c", "OTHER")
    reloaded = CandidateCache.load(different, path)
    assert reloaded.stale is True
    assert reloaded.entries == {}
    assert reloaded.get(1) is None


def test_a_corrupt_cache_file_is_ignored(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "club_candidates.json"
    path.write_text("{not json", encoding="utf-8")
    cache = CandidateCache.load(identity, path)
    # Unreadable is unusable: everything is a miss and the file is replaced by
    # the next save rather than trusted.
    assert cache.entries == {}
    assert cache.stale is True
    assert cache.get(1) is None
    cache.put(_detection(1))
    cache.save()
    assert CandidateCache.load(identity, path).get(1) is not None


def test_a_cache_from_an_older_version_is_ignored(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "club_candidates.json"
    payload = CandidateCache(identity, path=path).to_payload()
    payload["version"] = CANDIDATE_CACHE_VERSION - 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    cache = CandidateCache.load(identity, path)
    assert cache.stale is True
    assert cache.entries == {}


def test_a_missing_cache_file_is_not_an_error(tmp_path) -> None:
    cache = CandidateCache.load(CandidateIdentity("t", "v", "p", "c", "r"), tmp_path / "nope.json")
    assert cache.entries == {}
    cache.save()
    assert (tmp_path / "nope.json").exists()


def test_save_without_a_path_is_a_no_op() -> None:
    cache = CandidateCache(CandidateIdentity("t", "v", "p", "c", "r"))
    cache.put(_detection(1))
    cache.save()
    assert cache.entries


@pytest.mark.parametrize("broken", ["short_forearm", "infinite_index"])
def test_malformed_cached_frame_is_a_miss(tmp_path, broken) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "candidates.json"
    cache = CandidateCache(identity, path=path)
    cache.put(_detection(1))
    payload = cache.to_payload()
    frame = payload["frames"]["1"]
    if broken == "short_forearm":
        frame["hands"]["forearms"] = [[]]
    else:
        frame["frame_index"] = float("inf")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert CandidateCache.load(identity, path).get(1) is None


# ---------------------------------------------------------------------------
# The identity must cover every input the candidates depend on
# ---------------------------------------------------------------------------


def _weak_record(index: int) -> SimpleNamespace:
    """A record with no usable pixel hands, so the raw landmarks are the input."""

    return SimpleNamespace(
        index=index,
        timestamp_ms=index * 1000.0 / 30.0,
        width=400,
        height=600,
        points=SimpleNamespace(
            pixels={"hip_mid": np.array([150.0, 330.0]), "shoulder_mid": np.array([150.0, 230.0])},
            landmarks={
                "left_wrist": Landmark(
                    index=15, name="left_wrist", x=0.5, y=0.5, z=0.0, visibility=0.8, presence=0.8
                ),
                "right_wrist": Landmark(
                    index=16, name="right_wrist", x=0.6, y=0.4, z=0.0, visibility=0.9, presence=0.9
                ),
            },
        ),
    )


def test_pose_fingerprint_tracks_weak_fallback_landmark_coordinates() -> None:
    # hands_context only reaches the raw landmarks when the usable pixel
    # points are missing; those coordinates become candidates, so they must be
    # part of the cache identity.
    base = _weak_record(4)
    baseline = pose_fingerprint([base])

    moved = _weak_record(4)
    moved.points.landmarks["left_wrist"] = Landmark(
        index=15, name="left_wrist", x=0.5, y=0.5, z=0.0, visibility=0.8, presence=0.8
    )
    moved.points.landmarks["right_wrist"] = Landmark(
        index=16, name="right_wrist", x=0.61, y=0.4, z=0.0, visibility=0.9, presence=0.9
    )
    assert pose_fingerprint([moved]) != baseline

    for mutated in ("x", "y", "visibility", "presence"):
        changed = _weak_record(4)
        landmark = changed.points.landmarks["left_wrist"]
        value = getattr(landmark, mutated)
        changed.points.landmarks["left_wrist"] = Landmark(
            index=landmark.index,
            name=landmark.name,
            x=value + 0.01 if mutated == "x" else landmark.x,
            y=value + 0.01 if mutated == "y" else landmark.y,
            z=landmark.z,
            visibility=value + 0.01 if mutated == "visibility" else landmark.visibility,
            presence=value + 0.01 if mutated == "presence" else landmark.presence,
        )
        assert pose_fingerprint([changed]) != baseline, f"{mutated} is not part of the identity"


def test_pose_fingerprint_tracks_dimensions_and_raw_timestamps() -> None:
    baseline = pose_fingerprint([_weak_record(4)])
    wider = _weak_record(4)
    wider.width = 401
    assert pose_fingerprint([wider]) != baseline
    taller = _weak_record(4)
    taller.height = 601
    assert pose_fingerprint([taller]) != baseline
    shifted = _weak_record(4)
    shifted.timestamp_ms = shifted.timestamp_ms + 0.0001
    assert pose_fingerprint([shifted]) != baseline


def test_pose_fingerprint_tracks_usable_pixels_and_index() -> None:
    baseline = pose_fingerprint([_record(3)])
    moved = _record(3)
    moved.points.pixels["right_wrist"] = np.array([201.0, 300.0])
    assert pose_fingerprint([moved]) != baseline
    reindexed = _record(4)
    assert pose_fingerprint([reindexed]) != baseline


def test_a_weak_hand_change_invalidates_a_saved_cache(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", pose_fingerprint([_weak_record(4)]), "c", "r")
    path = tmp_path / "club_candidates.json"
    cache = CandidateCache(identity, path=path)
    cache.put(_detection(4))
    cache.save()

    moved = _weak_record(4)
    moved.points.landmarks["right_wrist"] = Landmark(
        index=16, name="right_wrist", x=0.7, y=0.3, z=0.0, visibility=0.9, presence=0.9
    )
    different = CandidateIdentity("t", "v", pose_fingerprint([moved]), "c", "r")
    assert CandidateCache.load(different, path).stale is True


# ---------------------------------------------------------------------------
# Malformed caches are a miss, never a crash
# ---------------------------------------------------------------------------


def _write(tmp_path, payload) -> Path:
    path = tmp_path / "club_candidates.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_malformed_identity_is_treated_as_a_miss(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    for raw_identity in ([], "identity", 5, None, {"tracker": "t"}):
        path = _write(
            tmp_path,
            {"version": CANDIDATE_CACHE_VERSION, "identity": raw_identity, "frames": {"1": {}}},
        )
        cache = CandidateCache.load(identity, path)
        assert cache.stale is True
        assert cache.entries == {}


def test_a_malformed_version_is_treated_as_a_miss(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    for version in ("one", None, [], {}, CANDIDATE_CACHE_VERSION + 1):
        path = _write(tmp_path, {"version": version, "identity": identity.to_dict(), "frames": {}})
        cache = CandidateCache.load(identity, path)
        assert cache.stale is True, version
        assert cache.entries == {}


def test_malformed_frames_are_dropped_without_crashing(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    good = encode_detection(_detection(7))
    payload = {
        "version": CANDIDATE_CACHE_VERSION,
        "identity": identity.to_dict(),
        "frames": {
            "7": good,
            "8": {"frame_index": 8},
            "9": {**good, "frame_index": 9, "candidates": [{"grip": [1.0], "tip": [0.0, 0.0]}]},
            "10": {**good, "frame_index": float("nan")},
            "not-a-number": good,
            "11": "not an object",
            "12": {**good, "frame_index": 99},  # key/index mismatch
        },
    }
    path = _write(tmp_path, payload)
    cache = CandidateCache.load(identity, path)
    assert cache.stale is False
    assert sorted(cache.entries) == [7]
    assert cache.entries[7].frame_index == 7


def test_non_finite_candidate_values_are_rejected(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    good = encode_detection(_detection(5))
    broken = json.loads(json.dumps(good))
    broken["candidates"][0]["score"] = float("inf")
    path = _write(
        tmp_path,
        {"version": CANDIDATE_CACHE_VERSION, "identity": identity.to_dict(), "frames": {"5": broken}},
    )
    cache = CandidateCache.load(identity, path)
    assert cache.entries == {}


def test_frames_must_be_an_object(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = _write(tmp_path, {"version": CANDIDATE_CACHE_VERSION, "identity": identity.to_dict(), "frames": []})
    cache = CandidateCache.load(identity, path)
    assert cache.stale is True


def test_save_writes_atomically_and_leaves_no_temporary_file(tmp_path) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "club_candidates.json"
    cache = CandidateCache(identity, path=path)
    cache.put(_detection(1))
    cache.save()
    assert path.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == ["club_candidates.json"]


def test_a_save_that_fails_keeps_the_previous_file(tmp_path, monkeypatch) -> None:
    identity = CandidateIdentity("t", "v", "p", "c", "r")
    path = tmp_path / "club_candidates.json"
    cache = CandidateCache(identity, path=path)
    cache.put(_detection(1))
    cache.save()
    before = path.read_text(encoding="utf-8")

    cache.put(_detection(2))
    monkeypatch.setattr("os.replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        cache.save()
    assert path.read_text(encoding="utf-8") == before
