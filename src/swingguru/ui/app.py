"""Swing Guru guided swing playback.

Run with::

    .venv\\Scripts\\python -m streamlit run src/swingguru/ui/app.py

The analysed swing plays as real video between automatically estimated
moments. At each moment the video holds still while one measurement-specific
overlay and value are shown; several checks share the same moment, so the
picture does not move between them. Moments are estimates from the measured
body motion, they are labelled as estimates, and every one of them can be
corrected by hand.

The engine flow is unchanged: analyse once into cached per-frame records, then
derive the timeline, prepare a browser-playable preview and export. Nothing
here scores a swing or claims calibrated accuracy.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import streamlit as st

# Streamlit executes this file as a script, so the package is not automatically
# importable and relative imports fail. Add the source root before importing
# the engine, which also makes the viewer work without an editable install.
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from swingguru.auto_moments import (  # noqa: E402
    MomentConfig,
    MomentSet,
    apply_manual_overrides,
    estimate_moments,
    moment_summary,
)
from swingguru.export import write_json  # noqa: E402
from swingguru.landmarks import LANDMARK_NAMES  # noqa: E402
from swingguru.measurements import (  # noqa: E402
    MEASUREMENT_KEYS,
    SPECS_BY_KEY,
    MeasurementConfig,
    landmark_rejection_reason,
)
from swingguru.overlays import OverlayConfig, render_overlay  # noqa: E402
from swingguru.phases import (  # noqa: E402
    PHASE_LABELS,
    PHASE_ORDER,
    ManualPhaseProvider,
    Phase,
    PhaseSelection,
)
from swingguru.pipeline import (  # noqa: E402
    FrameRecord,
    PipelineConfig,
    PipelineError,
    finalize_run,
    process_video,
)
from swingguru.pose import (  # noqa: E402
    DEFAULT_MODEL_URL,
    ModelNotFoundError,
    describe_runtime,
    default_model_path,
)
from swingguru.pose_rtm import RTMPoseAdapter, rtm_available  # noqa: E402
from swingguru.synthetic import (  # noqa: E402
    ScriptedPoseProvider,
    SyntheticClip,
    make_synthetic_clip,
)
from swingguru.ui import media  # noqa: E402
from swingguru.ui import player as player_component  # noqa: E402
from swingguru.ui.guided_fixture import (  # noqa: E402
    GuidedPoseProvider,
    GuidedSwingClip,
    make_guided_clip,
)
from swingguru.ui.presentation import (  # noqa: E402
    CAMERA_NOTE,
    CHECKPOINT_BY_PHASE,
    EM_DASH,
    measurement_rows,
    render_checkpoint_still,
)
from swingguru.ui.timeline import build_timeline, timeline_payload  # noqa: E402
from swingguru.video import VideoError, VideoInfo, VideoStream  # noqa: E402

# RTMW tracks both arms through down-the-line swings where MediaPipe loses the
# lead arm (docs/research/fastest-sensei-path.md); MediaPipe stays the fallback.
DEFAULT_POSE_MODEL = "rtmw" if rtm_available() else "mediapipe"

WORK_ROOT = Path(__file__).resolve().parents[3] / "var"
UPLOAD_DIR = WORK_ROOT / "uploads"
RUN_DIR = WORK_ROOT / "runs"
FIXTURE_DIR = WORK_ROOT / "fixtures"
SAMPLE_CLIP = FIXTURE_DIR / "plumbing_fixture.mp4"
GUIDED_CLIP = FIXTURE_DIR / "guided_playback.mp4"

HANDEDNESS_LABELS = ("Right-handed", "Left-handed")
HANDEDNESS_VALUES = {"Right-handed": "right", "Left-handed": "left"}
CAMERA_LABELS = ("Face-on", "Down-the-line", "Not sure")
CAMERA_VALUES = {
    "Face-on": "face-on",
    "Down-the-line": "down-the-line",
    "Not sure": "unknown",
}
GUIDED_PORTRAIT_CLIP = FIXTURE_DIR / "guided_playback_portrait.mp4"
GUIDED_PORTRAIT_SOURCE = "Guided playback sample (portrait)"
SOURCE_MODES = (
    "Upload",
    "File on this computer",
    "Sample clip",
    "Guided playback sample",
    GUIDED_PORTRAIT_SOURCE,
)
REFERENCE_MODES = ("Address frame", "None", "Custom frame")
HOLD_SECONDS = 1.8

# Session keys cleared whenever a new analysis replaces the old one.
_RESET_KEYS = (
    "analysis",
    "phase_frames",
    "last_result",
    "last_result_key",
    "moments",
    "moments_key",
    "preview",
    "preview_key",
    "poster",
    "poster_key",
    "player_state",
    "adjust_candidate",
    "last_error",
)

_STYLE = """
<style>
  .stApp { background: #F7F7F2; }
  .sg-masthead { display: flex; align-items: baseline; gap: 0.6rem; }
  .sg-brand { font-size: 1.7rem; font-weight: 700; color: #292929; letter-spacing: 0; }
  .sg-tagline { color: #7E7E7E; font-size: 0.95rem; }
  .sg-lead { color: #292929; font-size: 1rem; margin: 0.35rem 0 0.6rem 0; }
  .sg-card-title { font-size: 1.35rem; font-weight: 600; color: #292929; margin: 0.35rem 0 0.1rem 0; }
  .sg-card-description { color: #7E7E7E; font-size: 0.9rem; margin-bottom: 0.4rem; }
  .sg-rows { margin-top: 0.7rem; display: flex; flex-direction: column; gap: 0.4rem; }
  .sg-row { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; border-bottom: 1px solid #E7E5DE; padding-bottom: 0.3rem; }
  .sg-row-label { color: #7E7E7E; font-size: 0.95rem; }
  .sg-row-value { color: #292929; font-size: 1.55rem; font-weight: 600; white-space: nowrap; }
  .sg-row-unit { color: #7E7E7E; font-size: 0.85rem; font-weight: 400; }
  .sg-review { color: #C49A45 !important; font-size: 1rem !important; }
  .sg-row-reason { color: #7E7E7E; font-size: 0.85rem; margin: -0.1rem 0 0.45rem 0; }
  .sg-note { color: #7E7E7E; font-size: 0.85rem; }
  .sg-provenance { color: #4F7F5F; font-size: 0.85rem; font-weight: 600; }
  .sg-provenance-estimated { color: #C49A45; }
  div[data-testid="stMainBlockContainer"] { max-width: 860px; padding-top: 2.5rem; }
  div[data-testid="stImage"] img { border-radius: 8px; }
  div[data-testid="stVerticalBlockBorderWrapper"] {
    background: #FFFFFF;
    border: 1px solid #E7E5DE;
    border-radius: 8px;
  }
  div[data-testid="stExpander"] {
    background: #FFFFFF;
    border: 1px solid #E7E5DE;
    border-radius: 8px;
  }
  div[data-testid="stExpander"] details,
  div[data-testid="stExpander"] summary {
    background: #FFFFFF;
  }
  .stButton > button[kind="primary"] { background: #4F7F5F; color: #FFFFFF; border: none; border-radius: 999px; padding: 0.5rem 1.4rem; font-weight: 600; }
  .stButton > button[kind="primary"]:hover { background: #365B43; color: #FFFFFF; }
  #MainMenu, footer { visibility: hidden; }
  h1, h2, h3, h4 { letter-spacing: 0; }
</style>
"""


@dataclass
class Analysis:
    """Cached result of the pose analysis step for one video and settings."""

    source: Path
    info: VideoInfo
    records: list[FrameRecord]
    config: PipelineConfig
    provider_name: str
    settings_key: str
    warnings: list[str] = field(default_factory=list)


def _state() -> dict:
    return st.session_state


def _settings_key(source: Path | None, settings: dict[str, object]) -> str:
    """Identity of a run: the analysed file plus every setting that affects it."""

    try:
        stat = source.stat() if source is not None else None
        file_version = None if stat is None else (stat.st_size, stat.st_mtime_ns)
    except OSError:
        file_version = None
    payload = "|".join(
        str(value)
        for value in (
            source,
            file_version,
            settings["handedness"],
            settings["camera_view"],
            settings["min_visibility"],
            settings["min_presence"],
            settings["max_frames"],
            settings["mirrored"],
            settings["transcode"],
            settings["show_all_landmarks"],
            settings.get("pose_model", "mediapipe"),
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _selection_key(selection: PhaseSelection) -> str:
    return hashlib.sha1(
        json.dumps(selection.to_dict(), sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]


def _reset_analysis_state() -> None:
    state = _state()
    for key in _RESET_KEYS:
        state.pop(key, None)


def _mirror(key: str, value: object) -> None:
    """Remember a control value in plain session state.

    Streamlit drops a widget's session state when the widget is not rendered on
    a rerun, so an analysed run reads its settings from these mirrors instead.
    Mirrors are never widget keys, so nothing here fights the widget lifecycle.
    """

    _state()[f"control_{key}"] = value


def _control(key: str, default: object) -> object:
    """The live widget value when the widget exists, else the last mirror."""

    state = _state()
    live = state.get(key)
    if live is not None:
        return live
    return state.get(f"control_{key}", default)


def _current_settings() -> dict[str, object]:
    live_handedness = str(_control("handedness_choice", ""))
    live_camera = str(_control("camera_choice", ""))
    return {
        "handedness": (
            HANDEDNESS_VALUES[live_handedness]
            if live_handedness in HANDEDNESS_VALUES
            else str(_control("handedness", "right"))
        ),
        "camera_view": (
            CAMERA_VALUES[live_camera]
            if live_camera in CAMERA_VALUES
            else str(_control("camera_view", "unknown"))
        ),
        "min_visibility": float(_control("min_visibility", 0.5)),  # type: ignore[arg-type]
        "min_presence": float(_control("min_presence", 0.5)),  # type: ignore[arg-type]
        "max_frames": int(_control("max_frames", 9000)),  # type: ignore[arg-type]
        "mirrored": bool(_control("mirrored", False)),
        "transcode": bool(_control("transcode", True)),
        "show_all_landmarks": bool(_control("show_all_landmarks", False)),
        "pose_model": str(_control("pose_model", DEFAULT_POSE_MODEL)),
    }


def _save_upload(uploaded) -> Path:
    payload = uploaded.getvalue()
    digest = hashlib.sha1(payload).hexdigest()[:12]
    safe_name = Path(uploaded.name).name
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_DIR / f"{digest}_{safe_name}"
    if not target.exists():
        target.write_bytes(payload)
    return target


def _read_frame(path: Path, index: int) -> tuple[object | None, str | None]:
    """Decode a single frame with a short-lived capture."""

    try:
        with VideoStream.open(path) as stream:
            return stream.read_at(index), None
    except Exception as error:  # noqa: BLE001 - surfaced to the operator
        return None, str(error)


def _record_at(records: list[FrameRecord], index: int) -> FrameRecord | None:
    for record in records:
        if record.index == index:
            return record
    return None


def _measurement_table(record: FrameRecord) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for key in MEASUREMENT_KEYS:
        result = record.measurements.get(key)
        if result is None:
            rows.append(
                {
                    "measurement": key,
                    "value": None,
                    "unit": "",
                    "valid": False,
                    "warnings": "not computed",
                }
            )
            continue
        rows.append(
            {
                "measurement": key,
                "value": None if result.value is None else round(result.value, 3),
                "unit": result.unit,
                "valid": result.valid,
                "warnings": "; ".join(result.warnings),
            }
        )
    return rows


def _landmark_table(
    record: FrameRecord, config: MeasurementConfig
) -> list[dict[str, object]]:
    """Raw landmark values with the exact quality verdict used by the engine."""

    rows: list[dict[str, object]] = []
    for landmark in record.observation.landmarks:
        reason = landmark_rejection_reason(landmark, landmark.name, config)
        rows.append(
            {
                "idx": landmark.index,
                "landmark": landmark.name,
                "x": round(landmark.x, 4),
                "y": round(landmark.y, 4),
                "z": round(landmark.z, 4),
                "visibility": (
                    None
                    if landmark.visibility is None
                    else round(landmark.visibility, 3)
                ),
                "presence": (
                    None
                    if landmark.presence is None
                    else round(landmark.presence, 3)
                ),
                "usable": reason is None,
                "rejection_reason": reason or "",
            }
        )
    if not record.pose_detected:
        rows = [
            {
                "idx": index,
                "landmark": name,
                "x": None,
                "y": None,
                "z": None,
                "visibility": None,
                "presence": None,
                "usable": False,
                "rejection_reason": "no pose detected in this frame",
            }
            for index, name in enumerate(LANDMARK_NAMES)
        ]
    return rows


def _build_config(settings: dict[str, object]) -> PipelineConfig:
    measurement = MeasurementConfig(
        min_visibility=float(settings["min_visibility"]),
        min_presence=float(settings["min_presence"]),
        handedness=str(settings["handedness"]),
        camera_view=str(settings["camera_view"]),
        mirrored_input=bool(settings["mirrored"]),
    )
    return PipelineConfig(
        measurement=measurement,
        overlay=OverlayConfig(
            show_all_landmarks=bool(settings["show_all_landmarks"]),
            min_visibility=float(settings["min_visibility"]),
            quality=measurement,
        ),
        max_frames=int(settings["max_frames"]),
        write_annotated=False,
        transcode_h264=bool(settings["transcode"]),
    )


def _friendly_progress(fraction: float) -> str:
    if fraction < 0.8:
        return "Finding the golfer in each frame"
    if fraction < 0.97:
        return "Working out the measurements"
    return "Finishing up"


def _run_analysis(
    source: Path,
    settings: dict[str, object],
    *,
    provider_mode: str = "model",
    guided_clip: GuidedSwingClip | None = None,
) -> Analysis:
    digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:10]
    output_dir = RUN_DIR / digest
    config = _build_config(settings)

    provider = None
    provider_name = "mediapipe-pose-landmarker"
    if provider_mode == "sample":
        capture = cv2.VideoCapture(str(source))
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360
        fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
        count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 48
        capture.release()
        provider = ScriptedPoseProvider(
            clip=SyntheticClip(
                width=width,
                height=height,
                fps=fps,
                frame_count=count,
                handedness=str(settings["handedness"]),
            )
        )
        provider_name = provider.name
    elif provider_mode == "guided":
        provider = GuidedPoseProvider(clip=guided_clip or GuidedSwingClip())
        provider_name = provider.name
    elif settings.get("pose_model") == "rtmw" and rtm_available():
        provider = RTMPoseAdapter("wholebody", "balanced")
        provider_name = provider.name

    progress = st.progress(0.0, text="Starting")

    def report(fraction: float, message: str) -> None:
        progress.progress(
            min(1.0, max(0.0, fraction)), text=_friendly_progress(fraction)
        )

    try:
        result = process_video(
            source,
            output_dir,
            config=config,
            pose_provider=provider,
            phase_provider=ManualPhaseProvider(
                frames={}, derive_reference_from_address=False
            ),
            progress=report,
        )
    finally:
        progress.empty()

    return Analysis(
        source=source,
        info=result.info,
        records=result.records,
        config=config,
        provider_name=provider_name,
        settings_key=_settings_key(source, settings),
        warnings=list(result.warnings),
    )


def _analyse(
    source: Path,
    settings: dict[str, object],
    *,
    provider_mode: str,
    guided_clip: GuidedSwingClip | None = None,
) -> None:
    """Run the pose step for one source and leave the guided view showing."""

    _reset_analysis_state()
    try:
        _state()["analysis"] = _run_analysis(
            source, settings, provider_mode=provider_mode, guided_clip=guided_clip
        )
    except ModelNotFoundError as error:
        _state()["analysis"] = None
        _state()["last_error"] = (
            "friendly",
            "The swing model is not installed on this machine yet, so this video "
            "cannot be analysed. Download it with "
            "`python -m swingguru.cli download-model` and try again.",
            str(error),
        )
    except (PipelineError, VideoError) as error:
        _state()["analysis"] = None
        _state()["last_error"] = (
            "friendly",
            "This video could not be analysed.",
            str(error),
        )
    except Exception as error:  # noqa: BLE001 - surfaced to the operator
        _state()["analysis"] = None
        _state()["last_error"] = (
            "friendly",
            "Something went wrong while analysing this video.",
            f"{type(error).__name__}: {error}",
        )
    st.rerun()


def _show_error() -> None:
    error = _state().pop("last_error", None)
    if not error:
        return
    _, friendly, technical = error
    st.error(friendly)
    with st.expander("Technical details"):
        st.code(technical)


def _install_style() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)


def _masthead() -> None:
    st.markdown(
        '<div class="sg-masthead">'
        '<span class="sg-brand">Swing Guru</span>'
        '<span class="sg-tagline">Guided swing analysis</span>'
        "</div>",
        unsafe_allow_html=True,
    )


def _write_sample_clip(settings: dict[str, object]) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    clip = SyntheticClip(
        width=640,
        height=360,
        fps=30.0,
        frame_count=48,
        handedness=str(settings["handedness"]),
    )
    return make_synthetic_clip(SAMPLE_CLIP, clip=clip)


def _write_guided_clip(
    *, portrait: bool = False
) -> tuple[Path, GuidedSwingClip]:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    clip = (
        GuidedSwingClip(width=360, height=640, frame_count=90)
        if portrait
        else GuidedSwingClip()
    )
    target = GUIDED_PORTRAIT_CLIP if portrait else GUIDED_CLIP
    return make_guided_clip(target, clip=clip), clip


def _analyse_card() -> None:
    """Upload, playing hand, camera view and the one primary action."""

    state = _state()
    handedness_label = str(_control("handedness_label", HANDEDNESS_LABELS[0]))
    camera_label = str(_control("camera_label", CAMERA_LABELS[-1]))
    if handedness_label not in HANDEDNESS_LABELS:
        handedness_label = HANDEDNESS_LABELS[0]
    if camera_label not in CAMERA_LABELS:
        camera_label = CAMERA_LABELS[-1]

    with st.container(border=True):
        uploaded = st.file_uploader(
            "Your swing video", type=["mp4", "mov", "avi", "mkv", "m4v"]
        )
        columns = st.columns(2)
        handedness_label = columns[0].radio(
            "Playing hand",
            HANDEDNESS_LABELS,
            index=HANDEDNESS_LABELS.index(handedness_label),
            horizontal=True,
            key="handedness_choice",
            help="The hand holding the club. A right-handed golfer plays left hand on top.",
        )
        camera_label = columns[1].radio(
            "Camera angle",
            CAMERA_LABELS,
            index=CAMERA_LABELS.index(camera_label),
            horizontal=True,
            key="camera_choice",
        )
        _mirror("handedness_label", handedness_label)
        _mirror("handedness", HANDEDNESS_VALUES[handedness_label])
        _mirror("camera_label", camera_label)
        _mirror("camera_view", CAMERA_VALUES[camera_label])

        settings = _current_settings()
        source_mode = str(state.get("source_mode", SOURCE_MODES[0]))
        ready: Path | None = None
        if source_mode == "File on this computer":
            typed = str(state.get("local_path", "") or "").strip()
            ready = Path(typed) if typed else None
        elif source_mode == "Sample clip":
            ready = SAMPLE_CLIP
        elif source_mode == "Guided playback sample":
            ready = GUIDED_CLIP
        elif source_mode == GUIDED_PORTRAIT_SOURCE:
            ready = GUIDED_PORTRAIT_CLIP

        upload_path = _save_upload(uploaded) if uploaded is not None else None
        if upload_path is not None:
            st.caption(f"Ready to analyse: {upload_path.name}")
        elif ready is not None and ready.exists():
            st.caption(f"Ready to analyse: {ready.name}")
        elif source_mode in (
            "Sample clip",
            "Guided playback sample",
            GUIDED_PORTRAIT_SOURCE,
        ):
            st.caption("Ready to analyse: the sample clip is created when you analyse.")
        else:
            st.caption(
                "Choose an mp4, mov, avi, mkv or m4v video of one golf swing. Your "
                "video stays on this machine."
            )

        clicked = st.button("Analyse swing", type="primary")

    if not clicked:
        return
    if upload_path is not None:
        _analyse(upload_path, settings, provider_mode="model")
    elif source_mode == "Sample clip":
        _analyse(_write_sample_clip(settings), settings, provider_mode="sample")
    elif source_mode == "Guided playback sample":
        clip_path, _ = _write_guided_clip()
        _analyse(clip_path, settings, provider_mode="guided")
    elif source_mode == GUIDED_PORTRAIT_SOURCE:
        clip_path, clip = _write_guided_clip(portrait=True)
        _analyse(clip_path, settings, provider_mode="guided", guided_clip=clip)
    elif ready is not None and ready.exists():
        _analyse(ready, settings, provider_mode="model")
    else:
        st.warning("Choose a video first.")


def _moment_config(settings: dict[str, object]) -> MomentConfig:
    return MomentConfig(handedness=str(settings["handedness"]))


def _moments_for(analysis: Analysis, settings: dict[str, object]) -> MomentSet:
    """Estimate the moments once per analysed run and cache the result."""

    state = _state()
    key = analysis.settings_key
    if state.get("moments_key") == key and isinstance(state.get("moments"), MomentSet):
        return state["moments"]
    moments = estimate_moments(
        analysis.records, config=_moment_config(settings)
    )
    state["moments"] = moments
    state["moments_key"] = key
    return moments


def _store_moments(moments: MomentSet) -> None:
    _state()["moments"] = moments


def _reference_choice(
    moments: MomentSet, first_index: int, last_index: int
) -> tuple[str, int | None, str]:
    """Comparison frame: the Address moment, a custom frame, or nothing."""

    mode = str(_state().get("reference_mode", REFERENCE_MODES[0]))
    if mode not in REFERENCE_MODES:
        mode = REFERENCE_MODES[0]
    address = moments.get(Phase.ADDRESS)
    if mode == "Address frame":
        return mode, address.frame_index, address.source
    if mode == "Custom frame":
        raw = _state().get("custom_reference_frame", first_index)
        try:
            chosen = max(first_index, min(last_index, int(raw)))
        except (TypeError, ValueError):
            chosen = first_index
        return mode, chosen, "custom"
    return mode, None, "unavailable"


def _reference_caption(mode: str, frame: int | None, source: str) -> str:
    if mode == "None" or frame is None:
        return (
            "No comparison frame yet, so movement values stay unavailable until "
            "an Address moment exists or you pick one."
        )
    if mode == "Custom frame":
        return f"Movement is measured from frame {frame}, the comparison frame you picked."
    if source == "manual":
        return "Movement is measured from the Address moment you selected by hand."
    if source == "estimated":
        return (
            "Movement is measured from the estimated Address moment. Check it and "
            "adjust it under Adjust moments if it looks wrong."
        )
    return "Movement is measured from the Address moment in use."


def _ensure_preview(
    analysis: Analysis, settings: dict[str, object]
) -> media.PreviewResult:
    """Prepare (or reuse) the browser-playable preview of the analysed frames."""

    state = _state()
    records = analysis.records
    key = media.preview_cache_key(
        analysis.source,
        first_index=records[0].index,
        frame_count=len(records),
        fps=analysis.info.fps,
        settings_key=analysis.settings_key,
    )
    cached = state.get("preview")
    if state.get("preview_key") == key and isinstance(cached, media.PreviewResult):
        return cached
    preview = media.prepare_preview(
        analysis.source,
        output_dir=player_component.generated_dir(),
        first_index=records[0].index,
        frame_count=len(records),
        fps=analysis.info.fps,
        width=analysis.info.width,
        height=analysis.info.height,
        cache_key=key,
        settings_key=analysis.settings_key,
    )
    state["preview"] = preview
    state["preview_key"] = key
    return preview


def _ensure_poster(analysis: Analysis, frame_index: int) -> str | None:
    state = _state()
    key = f"{analysis.settings_key}:{frame_index}"
    if state.get("poster_key") == key:
        return state.get("poster")
    frame, _ = _read_frame(analysis.source, frame_index)
    poster = media.poster_data_url(frame)
    state["poster"] = poster
    state["poster_key"] = key
    return poster


def _provenance_html(source: str) -> str:
    if source == "manual":
        return '<span class="sg-provenance">Selected manually</span>'
    if source == "estimated":
        return (
            '<span class="sg-provenance sg-provenance-estimated">'
            "Estimated moment</span>"
        )
    return '<span class="sg-provenance">Not available</span>'


def _guided_section(
    analysis: Analysis,
    *,
    moments: MomentSet,
    preview: media.PreviewResult,
    reference_frame: int | None,
    reference_source: str,
    reference_mode: str,
) -> None:
    """The video-led main experience."""

    state = _state()
    first_index = analysis.records[0].index
    timeline = build_timeline(
        analysis.records,
        moments,
        fps=analysis.info.fps,
        measurement_config=analysis.config.measurement,
        reference_frame=reference_frame,
    )
    state["timeline"] = timeline
    poster = _ensure_poster(analysis, first_index)
    run_key = hashlib.sha1(
        "|".join(
            (
                analysis.settings_key,
                str(reference_frame),
                json.dumps(
                    [estimate.to_dict() for estimate in moments.estimates],
                    sort_keys=True,
                ),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    payload = timeline_payload(
        timeline,
        run_key=run_key,
        video_src=preview.src or "",
        hold_seconds=float(_control("hold_seconds", HOLD_SECONDS)),
        poster=poster,
        labels={
            "note": (
                "Moments are estimated from body movement — adjust any that "
                "look wrong. Values are camera-view measurements."
            )
        },
    )
    payload["width"] = int(analysis.info.width)
    payload["height"] = int(analysis.info.height)

    # The player never posts back during playback (a posted value reruns the
    # whole page); it only reports a playback error.
    player_value = player_component.render_player(
        payload, key=f"player_{analysis.settings_key}"
    )
    if player_value:
        state["player_state"] = player_value

    held = state.get("player_state") or {}
    st.caption(_reference_caption(reference_mode, reference_frame, reference_source))
    if isinstance(held, dict) and held.get("error"):
        st.warning(str(held["error"]))
    if timeline.review_notes:
        for note in timeline.review_notes:
            st.caption(note)
    if timeline.empty:
        st.info(
            "No moments are available to hold, so the video plays straight "
            "through. Use Adjust moments to pick them by hand."
        )


def _still_fallback(
    analysis: Analysis,
    *,
    moments: MomentSet,
    preview: media.PreviewResult,
    reference_frame: int | None,
) -> None:
    """Readable stills when guided playback cannot be prepared."""

    st.warning(
        "Guided playback is unavailable for this clip, so stills are shown "
        f"instead. {preview.message}"
    )
    available = [estimate for estimate in moments.estimates if estimate.available]
    if not available:
        st.info("No moments are available yet. Use Adjust moments to pick them.")
        return
    option_labels = [
        f"{estimate.label} ({estimate.provenance_label})" for estimate in available
    ]
    choice = st.radio("Moment", option_labels, horizontal=False)
    estimate = available[option_labels.index(choice)]
    checkpoint = CHECKPOINT_BY_PHASE[estimate.phase]
    frame, decode_error = _read_frame(analysis.source, int(estimate.frame_index))
    if frame is None:
        st.info(f"This frame could not be shown. {decode_error or ''}")
        return
    record = _record_at(analysis.records, int(estimate.frame_index))
    reference_record = (
        _record_at(analysis.records, int(reference_frame))
        if reference_frame is not None
        else None
    )
    still = frame
    if record is not None:
        still = render_checkpoint_still(
            frame,
            record,
            checkpoint,
            reference_record=reference_record,
            config=analysis.config.measurement,
        )
    st.image(cv2.cvtColor(still, cv2.COLOR_BGR2RGB), width="stretch")
    st.markdown(
        f'<div class="sg-card-title">{checkpoint.title}</div>'
        f'<div class="sg-card-description">{checkpoint.description}</div>'
        + _provenance_html(estimate.source),
        unsafe_allow_html=True,
    )
    if record is not None:
        st.markdown(
            _row_markup(measurement_rows(record, checkpoint)), unsafe_allow_html=True
        )
    st.markdown(f'<div class="sg-note">{CAMERA_NOTE}</div>', unsafe_allow_html=True)


def _row_markup(rows) -> str:
    parts: list[str] = []
    for row in rows:
        if row.needs_review:
            parts.append(
                '<div class="sg-row">'
                f'<span class="sg-row-label">{row.label}</span>'
                f'<span class="sg-row-value sg-review">{EM_DASH} Needs review</span>'
                "</div>"
                f'<div class="sg-row-reason">{row.reason}</div>'
            )
        else:
            parts.append(
                '<div class="sg-row">'
                f'<span class="sg-row-label">{row.label}</span>'
                f'<span class="sg-row-value">{row.value_text}'
                f'<span class="sg-row-unit"> {row.unit}</span></span>'
                "</div>"
            )
    return '<div class="sg-rows">' + "".join(parts) + "</div>"


def _adjust_moments_section(
    analysis: Analysis, *, moments: MomentSet, reference_frame: int | None
) -> None:
    """Compact manual correction: one moment at a time, with a live preview."""

    settings = _current_settings()
    first_index = analysis.records[0].index
    last_index = analysis.records[-1].index
    with st.expander("Adjust moments", expanded=False):
        st.caption(moment_summary(moments))
        actions = st.columns(2)
        if actions[0].button("Re-estimate moments"):
            fresh = estimate_moments(
                analysis.records,
                config=_moment_config(settings),
                previous=moments,
            )
            _store_moments(fresh)
            st.rerun()
        if actions[1].button("Clear all moments"):
            cleared = apply_manual_overrides(
                moments, {phase: None for phase in PHASE_ORDER}
            )
            _store_moments(cleared)
            st.rerun()

        phase = st.selectbox(
            "Moment to adjust",
            options=list(PHASE_ORDER),
            format_func=lambda item: PHASE_LABELS[item],
            index=0,
            key="adjust_phase",
        )
        estimate = moments.get(phase)
        checkpoint = CHECKPOINT_BY_PHASE[phase]
        default_candidate = (
            int(estimate.frame_index)
            if estimate.frame_index is not None
            else first_index
        )
        if last_index > first_index:
            candidate = int(
                st.slider(
                    "Candidate frame",
                    min_value=first_index,
                    max_value=last_index,
                    value=max(first_index, min(last_index, default_candidate)),
                    key=f"candidate_{phase.value}",
                )
            )
        else:
            candidate = first_index
            st.info(f"Only one frame was processed (frame {first_index}).")
        st.caption(
            f"Candidate frame {candidate} of {last_index}. This preview follows the "
            "slider; it is not saved until you use it."
        )

        frame, decode_error = _read_frame(analysis.source, candidate)
        record = _record_at(analysis.records, candidate)
        reference_record = (
            _record_at(analysis.records, int(reference_frame))
            if reference_frame is not None
            else None
        )
        if frame is None:
            st.info(f"This candidate could not be shown. {decode_error or ''}")
        else:
            still = frame
            if record is not None:
                still = render_checkpoint_still(
                    frame,
                    record,
                    checkpoint,
                    reference_record=reference_record,
                    config=analysis.config.measurement,
                )
            st.image(cv2.cvtColor(still, cv2.COLOR_BGR2RGB), width="stretch")

        buttons = st.columns(3)
        if buttons[0].button("Use this frame"):
            timestamps = {
                record.index: record.timestamp_ms for record in analysis.records
            }
            updated = apply_manual_overrides(
                moments,
                {phase: candidate},
                timestamps_ms=timestamps,
            )
            _store_moments(updated)
            st.rerun()
        if buttons[1].button("Clear this moment"):
            updated = apply_manual_overrides(moments, {phase: None})
            _store_moments(updated)
            st.rerun()
        declared = (
            "Selected manually"
            if estimate.source == "manual"
            else "Estimated moment"
            if estimate.source == "estimated"
            else "Not available"
        )
        buttons[2].caption(declared)


def _advanced_section(
    analysis: Analysis,
    *,
    moments: MomentSet,
    preview: media.PreviewResult,
    reference_mode: str,
    reference_frame: int | None,
    selection: PhaseSelection,
    first_index: int,
    last_index: int,
) -> None:
    """Raw measurements, provenance and the exports."""

    state = _state()
    timeline = state.get("timeline")
    with st.expander("Advanced analysis", expanded=False):
        st.caption(
            "Raw measurements, moment provenance and the exported data for this run."
        )
        st.radio(
            "Comparison frame",
            REFERENCE_MODES,
            index=REFERENCE_MODES.index(reference_mode),
            horizontal=True,
            key="reference_mode",
            help=(
                "Movement is measured against the frame chosen here. Nothing is "
                "chosen for you."
            ),
        )
        if reference_mode == "Custom frame":
            raw_custom = state.get("custom_reference_frame", first_index)
            try:
                custom_default = max(first_index, min(last_index, int(raw_custom)))
            except (TypeError, ValueError):
                custom_default = first_index
            st.number_input(
                "Custom comparison frame",
                min_value=first_index,
                max_value=last_index,
                value=custom_default,
                step=1,
                key="custom_reference_frame",
            )
        st.caption(
            f"Comparison frame in use: {reference_frame} "
            f"({_reference_caption(reference_mode, reference_frame, moments.get(Phase.ADDRESS).source)})"
        )
        st.caption(moment_summary(moments))

        moment_rows = [
            {
                "moment": estimate.label,
                "provenance": estimate.provenance_label,
                "frame_index": estimate.frame_index,
                "timestamp_ms": (
                    None
                    if estimate.timestamp_ms is None
                    else round(estimate.timestamp_ms, 2)
                ),
                "review_required": estimate.review_required,
                "confidence": estimate.confidence,
                "evidence": " | ".join(estimate.evidence),
            }
            for estimate in moments.estimates
        ]
        st.markdown("**Moment estimates**")
        st.dataframe(moment_rows, hide_index=True, width="stretch")

        current_index = (
            reference_frame
            if reference_frame is not None
            else next(
                (
                    estimate.frame_index
                    for estimate in moments.estimates
                    if estimate.frame_index is not None
                ),
                first_index,
            )
        )
        record = _record_at(analysis.records, int(current_index))
        columns = st.columns(2)
        with columns[0]:
            st.markdown("**All measurements for this frame**")
            if record is not None:
                st.dataframe(
                    _measurement_table(record), hide_index=True, width="stretch"
                )
            else:
                st.caption("No record for this frame.")
            if record is not None and record.notes:
                st.caption("; ".join(record.notes))
        with columns[1]:
            st.markdown("**Raw landmarks for this frame**")
            if record is not None:
                st.dataframe(
                    _landmark_table(record, analysis.config.measurement),
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.caption("No record for this frame.")

        definitions = [
            {
                "measurement": SPECS_BY_KEY[key].label,
                "key": key,
                "unit": SPECS_BY_KEY[key].unit,
                "method": SPECS_BY_KEY[key].method,
                "limitations": SPECS_BY_KEY[key].limitations,
            }
            for key in MEASUREMENT_KEYS
        ]
        with st.expander("How these numbers are measured", expanded=False):
            st.dataframe(definitions, hide_index=True, width="stretch")
        if timeline is not None:
            with st.expander("Timeline and check payload", expanded=False):
                st.json(timeline.to_dict())
        player_state = state.get("player_state")
        if player_state:
            with st.expander("Player state", expanded=False):
                st.json(player_state)
        with st.expander("Preview media", expanded=False):
            st.write(
                {
                    "ok": preview.ok,
                    "message": preview.message,
                    "cache_key": preview.cache_key,
                    "path": None if preview.path is None else str(preview.path),
                    "frames": preview.frame_count,
                    "fps": preview.fps,
                    "size_bytes": preview.size_bytes,
                    "first_index": preview.first_index,
                }
            )

        st.divider()
        if st.button("Render annotated video and exports"):
            try:
                _render_export(analysis, selection, moments)
            except PipelineError as error:
                st.error("The annotated video could not be rendered.")
                with st.expander("Technical details"):
                    st.code(str(error))
            except Exception as error:  # noqa: BLE001 - surfaced to the operator
                st.error("Something went wrong while rendering the exports.")
                with st.expander("Technical details"):
                    st.code(f"{type(error).__name__}: {error}")

        result = state.get("last_result")
        if result is not None:
            if state.get("last_result_key") == (
                analysis.settings_key,
                _selection_key(selection),
            ):
                _show_outputs(result)
            else:
                st.info(
                    "These exports are from an earlier moment or comparison frame "
                    "and are hidden. Render again to refresh them."
                )

        if analysis.warnings:
            with st.expander("Run warnings (technical)", expanded=False):
                for warning in analysis.warnings:
                    st.write(f"- {warning}")


def _developer_details(
    analysis: Analysis | None,
    *,
    reference_frame: int | None = None,
) -> None:
    """Runtime, source tools, tuning and the full-skeleton debug view."""

    state = _state()
    with st.expander("Developer details", expanded=False):
        st.caption(
            "Local tools, engine settings and the full-skeleton debug view. They "
            "are here for checking the engine, not for coaching."
        )
        runtime = describe_runtime()
        st.write(
            {
                "python": runtime.get("python"),
                "mediapipe": runtime.get("mediapipe"),
                "opencv": runtime.get("opencv"),
                "numpy": runtime.get("numpy"),
            }
        )
        pose_labels = {"rtmw": "RTMW whole-body (ONNX)", "mediapipe": "MediaPipe BlazePose"}
        options = ["rtmw", "mediapipe"] if rtm_available() else ["mediapipe"]
        current = str(_control("pose_model", DEFAULT_POSE_MODEL))
        chosen = st.selectbox(
            "Pose model",
            options,
            index=options.index(current) if current in options else 0,
            format_func=lambda value: pose_labels[value],
            key="pose_model",
            help=(
                "RTMW tracks both arms through the swing on down-the-line video "
                "(MediaPipe often loses the lead arm) but takes about 5x longer."
            ),
        )
        _mirror("pose_model", chosen)
        model_path = default_model_path()
        if model_path.exists():
            st.success(f"Pose model found ({model_path.stat().st_size // 1024} KiB)")
        else:
            st.error("Pose model missing. Run `python -m swingguru.cli download-model`.")
            st.caption(DEFAULT_MODEL_URL)

        st.divider()
        st.markdown("**Video source**")
        source_mode = st.radio(
            "Video source",
            SOURCE_MODES,
            index=SOURCE_MODES.index(str(state.get("source_mode", SOURCE_MODES[0]))),
            horizontal=True,
            key="source_mode",
        )
        if source_mode == "File on this computer":
            typed = st.text_input(
                "Absolute path to a video file",
                value=str(state.get("local_path", "") or ""),
                key="local_path",
            )
            candidate: Path | None = None
            if typed.strip():
                path = Path(typed.strip())
                if path.exists():
                    candidate = path
                    st.caption(f"Ready to analyse: {path.name}")
                else:
                    st.error("That file could not be found.")
            if st.button("Analyse this file"):
                if candidate is None:
                    st.warning("Enter a path to a video file first.")
                else:
                    _analyse(candidate, _current_settings(), provider_mode="model")
        elif source_mode == "Sample clip":
            st.caption(
                "Writes a small deterministic clip with a monotonic arm sweep and "
                "analyses it with a scripted provider. It has no swing shape, so the "
                "moment estimator should refuse it; that is the honest result."
            )
            if st.button("Create sample clip and analyse"):
                _analyse(
                    _write_sample_clip(_current_settings()),
                    _current_settings(),
                    provider_mode="sample",
                )
        elif source_mode == "Guided playback sample":
            st.caption(
                "Writes the deterministic guided fixture (a scripted set-up, "
                "backswing, downswing, impact region and finish) and analyses it "
                "with the matching scripted provider. It exercises the guided "
                "playback mechanics only: it is not evidence that the estimator "
                "finds real swing moments."
            )
            if st.button("Create guided fixture and analyse"):
                clip_path, _ = _write_guided_clip()
                _analyse(clip_path, _current_settings(), provider_mode="guided")
        elif source_mode == GUIDED_PORTRAIT_SOURCE:
            st.caption(
                "The same scripted fixture rendered in portrait (360x640). It checks "
                "that the guided playback and its overlays stay aligned in a "
                "portrait clip; it is still sample data, not a real swing."
            )
            if st.button("Create portrait guided fixture and analyse"):
                clip_path, clip = _write_guided_clip(portrait=True)
                _analyse(
                    clip_path,
                    _current_settings(),
                    provider_mode="guided",
                    guided_clip=clip,
                )
        else:
            st.caption("Upload a video in the panel above.")

        st.divider()
        st.markdown("**Engine settings**")
        settings = _current_settings()
        min_visibility = st.slider(
            "Minimum landmark visibility",
            0.0,
            0.95,
            float(settings["min_visibility"]),
            0.05,
            key="min_visibility",
        )
        min_presence = st.slider(
            "Minimum landmark presence",
            0.0,
            0.95,
            float(settings["min_presence"]),
            0.05,
            key="min_presence",
        )
        max_frames = st.number_input(
            "Maximum frames to process",
            min_value=1,
            max_value=200000,
            value=int(settings["max_frames"]),
            key="max_frames",
        )
        transcode = st.checkbox(
            "Transcode annotated video to H.264",
            value=bool(settings["transcode"]),
            key="transcode",
            help="Needed for reliable playback in a browser.",
        )
        show_all_landmarks = st.checkbox(
            "Label every landmark",
            value=bool(settings["show_all_landmarks"]),
            key="show_all_landmarks",
        )
        mirrored = st.checkbox(
            "Input video is mirrored",
            value=bool(settings["mirrored"]),
            key="mirrored",
            help=(
                "Records the mirroring convention in metadata. The engine does not "
                "flip anatomy: lead/trail labels assume an unmirrored video."
            ),
        )
        hold_seconds = st.slider(
            "Hold each check for (seconds)",
            min_value=0.6,
            max_value=4.0,
            value=float(_control("hold_seconds", 1.8)),
            step=0.2,
            key="hold_seconds",
        )
        _mirror("min_visibility", float(min_visibility))
        _mirror("min_presence", float(min_presence))
        _mirror("max_frames", int(max_frames))
        _mirror("transcode", bool(transcode))
        _mirror("show_all_landmarks", bool(show_all_landmarks))
        _mirror("mirrored", bool(mirrored))
        _mirror("hold_seconds", float(hold_seconds))

        if analysis is not None:
            st.divider()
            st.markdown("**Debug overlay (full skeleton, technical labels)**")
            frame_index = (
                reference_frame
                if reference_frame is not None
                else analysis.records[0].index
            )
            record = _record_at(analysis.records, int(frame_index))
            frame, _ = _read_frame(analysis.source, int(frame_index))
            if frame is not None and record is not None:
                debug = render_overlay(
                    frame,
                    record,
                    OverlayConfig(
                        show_all_landmarks=bool(
                            _current_settings()["show_all_landmarks"]
                        ),
                        min_visibility=float(
                            _current_settings()["min_visibility"]
                        ),
                        fps=analysis.info.fps,
                        quality=analysis.config.measurement,
                    ),
                )
                st.image(cv2.cvtColor(debug, cv2.COLOR_BGR2RGB), width="stretch")
            st.json(
                {
                    "source": str(analysis.source),
                    "settings_key": analysis.settings_key,
                    "pose_provider": analysis.provider_name,
                    "frames": [analysis.records[0].index, analysis.records[-1].index],
                    "frame_size": [analysis.info.width, analysis.info.height],
                    "fps": round(analysis.info.fps, 3),
                    "mirrored_input": bool(_current_settings()["mirrored"]),
                }
            )
        if st.button("Start over"):
            _reset_analysis_state()
            st.rerun()


def _render_export(
    analysis: Analysis, selection: PhaseSelection, moments: MomentSet
) -> None:
    output_dir = RUN_DIR / hashlib.sha1(
        str(analysis.source).encode("utf-8")
    ).hexdigest()[:10]
    export_config = PipelineConfig(
        measurement=analysis.config.measurement,
        overlay=analysis.config.overlay,
        max_frames=analysis.config.max_frames,
        write_annotated=True,
        transcode_h264=analysis.config.transcode_h264,
    )
    progress = st.progress(0.0, text="Rendering the annotated video")

    def report(fraction: float, message: str) -> None:
        progress.progress(
            min(1.0, max(0.0, fraction)), text="Rendering the annotated video"
        )

    try:
        result = finalize_run(
            analysis.source,
            output_dir,
            analysis.records,
            info=analysis.info,
            config=export_config,
            phases=selection,
            progress=report,
            progress_span=(0.0, 0.95),
            pose_provider_name=analysis.provider_name,
            warnings=analysis.warnings,
        )
    finally:
        progress.empty()

    moments_path = write_json(
        moments.to_dict(), output_dir / "moments.json"
    )
    result.artifacts["moments_json"] = moments_path

    state = _state()
    state["last_result"] = result
    state["last_result_key"] = (analysis.settings_key, _selection_key(selection))


def _show_outputs(result) -> None:
    annotated = result.annotated_path
    if annotated is not None and annotated.exists():
        st.markdown("**Debug video (full skeleton overlay)**")
        st.video(str(annotated))
        st.caption(f"{annotated}")
    else:
        st.info("No annotated video was produced for this run.")

    columns = st.columns(4)
    downloads = (
        ("measurements.csv", result.artifact("measurements_csv")),
        ("landmarks.csv", result.artifact("landmarks_csv")),
        ("metadata.json", result.artifact("metadata_json")),
        ("moments.json", result.artifact("moments_json")),
    )
    for column, (label, path) in zip(columns, downloads):
        with column:
            if path is not None and Path(path).exists():
                st.download_button(
                    label,
                    data=Path(path).read_bytes(),
                    file_name=Path(path).name,
                    key=f"download_{label}",
                )
    with st.expander("Run summary", expanded=False):
        st.json(
            {
                "frames": result.stats.get("frames"),
                "frames_with_pose": result.stats.get("frames_with_pose"),
                "reference_frame": result.phases.reference_frame,
                "phases": result.phases.to_dict(),
                "warnings": result.warnings,
                "artifacts": {k: str(v) for k, v in result.artifacts.items()},
            }
        )


def main() -> None:
    st.set_page_config(page_title="Swing Guru", layout="centered")
    _install_style()
    _masthead()

    state = _state()
    analysis: Analysis | None = state.get("analysis")
    settings = _current_settings()

    if analysis is not None and _settings_key(analysis.source, settings) != (
        analysis.settings_key
    ):
        st.warning(
            "Your settings changed, so this analysis no longer matches them. "
            "Analyse the swing again to see updated numbers."
        )
        analysis = None

    if analysis is None:
        _analyse_card()
        _show_error()
        _developer_details(None)
        return

    if settings["mirrored"]:
        st.warning(
            "This video is marked as mirrored. Lead and trail labels assume an "
            "unmirrored video, so check the left/right labels before reading the "
            "numbers."
        )

    moments = _moments_for(analysis, settings)
    reference_mode, reference_frame, reference_source = _reference_choice(
        moments, analysis.records[0].index, analysis.records[-1].index
    )
    selection = ManualPhaseProvider(
        frames=moments.available_frames(),
        reference_frame=reference_frame,
        derive_reference_from_address=False,
    ).select()

    preview = _ensure_preview(analysis, settings)
    if preview.ok and preview.src:
        _guided_section(
            analysis,
            moments=moments,
            preview=preview,
            reference_frame=reference_frame,
            reference_source=reference_source,
            reference_mode=reference_mode,
        )
    else:
        _still_fallback(
            analysis,
            moments=moments,
            preview=preview,
            reference_frame=reference_frame,
        )

    _adjust_moments_section(
        analysis, moments=moments, reference_frame=reference_frame
    )
    _advanced_section(
        analysis,
        moments=moments,
        preview=preview,
        reference_mode=reference_mode,
        reference_frame=reference_frame,
        selection=selection,
        first_index=analysis.records[0].index,
        last_index=analysis.records[-1].index,
    )
    _developer_details(analysis, reference_frame=reference_frame)


main()
