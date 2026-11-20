"""Run the production analysis on one uploaded video (shared by the local app server and the hosted worker).

``run_analysis`` validates the video, finds the swing, runs the unchanged
``scripts/build_analysis.py`` in a subprocess (argument list, no shell, with a
timeout) and applies the app's rejection rules. It never raises for a bad
video: it returns a :class:`RunResult` with a golfer-facing error code.

Progress is recorded by :mod:`swingguru.progress` at the real stage boundaries
(this module for the upload/moment stages, the engine for the rest) and read
back by ``stage_states``; the file-existence inference is kept only as a
fallback for old bundles and test runners. Every attempt also carries a
structured, internal diagnostics object (see ``swingguru.diagnostics``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import diagnostics as diag
from . import progress
from .progress import STAGES, stage_states  # noqa: F401 - re-exported for existing callers

ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = ROOT / "scripts" / "build_analysis.py"

MIN_SECONDS, MAX_SECONDS = 1.5, 180.0
# Swing *discovery* is tolerant, measurement stays strict. A clip this short has already been trimmed
# to (about) one swing, so when the cheap window finder is inconclusive - typically a tight trim that
# leaves less than its 0.25 s "still set-up" - the whole clip goes to the engine, whose own gates
# (>= 4 phases located, >= 1 guided check, camera/framing checks, per-measurement confidence) decide.
# Longer clips keep the finder's rejection: the engine assumes one swing per clip.
DEFER_WINDOW_MAX_S = 12.0
ANALYSIS_VERSION = "engine-2026.09.30"

ERRORS = {
    "unreadable": ("We couldn't read this video",
                   "Try exporting it again as an MP4 or MOV from your camera roll, then upload the original file."),
    "too_short": ("This video is too short", "Record from set-up to finish, a few seconds in total."),
    "too_long": ("This video is too long", "Trim it to one swing (under 3 minutes) and try again."),
    "too_large": ("This video is too large", "Trim it to one swing, or record at 1080p."),
    "no_swing": ("We couldn't find a swing", "Make sure the whole swing is in the clip, from a still set-up to the finish."),
    "no_setup": ("Your set-up isn't in the clip",
                 "We found your swing, but the clip starts after you addressed the ball. Trim it to start a moment "
                 "before your set-up, then try again."),
    "no_clear_swing": ("We couldn't find a clear swing",
                       "We need a still set-up before the swing. Edited clips and slow-motion montages don't work yet."),
    "camera_angle": ("This doesn't look like a down-the-line view",
                     "Film from behind your hands, looking along your target line."),
    "framing": ("Part of you is out of the frame", "Keep your head and both feet in the picture for the whole swing."),
    "too_far": ("You're too small in the frame", "Move the phone closer so you fill most of the picture height."),
    "timeout": ("This analysis took too long", "Please try again. A shorter clip of just one swing is quickest."),
    "upload_missing": ("Your video didn't finish uploading", "Check your connection and try again."),
    "failed": ("Something went wrong analysing this swing", "Please try again. If it keeps happening, try another video."),
}


def error_text(code: str) -> tuple[str, str]:
    return ERRORS.get(code, ERRORS["failed"])


@dataclass
class RunResult:
    ok: bool
    error: str | None = None
    detail: str = ""                    # internal only - logs, never shown to a golfer
    bundle: Path | None = None          # analysis.json, swing.mp4, ... when ok
    notice: str | None = None           # e.g. "We found 5 swings ..."
    window: dict = field(default_factory=dict)
    video_info: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)  # monotonic seconds per stage (logs and profiling)
    diagnostics: dict = field(default_factory=dict)  # versioned internal record (swingguru.diagnostics)


def probe(video: Path) -> dict | None:
    import cv2

    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return None
    fps = capture.get(cv2.CAP_PROP_FPS) or 0.0
    frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    ok, _ = capture.read()
    capture.release()
    if not ok or fps <= 0 or frames <= 0:
        return None
    return {"fps": round(fps, 3), "frames": frames, "duration_s": round(frames / fps, 3), "width": width, "height": height}


# Larger clips (4K: 2160x3840) are re-encoded to this long side before analysis. Measured on the sample
# swing: 6.2 GB peak memory at 4K vs 2.6 GB at 1080p; on production 18 of 21 generic "failed" analyses
# (6-14 Nov 2026) were 4K clips whose engine process died without a Python error. 1080p is what most
# phones send, and what the app itself uploads from 1.0.4, so the engine sees the same input it always has.
MAX_LONG_SIDE = 1920


def shrink_command(binary: str, source: Path, destination: Path) -> list[str]:
    """Every frame kept (same count and timestamps), long side 1920, upright (ffmpeg applies rotation)."""

    scale = (f"scale='if(gte(iw,ih),{MAX_LONG_SIDE},-2)':'if(gte(iw,ih),-2,{MAX_LONG_SIDE})'"
             ":flags=bicubic")
    return [binary, "-y", "-loglevel", "error", "-i", str(source), "-vf", scale, "-fps_mode", "passthrough",
            "-an", "-c:v", "libx264", "-crf", "12", "-preset", "fast", "-pix_fmt", "yuv420p", str(destination)]


def shrink_if_large(upload: Path, work: Path, info: dict) -> tuple[Path, dict, bool]:
    """Return (clip to analyse, its probe, shrunk?). Leaves clips at or under 1080p untouched."""

    if max(info.get("width", 0), info.get("height", 0)) <= MAX_LONG_SIDE:
        return upload, info, False
    from .video import ffmpeg_path

    binary = ffmpeg_path()
    if binary is None:
        raise RuntimeError("ffmpeg is needed to shrink a large clip")
    destination = work / "shrunk.mp4"
    subprocess.run(shrink_command(binary, upload, destination), check=True, capture_output=True, timeout=300)
    shrunk = probe(destination)
    if shrunk is None:
        raise RuntimeError("the shrunk clip could not be read")
    return destination, shrunk, True


def reject_reason(analysis: dict) -> str | None:
    """Videos the app cannot give a useful analysis for (camera set-up, framing, no swing)."""

    signals = (analysis.get("camera_check") or {}).get("signals") or {}
    status = lambda name: (signals.get(name) or {}).get("status")  # noqa: E731
    if status("orientation_width") == "INVALID" or status("hands_in_front") == "INVALID":
        return "camera_angle"
    if status("full_body") == "INVALID":
        return "framing"
    if status("golfer_size") == "INVALID":
        return "too_far"
    found = sum(1 for p in analysis.get("phases", []) if p.get("frame_index") is not None)
    shown = sum(1 for p in analysis.get("phases", []) for c in p.get("checks", []) if c.get("guided"))
    if found < 4 or shown == 0:
        frames = {p.get("phase"): p.get("frame_index") for p in analysis.get("phases", [])}
        # The swing itself was found (Top and Impact) but the clip starts after Address, so nothing
        # can be measured against the set-up: say exactly that instead of "no swing".
        if frames.get("address") is None and frames.get("top") is not None and frames.get("impact") is not None:
            return "no_setup"
        return "no_swing"
    return None


def run_analysis(upload: Path, work: Path, *, on_prepared: Callable[[str | None], None] | None = None,
                 timeout_s: float = 600.0) -> RunResult:
    """Analyse ``upload`` inside ``work`` (created if needed). Never raises for a bad video."""

    from .swing_window import NO_SWING_FOUND, cut_window, locate_swing

    # Resolve once: the engine subprocess runs with the repository as its working
    # directory, so a relative work path would write its bundle somewhere else.
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    timings: dict = {}
    started = time.monotonic()
    progress.record(work, "prepare", progress.ACTIVE, detail="upload received")
    progress.record(work, "probe", progress.ACTIVE, internal=True)
    mark = time.monotonic()
    info = probe(upload)
    timings["probe_s"] = round(time.monotonic() - mark, 2)
    if info is None:
        return _result(False, "unreadable", work=work, timings=timings, started=started)
    progress.record(work, "probe", progress.DONE, detail="clip decoded", internal=True)
    if info["duration_s"] < MIN_SECONDS:
        return _result(False, "too_short", work=work, video_info=info, timings=timings, started=started)
    if info["duration_s"] > MAX_SECONDS:
        return _result(False, "too_long", work=work, video_info=info, timings=timings, started=started)
    original_long_side = max(info["width"], info["height"])
    if original_long_side > MAX_LONG_SIDE:
        mark = time.monotonic()
        try:
            upload, info, _ = shrink_if_large(upload, work, info)
        except Exception as error:
            timings["shrink_s"] = round(time.monotonic() - mark, 2)
            return _result(False, "failed", f"shrink failed: {type(error).__name__}", work=work, video_info=info,
                           timings=timings, started=started)
        timings["shrink_s"] = round(time.monotonic() - mark, 2)
        info = {**info, "shrunk_from_long_side": original_long_side}
    progress.record(work, "prepare", progress.DONE, detail="clip validated")
    progress.record(work, "moments", progress.ACTIVE)
    progress.record(work, "window", progress.ACTIVE, internal=True)
    mark = time.monotonic()
    try:
        from .engine_profile import profile as _profile

        # Only the candidate profile (slowmo) changes this call; every other profile calls it exactly as before.
        if _profile().on("slowmo"):
            decision, signal = locate_swing(upload, slowmo=True)
        else:
            decision, signal = locate_swing(upload)
    except Exception as error:  # the window stage is a real failure boundary
        timings["locate_swing_s"] = round(time.monotonic() - mark, 2)
        progress.record(work, "window", progress.ACTIVE, detail=f"raised {type(error).__name__}", internal=True)
        return _result(False, "failed", f"swing window failed: {type(error).__name__}", work=work,
                       video_info=info, timings=timings, started=started)
    timings["locate_swing_s"] = round(time.monotonic() - mark, 2)
    progress.record(work, "window", progress.DONE, detail=decision.status.lower(), internal=True)
    window = decision.to_dict()
    inconclusive = decision.status == NO_SWING_FOUND or (decision.confidence == "LOW" and not decision.trimmed)
    if inconclusive and info["duration_s"] <= DEFER_WINDOW_MAX_S:
        # Not a rejection: the engine sees the whole (short) clip and its stricter gates decide.
        window["deferred_to_engine"] = True
        window["deferred_reason"] = decision.reason
        decision.trimmed = False
    elif decision.status == NO_SWING_FOUND:
        return _result(False, "no_swing", decision.reason, work=work, window=window, video_info=info,
                       timings=timings, started=started)
    elif decision.confidence == "LOW" and not decision.trimmed:
        return _result(False, "no_clear_swing", decision.reason, work=work, window=window, video_info=info,
                       timings=timings, started=started)
    video = upload
    time_scale = float(getattr(decision, "time_scale", 1.0))
    if decision.trimmed:
        fps = float(signal["fps"])
        video = work / "window.mp4"
        progress.record(work, "cut", progress.ACTIVE, internal=True)
        mark = time.monotonic()
        try:
            if time_scale != 1.0:
                # Candidate profile only (slowmo): a 4x+ slow-motion window keeps every few frames, so
                # pose has fewer to fit; the rest of the engine sees a less slowed clip.
                from .swing_window import slowmo_step

                step = slowmo_step(time_scale)
                cut_window(upload, video, int(round(decision.start_s * fps)), int(round(decision.end_s * fps)),
                           step=step, fps=fps)
                time_scale /= step
                window["frame_step"] = step
            else:
                cut_window(upload, video, int(round(decision.start_s * fps)), int(round(decision.end_s * fps)))
        except Exception as error:
            timings["cut_window_s"] = round(time.monotonic() - mark, 2)
            return _result(False, "failed", f"window cut failed: {type(error).__name__}", work=work,
                           window=window, video_info=info, timings=timings, started=started)
        timings["cut_window_s"] = round(time.monotonic() - mark, 2)
        progress.record(work, "cut", progress.DONE, internal=True)
    notice = None
    if decision.status == "MULTIPLE_SWINGS" and not window.get("deferred_to_engine"):
        notice = f"We found {len(decision.candidates)} swings in this video and analysed the clearest one."
    (work / "swing_window.json").write_text(json.dumps({"window": window}, default=float), encoding="utf-8")
    # "moments" stays active: the engine still has to fit the pose and estimate
    # the six key moments on the (possibly trimmed) clip.
    progress.record(work, "moments", progress.ACTIVE,
                    detail=f"window {decision.status.lower()}, confidence {decision.confidence.lower()}")
    if on_prepared:
        on_prepared(notice)

    bundle = work / "bundle"
    command = [sys.executable, str(BUILD_SCRIPT), str(video), "--out", str(bundle),
               "--records", str(bundle / "pose_records.pkl"), "--progress", str(work)]
    if time_scale != 1.0:
        command += ["--time-scale", f"{time_scale:g}"]
    mark = time.monotonic()
    try:
        process = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=timeout_s,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        timings["engine_process_s"] = round(time.monotonic() - mark, 1)
        return _result(False, "timeout", f"build_analysis exceeded {timeout_s:.0f} s", work=work, window=window,
                       video_info=info, timings=timings, started=started)
    timings["engine_process_s"] = round(time.monotonic() - mark, 1)
    (work / "build.log").write_text(process.stdout + "\n--- stderr ---\n" + process.stderr, encoding="utf-8")
    analysis_path = bundle / "analysis.json"
    if process.returncode != 0 or not analysis_path.is_file():
        # The exit code first: a process killed for memory (-9) leaves only library warnings in stderr.
        tail = [line for line in process.stderr.strip().splitlines()
                if not any(noise in line for noise in ("inference_feedback_manager", "CleanUnusedInitializers"))]
        return _result(False, "failed", f"engine exit {process.returncode}: " + "\n".join(tail[-3:]), work=work,
                       window=window, video_info=info, timings=timings, started=started)
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return _result(False, "failed", f"analysis.json unreadable: {type(error).__name__}", work=work,
                       window=window, video_info=info, timings=timings, started=started)
    rejection = reject_reason(analysis)
    if not rejection and (analysis.get("time_scale") or {}).get("notice"):
        notice = analysis["time_scale"]["notice"]           # candidate profile: a slow-motion clip
    if rejection:
        return _result(False, rejection, work=work, window=window, video_info=info, timings=timings,
                       started=started, analysis=analysis)
    return _result(True, bundle=bundle, notice=notice, work=work, window=window, video_info=info,
                   timings=timings, started=started, analysis=analysis)


def _result(ok: bool, error: str | None = None, detail: str = "", *, work: Path, bundle: Path | None = None,
            notice: str | None = None, window: dict | None = None, video_info: dict | None = None,
            timings: dict | None = None, started: float | None = None,
            analysis: dict | None = None) -> RunResult:
    """Build the result and its diagnostics; every measured timing is retained."""

    timings = dict(timings or {})
    if started is not None:
        timings["total_s"] = round(time.monotonic() - started, 2)
    try:
        record = diag.build(ok=ok, error=error, detail=detail, window=window, video_info=video_info,
                            timings=timings, work=work, bundle=bundle, analysis=analysis,
                            analysis_version=ANALYSIS_VERSION, stage_rows=progress.stage_rows(work),
                            internal_rows=progress.internal_rows(work))
    except Exception as failure:  # diagnostics must never change the analysis outcome
        record = {"schema": diag.SCHEMA, "outcome": "ok" if ok else "failed",
                  "error": f"diagnostics failed: {type(failure).__name__}"}
    return RunResult(ok, error=error, detail=detail, bundle=bundle, notice=notice, window=window or {},
                     video_info=video_info or {}, timings=timings, diagnostics=record)


__all__ = ["ANALYSIS_VERSION", "ERRORS", "MAX_SECONDS", "MIN_SECONDS", "RunResult", "STAGES", "error_text", "probe",
           "reject_reason", "run_analysis", "stage_states"]
