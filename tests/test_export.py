"""CSV and JSON export shape, rounding and summaries."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from swingguru import export
from swingguru.landmarks import PoseObservation
from swingguru.measurements import MEASUREMENT_KEYS, MeasurementConfig
from swingguru.phases import PhaseSelection
from swingguru.pipeline import apply_reference, build_record
from swingguru.synthetic import SyntheticClip, joints_to_observation, synthetic_joints

CLIP = SyntheticClip(width=320, height=180, fps=24.0, frame_count=6)


def _records(config: MeasurementConfig, *, lost_frames: tuple[int, ...] = ()):
    frame = np.zeros((CLIP.height, CLIP.width, 3), dtype=np.uint8)
    records = []
    for index in range(CLIP.frame_count):
        observation = (
            PoseObservation.missing("scripted loss")
            if index in lost_frames
            else joints_to_observation(synthetic_joints(index, clip=CLIP))
        )
        records.append(
            build_record(
                index,
                index * 1000.0 / CLIP.fps,
                frame,
                observation,
                config=config,
            )
        )
    # Frame 0 anchors the relative measurements, exactly as a run does when the
    # user selects the address frame as the reference.
    apply_reference(records, 0, config=config)
    return records


def test_measurements_header_covers_every_measurement() -> None:
    header = export.measurements_header()
    for key in MEASUREMENT_KEYS:
        assert key in header
        assert f"{key}_valid" in header
    assert header[:6] == [
        "frame_index",
        "timestamp_ms",
        "frame_width",
        "frame_height",
        "pose_detected",
        "reference_frame",
    ]


def test_measurements_csv_has_one_row_per_frame(tmp_path: Path) -> None:
    config = MeasurementConfig()
    records = _records(config)
    path = export.write_measurements_csv(records, tmp_path / "measurements.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == CLIP.frame_count
    assert [row["frame_index"] for row in rows] == [str(i) for i in range(CLIP.frame_count)]
    assert all(row["pose_detected"] == "1" for row in rows)
    assert rows[0]["torso_inclination_deg_valid"] == "1"
    # The CSV value must round-trip the in-memory measurement exactly.
    assert float(rows[0]["torso_inclination_deg"]) == pytest.approx(
        records[0].measurements["torso_inclination_deg"].value, abs=1e-6
    )
    assert 0.0 <= float(rows[0]["torso_inclination_deg"]) <= 180.0


def test_measurements_csv_records_rejections(tmp_path: Path) -> None:
    config = MeasurementConfig()
    records = _records(config, lost_frames=(2,))
    path = export.write_measurements_csv(records, tmp_path / "measurements.csv")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    lost = rows[2]
    assert lost["pose_detected"] == "0"
    assert lost["torso_inclination_deg"] == ""
    assert lost["torso_inclination_deg_valid"] == "0"
    assert "no pose detected" in lost["warnings"]


def test_landmarks_csv_is_long_form_with_quality_flags(tmp_path: Path) -> None:
    config = MeasurementConfig(min_visibility=0.6)
    records = _records(config, lost_frames=(4,))
    path = export.write_landmarks_csv(
        records, tmp_path / "landmarks.csv", config=config
    )
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == (CLIP.frame_count - 1) * 33
    assert {row["frame_index"] for row in rows} == {"0", "1", "2", "3", "5"}
    assert all(row["in_frame"] == "1" for row in rows)
    assert all(row["usable"] == "1" for row in rows)
    assert rows[0]["landmark_name"] == "nose"
    assert float(rows[0]["x"]) == pytest.approx(
        synthetic_joints(0, clip=CLIP)["nose"][0], abs=1e-6
    )


def test_landmarks_csv_marks_unusable_landmarks(tmp_path: Path) -> None:
    # The drawn figure's knees sit at visibility 0.94, so a 0.90 threshold
    # keeps the right knee usable while the knocked-down left knee fails.
    config = MeasurementConfig(min_visibility=0.90)
    frame = np.zeros((CLIP.height, CLIP.width, 3), dtype=np.uint8)
    joints = synthetic_joints(0, clip=CLIP)
    joints["left_knee"] = (*joints["left_knee"][:3], 0.2, 0.2)
    record = build_record(
        0, 0.0, frame, joints_to_observation(joints), config=config
    )
    path = export.write_landmarks_csv(
        [record], tmp_path / "landmarks.csv", config=config
    )
    with path.open(newline="", encoding="utf-8") as handle:
        rows = {row["landmark_name"]: row for row in csv.DictReader(handle)}
    assert rows["left_knee"]["usable"] == "0"
    assert rows["right_knee"]["usable"] == "1"


def test_landmarks_csv_fails_closed_on_nan_quality(tmp_path: Path) -> None:
    """A NaN visibility must not read as usable anywhere in the export."""

    config = MeasurementConfig()
    frame = np.zeros((CLIP.height, CLIP.width, 3), dtype=np.uint8)
    joints = synthetic_joints(0, clip=CLIP)
    nose = joints["nose"]
    joints["nose"] = (nose[0], nose[1], nose[2], float("nan"), nose[4])
    record = build_record(
        0, 0.0, frame, joints_to_observation(joints), config=config
    )
    path = export.write_landmarks_csv(
        [record], tmp_path / "landmarks.csv", config=config
    )
    with path.open(newline="", encoding="utf-8") as handle:
        rows = {row["landmark_name"]: row for row in csv.DictReader(handle)}
    assert "quality" in export.landmarks_header()
    assert rows["nose"]["usable"] == "0"
    assert "not finite or outside 0..1" in rows["nose"]["quality"]
    assert rows["left_knee"]["usable"] == "1"
    assert rows["left_knee"]["quality"] == ""


def test_metadata_specs_carry_handedness_correct_indices(tmp_path: Path) -> None:
    config = MeasurementConfig(handedness="left")
    metadata = export.build_metadata(
        input_path=tmp_path / "clip.mp4",
        info={"width": 320, "height": 180},
        config=config,
        pipeline_config={},
        phases=PhaseSelection(frames={}, reference_frame=None),
        artifacts={},
        runtime={},
        stats=export.summarize_records([]),
        warnings=[],
    )
    specs = {spec["key"]: spec for spec in metadata["measurements"]}
    assert specs["lead_knee_flex_deg"]["landmark_indices"] == [24, 26, 28]
    assert specs["trail_knee_flex_deg"]["landmark_indices"] == [23, 25, 27]


def test_json_writer_round_trips(tmp_path: Path) -> None:
    path = export.write_json({"a": 1, "b": [1.5, None]}, tmp_path / "payload.json")
    assert json.loads(path.read_text("utf-8")) == {"a": 1, "b": [1.5, None]}


def test_summarize_records_counts_validity_and_stability() -> None:
    config = MeasurementConfig()
    records = _records(config, lost_frames=(3,))
    summary = export.summarize_records(records)
    assert summary["frames"] == CLIP.frame_count
    assert summary["frames_with_pose"] == CLIP.frame_count - 1
    assert summary["frames_without_pose"] == 1
    assert summary["pose_detection_rate"] == pytest.approx(
        (CLIP.frame_count - 1) / CLIP.frame_count
    )
    torso = summary["measurements"]["torso_inclination_deg"]
    assert torso["valid_frames"] == CLIP.frame_count - 1
    assert torso["invalid_frames"] == 1
    assert "no pose detected in this frame" in torso["rejection_reasons"]
    # The synthetic figure moves, so head displacement away from the reference
    # frame has real spread.
    assert summary["stability"]["head_displacement_px_range"] > 0.0
    assert summary["stability"]["head_displacement_px_range"] == pytest.approx(
        torso["stats"] and summary["measurements"]["head_displacement_px"]["stats"]["range"]
    )
    assert "stability_note" in summary


def test_build_metadata_includes_config_and_artifacts(tmp_path: Path) -> None:
    config = MeasurementConfig(handedness="left", camera_view="down-the-line")
    records = _records(config)
    phases = PhaseSelection(frames={}, reference_frame=None)
    metadata = export.build_metadata(
        input_path=tmp_path / "clip.mp4",
        info={"width": 320, "height": 180},
        config=config,
        pipeline_config={"max_frames": 10},
        phases=phases,
        artifacts={"measurements_csv": "measurements.csv"},
        runtime={"python": "3.12.10"},
        stats=export.summarize_records(records),
        warnings=["example warning"],
    )
    assert metadata["engine_version"] == "0.1.0"
    assert metadata["measurement_config"]["lead_side"] == "right"
    assert metadata["measurement_config"]["camera_view"] == "down-the-line"
    assert metadata["reference_frame"] is None
    assert metadata["warnings"] == ["example warning"]
    assert metadata["artifacts"]["measurements_csv"] == "measurements.csv"
    assert len(metadata["landmark_names"]) == 33
    assert any("uncalibrated" in note for note in metadata["notes"])


def test_phase_rows_mark_the_reference() -> None:
    from swingguru.phases import Phase

    rows = list(
        export.phase_rows(
            PhaseSelection(frames={Phase.ADDRESS: 0, Phase.TOP: 9}, reference_frame=0)
        )
    )
    address = next(row for row in rows if row["phase"] == "address")
    takeaway = next(row for row in rows if row["phase"] == "takeaway")
    assert address["is_reference"] is True
    assert address["frame_index"] == 0
    assert takeaway["assigned"] is False
