"""Documentation must stay in step with the implemented measurements."""

from __future__ import annotations

from pathlib import Path

from swingguru.measurements import MEASUREMENT_SPECS

ROOT = Path(__file__).resolve().parents[1]


def test_measurements_doc_exists_and_documents_every_measurement() -> None:
    path = ROOT / "docs" / "measurements.md"
    assert path.exists(), "docs/measurements.md is part of the v0.1 deliverable"
    text = path.read_text(encoding="utf-8")
    for spec in MEASUREMENT_SPECS:
        assert spec.key in text, f"{spec.key} is not documented"
        assert spec.unit in text, f"{spec.key} unit is not documented"


def test_measurements_doc_states_the_camera_limitations() -> None:
    text = (ROOT / "docs" / "measurements.md").read_text(encoding="utf-8").lower()
    assert "single-camera" in text
    assert "not" in text and "centimet" in text
    assert "forward bend" in text


def test_readme_documents_install_and_launch() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python -m venv .venv" in text
    assert "streamlit run src/swingguru/ui/app.py" in text
    assert "download-model" in text
    assert "pytest" in text


def test_report_exists() -> None:
    assert (ROOT / "docs" / "agent-work" / "engine-v01" / "REPORT.md").exists()


def test_moment_detection_doc_documents_the_estimator() -> None:
    path = ROOT / "docs" / "moment-detection.md"
    assert path.exists(), "the moment estimator needs its own documentation"
    text = path.read_text(encoding="utf-8")
    from swingguru.auto_moments import MomentConfig

    for field in MomentConfig.__dataclass_fields__:
        assert field in text, f"{field} is not documented"
    lowered = text.lower()
    assert "estimated impact" in lowered
    assert "not" in lowered and "contact" in lowered
    assert "percentage" in lowered or "percentages" in lowered
