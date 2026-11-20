"""Stage timings must survive the log they were printed into."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import summarise_analysis_runs as summary  # noqa: E402


def _payload() -> dict:
    return {
        "shown": ["address:shaft 56 deg"],
        "engine": {
            "pose": "rtmw",
            "club_tracker": "classical-ridge-v1",
            "timings": {"pose_s": 37.8, "club_classical_s": 6.4, "total_s": 71.6},
            "counters": {"club_frames_analysed": 40},
            "club_windows": {"mode": "event-windows", "radius_frames": 5, "candidate_cache": {"hits": 0}},
            "club_fallback": [{"phase": "mid_back", "frame": 120, "level": "MEDIUM", "angle": 138.8, "cached": False}],
        },
    }


def test_a_build_log_with_trailing_chatter_is_read(tmp_path) -> None:
    path = tmp_path / "run.log"
    body = json.dumps(_payload(), indent=2)
    path.write_text("load model\n" + body + "\n[onnxruntime] extra chatter\n", encoding="utf-8")
    result = summary.summarise(path)
    assert result["club_mode"] == "event-windows"
    assert result["window_radius_frames"] == 5
    assert result["timings_s"]["total_s"] == 71.6
    assert result["counters"]["club_frames_analysed"] == 40
    assert result["club_fallback"][0]["phase"] == "mid_back"


def test_an_analysis_json_file_is_read(tmp_path) -> None:
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    result = summary.summarise(path)
    assert result["timings_s"]["pose_s"] == 37.8
    assert result["candidate_cache"] == {"hits": 0}


def test_a_log_without_a_summary_is_reported(tmp_path) -> None:
    import pytest

    path = tmp_path / "empty.log"
    path.write_text("nothing here", encoding="utf-8")
    with pytest.raises(SystemExit):
        summary.summarise(path)


def test_the_main_writes_the_summary_file(tmp_path, capsys) -> None:
    log = tmp_path / "run.log"
    log.write_text(json.dumps(_payload(), indent=2), encoding="utf-8")
    out = tmp_path / "timings.json"
    assert summary.main([f"one={log}", "--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["one"]["timings_s"]["total_s"] == 71.6
    assert "one" in capsys.readouterr().out
