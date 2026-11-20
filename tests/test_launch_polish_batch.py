"""The batch diagnostics report: drop-in manifests, explicit errors, honest rows."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from swingguru import progress
from swingguru.analysis_runner import RunResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def batch():
    spec = importlib.util.spec_from_file_location("launch_polish_batch", ROOT / "scripts" / "launch_polish_batch.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["launch_polish_batch"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _manifest(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"clips": entries}), encoding="utf-8")
    return path


def _analysis() -> dict:
    return {"video": "swing.mp4", "phases": [{"phase": "address", "frame_index": 3, "checks": [{"guided": True}]}],
            "hidden": [], "camera_check": {"signals": {}},
            "swing_score": {"available": True, "total": 7.5, "version": "swing_score_v1", "measured": 8,
                            "eligible": 8, "coverage": 1.0}}


def test_missing_clip_is_an_explicit_error_row(tmp_path, batch, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(batch, "run_analysis", lambda *a, **k: called.append(1))
    manifest = _manifest(tmp_path, [{"id": "gone", "path": str(tmp_path / "nope.mp4")}])
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out)]) == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    row = results["clips"][0]
    assert row["status"] == "error" and row["error"] == "missing_clip" and "no file at" in row["detail"]
    assert not called, "a missing clip must never reach the engine"
    assert (out / "SUMMARY.md").is_file() and (out / "results.csv").is_file()


def test_report_carries_identity_score_coverage_and_cache_mode(tmp_path, batch, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"payload" * 200)

    def runner(upload: Path, work: Path, *, on_prepared=None, timeout_s=1.0):
        progress.record(work, "prepare", progress.DONE)
        progress.record(work, "moments", progress.DONE)
        progress.record(work, "club", progress.DONE)
        progress.record(work, "positions", progress.DONE)
        progress.record(work, "build", progress.DONE)
        bundle = work / "bundle"
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "analysis.json").write_text(json.dumps(_analysis()), encoding="utf-8")
        diagnostics = {"schema": "swingguru.run_diagnostics/1", "outcome": "ok", "failure": None,
                       "cache": {"pose_cache": "hit"}, "stages": progress.stage_rows(work),
                       "internal_stages": progress.internal_rows(work),
                       "signals": {"pose_coverage": {"detected_frame_coverage": 0.98,
                                                     "reliable_landmark_coverage": 0.9},
                                   "club_coverage": {"phases_with_club": 4},
                                   "phase_confidence": {"located": 6},
                                   "person_confidence": {"value": None, "unavailable": "not retained"}}}
        return RunResult(True, bundle=bundle, timings={"probe_s": 0.1, "total_s": 3.0}, diagnostics=diagnostics)

    monkeypatch.setattr(batch, "run_analysis", runner)
    manifest = _manifest(tmp_path, [{"id": "modern", "path": str(clip), "kind": "source"}])
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out), "--label", "modern-controls"]) == 0
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    row = results["clips"][0]
    assert row["status"] == "accepted" and row["score"]["total"] == 7.5
    assert row["pose_coverage"]["detected_frame_coverage"] == 0.98
    assert row["pose_coverage"]["reliable_landmark_coverage"] == 0.9
    assert row["club_coverage"]["phases_with_club"] == 4
    assert row["phases_located"] == 1 and row["cache_mode"] == "warm"
    assert row["diagnostics_schema"] == "swingguru.run_diagnostics/1"
    assert set(results["identity"]["files"]) >= {"src/swingguru/analysis_runner.py",
                                                 "scripts/build_analysis.py"}
    assert all(len(value) == 16 for value in results["identity"]["files"].values())
    csv_text = (out / "results.csv").read_text(encoding="utf-8")
    assert "modern" in csv_text and "warm" in csv_text


def test_degraded_variant_manifest_is_marked_synthetic(tmp_path, batch, monkeypatch) -> None:
    clip = tmp_path / "clip.fps24.mp4"
    clip.write_bytes(b"x" * 100)
    monkeypatch.setattr(batch, "run_analysis", lambda *a, **k: RunResult(False, "no_swing", detail=""))
    manifest = tmp_path / "variants.json"
    manifest.write_text(json.dumps({"schema": "swingguru.synthetic_variant/1", "synthetic": True, "variants": [
        {"variant": "fps24", "path": str(clip), "synthetic": True, "parameters": {"fps": 24},
         "description": "re-timed to 24 fps"}]}), encoding="utf-8")
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out), "--label", "degraded"]) == 0
    row = json.loads((out / "results.json").read_text(encoding="utf-8"))["clips"][0]
    assert row["synthetic"] is True and row["variant"] == "fps24" and row["parameters"] == {"fps": 24}
    assert row["status"] == "rejected" and row["error"] == "no_swing"
    assert "(synthetic)" in (out / "SUMMARY.md").read_text(encoding="utf-8")


def test_wall_budget_marks_the_remainder_not_run(tmp_path, batch, monkeypatch) -> None:
    monkeypatch.setattr(batch, "run_analysis", lambda *a, **k: RunResult(False, "no_swing"))
    first = tmp_path / "a.mp4"
    first.write_bytes(b"x" * 10)
    manifest = _manifest(tmp_path, [{"id": "a", "path": str(first)}, {"id": "b", "path": str(first)}])
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out), "--max-seconds", "0"]) == 0
    rows = json.loads((out / "results.json").read_text(encoding="utf-8"))["clips"]
    assert [row["status"] for row in rows] == ["not_run", "not_run"]


def test_adversarial_ids_never_delete_outside_the_run_folder(tmp_path, batch) -> None:
    work_root = tmp_path / "out" / "work"
    work_root.mkdir(parents=True)
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep", encoding="utf-8")

    assert batch.remove_run_dir(work_root, work_root, source=None) == \
        "refused: the run folder is the output work root"
    assert batch.remove_run_dir(victim, work_root, source=None) == \
        "refused: run folder is outside the output work root"
    assert victim.is_dir() and (victim / "keep.txt").is_file()

    # An empty or colliding id still yields a unique, contained run folder.
    assert batch.run_ident({"id": "///", "path": "clip.mp4"}).startswith("clip-")
    assert batch.run_ident({"id": "", "path": "clip.mp4"}) == batch.run_ident({"id": "", "path": "clip.mp4"})


def test_a_source_clip_inside_the_output_folder_is_never_deleted(tmp_path, batch, monkeypatch) -> None:
    work_root = tmp_path / "out" / "work"
    run_dir = work_root / "clip-deadbeef"
    run_dir.mkdir(parents=True)
    source = run_dir / "clip.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    monkeypatch.setattr(batch, "run_analysis", lambda *a, **k: RunResult(False, "no_swing", detail=""))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"clips": [{"id": "clip", "path": str(source)}]}), encoding="utf-8")
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out)]) == 0
    assert source.is_file(), "a source clip inside the output tree must survive"
    row = json.loads((out / "results.json").read_text(encoding="utf-8"))["clips"][0]
    assert row["status"] == "rejected"


@pytest.mark.parametrize("payload", [
    "{not json",
    json.dumps({"nope": []}),
    json.dumps([1, 2, 3]),
    json.dumps({"clips": [{"id": "no-path"}]}),
    json.dumps({"clips": ["not-an-object"]}),
])
def test_malformed_manifests_are_reported_not_raised(tmp_path, batch, payload, capsys) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(payload, encoding="utf-8")
    assert batch.main(["--manifest", str(manifest), "--out", str(tmp_path / "report")]) == 2
    assert "manifest error" in capsys.readouterr().err


def test_runner_exception_keeps_the_real_stage_evidence(tmp_path, batch, monkeypatch) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)

    def runner(upload: Path, work: Path, *, on_prepared=None, timeout_s=1.0):
        progress.record(work, "prepare", progress.DONE)
        progress.record(work, "probe", progress.DONE, internal=True)
        progress.record(work, "window", progress.ACTIVE, internal=True)
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(batch, "run_analysis", runner)
    manifest = _manifest(tmp_path, [{"id": "boom", "path": str(clip)}])
    out = tmp_path / "report"
    assert batch.main(["--manifest", str(manifest), "--out", str(out)]) == 0
    row = json.loads((out / "results.json").read_text(encoding="utf-8"))["clips"][0]
    assert row["status"] == "error" and row["error"] == "runner_raised"
    assert row["internal_stage"] == "window" and row["failure"]["stage"] == "moments"
    assert [stage["id"] for stage in row["internal_stages"]][:3] == ["probe", "window", "cut"]
    assert "engine exploded" in row["detail"]


def test_cache_mode_uses_the_real_pose_cache_status(batch) -> None:
    for status, expected in (("hit", "warm"), ("hit-legacy", "warm"), ("miss-missing", "cold"),
                             ("miss-identity", "cold"), (None, "unknown"), ("something-new", "unknown")):
        assert batch.cache_mode({"cache": {"pose_cache": status}}) == expected
