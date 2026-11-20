"""The optional OWLv2 club model missing or broken must not fail the analysis.

Body measurements and the classical club tracker carry on; only the club lines the fallback would
have rescued are withheld (fail closed). One load attempt per run, nothing cached for frames the
model never saw, and genuine programming errors still surface.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from test_performance_build import ANALYSIS_PHASES, PHASES, _canvas, _moments, _record, _track, _video_file  # isort: skip

import build_analysis  # noqa: E402  (scripts/ is on sys.path via test_performance_build)
from swingguru import diagnostics as diag  # noqa: E402
from swingguru.club import fallback as fb  # noqa: E402


def _fake_transformers(monkeypatch, error: BaseException, calls: list) -> None:
    class Loader:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(kwargs)
            raise error

    module = types.SimpleNamespace(Owlv2ForObjectDetection=Loader, Owlv2Processor=Loader)
    monkeypatch.setitem(sys.modules, "transformers", module)


@pytest.mark.parametrize("error", [OSError("no file named model.safetensors"), ValueError("bad config"),
                                   ImportError("no transformers")])
def test_a_missing_or_corrupt_model_is_unavailable_after_one_attempt(monkeypatch, error) -> None:
    calls: list = []
    _fake_transformers(monkeypatch, error, calls)
    owl = fb.OwlShaftFallback()
    with pytest.raises(fb.ModelUnavailable):
        owl._ensure()
    with pytest.raises(fb.ModelUnavailable):
        owl._ensure()
    assert len(calls) == 1, "a failed load is attempted once per run"
    assert calls[0].get("local_files_only") is True, "nothing is downloaded at analysis time"
    assert owl.unavailable and "not available locally" in owl.unavailable


def test_a_programming_error_while_loading_still_raises(monkeypatch) -> None:
    _fake_transformers(monkeypatch, TypeError("unexpected keyword"), [])
    with pytest.raises(TypeError):
        fb.OwlShaftFallback()._ensure()


class _UnavailableOwl:
    instances: list = []

    def __init__(self) -> None:
        self.load_seconds = 0.0
        self.calls = 0
        _UnavailableOwl.instances.append(self)

    def boxes(self, *args, **kwargs):
        self.calls += 1
        raise fb.ModelUnavailable("OSError: the optional club model is not available locally")


def test_run_fallback_degrades_to_classical_only(tmp_path, monkeypatch) -> None:
    moments = _moments(PHASES)
    records = [_record(index) for index in range(60)]
    frames = {frame: _canvas(index) for index, frame in enumerate(PHASES.values())}
    levels = {frame: "HIGH" for frame in PHASES.values()}
    levels[PHASES[ANALYSIS_PHASES[2]]] = "LOW"
    levels[PHASES[ANALYSIS_PHASES[3]]] = "UNAVAILABLE"
    monkeypatch.setattr(build_analysis, "OwlShaftFallback", _UnavailableOwl)
    timings: dict = {}
    overrides, log = build_analysis.run_fallback(_video_file(tmp_path), tmp_path, records, frames, moments,
                                                 _track(levels), timings)
    assert overrides == {}, "no club line is invented for the phases the model would have checked"
    assert [e["phase"] for e in log] == ["mid_back", "top"]
    assert all(e["level"] == "UNAVAILABLE" and e["model_unavailable"] for e in log)
    assert timings["owlv2_unavailable"] and timings["owlv2_frames_run"] == 0
    assert timings["owlv2_inference_s"] == 0.0, "a failed load is never reported as inference time"
    cache = json.loads((tmp_path / "club_fallback.json").read_text(encoding="utf-8"))
    assert cache == {}, "no negative box cache: a later run with the model installed still gets a real answer"


def test_diagnostics_report_the_unavailable_model() -> None:
    analysis = {"phases": [{"phase": "address", "checks": []}], "hidden": [],
                "engine": {"club_fallback": [{"phase": "top", "level": "UNAVAILABLE", "model_unavailable": True}],
                           "counters": {"owlv2_frames_run": 0}, "timings": {"owlv2_unavailable": "OSError: ..."}}}
    assert diag.club_coverage(analysis)["fallback_model_unavailable"] is True
    analysis["engine"]["timings"] = {"owlv2_unavailable": None}
    assert diag.club_coverage(analysis)["fallback_model_unavailable"] is False
