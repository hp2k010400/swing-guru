"""Analyse one swing video and build the standalone viewer bundle.

    .venv\\Scripts\\python.exe scripts\\build_analysis.py VIDEO --out var\\analysis\\real --serve

Pipeline: RTMW pose (or MediaPipe) -> automatic moments -> Address-relative
measurements -> club tracking on the same pose -> ``analysis.json`` + a
browser-friendly ``swing.mp4`` + the viewer (``index.html``/``app.css``/
``app.js``). ``--serve`` then serves the folder on http://127.0.0.1:8765.

Club tracking only needs the frames around the six analysis moments, so each
run builds merged ``--club-window`` windows around them, decodes just those
frames plus the background-reference frames, and runs one selection pass per
window. ``--club-full`` restores the original whole-clip pass for diagnostics
and for the benchmark baseline.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
from typing import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from swingguru.analysis_bundle import ANALYSIS_PHASES, build_analysis  # noqa: E402
from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.club import ClassicalClubTracker  # noqa: E402
from swingguru.club.cache import (  # noqa: E402
    CANDIDATE_CACHE_VERSION,
    CandidateCache,
    CandidateIdentity,
    config_fingerprint,
    pose_fingerprint,
    reference_fingerprint,
)
from swingguru.club.fallback import BoxCandidate, ModelUnavailable, OwlShaftFallback, fallback_shaft  # noqa: E402
from swingguru.club.metrics import reportable  # noqa: E402
from swingguru.club.tracker import (  # noqa: E402
    hands_context,
    reference_hands,
    torso_length_px,
)
from swingguru.club.windows import CLUB_WINDOW_VERSION, frames_in, phase_windows  # noqa: E402
from swingguru.frames import decode_selected_frames, wanted_frames  # noqa: E402
from swingguru.analysis_bundle import swing_context  # noqa: E402
from swingguru.engine_profile import profile as engine_profile_for, rebind_sides  # noqa: E402
from swingguru.camera_check import CameraCheckConfig, check_camera, motion_signal  # noqa: E402
from swingguru.pelvis_depth import PersonSegmenter, measure_pelvis_depth  # noqa: E402
from swingguru.overlay_points import overlay_points  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import ManualPhaseProvider, Phase  # noqa: E402
from swingguru.pipeline import PipelineConfig, apply_reference, process_video  # noqa: E402
from swingguru import progress  # noqa: E402
from swingguru.pose_cache import (  # noqa: E402
    PoseCacheIdentity,
    asset_fingerprint,
    load_pose_cache,
    runtime_fingerprint,
    save_pose_cache,
    settings_fingerprint,
)
from swingguru.ui import media  # noqa: E402
from swingguru.video import probe_video  # noqa: E402

VIEWER = ROOT / "src" / "swingguru" / "viewer"

# Frames either side of each moment. The tracker needs neighbouring frames for
# temporal continuity, angular consistency and confidence; five either side
# matched the whole-clip result exactly on the real DTL swing (see REPORT.md).
DEFAULT_CLUB_WINDOW = 5

# The detected-person cadence the pose tracker re-anchors on. Measured on the
# real swing: 6 and above move the Address moment and break the body
# measurements, so 5 is the retained value; it is explicit here (and part of
# the pose cache identity) so a different cadence cannot reuse this cache.
DEFAULT_DET_FREQUENCY = 5
POSE_MODEL = "wholebody"
POSE_MODE = "balanced"
POSE_TRACKER = "rtmlib-pose-tracker"


def pose_identity(
    video: Path,
    pose: str,
    det_frequency: int,
    *,
    options: Mapping[str, object] | None = None,
) -> PoseCacheIdentity:
    """Identity of one pose configuration.

    Covers the video content, the provider/model/mode/cadence/tracker, the
    digests of the ONNX assets that will be loaded, the installed runtime
    versions and the measurement settings the stored records were derived with,
    plus any options of a pose variant in use.
    """

    rtmw = pose == "rtmw"
    model_digest = ""
    if rtmw:
        from swingguru.pose_rtm import local_model_paths

        model_digest = asset_fingerprint(local_model_paths(POSE_MODEL, POSE_MODE))
    return PoseCacheIdentity(
        video=video_digest(video),
        provider=pose,
        model=POSE_MODEL if rtmw else "",
        mode=POSE_MODE if rtmw else "",
        det_frequency=int(det_frequency) if rtmw else 0,
        tracker=POSE_TRACKER if rtmw else "mediapipe-pose",
        model_digest=model_digest,
        runtime=runtime_fingerprint(),
        measurement=settings_fingerprint(MeasurementConfig()),
        options=json.dumps(dict(options or {}), sort_keys=True),
    )


def pose_records(
    video: Path,
    pose: str,
    cache: Path | None,
    work: Path,
    *,
    det_frequency: int = DEFAULT_DET_FREQUENCY,
    allow_legacy_cache: bool = False,
    options: Mapping[str, object] | None = None,
) -> tuple[list, str, PoseCacheIdentity]:
    """Pose records for this video, from the keyed cache or from the model.

    Returns ``(records, cache_status, identity)``. The status is recorded in
    the bundle so a run never silently reuses records it did not identify.
    """

    identity = pose_identity(video, pose, det_frequency, options=options)
    cached = load_pose_cache(cache, identity, allow_legacy=allow_legacy_cache)
    if cached.hit:
        return cached.records or [], cached.status, identity

    provider = None
    if pose == "rtmw":
        from swingguru.pose_rtm import RTMPoseAdapter

        provider = RTMPoseAdapter(POSE_MODEL, POSE_MODE, det_frequency=det_frequency)
    result = process_video(
        video,
        work / "pose",
        config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
        pose_provider=provider,
        phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
    )
    records = list(result.records)
    if cache is not None:
        save_pose_cache(cache, records, identity)
    return records, cached.status, identity


def video_digest(video: Path) -> str:
    digest = hashlib.sha1()
    with video.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def run_fallback(video: Path, out: Path, records, frames, moments, track, timings: dict) -> tuple[dict, list]:
    """OWLv2 only on key phases the classical tracker could not confirm.

    OWLv2 boxes are cached in ``club_fallback.json`` inside the bundle, keyed
    by the video's content hash and frame, so rebuilding or replaying never
    runs the model again for the same frame.

    ``frames`` is the decoded ``{frame_index: image}`` map, so the fallback
    also never triggers a second decode.
    """

    cache_path = out / "club_fallback.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    digest = video_digest(video)
    torso = torso_length_px(records)
    owl = OwlShaftFallback()
    overrides, log = {}, []
    started = time.monotonic()
    model_seconds = 0.0
    ran, cached_hits = 0, 0
    undecoded = 0
    unavailable: str | None = None
    for phase in ANALYSIS_PHASES:
        frame_index = moments.frames().get(phase)
        if frame_index is None or reportable(track.at(int(frame_index))):
            continue
        image = frames.get(int(frame_index))
        if image is None:
            log.append({
                "method": "owlv2-fallback",
                "phase": phase.value,
                "frame": int(frame_index),
                "reason": "the frame was not decoded, so no fallback was attempted",
                "level": "UNAVAILABLE",
                "angle": None,
            })
            undecoded += 1
            continue
        record = next(r for r in records if int(r.index) == int(frame_index))
        hands = hands_context(record, torso_px=torso, config=track.config)
        key = f"{digest}:{frame_index}"
        cached = key in cache
        if not cached and hands.origin is not None:
            began = time.monotonic()
            try:
                boxes = owl.boxes(image, hands.origin, torso)
            except ModelUnavailable as error:
                # Optional model missing: this phase keeps only what the classical tracker found
                # (its club checks are withheld downstream). Nothing is cached, so a later run
                # with the model installed still gets a real answer for this frame.
                model_seconds += time.monotonic() - began
                unavailable = str(error)
                log.append({"method": "owlv2-fallback", "phase": phase.value, "frame": int(frame_index),
                            "reason": "the optional club model is unavailable, so no fallback was attempted",
                            "level": "UNAVAILABLE", "angle": None, "model_unavailable": True})
                continue
            model_seconds += time.monotonic() - began
            ran += 1
            cache[key] = [[*b.box, b.score] for b in boxes]
        elif cached:
            cached_hits += 1
        boxes = [BoxCandidate(tuple(item[:4]), item[4]) for item in cache.get(key, [])]
        shaft, diagnostics = fallback_shaft(
            image, int(frame_index), record.timestamp_ms,
            hands, track, boxes,
        )
        diagnostics.update({"phase": phase.value, "frame": int(frame_index), "cached": cached,
                            "level": shaft.level, "angle": shaft.angle_deg})
        log.append(diagnostics)
        if shaft.available:
            overrides[int(frame_index)] = shaft
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    timings["owlv2_model_load_s"] = round(owl.load_seconds, 1)
    # Only frames the model really ran count as inference (a failed load is load time, not inference).
    timings["owlv2_inference_s"] = round(max(0.0, model_seconds - owl.load_seconds), 1) if ran else 0.0
    timings["club_fallback_total_s"] = round(time.monotonic() - started, 1)
    timings["owlv2_frames_run"] = ran
    timings["owlv2_frames_cached"] = cached_hits
    timings["owlv2_frames_undecoded"] = undecoded
    timings["owlv2_unavailable"] = unavailable
    return overrides, log


def club_windows(moments, records, radius: int):
    """Merged, clipped windows around every analysis moment."""

    first = int(getattr(records[0], "index"))
    last = int(getattr(records[-1], "index"))
    frames = {phase: moments.frames().get(phase) for phase in ANALYSIS_PHASES}
    return phase_windows(frames, radius, lower=first, upper=last, phases=ANALYSIS_PHASES)


def open_candidate_cache(out: Path, identity: CandidateIdentity, *, club_cache: bool, full_sequence: bool):
    """The candidate cache only belongs to the windowed pass.

    The whole-clip pass computes every frame, so loading the cache there would
    neither save work nor describe what happened, and saving it afterwards
    would replace a populated windowed cache with whole-clip entries. Both are
    avoided by not opening it at all.
    """

    if not club_cache or full_sequence:
        return None
    return CandidateCache.load(identity, out / "club_candidates.json")


PELVIS_PHASES = {"address": Phase.ADDRESS, "top": Phase.TOP, "downswing": Phase.DOWNSWING, "impact": Phase.IMPACT}


def measure_pelvis_and_camera(video: Path, records, moments, frames, pose: str,
                              timings: dict, profile=None) -> tuple[dict, dict, dict | None]:
    """Rear-of-pelvis (tush line), DTL camera validity and the plane-reference registration.

    Uses frames already decoded for the club windows (each moment is a window
    centre); a frame missing from that set is read directly. Segmentation runs
    on five frames, so no cache is needed. The Address -> Mid-Back background
    comparison only places the plane reference; it is not part of the camera
    check.
    """

    import cv2

    mark = time.monotonic()
    by_index = {int(r.index): r for r in records}
    context = swing_context(records, moments.frames().get(Phase.ADDRESS), pose, profile)
    # Camera checks keep the Address height their tolerances were set against; the rear-pelvis
    # percentage uses the movement normaliser (posture-independent under the ``stature`` fix).
    camera_height = context.camera_height_px if profile is not None else context.body_height_px
    capture = None
    selected: dict[str, tuple] = {}
    for name, phase in {**PELVIS_PHASES, "mid_back": Phase.MID_BACK}.items():
        index = moments.frames().get(phase)
        if index is None:
            continue
        frame = frames.get(int(index)) if hasattr(frames, "get") else None
        if frame is None:
            capture = capture or cv2.VideoCapture(str(video))
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(index))
            ok, frame = capture.read()
            frame = frame if ok else None
        selected[name] = (int(index), frame, by_index.get(int(index)))
    if capture is not None:
        capture.release()
    segmenter = PersonSegmenter()
    masks = {name: (None if frame is None else segmenter.mask(frame)) for name, (_, frame, _) in selected.items()}
    segmenter.close()
    mid_back = selected.pop("mid_back", None)
    mid_back_mask = masks.pop("mid_back", None)

    def hip_score(record):
        landmarks = getattr(getattr(record, "points", None), "landmarks", {}) or {}
        values = [min(l.visibility or 0.0, l.presence or 0.0) for n in ("left_hip", "right_hip")
                  for l in [landmarks.get(n)] if l is not None]
        return min(values) if values else None

    pelvis = measure_pelvis_depth(
        selected, masks,
        toward_ball=context.toward_ball if context.toward_ball_known else None,
        torso_px=context.torso_px, body_height_px=context.body_height_px,
        hip_scores={name: hip_score(record) for name, (_, _, record) in selected.items()},
    )
    address, impact = selected.get("address", (None, None, None)), selected.get("impact", (None, None, None))
    camera = check_camera(
        address[2], impact[2], frame_size=(context.width, context.height),
        body_height_px=camera_height, torso_px=context.torso_px,
        address_frame=address[1], impact_frame=impact[1],
        address_mask=masks.get("address"), impact_mask=masks.get("impact"),
        # Camera motion is checked Address -> Top and Address -> Mid-Down too.
        motion_frames={name: (selected[name][1], masks.get(name)) for name in ("top", "downswing") if name in selected},
    )
    reference_motion = None
    if mid_back is not None and mid_back[1] is not None and address[1] is not None:
        signal = motion_signal((address[1], masks.get("address")), {"mid_back": (mid_back[1], mid_back_mask)},
                               context.height, CameraCheckConfig(), body_height_px=camera_height)
        reference_motion = signal["comparisons"].get("address->mid_back")
    timings["pelvis_and_camera_s"] = round(time.monotonic() - mark, 2)
    return pelvis, camera, reference_motion


def plane_frames(analysis: dict, track, overrides: Mapping | None) -> dict:
    """The club around Mid-Back and Mid-Down (+-2 frames): angle from horizontal and its source."""

    from swingguru.analysis_bundle import shaft_line_angle

    out = {}
    frames = {p["phase"]: p.get("frame_index") for p in analysis.get("phases", [])}
    for phase in ("address", "mid_back", "downswing"):
        centre = frames.get(phase)
        if centre is None:
            continue
        rows = []
        for f in range(int(centre) - 2, int(centre) + 3):
            for source, shaft in (("override", (overrides or {}).get(f)), ("track", track.at(f) if track else None)):
                if shaft is None:
                    continue
                usable = shaft.available and not shaft.interpolated and shaft.level in ("HIGH", "MEDIUM")
                rows.append({"frame": f, "source": source, "usable": bool(usable), "level": shaft.level,
                             "line_deg": None if shaft.angle_deg is None else round(shaft_line_angle(float(shaft.angle_deg)), 2)})
        out[phase] = {"frame": int(centre), "frames": rows}
    return out


def recover_club(tracker, track, frames, records, torso: float, timings: dict):
    """Temporal recovery through short backswing gaps (diagnostic track; phase checks keep ``track``)."""

    from swingguru.club.temporal import recover_track
    from swingguru.club.tracker import build_references

    mark = time.monotonic()
    references = build_references(frames, records, torso_px=torso, config=tracker.config)
    refined, log = recover_track(tracker, track, frames, records, torso_px=torso, references=references)
    timings["club_temporal_s"] = round(time.monotonic() - mark, 1)
    recovered = [f.frame_index for f in refined.frames if f.status == "recovered" and f.level in ("HIGH", "MEDIUM")]
    return refined, {"tracker": refined.tracker, "recovered_frames": recovered, "gaps": log}


def find_takeaway_p2(records, moments, track, pose: str, *, fps: float | None = None) -> tuple[list, dict]:
    """Shaft parallel to the ground on the way back (P2) from the (temporally refined) club track."""

    from swingguru.takeaway_p2 import detect

    frames_of = moments.frames()
    takeaway, mid_back = frames_of.get(Phase.TAKEAWAY), frames_of.get(Phase.MID_BACK)
    address = frames_of.get(Phase.ADDRESS)
    path = []
    if takeaway is not None and mid_back is not None:
        # A few frames past Mid-Back so the frame after the crossing is seen.
        for index in range(int(takeaway) - 3, int(mid_back) + 4):
            shaft = track.at(index)
            if shaft is None:
                continue
            path.append({"frame": index, "available": bool(shaft.available), "interpolated": bool(shaft.interpolated),
                         "status": shaft.status, "confidence": round(float(shaft.confidence), 3),
                         "level": shaft.level, "angle_deg": None if shaft.angle_deg is None else round(float(shaft.angle_deg), 2),
                         "grip": None if shaft.grip is None else [round(float(v), 2) for v in shaft.grip],
                         "tip": None if shaft.tip is None else [round(float(v), 2) for v in shaft.tip]})
    context = swing_context(records, address, pose)
    reference = track.at(int(address)) if address is not None else None
    length = None
    if reference is not None and reference.available and reference.grip is not None and reference.tip is not None:
        length = float(np.hypot(*(np.asarray(reference.tip, float) - np.asarray(reference.grip, float))))
    result = detect(path, takeaway_frame=takeaway, mid_back_frame=mid_back,
                    toward_ball=context.toward_ball if context.toward_ball_known else None, address_shaft_px=length,
                    fps=fps, first_frame=int(records[0].index))
    return path, result


def build(
    video: Path,
    out: Path,
    *,
    pose: str,
    cache: Path | None,
    window: int = DEFAULT_CLUB_WINDOW,
    full_sequence: bool = False,
    club_cache: bool = True,
    det_frequency: int = DEFAULT_DET_FREQUENCY,
    allow_legacy_pose_cache: bool = False,
    pose_options: Mapping[str, object] | None = None,
    progress_dir: Path | None = None,
    engine_profile: str | None = None,
    engine_fixes: Sequence[str] | None = None,
    time_scale_hint: float | None = None,
) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    # ``current`` (default, production) passes no profile at all, so every code path is unchanged.
    profile = engine_profile_for(engine_profile, only=engine_fixes)
    profile = None if profile.name == "current" else profile
    started = time.monotonic()
    timings: dict = {}

    def step(stage: str, state: str, detail: str | None = None, *, internal: bool = False) -> None:
        """Record a real stage boundary for the run-progress artifact (no-op without one)."""

        if progress_dir is not None:
            progress.record(progress_dir, stage, state, detail=detail, source="engine", internal=internal)

    step("moments", progress.ACTIVE, "engine starting")
    mark = time.monotonic()
    step("pose", progress.ACTIVE, internal=True)
    records, pose_cache_status, pose_cache_identity = pose_records(
        video,
        pose,
        cache,
        out,
        det_frequency=det_frequency,
        allow_legacy_cache=allow_legacy_pose_cache,
        options=pose_options,
    )
    timings["pose_s"] = round(time.monotonic() - mark, 1)
    timings["pose_cache"] = pose_cache_status
    timings["pose_cached"] = pose_cache_status in {"hit", "hit-legacy"}
    step("pose", progress.DONE, f"cache {pose_cache_status}", internal=True)
    step("phase_detection", progress.ACTIVE, internal=True)
    mark = time.monotonic()
    time_scale = None
    # Only the candidate profile's address_recheck fix changes the moment config; otherwise it is the
    # default, exactly as before.
    if profile is not None and profile.on("address_recheck"):
        from functools import partial

        from swingguru.auto_moments import MomentConfig

        estimate = partial(estimate_moments, config=MomentConfig(address_recheck=True))
    else:
        estimate = estimate_moments
    if profile is not None and profile.on("slowmo"):
        from swingguru.slowmo import estimate_with_slowmo

        moments, time_scale = estimate_with_slowmo(records, estimate, hint=time_scale_hint)
    else:
        moments = estimate(records)
    address = moments.frames().get(Phase.ADDRESS)
    handedness = "right"
    if profile is not None and profile.on("handedness"):
        # Left/right-handed from the down-the-line ball side; the lead/trail aliases on the cached
        # records were written right-handed, so they are rebound before measuring.
        context = swing_context(records, address, pose, profile)
        handedness = context.handedness
        if handedness != "right":
            rebind_sides(records, handedness)
    apply_reference(records, address, config=MeasurementConfig(handedness=handedness))
    timings["phases_and_measurements_s"] = round(time.monotonic() - mark, 2)
    step("moments", progress.DONE, f"pose cache {pose_cache_status}")
    step("phase_detection", progress.DONE, internal=True)

    step("club", progress.ACTIVE)
    step("club_tracking", progress.ACTIVE, internal=True)
    tracker = ClassicalClubTracker()
    torso = torso_length_px(records)
    windows = () if full_sequence else club_windows(moments, records, window)
    detection_frames = tuple(range(int(records[0].index), int(records[-1].index) + 1)) if full_sequence else frames_in(windows)
    references = reference_hands(records, torso_px=torso, config=tracker.config)
    reference_indices = tuple(index for index, _ in references)
    roles = wanted_frames(detection_frames, reference_indices)
    mark = time.monotonic()
    decoded = decode_selected_frames(video, roles, max_index=int(records[-1].index))
    timings["club_decode_s"] = round(time.monotonic() - mark, 2)
    frames = decoded.frames

    identity = CandidateIdentity(
        tracker=tracker.name,
        video=video_digest(video),
        pose=pose_fingerprint(records),
        config=config_fingerprint(tracker.config),
        references=reference_fingerprint(references),
    )
    candidates = open_candidate_cache(
        out, identity, club_cache=club_cache, full_sequence=full_sequence
    )
    mark = time.monotonic()
    if full_sequence:
        track = tracker.track_sequence(frames, records)
    else:
        track = tracker.track_windows(frames, records, windows, cache=candidates)
    timings["club_classical_s"] = round(time.monotonic() - mark, 1)
    if candidates is not None:
        candidates.save()
    timings["club_frames_analysed"] = len(track.detections)
    timings["club_frames_computed"] = (
        len(track.detections) if candidates is None else candidates.misses
    )
    timings["club_frames_from_cache"] = 0 if candidates is None else candidates.hits
    overrides, fallback_log = run_fallback(video, out, records, frames, moments, track, timings)
    step("club_tracking", progress.DONE,
         f"{len(track.detections)} frame(s) tracked, {len(fallback_log)} fallback attempt(s)", internal=True)
    step("club", progress.DONE,
         f"{len(track.detections)} frame(s) tracked, {len(fallback_log)} fallback attempt(s)")
    step("positions", progress.ACTIVE)
    step("camera", progress.ACTIVE, internal=True)
    pelvis_depth, camera, reference_motion = measure_pelvis_and_camera(video, records, moments, frames, pose, timings,
                                                                       profile)
    step("camera", progress.DONE, internal=True)
    step("positions", progress.DONE)
    step("build", progress.ACTIVE)
    # "render" covers the remaining real work: temporal club recovery, takeaway
    # P2, the preview transcode and writing analysis.json / the viewer.
    step("render", progress.ACTIVE, internal=True)
    refined, club_temporal = recover_club(tracker, track, frames, records, torso, timings)
    mark = time.monotonic()
    info = probe_video(video)
    fps = float(info.fps) if info.fps else 30.0
    club_path, takeaway_p2 = find_takeaway_p2(records, moments, refined, pose, fps=fps)

    preview = media.prepare_preview(
        video,
        output_dir=out / "preview",
        first_index=int(records[0].index),
        frame_count=len(records),
        fps=fps,
        width=int(info.width),
        height=int(info.height),
    )
    if not preview.ok or preview.path is None:
        raise SystemExit(f"could not build the preview video: {preview.message}")
    shutil.copyfile(preview.path, out / "swing.mp4")

    # The plane checks read the temporally refined club track only under the ``refined_club`` fix.
    plane_track = refined if (profile is not None and profile.on("refined_club")) else track
    analysis = build_analysis(
        records, moments, plane_track, fps=fps, video="swing.mp4", source_name=video.name, pose_model=pose,
        shaft_overrides=overrides, pelvis_depth=pelvis_depth, camera_check=camera, takeaway_p2=takeaway_p2,
        reference_motion=reference_motion, profile=profile,
    )
    # Presentation only: tracked body points at each moment for the replay's guide lines.
    analysis["overlay"] = overlay_points(records, analysis.get("phases", []), float(info.width), float(info.height),
                                         stabilise=profile is not None)
    timings["preview_and_bundle_s"] = round(time.monotonic() - mark, 1)
    timings["total_s"] = round(time.monotonic() - started, 1)
    analysis["engine"] = {
        "pose": pose,
        "pose_cache": {**pose_cache_identity.to_dict(), "status": pose_cache_status},
        "club_tracker": track.tracker,
        "club_fallback": fallback_log,
        "club_path": club_path,
        "club_temporal": club_temporal,
        "timings": timings,
        "club_windows": {
            "mode": "full-sequence" if full_sequence else "event-windows",
            "version": CLUB_WINDOW_VERSION,
            "radius_frames": None if full_sequence else window,
            "cache_version": CANDIDATE_CACHE_VERSION,
            "candidate_cache": None if candidates is None else candidates.stats,
            "windows": [
                {"start": item.start, "end": item.end, "frames": item.length} for item in windows
            ],
        },
        "counters": {
            "video_frames": info.frame_count,
            "pose_frames": len(records),
            "club_frames_requested": len(detection_frames),
            "club_frames_analysed": len(track.detections),
            "club_frames_detected": timings["club_frames_computed"],
            "club_frames_from_cache": timings["club_frames_from_cache"],
            "reference_frames": len(reference_indices),
            "reference_only_frames": len([index for index in reference_indices if index not in set(detection_frames)]),
            "decoded_frames": decoded.kept,
            "decoded_scanned": decoded.scanned,
            "decoded_skipped": decoded.skipped,
            "decoded_missing": list(decoded.missing),
            "owlv2_frames_run": timings.get("owlv2_frames_run", 0),
            "owlv2_frames_cached": timings.get("owlv2_frames_cached", 0),
        },
    }
    if time_scale is not None:
        analysis["time_scale"] = time_scale
    if moments.sequence.get("address_recheck"):
        analysis["address_recheck"] = {**moments.sequence["address_recheck"], "status": "applied"}
    if profile is not None:
        # Diagnostics for measurement-noise estimates (next profile only): the club at each plane
        # moment +-2 frames, from the same track the plane checks read (OWLv2 override marked).
        analysis["engine"]["plane_frames"] = plane_frames(analysis, plane_track, overrides)
    (out / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    for name in ("index.html", "app.css", "app.js"):
        shutil.copyfile(VIEWER / name, out / name)
    step("render", progress.DONE, "analysis.json written", internal=True)
    step("build", progress.DONE, "analysis.json written")
    return analysis


def serve(directory: Path, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"Serving {directory} at http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


# Address fix A' (candidate fix address_recheck): the re-checked Address is kept only if the club is
# steady there - every frame of Address +-2 usable and within this many degrees. The club starts the
# takeaway before the hands and body register movement, so a body-still frame can already have a
# moving club (FIX_address_recheck.md: prod/02 123 -> 109 deg, 1_0_1/01 114 -> 51 deg).
CLUB_STEADY_DEG = 3.0


def club_steady_at_address(analysis: dict) -> tuple[bool, str]:
    """Whether the club line is steady across the Address +-2 frames (address-reference rows)."""

    rows = (analysis.get("address_reference") or {}).get("frames") or []
    if len(rows) < 5:
        return False, "no club readings around the Address"
    if not all(r.get("usable") and r.get("line_deg") is not None for r in rows):
        return False, "club not clearly detected on every frame around the Address"
    lines = [float(r["line_deg"]) for r in rows]
    spread = max(lines) - min(lines)
    if spread > CLUB_STEADY_DEG:
        return False, f"club moving at the Address ({spread:.1f} deg across 5 frames)"
    return True, f"club steady ({spread:.1f} deg across 5 frames)"


def build_guarded(video: Path, out: Path, **kwargs) -> dict:
    """``build``, with fix A''s club-steadiness guard: a re-checked Address whose club is not steady is
    undone by building again without the re-check (only these rare swings pay for a second pass)."""

    analysis = build(video, out, **kwargs)
    recheck = analysis.get("address_recheck")
    if not recheck:
        return analysis
    steady, reason = club_steady_at_address(analysis)
    if steady:
        analysis["address_recheck"] = {**recheck, "guard": reason}
        (out / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
        return analysis
    fixes = kwargs.get("engine_fixes")
    if fixes is None:
        fixes = sorted(engine_profile_for(kwargs.get("engine_profile")).fixes)
    analysis = build(video, out, **{**kwargs, "engine_fixes": [f for f in fixes if f != "address_recheck"]})
    analysis["address_recheck"] = {**recheck, "status": "reverted", "guard": reason}
    (out / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    return analysis


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--out", default=str(ROOT / "var" / "analysis" / "latest"))
    parser.add_argument("--pose", choices=("rtmw", "mediapipe"), default="rtmw")
    parser.add_argument("--records", default=None, help="cache file for pose records")
    parser.add_argument(
        "--club-window",
        type=int,
        default=DEFAULT_CLUB_WINDOW,
        help="frames either side of each moment to run club detection on",
    )
    parser.add_argument(
        "--club-full",
        action="store_true",
        help="run the classical tracker over the whole clip (benchmark baseline)",
    )
    parser.add_argument(
        "--no-club-cache",
        action="store_true",
        help="do not read or write the cached club frame candidates",
    )
    parser.add_argument(
        "--det-frequency",
        type=int,
        default=DEFAULT_DET_FREQUENCY,
        help="frames between person-detector re-anchoring in the RTMW tracker",
    )
    parser.add_argument(
        "--allow-legacy-pose-cache",
        action="store_true",
        help="accept a pre-keying pose cache that carries no identity",
    )
    parser.add_argument(
        "--progress",
        default=None,
        help="directory for the run-local progress artifact (written by the analysis runner)",
    )
    parser.add_argument("--engine-profile", choices=("current", "next", "candidate"), default=None,
                        help="engine profile (default: SG_ENGINE_PROFILE, else current = production)")
    parser.add_argument("--engine-fixes", default=None,
                        help="with --engine-profile next/candidate: comma-separated subset of fixes (validation only)")
    parser.add_argument("--time-scale", type=float, default=None,
                        help="slow-motion factor found by the swing-window finder (candidate profile, slowmo)")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.club_window < 0:
        parser.error("--club-window must not be negative")
    if args.det_frequency < 1:
        parser.error("--det-frequency must be at least 1")
    out = Path(args.out)
    analysis = build_guarded(
        Path(args.video),
        out,
        pose=args.pose,
        cache=Path(args.records) if args.records else None,
        window=args.club_window,
        full_sequence=args.club_full,
        club_cache=not args.no_club_cache,
        det_frequency=args.det_frequency,
        allow_legacy_pose_cache=args.allow_legacy_pose_cache,
        engine_profile=args.engine_profile,
        # SG_ENGINE_FIXES: an explicit fix list for experiments run through the app pipeline (the trim
        # study); never set on staging or production.
        engine_fixes=(args.engine_fixes or os.environ.get("SG_ENGINE_FIXES") or "").split(",") if (
            args.engine_fixes or os.environ.get("SG_ENGINE_FIXES")) else None,
        progress_dir=Path(args.progress) if args.progress else None,
        time_scale_hint=args.time_scale,
    )
    summary = analysis["summary"]
    print(json.dumps({
        "shown": [f"{c['phase']}:{c['kind']} {c['value']} ({c['level']}{', guided' if c['guided'] else ''}{', ' + c['source'] if c.get('source') else ''})" for p in analysis["phases"] for c in p["checks"]],
        "hidden": [f"{h['phase']}:{h['kind']} - {h['reason']}" for h in analysis["hidden"]],
        "summary": summary,
        "focus": analysis["focus"],
        "engine": analysis["engine"],
    }, indent=2))
    if args.serve:
        serve(out, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
