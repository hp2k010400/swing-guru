"""Measurement validity, units and handing of poor input quality."""

from __future__ import annotations

import pytest

from swingguru.landmarks import PoseObservation, build_landmarks
from swingguru.measurements import (
    MEASUREMENT_KEYS,
    MEASUREMENT_SPECS,
    W_LANDMARK_MISSING,
    W_LOW_VISIBILITY,
    W_NO_POSE,
    W_NO_REFERENCE,
    W_OUT_OF_FRAME,
    W_PRESENCE_INVALID,
    W_PRESENCE_MISSING,
    W_VISIBILITY_INVALID,
    W_VISIBILITY_MISSING,
    MeasurementConfig,
    build_frame_points,
    build_reference,
    compute_frame_measurements,
    landmark_rejection_reason,
    normalize_camera_view,
    summarize_series,
)

WIDTH = 200
HEIGHT = 100


def _rows(**overrides: tuple[float, float]) -> list[list[float | None]]:
    """33 landmark rows with everything visible and inside the frame."""

    rows: list[list[float | None]] = [[0.5, 0.5, 0.0, 1.0, 1.0] for _ in range(33)]
    for name, (x, y) in overrides.items():
        index = {
            "nose": 0,
            "left_shoulder": 11,
            "right_shoulder": 12,
            "left_hip": 23,
            "right_hip": 24,
            "left_knee": 25,
            "right_knee": 26,
            "left_ankle": 27,
            "right_ankle": 28,
        }[name]
        rows[index] = [x, y, 0.0, 1.0, 1.0]
    return rows


def _observation(**overrides: tuple[float, float]) -> PoseObservation:
    return PoseObservation(detected=True, landmarks=build_landmarks(_rows(**overrides)))


def _points(observation: PoseObservation, config: MeasurementConfig):
    return build_frame_points(
        observation, width=WIDTH, height=HEIGHT, config=config
    )


def _upright_observation() -> PoseObservation:
    # Left and right placed symmetrically so the midpoints sit on one vertical.
    return _observation(
        nose=(0.50, 0.20),
        left_shoulder=(0.45, 0.30),
        right_shoulder=(0.55, 0.30),
        left_hip=(0.45, 0.60),
        right_hip=(0.55, 0.60),
        left_knee=(0.45, 0.75),
        right_knee=(0.55, 0.75),
        left_ankle=(0.45, 0.90),
        right_ankle=(0.55, 0.90),
    )


def test_normalize_camera_view_aliases() -> None:
    assert normalize_camera_view(None) == "unknown"
    assert normalize_camera_view("Front") == "face-on"
    assert normalize_camera_view("dtl") == "down-the-line"
    with pytest.raises(ValueError):
        normalize_camera_view("drone")


def test_measurement_config_validates_thresholds() -> None:
    with pytest.raises(ValueError):
        MeasurementConfig(min_visibility=1.5)
    with pytest.raises(ValueError):
        MeasurementConfig(min_presence=-0.1)
    with pytest.raises(ValueError):
        MeasurementConfig(frame_margin=-1.0)
    config = MeasurementConfig(handedness="lh")
    assert config.lead == "right"
    assert config.trail == "left"


def test_compute_returns_every_measurement_in_order() -> None:
    config = MeasurementConfig()
    measurements = compute_frame_measurements(
        _points(_upright_observation(), config), None, config
    )
    assert tuple(measurements) == MEASUREMENT_KEYS


def test_upright_torso_measures_zero_degrees() -> None:
    config = MeasurementConfig(camera_view="face-on")
    measurements = compute_frame_measurements(
        _points(_upright_observation(), config), None, config
    )
    result = measurements["torso_inclination_deg"]
    assert result.valid
    assert result.value == pytest.approx(0.0, abs=1e-6)
    assert result.unit == "degrees"
    assert any("face-on" in warning for warning in result.warnings)


def test_lean_produces_the_expected_projected_angle() -> None:
    config = MeasurementConfig()
    # Frame is 200x100, so a normalised dx of 0.15 equals a normalised dy of
    # 0.30 in pixels: the hip-to-shoulder vector leans exactly 45 degrees.
    rows = [list(row) for row in _rows()]
    rows[11] = [0.20, 0.30, 0.0, 1.0, 1.0]
    rows[12] = [0.30, 0.30, 0.0, 1.0, 1.0]
    rows[23] = [0.35, 0.60, 0.0, 1.0, 1.0]
    rows[24] = [0.45, 0.60, 0.0, 1.0, 1.0]
    leaned = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(leaned, config), None, config)
    result = measurements["torso_inclination_deg"]
    assert result.valid
    assert result.value == pytest.approx(45.0, abs=1e-6)


def test_straight_lead_leg_has_no_knee_flex() -> None:
    config = MeasurementConfig()
    rows = [list(row) for row in _rows()]
    rows[23] = [0.45, 0.60, 0.0, 1.0, 1.0]
    rows[25] = [0.45, 0.75, 0.0, 1.0, 1.0]
    rows[27] = [0.45, 0.90, 0.0, 1.0, 1.0]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    assert measurements["lead_knee_flex_deg"].value == pytest.approx(0.0, abs=1e-6)


def test_handedness_swaps_lead_and_trail_knees() -> None:
    rows = [list(row) for row in _rows()]
    # Left leg straight, right leg bent: lead/trail values must swap.
    rows[23] = [0.45, 0.60, 0.0, 1.0, 1.0]
    rows[25] = [0.45, 0.75, 0.0, 1.0, 1.0]
    rows[27] = [0.45, 0.90, 0.0, 1.0, 1.0]
    rows[24] = [0.55, 0.60, 0.0, 1.0, 1.0]
    rows[26] = [0.55, 0.75, 0.0, 1.0, 1.0]
    rows[28] = [0.62, 0.75, 0.0, 1.0, 1.0]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))

    right_handed = MeasurementConfig(handedness="right")
    left_handed = MeasurementConfig(handedness="left")
    right_values = compute_frame_measurements(
        _points(observation, right_handed), None, right_handed
    )
    left_values = compute_frame_measurements(
        _points(observation, left_handed), None, left_handed
    )
    assert right_values["lead_knee_flex_deg"].value == pytest.approx(0.0, abs=1e-6)
    assert right_values["trail_knee_flex_deg"].value == pytest.approx(90.0, abs=1e-6)
    assert left_values["lead_knee_flex_deg"].value == pytest.approx(90.0, abs=1e-6)
    assert left_values["trail_knee_flex_deg"].value == pytest.approx(0.0, abs=1e-6)


def test_low_visibility_rejects_the_measurement_with_a_reason() -> None:
    config = MeasurementConfig(min_visibility=0.6)
    rows = [list(row) for row in _rows()]
    rows[25] = [0.45, 0.75, 0.0, 0.2, 1.0]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    result = measurements["lead_knee_flex_deg"]
    assert not result.valid
    assert result.value is None
    assert any(warning.startswith(W_LOW_VISIBILITY) for warning in result.warnings)


def test_low_presence_rejects_the_measurement() -> None:
    config = MeasurementConfig(min_presence=0.6)
    rows = [list(row) for row in _rows()]
    rows[24] = [0.55, 0.60, 0.0, 1.0, 0.1]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    assert not measurements["pelvis_displacement_px"].valid
    assert not measurements["torso_inclination_deg"].valid


def test_missing_landmark_rejects_the_measurement() -> None:
    config = MeasurementConfig()
    # The lead ankle is simply absent from the pose output, so the lead knee
    # has no complete hip-knee-ankle chain to measure.
    landmarks = tuple(
        item for item in build_landmarks(_rows()) if item.name != "left_ankle"
    )
    observation = PoseObservation(detected=True, landmarks=landmarks)
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    result = measurements["lead_knee_flex_deg"]
    assert not result.valid
    assert any(warning.startswith(W_LANDMARK_MISSING) for warning in result.warnings)


def test_out_of_frame_landmark_rejects_the_measurement() -> None:
    config = MeasurementConfig()
    rows = [list(row) for row in _rows()]
    rows[0] = [1.35, 0.4, 0.0, 1.0, 1.0]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    result = measurements["head_displacement_px"]
    assert not result.valid
    assert any(warning.startswith(W_OUT_OF_FRAME) for warning in result.warnings)


def test_no_pose_rejects_every_measurement() -> None:
    config = MeasurementConfig()
    points = _points(PoseObservation.missing(), config)
    assert points.warnings == (W_NO_POSE,)
    measurements = compute_frame_measurements(points, None, config)
    assert all(not result.valid for result in measurements.values())
    assert all(result.value is None for result in measurements.values())


def test_relative_measurements_need_a_reference() -> None:
    config = MeasurementConfig()
    points = _points(_upright_observation(), config)
    measurements = compute_frame_measurements(points, None, config)
    for key in (
        "head_displacement_px",
        "head_displacement_pct_height",
        "pelvis_displacement_px",
        "pelvis_displacement_pct_height",
        "torso_angle_change_deg",
    ):
        result = measurements[key]
        assert not result.valid
        assert W_NO_REFERENCE in result.warnings


def test_reference_produces_known_displacement() -> None:
    config = MeasurementConfig()
    reference_points = _points(_upright_observation(), config)
    reference = build_reference(
        0, reference_points, width=WIDTH, height=HEIGHT, config=config
    )
    assert reference.selected

    rows = [list(row) for row in _rows()]
    rows[0] = [0.60, 0.20, 0.0, 1.0, 1.0]  # nose moves 0.10 in x
    rows[11] = [0.45, 0.30, 0.0, 1.0, 1.0]
    rows[12] = [0.55, 0.30, 0.0, 1.0, 1.0]
    rows[23] = [0.45, 0.60, 0.0, 1.0, 1.0]
    rows[24] = [0.55, 0.60, 0.0, 1.0, 1.0]
    moved = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(moved, config), reference, config)

    head = measurements["head_displacement_px"]
    assert head.valid
    assert head.value == pytest.approx(0.10 * WIDTH, rel=1e-6)
    assert head.reference_frame == 0

    percent = measurements["head_displacement_pct_height"]
    assert percent.valid
    assert percent.value == pytest.approx(0.10 * WIDTH / HEIGHT * 100.0, rel=1e-6)


def test_torso_angle_change_requires_both_frames() -> None:
    config = MeasurementConfig()
    reference_points = _points(_upright_observation(), config)
    reference = build_reference(
        0, reference_points, width=WIDTH, height=HEIGHT, config=config
    )
    rows = [list(row) for row in _rows()]
    rows[11] = [0.20, 0.30, 0.0, 1.0, 1.0]
    rows[12] = [0.30, 0.30, 0.0, 1.0, 1.0]
    rows[23] = [0.35, 0.60, 0.0, 1.0, 1.0]
    rows[24] = [0.45, 0.60, 0.0, 1.0, 1.0]
    leaned = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(leaned, config), reference, config)
    change = measurements["torso_angle_change_deg"]
    assert change.valid
    assert change.value == pytest.approx(45.0, abs=1e-6)
    assert change.components["reference_deg"] == pytest.approx(0.0, abs=1e-6)


def test_torso_angle_change_unavailable_when_reference_is_invalid() -> None:
    config = MeasurementConfig()
    reference = build_reference(
        0, None, width=WIDTH, height=HEIGHT, config=config
    )
    measurements = compute_frame_measurements(
        _points(_upright_observation(), config), reference, config
    )
    assert not measurements["torso_angle_change_deg"].valid


def test_reference_from_a_low_quality_frame_is_reported() -> None:
    config = MeasurementConfig(min_visibility=0.9)
    rows = [list(row) for row in _rows()]
    rows[0] = [0.5, 0.2, 0.0, 0.3, 0.3]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    reference = build_reference(
        0, _points(observation, config), width=WIDTH, height=HEIGHT, config=config
    )
    assert reference.selected
    assert reference.head_point is None
    assert reference.notes


def test_summarize_series_ignores_missing_values() -> None:
    summary = summarize_series([1.0, None, 3.0])
    assert summary["count"] == 2
    assert summary["min"] == pytest.approx(1.0)
    assert summary["max"] == pytest.approx(3.0)
    assert summary["range"] == pytest.approx(2.0)
    empty = summarize_series([None, None])
    assert empty["count"] == 0
    assert empty["range"] is None


# ---------------------------------------------------------------------------
# Fail-closed landmark quality
# ---------------------------------------------------------------------------


def _landmark(**overrides):
    from swingguru.landmarks import Landmark

    values = {
        "index": 0,
        "name": "nose",
        "x": 0.5,
        "y": 0.5,
        "z": 0.0,
        "visibility": 0.9,
        "presence": 0.9,
    }
    values.update(overrides)
    return Landmark(**values)


def test_missing_visibility_is_rejected() -> None:
    config = MeasurementConfig()
    assert landmark_rejection_reason(
        _landmark(visibility=None), "nose", config
    ).startswith(W_VISIBILITY_MISSING)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.5])
def test_invalid_visibility_is_rejected(value: float) -> None:
    config = MeasurementConfig()
    reason = landmark_rejection_reason(_landmark(visibility=value), "nose", config)
    assert reason is not None and reason.startswith(W_VISIBILITY_INVALID)


def test_missing_presence_is_rejected_when_required() -> None:
    config = MeasurementConfig(require_presence=True)
    assert landmark_rejection_reason(
        _landmark(presence=None), "nose", config
    ).startswith(W_PRESENCE_MISSING)
    # Presence is optional when the config says so.
    relaxed = MeasurementConfig(require_presence=False)
    assert landmark_rejection_reason(_landmark(presence=None), "nose", relaxed) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.5, 2.0])
def test_invalid_presence_is_rejected(value: float) -> None:
    config = MeasurementConfig()
    reason = landmark_rejection_reason(_landmark(presence=value), "nose", config)
    assert reason is not None and reason.startswith(W_PRESENCE_INVALID)


def test_nan_visibility_rejects_the_measurement_not_just_the_landmark() -> None:
    config = MeasurementConfig()
    rows = [list(row) for row in _rows()]
    rows[0] = [0.5, 0.2, 0.0, float("nan"), 1.0]
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    assert not measurements["head_displacement_px"].valid
    assert any(
        warning.startswith(W_VISIBILITY_INVALID)
        for warning in measurements["head_displacement_px"].warnings
    )


def test_undetected_frame_yields_no_points_even_with_stale_landmarks() -> None:
    config = MeasurementConfig()
    stale = PoseObservation(detected=False, landmarks=build_landmarks(_rows()))
    points = build_frame_points(stale, width=WIDTH, height=HEIGHT, config=config)
    assert points.pixels == {}
    assert points.landmarks == {}
    assert points.usable == ()
    assert points.rejected
    assert set(points.rejected.values()) == {W_NO_POSE}
    measurements = compute_frame_measurements(points, None, config)
    assert all(not result.valid for result in measurements.values())
    assert measurements["torso_inclination_deg"].warnings[0] == W_NO_POSE


def test_absent_nose_reports_the_missing_landmark_reason() -> None:
    config = MeasurementConfig()
    landmarks = tuple(
        item for item in build_landmarks(_rows()) if item.name != "nose"
    )
    observation = PoseObservation(detected=True, landmarks=landmarks)
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    result = measurements["head_displacement_px"]
    assert not result.valid
    assert any(
        warning.startswith(W_LANDMARK_MISSING) and "nose" in warning
        for warning in result.warnings
    )


# ---------------------------------------------------------------------------
# Handedness-aware landmark indices
# ---------------------------------------------------------------------------


def test_knee_measurements_report_handedness_correct_indices() -> None:
    right = MeasurementConfig(handedness="right")
    left = MeasurementConfig(handedness="left")
    right_values = compute_frame_measurements(
        _points(_upright_observation(), right), None, right
    )
    left_values = compute_frame_measurements(
        _points(_upright_observation(), left), None, left
    )
    assert right_values["lead_knee_flex_deg"].landmark_indices == (23, 25, 27)
    assert right_values["trail_knee_flex_deg"].landmark_indices == (24, 26, 28)
    assert left_values["lead_knee_flex_deg"].landmark_indices == (24, 26, 28)
    assert left_values["trail_knee_flex_deg"].landmark_indices == (23, 25, 27)


def test_rejected_knee_measurement_also_reports_handedness_correct_indices() -> None:
    config = MeasurementConfig(handedness="left", min_visibility=0.9)
    rows = [list(row) for row in _rows()]
    rows[26] = [0.55, 0.75, 0.0, 0.1, 0.1]  # right knee, the lead knee for LH
    observation = PoseObservation(detected=True, landmarks=build_landmarks(rows))
    measurements = compute_frame_measurements(_points(observation, config), None, config)
    result = measurements["lead_knee_flex_deg"]
    assert not result.valid
    assert result.landmark_indices == (24, 26, 28)


def test_measurement_specs_resolve_indices_from_config() -> None:
    spec = next(s for s in MEASUREMENT_SPECS if s.key == "lead_knee_flex_deg")
    right = spec.to_dict(MeasurementConfig(handedness="right"))
    left = spec.to_dict(MeasurementConfig(handedness="left"))
    assert right["landmarks"] == ["left_hip", "left_knee", "left_ankle"]
    assert right["landmark_indices"] == [23, 25, 27]
    assert left["landmarks"] == ["right_hip", "right_knee", "right_ankle"]
    assert left["landmark_indices"] == [24, 26, 28]
    assert right["resolved_side_rules"] == ["lead_hip", "lead_knee", "lead_ankle"]
