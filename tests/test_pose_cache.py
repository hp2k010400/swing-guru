"""The pose cache must not be able to stand in for a different pose run.

An unkeyed pickle from an earlier experiment would silently return the old
landmarks and make a new configuration look exactly like the baseline, so the
identity is the whole point of the file format.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from swingguru.pose_cache import (
    POSE_CACHE_VERSION,
    PoseCacheIdentity,
    asset_fingerprint,
    file_digest,
    legacy_identity,
    load_pose_cache,
    load_records_any,
    runtime_fingerprint,
    save_pose_cache,
    settings_fingerprint,
)


def _identity(**overrides) -> PoseCacheIdentity:
    base = {
        "video": "abcd1234",
        "provider": "rtmw",
        "model": "wholebody",
        "mode": "balanced",
        "det_frequency": 5,
        "tracker": "rtmlib-pose-tracker",
    }
    base.update(overrides)
    return PoseCacheIdentity(**base)


def test_identity_round_trips_and_keys_are_distinct() -> None:
    identity = _identity()
    assert PoseCacheIdentity.from_dict(identity.to_dict()) == identity
    assert identity.key() == _identity().key()
    for field, value in (
        ("video", "other-video"),
        ("provider", "mediapipe"),
        ("model", "body"),
        ("mode", "performance"),
        ("det_frequency", 10),
        ("tracker", "other-tracker"),
    ):
        assert _identity(**{field: value}).key() != identity.key(), field
    assert PoseCacheIdentity(**{**_identity().to_dict(), "version": 99}).key() != identity.key()


def test_identity_from_dict_rejects_malformed_payloads() -> None:
    for payload in ([], "identity", 5, None, {"video": "x"}, {"version": "one"}):
        with pytest.raises(ValueError):
            PoseCacheIdentity.from_dict(payload)


def test_a_matching_cache_is_a_hit(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    identity = _identity()
    save_pose_cache(path, ["a", "b"], identity)
    result = load_pose_cache(path, identity)
    assert result.hit and result.records == ["a", "b"]
    assert result.status == "hit"


def test_a_different_configuration_is_a_miss(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    save_pose_cache(path, ["a"], _identity())
    assert load_pose_cache(path, _identity(det_frequency=10)).status == "miss-identity"
    assert load_pose_cache(path, _identity(video="other")).status == "miss-identity"
    assert load_pose_cache(path, _identity(provider="mediapipe")).status == "miss-identity"
    assert load_pose_cache(path, _identity()).hit


def test_a_legacy_unkeyed_pickle_is_a_miss_unless_allowed(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    path.write_bytes(pickle.dumps(["legacy"]))
    assert load_pose_cache(path, _identity()).status == "miss-legacy"
    allowed = load_pose_cache(path, _identity(det_frequency=99), allow_legacy=True)
    assert allowed.hit and allowed.records == ["legacy"]
    assert allowed.status == "hit-legacy"


def test_a_legacy_pickle_cannot_masquerade_as_a_new_configuration(tmp_path) -> None:
    # The point of the identity: an old run must not make a new detector
    # cadence look like it produced the baseline landmark set.
    path = tmp_path / "pose_records.pkl"
    path.write_bytes(pickle.dumps(["old-landmarks"]))
    assert load_pose_cache(path, _identity(det_frequency=10)).records is None
    assert load_pose_cache(path, _identity(det_frequency=15)).records is None


def test_missing_corrupt_and_wrong_shape_files_are_misses(tmp_path) -> None:
    assert load_pose_cache(tmp_path / "nope.pkl", _identity()).status == "miss-missing"
    assert load_pose_cache(None, _identity()).status == "miss-no-path"

    corrupt = tmp_path / "corrupt.pkl"
    corrupt.write_bytes(b"not a pickle at all")
    assert load_pose_cache(corrupt, _identity()).status == "miss-corrupt"

    wrong = tmp_path / "wrong.pkl"
    wrong.write_bytes(pickle.dumps({"version": POSE_CACHE_VERSION, "identity": _identity().to_dict(), "records": 5}))
    assert load_pose_cache(wrong, _identity()).status == "miss-corrupt"

    no_identity = tmp_path / "no_identity.pkl"
    no_identity.write_bytes(pickle.dumps({"version": POSE_CACHE_VERSION, "records": []}))
    assert load_pose_cache(no_identity, _identity()).status == "miss-corrupt"


def test_saving_is_atomic_and_leaves_no_temporary_file(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    save_pose_cache(path, ["a"], _identity())
    assert [item.name for item in tmp_path.iterdir()] == ["pose_records.pkl"]
    assert load_pose_cache(path, _identity()).records == ["a"]


def test_a_failed_save_keeps_the_previous_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "pose_records.pkl"
    save_pose_cache(path, ["first"], _identity())
    before = path.read_bytes()
    monkeypatch.setattr("os.replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        save_pose_cache(path, ["second"], _identity())
    assert path.read_bytes() == before


def test_the_file_records_its_identity(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    identity = _identity()
    save_pose_cache(path, ["a"], identity)
    payload = pickle.loads(path.read_bytes())
    assert payload["identity"] == identity.to_dict()
    assert payload["version"] == POSE_CACHE_VERSION


def test_load_records_any_reads_both_formats(tmp_path) -> None:
    keyed = tmp_path / "keyed.pkl"
    save_pose_cache(keyed, ["a", "b"], _identity())
    assert load_records_any(keyed) == ["a", "b"]

    legacy = tmp_path / "legacy.pkl"
    legacy.write_bytes(pickle.dumps(["old"]))
    assert load_records_any(legacy) == ["old"]

    broken = tmp_path / "broken.pkl"
    broken.write_bytes(pickle.dumps({"records": "not a list"}))
    with pytest.raises(ValueError):
        load_records_any(broken)
    assert isinstance(legacy_identity().key(), str)


def test_the_legacy_identity_matches_the_default_configuration() -> None:
    legacy = legacy_identity()
    assert legacy.version == 0
    assert legacy.video == ""
    assert legacy.det_frequency == 5
    assert legacy.model == "wholebody"
    assert legacy.mode == "balanced"


# ---------------------------------------------------------------------------
# The identity must also cover the model assets, runtime and settings
# ---------------------------------------------------------------------------


def test_asset_fingerprint_changes_with_the_model_file(tmp_path) -> None:
    first = tmp_path / "model.onnx"
    first.write_bytes(b"weights-v1")
    baseline = asset_fingerprint([first])
    assert asset_fingerprint([first]) == baseline
    first.write_bytes(b"weights-v2-longer")
    assert asset_fingerprint([first]) != baseline
    missing = asset_fingerprint([tmp_path / "nope.onnx"])
    assert missing != baseline
    assert file_digest(tmp_path / "nope.onnx") == "missing"


def test_an_asset_change_invalidates_a_saved_cache(tmp_path) -> None:
    model = tmp_path / "model.onnx"
    model.write_bytes(b"weights-v1")
    path = tmp_path / "pose_records.pkl"
    identity = _identity(model_digest=asset_fingerprint([model]))
    save_pose_cache(path, ["landmarks"], identity)
    assert load_pose_cache(path, identity).hit
    model.write_bytes(b"re-exported weights")
    changed = _identity(model_digest=asset_fingerprint([model]))
    assert load_pose_cache(path, changed).status == "miss-identity"


def test_runtime_and_measurement_and_options_are_part_of_the_identity(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    identity = _identity(
        runtime=runtime_fingerprint(),
        measurement=settings_fingerprint({"torso": 1}),
        options='{"variant": "adaptive"}',
    )
    save_pose_cache(path, ["landmarks"], identity)
    assert load_pose_cache(path, identity).hit
    for field, value in (
        ("runtime", "other-runtime"),
        ("measurement", settings_fingerprint({"torso": 2})),
        ("options", "{}"),
    ):
        assert load_pose_cache(path, _identity(**{field: value})).status == "miss-identity", field
    assert runtime_fingerprint() == runtime_fingerprint()
    assert settings_fingerprint({"a": 1}) == settings_fingerprint({"a": 1})
    assert settings_fingerprint({"a": 1}) != settings_fingerprint({"a": 2})


def test_the_outer_cache_version_is_checked(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    identity = _identity()
    save_pose_cache(path, ["landmarks"], identity)
    payload = pickle.loads(path.read_bytes())
    payload["version"] = POSE_CACHE_VERSION + 1
    path.write_bytes(pickle.dumps(payload))
    assert load_pose_cache(path, identity).status == "miss-version"


def test_a_non_finite_identity_version_is_a_miss(tmp_path) -> None:
    path = tmp_path / "pose_records.pkl"
    identity = _identity()
    for bad in (float("inf"), float("nan"), float("-inf")):
        payload = {
            "version": POSE_CACHE_VERSION,
            "identity": {**identity.to_dict(), "version": bad, "det_frequency": bad},
            "records": ["x"],
        }
        path.write_bytes(pickle.dumps(payload))
        assert load_pose_cache(path, identity).status == "miss-corrupt"
    payload = {
        "version": POSE_CACHE_VERSION,
        "identity": {**identity.to_dict(), "options": {"not": "a string"}},
        "records": ["x"],
    }
    path.write_bytes(pickle.dumps(payload))
    assert load_pose_cache(path, identity).status == "miss-corrupt"
