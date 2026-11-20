"""Deterministic geometry: angles, aspect correction and rejection cases."""

from __future__ import annotations

import math

import numpy as np
import pytest

from swingguru import geometry


def test_to_pixel_scales_each_axis_independently() -> None:
    point = geometry.to_pixel((0.5, 0.9), 200, 100)
    assert point[0] == pytest.approx(100.0)
    assert point[1] == pytest.approx(90.0)


def test_aspect_correction_changes_the_measured_angle() -> None:
    # The hip-to-shoulder vector is 45 degrees from vertical in normalised
    # space, but the frame is 200x100, so in pixels it is atan(20/10) = 63.4
    # degrees. Measuring in normalised coordinates would be wrong here.
    hip = geometry.to_pixel((0.0, 0.2), 200, 100)
    shoulder = geometry.to_pixel((0.1, 0.1), 200, 100)
    angle = geometry.torso_inclination_degrees(hip, shoulder)
    assert angle == pytest.approx(math.degrees(math.atan2(20.0, 10.0)), rel=1e-6)
    assert angle != pytest.approx(45.0)


def test_torso_inclination_is_zero_when_directly_above() -> None:
    hip = np.array([100.0, 200.0])
    shoulder = np.array([100.0, 100.0])
    assert geometry.torso_inclination_degrees(hip, shoulder) == pytest.approx(0.0)


def test_torso_inclination_reports_45_degrees_for_a_45_degree_lean() -> None:
    hip = np.array([100.0, 200.0])
    shoulder = np.array([200.0, 100.0])
    assert geometry.torso_inclination_degrees(hip, shoulder) == pytest.approx(45.0)


def test_torso_inclination_is_direction_agnostic() -> None:
    hip = np.array([100.0, 200.0])
    left = geometry.torso_inclination_degrees(hip, np.array([50.0, 100.0]))
    right = geometry.torso_inclination_degrees(hip, np.array([150.0, 100.0]))
    assert left == pytest.approx(right)


def test_straight_leg_has_no_flex() -> None:
    hip = np.array([0.0, 0.0])
    knee = np.array([0.0, 50.0])
    ankle = np.array([0.0, 100.0])
    assert geometry.interior_knee_angle(hip, knee, ankle) == pytest.approx(180.0)
    assert geometry.knee_flex_degrees(hip, knee, ankle) == pytest.approx(0.0)


def test_right_angled_knee_is_90_degrees_of_flex() -> None:
    hip = np.array([0.0, 0.0])
    knee = np.array([0.0, 50.0])
    ankle = np.array([50.0, 50.0])
    assert geometry.interior_knee_angle(hip, knee, ankle) == pytest.approx(90.0)
    assert geometry.knee_flex_degrees(hip, knee, ankle) == pytest.approx(90.0)


def test_degenerate_geometry_is_rejected() -> None:
    point = np.array([10.0, 10.0])
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.torso_inclination_degrees(point, point.copy())
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.knee_flex_degrees(point, point.copy(), np.array([20.0, 20.0]))
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.angle_at_vertex(point, point.copy(), point.copy())


def test_non_finite_geometry_is_rejected() -> None:
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.torso_inclination_degrees(
            np.array([0.0, 0.0]), np.array([math.nan, 1.0])
        )


def test_zero_length_vector_is_rejected_for_angle_between() -> None:
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.angle_between_vectors(np.array([0.0, 0.0]), np.array([1.0, 0.0]))


def test_percent_of_reference() -> None:
    assert geometry.percent_of_reference(10.0, 50.0) == pytest.approx(20.0)
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.percent_of_reference(10.0, 0.0)
    with pytest.raises(geometry.DegenerateGeometryError):
        geometry.percent_of_reference(math.nan, 50.0)


def test_displacement_is_euclidean_in_pixels() -> None:
    assert geometry.displacement_px((0.0, 0.0), (3.0, 4.0)) == pytest.approx(5.0)


def test_midpoint_and_diagonal() -> None:
    assert geometry.midpoint((0.0, 0.0), (2.0, 4.0)).tolist() == [1.0, 2.0]
    assert geometry.image_diagonal(3, 4) == pytest.approx(5.0)


def test_signed_angle_difference() -> None:
    assert geometry.signed_angle_difference(35.0, 30.0) == pytest.approx(5.0)
    assert geometry.signed_angle_difference(20.0, 30.0) == pytest.approx(-10.0)


def test_frame_dimensions_must_be_positive() -> None:
    with pytest.raises(ValueError):
        geometry.to_pixel((0.5, 0.5), 0, 100)
    with pytest.raises(ValueError):
        geometry.image_diagonal(0, 100)
