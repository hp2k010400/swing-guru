"""Headless Streamlit UI checks using Streamlit's own AppTest harness.

These execute the real app script - upload panel, guided fixture analysis, the
adjust-moments editor, the advanced provenance tables and the render/export
step - so a broken widget call, a stale-state bug, a dishonest label or a lost
manual choice fails here rather than in a browser. The browser-only playback
timing is covered by ``scripts/qa_guided_playback.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from streamlit.testing.v1 import AppTest

from swingguru.phases import Phase

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "swingguru" / "ui" / "app.py"
GUIDED_FIXTURE = ROOT / "var" / "fixtures" / "guided_playback.mp4"

TIMEOUT = 300


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=TIMEOUT)


def _button(app: AppTest, label: str):
    for button in app.button:
        if button.label == label:
            return button
    raise AssertionError(
        f"no button labelled {label!r} was rendered; saw "
        f"{[item.label for item in app.button]}"
    )


def _text(app: AppTest) -> str:
    parts: list[str] = []
    for collection in (
        app.title,
        app.header,
        app.subheader,
        app.caption,
        app.markdown,
        app.info,
        app.warning,
        app.error,
        app.success,
    ):
        parts.extend(str(element.value) for element in collection)
    return " ".join(parts)


def _choose_option(app: AppTest, option: str) -> AppTest:
    radio = next(item for item in app.radio if option in item.options)
    radio.set_value(option).run()
    assert not app.exception, app.exception
    return app


def _click(app: AppTest, label: str) -> AppTest:
    _button(app, label).click().run()
    assert not app.exception, app.exception
    return app


def _analyse_guided(app: AppTest) -> AppTest:
    app.run()
    _choose_option(app, "Guided playback sample")
    _click(app, "Create guided fixture and analyse")
    assert "Adjust moments" in _text(app)
    return app


def _slider(app: AppTest, label: str):
    for slider in app.slider:
        if slider.label == label:
            return slider
    raise AssertionError(
        f"no slider labelled {label!r}; saw {[s.label for s in app.slider]}"
    )


def _select(app: AppTest, label: str, value) -> AppTest:
    box = next(item for item in app.selectbox if item.label == label)
    try:
        box.select(value)
    except AttributeError:  # pragma: no cover - older AppTest API
        box.set_value(value)
    box.run()
    assert not app.exception, app.exception
    return app


def test_first_screen_is_the_golfer_upload_panel() -> None:
    app = _app().run()
    assert not app.exception, app.exception
    text = _text(app)
    assert "Swing Guru" in text
    assert _button(app, "Analyse swing") is not None
    assert app.file_uploader, "the first screen offers a video upload"
    assert not app.dataframe
    assert not app.image


def test_playing_hand_is_the_golfer_facing_label() -> None:
    app = _app().run()
    labels = [radio.label for radio in app.radio]
    assert "Playing hand" in labels
    assert all("Which hand is on top" not in label for label in labels)
    handedness = next(radio for radio in app.radio if radio.label == "Playing hand")
    assert list(handedness.options) == ["Right-handed", "Left-handed"]


def test_sidebar_console_is_gone() -> None:
    app = _app().run()
    assert not app.sidebar.button
    assert not app.sidebar.selectbox
    assert not app.sidebar.markdown
    assert not app.sidebar.slider


def test_model_availability_is_reported_in_developer_details() -> None:
    app = _app().run()
    model_path = ROOT / "models" / "pose_landmarker_full.task"
    messages = [element.value for element in app.success] + [
        element.value for element in app.error
    ]
    joined = " ".join(str(message) for message in messages)
    if model_path.exists():
        assert "Pose model found" in joined
    else:
        assert "Pose model missing" in joined
    assert any(expander.label == "Developer details" for expander in app.expander)


def test_guided_fixture_reaches_the_player_view() -> None:
    app = _analyse_guided(_app())
    text = _text(app)
    assert "estimated Address moment" in text
    assert "7 of 7 moments available" in text
    labels = {expander.label for expander in app.expander}
    assert {"Adjust moments", "Advanced analysis", "Developer details"} <= labels
    assert app.image, "the adjust editor previews the candidate frame"
    assert app.dataframe


def test_checks_carry_the_degree_symbol_and_human_labels() -> None:
    app = _analyse_guided(_app())
    timeline = app.session_state["timeline"]
    labels = [check.label for check in timeline.checks]
    assert "Torso inclination" in labels
    assert "Posture change" in labels
    degree_checks = [check for check in timeline.checks if check.unit == "\u00b0"]
    assert degree_checks
    assert all(check.value_text for check in timeline.checks)


def test_several_checks_share_one_video_time() -> None:
    app = _analyse_guided(_app())
    timeline = app.session_state["timeline"]
    address = next(
        event for event in timeline.events if event.phase.value == "address"
    )
    assert len(address.checks) == 3
    assert len({check.time_s for check in address.checks}) == 1
    assert len({check.frame_index for check in address.checks}) == 1


def test_reference_copy_reflects_the_choice() -> None:
    app = _analyse_guided(_app())
    assert "estimated Address moment" in _text(app)
    app = _choose_option(app, "None")
    text = _text(app)
    assert "movement values stay unavailable" in text
    assert "estimated Address moment" not in text
    app = _choose_option(app, "Custom frame")
    assert "the comparison frame you picked" in _text(app)


def test_adjust_moments_previews_the_candidate_after_saving() -> None:
    app = _analyse_guided(_app())
    app = _select(app, "Moment to adjust", Phase.TOP)
    slider = _slider(app, "Candidate frame")
    candidate = min(slider.max, 30)
    slider.set_value(candidate).run()
    assert not app.exception, app.exception
    assert f"Candidate frame {candidate} of" in _text(app)
    assert "not saved until you use it" in _text(app)

    app = _click(app, "Use this frame")
    text = _text(app)
    assert "Selected manually" in text
    moments = app.session_state["moments"]
    top = moments.get("top")
    assert top.source == "manual"
    assert top.frame_index == candidate

    # Prior-review fix: the editor keeps previewing the slider, not the saved
    # frame, so a second edit is possible without losing the first one.
    second = min(slider.max, candidate + 5)
    _slider(app, "Candidate frame").set_value(second).run()
    assert not app.exception, app.exception
    assert f"Candidate frame {second} of" in _text(app)
    assert app.session_state["moments"].get("top").frame_index == candidate


def test_moment_provenance_is_preserved_in_advanced_and_exports() -> None:
    app = _analyse_guided(_app())
    app = _click(app, "Use this frame")
    assert "Selected manually" in _text(app)

    app = _click(app, "Render annotated video and exports")
    labels = {button.label for button in app.download_button}
    assert {"measurements.csv", "landmarks.csv", "metadata.json", "moments.json"} <= labels
    digest = hashlib.sha1(str(GUIDED_FIXTURE).encode("utf-8")).hexdigest()[:10]
    sidecar = ROOT / "var" / "runs" / digest / "moments.json"
    assert sidecar.exists()
    payload = sidecar.read_text(encoding="utf-8")
    assert '"manual"' in payload and '"estimated"' in payload


def test_reference_change_hides_the_previous_exports() -> None:
    app = _analyse_guided(_app())
    app = _click(app, "Render annotated video and exports")
    assert app.download_button
    app = _choose_option(app, "None")
    assert not app.download_button
    assert "earlier moment or comparison frame" in _text(app)


def test_settings_change_invalidates_the_cached_analysis() -> None:
    app = _analyse_guided(_app())
    assert app.image
    checkbox = next(
        item for item in app.checkbox if item.label == "Label every landmark"
    )
    checkbox.set_value(True).run()
    assert not app.exception, app.exception
    text = _text(app)
    assert "no longer matches them" in text
    assert not app.image
    assert not app.dataframe
    assert _button(app, "Analyse swing") is not None


def test_mirrored_input_warning_survives_a_fresh_analysis() -> None:
    app = _app().run()
    mirrored = next(
        item for item in app.checkbox if item.label == "Input video is mirrored"
    )
    mirrored.set_value(True).run()
    _choose_option(app, "Guided playback sample")
    app = _click(app, "Create guided fixture and analyse")
    assert "marked as mirrored" in _text(app)


def test_single_frame_analysis_reports_the_single_frame() -> None:
    app = _app().run()
    frames = next(
        item for item in app.number_input if item.label == "Maximum frames to process"
    )
    frames.set_value(1).run()
    _choose_option(app, "Guided playback sample")
    app = _click(app, "Create guided fixture and analyse")
    assert not app.exception, app.exception
    text = _text(app)
    assert "Only one frame was processed" in text
    assert "No moments are available to hold" in text


def test_plumbing_sample_clip_is_refused_by_the_estimator() -> None:
    app = _app().run()
    _choose_option(app, "Sample clip")
    app = _click(app, "Create sample clip and analyse")
    text = _text(app)
    assert "No moments could be estimated from this clip" in text
    moments = app.session_state["moments"]
    assert all(estimate.frame_index is None for estimate in moments.estimates)


def test_unreadable_video_gets_friendly_copy_and_technical_details(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "not-a-video.mp4"
    broken.write_bytes(b"this is definitely not an mp4 file")

    app = _app().run()
    _choose_option(app, "File on this computer")
    path_input = next(item for item in app.text_input if item.key == "local_path")
    path_input.set_value(str(broken)).run()
    app = _click(app, "Analyse this file")
    assert not app.exception, app.exception
    text = _text(app)
    assert "This video could not be analysed." in text
    assert any(expander.label == "Technical details" for expander in app.expander)
    assert not app.image
