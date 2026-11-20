"""The explicit run-progress artifact: real events, atomic writes, old-bundle fallback."""

from __future__ import annotations

import json

import pytest

from swingguru import progress


def test_record_writes_the_versioned_artifact_atomically(tmp_path) -> None:
    payload = progress.record(tmp_path, "prepare", progress.ACTIVE, detail="upload received")
    assert payload is not None and payload["schema"] == progress.SCHEMA and payload["version"] == progress.VERSION
    on_disk = json.loads((tmp_path / progress.FILENAME).read_text(encoding="utf-8"))
    assert on_disk == payload
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be replaced, never left behind"


def test_states_advance_and_a_late_event_never_regresses_a_stage(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.ACTIVE)
    progress.record(tmp_path, "prepare", progress.DONE, detail="clip validated")
    progress.record(tmp_path, "prepare", progress.ACTIVE)  # a stale writer arrives late
    rows = progress.stage_rows(tmp_path)
    assert rows[0]["state"] == progress.DONE
    assert rows[0]["detail"] == "clip validated"


def test_explicit_artifact_never_invents_an_active_stage(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.DONE, detail="clip validated")
    progress.record(tmp_path, "moments", progress.DONE, detail="pose cache hit")
    states = progress.stage_states(tmp_path, prepared=True, finished=False, running=True)
    # With a real artifact, a stage that never reported stays pending: no
    # "first pending looks active" guessing (that heuristic is legacy-only).
    assert [row["state"] for row in states] == [progress.DONE, progress.DONE, "pending", "pending", "pending"]
    assert [row["id"] for row in states] == [key for key, _ in progress.STAGES]


def test_a_running_job_is_never_all_done_before_the_result_is_committed(tmp_path) -> None:
    for stage, _ in progress.STAGES:
        progress.record(tmp_path, stage, progress.DONE)
    running = progress.stage_states(tmp_path, prepared=True, finished=False, running=True)
    assert [row["state"] for row in running] == [progress.DONE, progress.DONE, progress.DONE, progress.DONE,
                                                 progress.ACTIVE]
    assert running[-1]["id"] == "build"
    committed = progress.stage_states(tmp_path, prepared=True, finished=True, running=False)
    assert all(row["state"] == progress.DONE for row in committed)


def test_internal_substages_stay_separate_from_the_public_five(tmp_path) -> None:
    progress.record(tmp_path, "window", progress.DONE, internal=True, source="engine")
    progress.record(tmp_path, "pose", progress.ACTIVE, internal=True, source="engine")
    assert progress.INTERNAL_IDS == ("probe", "window", "cut", "pose", "phase_detection", "club_tracking",
                                     "camera", "render")
    internal = progress.internal_rows(tmp_path)
    assert [row["id"] for row in internal] == list(progress.INTERNAL_IDS)
    assert [row["state"] for row in internal[:4]] == ["pending", "done", "pending", "active"]
    public = progress.stage_states(tmp_path, prepared=False, finished=False, running=True)
    assert [row["state"] for row in public] == ["pending", "pending", "pending", "pending", "pending"]
    assert [row["id"] for row in progress.stage_rows(tmp_path)] == [key for key, _ in progress.STAGES]


def test_internal_and_public_stage_ids_are_not_interchangeable(tmp_path) -> None:
    with pytest.raises(progress.UnknownStage):
        progress.record(tmp_path, "pose", progress.ACTIVE)  # internal id without internal=True
    with pytest.raises(progress.UnknownStage):
        progress.record(tmp_path, "prepare", progress.ACTIVE, internal=True)


def test_stage_durations_come_from_real_events_or_stay_unknown(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.ACTIVE)
    progress.record(tmp_path, "probe", progress.ACTIVE, internal=True)
    progress.record(tmp_path, "prepare", progress.DONE)          # measured duration
    progress.record(tmp_path, "moments", progress.DONE)          # cached: no start event
    durations = progress.stage_durations_ms(tmp_path)
    assert durations["prepare"] is not None and durations["prepare"] >= 0
    assert durations["moments"] is None, "a stage with no start event has no measured duration"
    assert durations["probe"] is None


@pytest.mark.parametrize("payload", [
    {"schema": progress.SCHEMA, "events": None},
    {"schema": progress.SCHEMA, "events": [None, 7, "x", [], {"stage": "prepare"}]},
    {"schema": progress.SCHEMA, "events": [{"stage": "prepare", "state": "done", "at_ms": "soon"}]},
    {"schema": progress.SCHEMA, "events": [{"stage": "nonsense", "state": "done", "at_ms": 1.0}]},
    {"schema": progress.SCHEMA, "events": [{"stage": "prepare", "state": "halfway", "at_ms": 1.0}]},
    {"schema": progress.SCHEMA},
])
def test_a_malformed_artifact_never_breaks_an_analysis(tmp_path, payload) -> None:
    (tmp_path / progress.FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert isinstance(progress.read(tmp_path), dict)      # the file still reads as an artifact
    assert progress.stage_rows(tmp_path) is not None      # and degrades to "nothing recorded"
    assert all(row["state"] == "pending" for row in progress.stage_rows(tmp_path))
    assert progress.stage_states(tmp_path, prepared=False, finished=False, running=True)[0]["state"] == "pending"
    assert progress.stage_durations_ms(tmp_path) is not None


def test_a_still_writable_artifact_keeps_recording_after_a_malformed_one(tmp_path) -> None:
    (tmp_path / progress.FILENAME).write_text("{not json", encoding="utf-8")
    assert progress.record(tmp_path, "prepare", progress.DONE) is not None
    assert progress.stage_rows(tmp_path)[0]["state"] == progress.DONE


def test_public_stage_contract_is_stable() -> None:
    assert progress.STAGES == (
        ("prepare", "Preparing your swing"),
        ("moments", "Finding key moments"),
        ("club", "Tracking the club"),
        ("positions", "Checking your positions"),
        ("build", "Building your analysis"),
    )
    assert progress.ACTIVE == "active" and progress.PENDING == "pending" and progress.DONE == "done"


def test_unknown_stage_or_state_is_rejected(tmp_path) -> None:
    with pytest.raises(progress.UnknownStage):
        progress.record(tmp_path, "thinking", progress.ACTIVE)
    with pytest.raises(ValueError):
        progress.record(tmp_path, "prepare", "halfway")


def test_old_bundles_and_fake_runners_fall_back_to_file_inference(tmp_path) -> None:
    # No artifact at all: the original inference must still describe the run.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    assert [row["state"] for row in progress.stage_states(tmp_path, prepared=True, finished=False, running=True)] == \
        ["done", "active", "pending", "pending", "pending"]
    (bundle / "pose_records.pkl").write_bytes(b"x")
    (bundle / "club_fallback.json").write_text("{}", encoding="utf-8")
    states = progress.stage_states(tmp_path, prepared=True, finished=False, running=True)
    assert [row["state"] for row in states] == ["done", "done", "done", "active", "pending"]


def test_corrupt_artifact_is_ignored_rather_than_fatal(tmp_path) -> None:
    (tmp_path / progress.FILENAME).write_text("{not json", encoding="utf-8")
    assert progress.read(tmp_path) is None
    assert progress.timeline(tmp_path) is None
    states = progress.stage_states(tmp_path, prepared=False, finished=False, running=True)
    assert states[0]["state"] == "active"


def test_a_finished_run_is_always_all_done(tmp_path) -> None:
    assert all(row["state"] == "done"
               for row in progress.stage_states(tmp_path, prepared=False, finished=True, running=False))


def test_timeline_keeps_every_event_for_diagnostics(tmp_path) -> None:
    progress.record(tmp_path, "prepare", progress.ACTIVE)
    progress.record(tmp_path, "prepare", progress.DONE)
    progress.record(tmp_path, "moments", progress.ACTIVE, source="engine")
    timeline = progress.timeline(tmp_path)
    assert [event["state"] for event in timeline["events"]] == ["active", "done", "active"]
    assert timeline["events"][-1]["source"] == "engine"
    assert all("at_ms" in event for event in timeline["events"])
