"""Structured, versioned run diagnostics for one analysis attempt.

The diagnostics object is additive on :class:`swingguru.analysis_runner.RunResult`
and is an internal/developer record: it is safe to log and to write to a batch
report, and it never contains secrets, signed URLs, raw video, full user paths
or personal data. Values the provider does not measure stay ``None`` with an
``unavailable`` reason - a pose score is never substituted for a person
detection score, and no timer, confidence or stage is invented.

Contract (schema ``swingguru.run_diagnostics/1``)::

    {
      "schema": "swingguru.run_diagnostics/1",
      "analysis_version": "engine-2026.09.30",
      "outcome": "ok" | "failed",
      "public_code": null | "no_swing",
      "failure": null | {"stage", "internal_stage", "category", "observed", "inferred_cause", "public_code"},
      "video": {"width", "height", "fps", "frames", "duration_s", "rotation_deg", "unavailable"},
      "signals": {"person_confidence", "pose_coverage", "phase_confidence", "club_coverage"},
      "stages": [{"id", "label", "state", "at_ms"}],
      "timings": {...real measurements only, omitted when unknown...}
    }

``stage`` is the public stage id; ``internal_stage`` is the real boundary the
run stopped in (``swingguru.progress.INTERNAL_STAGES``). Failure categories:
decode, window, person, pose, orientation, phases, club, camera, confidence,
insufficient_frames, framing, other.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import progress

SCHEMA = "swingguru.run_diagnostics/1"

CATEGORIES = ("decode", "window", "person", "pose", "orientation", "phases", "club", "camera",
              "confidence", "insufficient_frames", "framing", "other")

# Which public stage a failure at each category stops in.
CATEGORY_STAGE = {
    "decode": "prepare",
    "insufficient_frames": "prepare",
    "window": "moments",
    "person": "moments",
    "pose": "moments",
    "orientation": "positions",
    "camera": "positions",
    "framing": "positions",
    "phases": "positions",
    "club": "club",
    "confidence": "build",
    "other": "build",
}

# The real internal boundary a failure belongs to -> (category, public stage).
INTERNAL_FAILURE = {
    "probe": ("decode", "prepare"),
    "window": ("window", "moments"),
    "cut": ("window", "moments"),
    "pose": ("pose", "moments"),
    "phase_detection": ("phases", "positions"),
    "club_tracking": ("club", "club"),
    "camera": ("camera", "positions"),
    "render": ("other", "build"),
}

# The engine's phase set, so "found N of M" is a real count, not a guess.
EXPECTED_PHASES = ("address", "takeaway", "mid_back", "top", "downswing", "impact")
# Key landmarks whose retained model scores summarise usable pose coverage.
COVERAGE_LANDMARKS = ("left_shoulder", "right_shoulder", "left_hip", "right_hip", "left_wrist",
                      "right_wrist", "left_ankle", "right_ankle")
COVERAGE_THRESHOLD = 0.5
MIN_PHASES_FOR_SWING = 4

# Windows drive paths may contain spaces ("C:\Users\harry phillips\swing\a.mp4"),
# so they run to a sentence delimiter or to the next ``key=value`` field rather
# than stopping at the first space. Prose after a path can be captured too; the
# security property that matters is that no path fragment survives.
_PATH_STOP = r"\s+[A-Za-z_][A-Za-z0-9_.-]*\s*[:=]|[\r\n\"'|<>*?,;]|$"
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:(?!" + _PATH_STOP + r").)*")
_UNC_PATH = re.compile(r"\\\\[^\r\n\"'|<>*?,;]*")
# POSIX: two or more segments (spaces allowed after the first), or a known root.
_POSIX_PATH = re.compile(r"(?<![\w.])(?:"
                         r"/(?:[^\s\"'|<>*?,;/]+/)+[^\r\n\"'|<>*?,;]*"
                         r"|/(?:home|Users|tmp|var|private|mnt|opt|srv)/(?:(?!" + _PATH_STOP + r").)*)")
_BEARER = re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{6,}")
_SECRET = re.compile(r"(?i)\b(?:sig|signature|token|access[_-]?token|refresh[_-]?token|id[_-]?token|apikey|"
                     r"api[_-]?key|x-api-key|client[_-]?secret|password|passwd|secret|cookie|session)\b"
                     r"\s*[:=]\s*[^\s&;,\"']+")
_SECRET_QUERY = re.compile(r"[?&](?:sig|signature|token|apikey|api_key|key|expires|exp)=[^\s&\"']+", re.I)
_URL_WITH_QUERY = re.compile(r"https?://[^\s\"']+")


def _trim(match: re.Match) -> str:
    """Replace a matched path, keeping a single placeholder."""

    return "<path>" if match.group(0).strip() else match.group(0)


def sanitize(text: object, limit: int = 300, *, strip_paths: Iterable[str | Path] = ()) -> str:
    """Remove paths, URLs and credential-looking values from internal text.

    ``strip_paths`` removes exact known locations first (repo root, the run
    folder, the user home). That is the reliable way to redact a path that
    contains spaces; the regexes are the backstop for anything else. This never
    raises: an unmatchable value is simply truncated.
    """

    try:
        value = "" if text is None else str(text)
        for known in strip_paths:
            candidate = str(known).rstrip("\\/")
            if not candidate:
                continue
            # Redact the known root *and* whatever path continues after it, in
            # either separator style, so a spaced path never survives in part.
            for form in {candidate, candidate.replace("\\", "/")}:
                value = re.sub(re.escape(form) + r"[^\r\n\"'|<>*?,;]*", "<path>", value, flags=re.I)
        value = _BEARER.sub("Bearer <redacted>", value)
        value = _URL_WITH_QUERY.sub("<url>", value)
        value = _SECRET_QUERY.sub("&<redacted>", value)
        value = _WINDOWS_PATH.sub(_trim, value)
        value = _UNC_PATH.sub(_trim, value)
        value = _POSIX_PATH.sub(_trim, value)
        value = _SECRET.sub("<redacted>", value)
        value = " ".join(value.split())
        return value[:limit]
    except Exception:  # pragma: no cover - sanitising must never raise
        return "<unavailable>"


def unavailable(reason: str) -> dict:
    return {"value": None, "unavailable": reason}


# ---------------------------------------------------------------- video / pose

def video_block(info: Mapping[str, Any] | None) -> dict:
    """Geometry a probe really measured; ``None`` plus a reason when it did not."""

    if not info:
        return {"width": None, "height": None, "fps": None, "frames": None, "duration_s": None,
                "rotation_deg": None, "unavailable": "the clip could not be probed"}
    return {"width": info.get("width"), "height": info.get("height"), "fps": info.get("fps"),
            "frames": info.get("frames"), "duration_s": info.get("duration_s"),
            "rotation_deg": None,
            "unavailable": None if info.get("fps") else "the container reported no frame rate"}


def pose_coverage(records_path: Path | str | None) -> dict:
    """Usable pose coverage from the retained frame records.

    This is *pose* coverage (retained model scores), never person-detection
    confidence: the provider keeps the per-keypoint score, not the detector's
    person box score.
    """

    def empty(reason: str) -> dict:
        """The same field set as a measured result, with nulls and a reason."""

        return {"frames": None, "detected_frame_coverage": None, "reliable_landmark_coverage": None,
                "detected_frames": None, "threshold": COVERAGE_THRESHOLD, "landmark_presence": {},
                "landmark_mean_score": {}, "unavailable": reason}

    if not records_path:
        return empty("no pose records were written for this run")
    path = Path(records_path)
    if not path.is_file():
        return empty("no pose records were written for this run")
    try:
        # The cache is written keyed (identity + records); older bundles can hold
        # a bare list. ``load_records_any`` understands both.
        from .pose_cache import load_records_any

        records = load_records_any(path)
    except Exception as error:  # pragma: no cover - corrupt cache is reported, never fatal
        return empty(f"pose records could not be read ({type(error).__name__})")
    if not isinstance(records, list) or not records:
        return empty("the pose records were empty")

    frames = len(records)
    detected = 0
    reliable_frames = 0
    landmark_hits = {name: 0 for name in COVERAGE_LANDMARKS}
    landmark_scores = {name: [] for name in COVERAGE_LANDMARKS}
    for record in records:
        points = getattr(record, "points", None)
        landmarks = getattr(points, "landmarks", None) or {}
        observation = getattr(record, "observation", None)
        frame_detected = bool(getattr(record, "pose_detected", False) or
                              (observation is not None and getattr(observation, "detected", False)))
        if frame_detected:
            detected += 1
        reliable_here = 0
        for name in COVERAGE_LANDMARKS:
            landmark = landmarks.get(name)
            if landmark is None:
                continue
            score = min(float(getattr(landmark, "visibility", 0.0) or 0.0),
                        float(getattr(landmark, "presence", 0.0) or 0.0))
            landmark_scores[name].append(score)
            if score >= COVERAGE_THRESHOLD:
                landmark_hits[name] += 1
                reliable_here += 1
        if reliable_here == len(COVERAGE_LANDMARKS):
            reliable_frames += 1
    return {
        "frames": frames,
        # Two different ideas, never conflated: a frame can hold a pose while
        # individual keypoints are still below the reliability threshold.
        "detected_frame_coverage": round(detected / frames, 4),
        "reliable_landmark_coverage": round(reliable_frames / frames, 4),
        "detected_frames": detected,
        "threshold": COVERAGE_THRESHOLD,
        "landmark_presence": {name: round(hits / frames, 4) for name, hits in landmark_hits.items()},
        "landmark_mean_score": {name: (round(sum(scores) / len(scores), 4) if scores else None)
                                for name, scores in landmark_scores.items()},
        "unavailable": None if detected else "no frame retained a pose",
    }


def setup_evidence(records_path: Path | str | None, analysis: Mapping[str, Any] | None) -> dict:
    """Why Address was (or was not) found: footage before Top and the stillest set-up window.

    Measured with the engine's own Address test (``auto_moments``): a set-up needs a window of
    ``stability_window_s`` with every hand speed <= ``stability_speed`` torso lengths/s. This only
    *reports* the closest the clip came; it never changes a moment or a threshold.
    """

    result: dict = {"pre_top_s": None, "quietest_window_speed": None, "threshold": None, "window_s": None,
                    "still_window_found": None, "unavailable": None}
    frames = {p.get("phase"): p.get("frame_index") for p in (analysis or {}).get("phases", []) or []}
    top = frames.get("top") if frames.get("top") is not None else frames.get("impact")
    if not records_path or not Path(records_path).is_file() or top is None:
        result["unavailable"] = "no pose records or no Top/Impact moment to measure the set-up against"
        return result
    try:
        from .auto_moments import MomentConfig, _smoothed, _speeds, _window_end, build_motion_series
        from .pose_cache import load_records_any

        config = MomentConfig()
        series = build_motion_series(load_records_any(Path(records_path)), config=config)
        smoothed = _smoothed(series, config)
        speeds = _speeds(series, smoothed)
    except Exception as error:  # pragma: no cover - diagnostics never break a run
        result["unavailable"] = f"set-up evidence could not be computed ({type(error).__name__})"
        return result
    before = [i for i, frame in enumerate(series.frames) if frame < int(top)]
    if not before:
        result.update(pre_top_s=0.0, still_window_found=False, threshold=config.stability_speed,
                      window_s=config.stability_window_s)
        return result
    end = before[-1] + 1
    quietest = None
    for index in before:
        window_end = _window_end(series, index, config.stability_window_s, end)
        window = speeds[index + 1: window_end + 1]
        if window_end <= index or any(speed is None for speed in window):
            continue
        peak = max(window)
        quietest = peak if quietest is None else min(quietest, peak)
    result.update(
        pre_top_s=round(series.times_s[before[-1]] - series.times_s[0], 3),
        quietest_window_speed=None if quietest is None else round(float(quietest), 3),
        threshold=config.stability_speed, window_s=config.stability_window_s,
        still_window_found=quietest is not None and quietest <= config.stability_speed,
    )
    if quietest is None:
        result["unavailable"] = "no complete window before Top (gaps in the hand track)"
    return result


def person_confidence(analysis: Mapping[str, Any] | None) -> dict:
    """Person-detection confidence is not retained by the pose provider."""

    del analysis
    return unavailable("the pose provider retains keypoint scores only, not the person-detector score")


# ---------------------------------------------------------------- analysis views

def phase_confidence(analysis: Mapping[str, Any] | None) -> dict:
    if not analysis:
        return unavailable("no analysis.json was produced")
    phases = analysis.get("phases") or []
    located = [p for p in phases if p.get("frame_index") is not None]
    guided = sum(1 for p in phases for c in (p.get("checks") or []) if c.get("guided"))
    levels: dict[str, int] = {}
    for phase in phases:
        for check in phase.get("checks") or []:
            level = check.get("level")
            if level:
                levels[level] = levels.get(level, 0) + 1
    retained = {p.get("phase"): p.get("confidence") for p in phases if p.get("confidence") is not None}
    return {"located": len(located), "expected": max(len(phases), len(EXPECTED_PHASES)),
            "guided_checks": guided,
            "sources": {p.get("phase"): p.get("source") for p in phases},
            # The engine does not retain the moment estimator's per-phase
            # confidence in analysis.json, so it is reported unavailable rather
            # than inferred from the checks.
            "moment_confidence": ({"value": retained, "unavailable": None} if retained else
                                  unavailable("the engine does not retain per-phase moment confidence in analysis.json")),
            "check_confidence_levels": levels,
            "unavailable": None if phases else "the engine produced no phase rows"}


def club_coverage(analysis: Mapping[str, Any] | None) -> dict:
    if not analysis:
        return unavailable("no analysis.json was produced")
    phases = analysis.get("phases") or []
    shaft_phases, sources = [], {}
    for phase in phases:
        for check in phase.get("checks") or []:
            if check.get("kind") == "shaft":
                shaft_phases.append(phase.get("phase"))
                # Never invent a source: a build without the field is unknown.
                source = check.get("source")
                sources[phase.get("phase")] = source if isinstance(source, str) else None
                break
    hidden_shaft = [h.get("phase") for h in (analysis.get("hidden") or []) if h.get("kind") == "shaft"]
    engine = analysis.get("engine") or {}
    fallback = engine.get("club_fallback") or []
    counters = engine.get("counters") or {}
    return {
        "phases_with_club": len(shaft_phases),
        "phases_expected": max(len(phases), len(EXPECTED_PHASES)),
        "sources": sources,
        "source_unknown_phases": [phase for phase, source in sources.items() if source is None],
        "withheld_phases": hidden_shaft,
        "fallback_attempts": len(fallback) if isinstance(fallback, list) else None,
        "fallback_frames_run": counters.get("owlv2_frames_run"),
        "fallback_frames_cached": counters.get("owlv2_frames_cached"),
        # The optional OWLv2 model could not be loaded: club checks it would have rescued were withheld.
        "fallback_model_unavailable": bool((engine.get("timings") or {}).get("owlv2_unavailable")),
        "tracker": engine.get("club_tracker"),
        "unavailable": None if phases else "the engine produced no phase rows",
    }


# ---------------------------------------------------------------- failure

def _invalid_signals(analysis: Mapping[str, Any] | None, names: Iterable[str]) -> list[str]:
    signals = ((analysis or {}).get("camera_check") or {}).get("signals") or {}
    return [name for name in names if (signals.get(name) or {}).get("status") == "INVALID"]


def _first_unfinished_stage(stage_rows: list[dict] | None) -> str:
    for row in stage_rows or []:
        if row.get("state") != progress.DONE:
            return row.get("id") or "build"
    return "build"


def _infer_cause(text: str) -> str | None:
    """A conservative inferred cause - only when the engine said so explicitly."""

    lowered = text.lower()
    if "could not open" in lowered or "videoopenerror" in lowered:
        return "the decoder could not open the clip"
    if "no person detected" in lowered:
        return "the pose model found no person in the sampled frames"
    if "wall-clock" in lowered or "timed out" in lowered:
        return "the engine exceeded the analysis time limit"
    return None


def _stopped_internal(internal_rows: list[dict] | None) -> dict | None:
    """The internal substage the run stopped in, from real events only."""

    rows = [row for row in (internal_rows or []) if row.get("state") in ("active", "done")]
    if not rows:
        return None
    active = [row for row in rows if row.get("state") == "active"]
    return (active or rows)[-1]


def failure_block(*, error: str | None, detail: str = "", analysis: Mapping[str, Any] | None = None,
                  window: Mapping[str, Any] | None = None, pose: Mapping[str, Any] | None = None,
                  stage_rows: list[dict] | None = None,
                  internal_rows: list[dict] | None = None) -> dict | None:
    """The observed failure, its category, and only evidenced inferred causes."""

    if not error:
        return None
    detail = sanitize(detail, 240)
    window = window or {}
    internal = _stopped_internal(internal_rows)
    located = (phase_confidence(analysis).get("located") if analysis else None)
    guided = (phase_confidence(analysis).get("guided_checks") if analysis else None)
    stage = _first_unfinished_stage(stage_rows)
    category, observed = "other", detail or "the analysis did not complete"
    # How each conclusion is known (see DIAGNOSTICS.md): "confirmed" = an explicit rejection rule or a
    # boundary that was really active when the run stopped; "inferred" = deduced from where the run
    # stopped between boundaries; "unknown" = no evidence (no progress rows at all).
    stage_basis = "confirmed" if stage_rows else "unknown"
    category_basis = "confirmed"

    if error == "unreadable":
        stage, category, observed = "prepare", "decode", "the clip could not be opened or decoded"
    elif error == "too_short":
        stage, category, observed = "prepare", "insufficient_frames", "the clip is shorter than the minimum analysis length"
    elif error == "too_long":
        stage, category, observed = "prepare", "other", "the clip is longer than the maximum analysis length"
    elif error == "too_large":
        stage, category, observed = "prepare", "other", "the upload is larger than the accepted size"
    elif error == "upload_missing":
        stage, category, observed = "prepare", "other", "the uploaded object was not found in storage"
    elif error == "no_clear_swing":
        stage, category = "moments", "window"
        observed = sanitize(window.get("reason") or "no unambiguous swing window was found")
    elif error == "no_swing":
        stage = "positions"
        if analysis is None:
            # The swing window was never found: the run stopped before the engine.
            stage, category = "moments", "window"
            observed = sanitize(window.get("reason") or "no swing-shaped movement was found")
        elif pose is not None and pose.get("detected_frames") == 0:
            # Zero *retained* poses is all we know: the provider's detector score
            # is not kept, so this is not proof that no person was present.
            category = "pose"
            observed = "no usable pose was retained for any frame"
        elif located is not None and located < MIN_PHASES_FOR_SWING:
            category = "phases"
            observed = f"only {located} of {len(EXPECTED_PHASES)} phases were located"
        else:
            category = "confidence"
            observed = f"{guided or 0} guided checkpoint(s) were produced"
    elif error == "no_setup":
        stage, category = "positions", "phases"
        observed = "Top and Impact were located but Address was not: the clip starts after the set-up"
    elif error == "camera_angle":
        stage, category = "positions", "camera"
        invalid = _invalid_signals(analysis, ("orientation_width", "hands_in_front"))
        observed = "camera check rejected: " + (", ".join(invalid) or "down-the-line signals")
    elif error == "framing":
        stage, category = "positions", "framing"
        invalid = _invalid_signals(analysis, ("full_body", "golfer_size"))
        observed = "framing check rejected: " + (", ".join(invalid) or "body not fully in frame")
    elif error == "too_far":
        stage, category = "positions", "framing"
        invalid = _invalid_signals(analysis, ("golfer_size",))
        observed = "framing check rejected: " + (", ".join(invalid) or "golfer too small in frame")
    elif error == "timeout":
        category, category_basis = "other", "unknown"
        observed = detail or "the engine exceeded the analysis time limit"
    elif error == "failed":
        if internal is not None and internal.get("state") == "active" and internal["id"] in INTERNAL_FAILURE:
            category, stage = INTERNAL_FAILURE[internal["id"]]
            stage_basis = "confirmed"
            observed = detail or f"the engine stopped during the {internal['id']} stage"
        else:
            # Nothing was active: the category is only a deduction from the stopping position.
            category_basis = "inferred" if stage_rows else "unknown"
            category = {"prepare": "decode", "moments": "pose", "club": "club", "positions": "phases",
                        "build": "other"}.get(stage, "other")
            # Nothing was in progress: the run stopped *between* boundaries, so
            # the last completed internal stage is named rather than invented.
            where = f" after the {internal['id']} stage" if internal else f" during the {stage} stage"
            observed = detail or f"the engine stopped{where}"

    if internal is not None and error == "timeout" and internal.get("state") == "active":
        # Still an unknown cause ("other"): all we can say is where it stopped.
        observed = (detail or "the engine exceeded the analysis time limit") + \
                   f" (stopped in the {internal['id']} stage)"

    if error in ("unreadable", "too_short", "too_long", "too_large", "upload_missing", "no_clear_swing",
                 "no_swing", "no_setup", "camera_angle", "framing", "too_far"):
        stage_basis = "confirmed"  # an explicit rule rejected the clip at this stage
    cause = _infer_cause(detail)
    return {"stage": stage, "internal_stage": None if internal is None else internal["id"],
            "category": category, "observed": observed,
            "inferred_cause": cause, "public_code": error,
            "evidence": {"stage": stage_basis, "category": category_basis,
                         "cause": "engine_message" if cause else "none"}}


# ---------------------------------------------------------------- assembly

def build(*, ok: bool, error: str | None, detail: str = "", window: Mapping[str, Any] | None = None,
          video_info: Mapping[str, Any] | None = None, timings: Mapping[str, Any] | None = None,
          work: Path | str | None = None, bundle: Path | str | None = None,
          analysis: Mapping[str, Any] | None = None, analysis_version: str = "",
          stage_rows: list[dict] | None = None, internal_rows: list[dict] | None = None,
          extra: Mapping[str, Any] | None = None) -> dict:
    """Assemble the diagnostics object for one finished attempt."""

    records = None if work is None else Path(work) / "bundle" / "pose_records.pkl"
    pose = pose_coverage(records)
    timeline = None if work is None else progress.timeline(work)
    if stage_rows is None and timeline:
        stage_rows = timeline.get("stages")
    if internal_rows is None and timeline:
        internal_rows = timeline.get("internal")
    engine_timings = ((analysis or {}).get("engine") or {}).get("timings") if analysis else None
    block = {
        "schema": SCHEMA,
        "analysis_version": analysis_version or None,
        "outcome": "ok" if ok else "failed",
        "public_code": None if ok else (error or "failed"),
        "failure": failure_block(error=error, detail=detail, analysis=analysis, window=window,
                                 pose=pose, stage_rows=stage_rows, internal_rows=internal_rows) if not ok else None,
        "video": video_block(video_info),
        "signals": {
            "person_confidence": person_confidence(analysis),
            "pose_coverage": pose,
            "phase_confidence": phase_confidence(analysis),
            "club_coverage": club_coverage(analysis),
            # Only when Address is the question: a rejected swing, or a run without an Address moment.
            "setup": setup_evidence(records, analysis) if analysis is not None and (not ok or not any(
                p.get("phase") == "address" and p.get("frame_index") is not None
                for p in analysis.get("phases", []) or [])) else None,
        },
        "stages": [{"id": row.get("id"), "label": row.get("label"), "state": row.get("state"),
                    "at_ms": row.get("at_ms")} for row in (stage_rows or [])],
        "internal_stages": [{"id": row.get("id"), "label": row.get("label"), "state": row.get("state"),
                             "at_ms": row.get("at_ms")} for row in (internal_rows or [])],
        "cache": _cache_block(analysis),
        "timings": _timings_block(timings, engine_timings, work),
    }
    if extra:
        block.update(extra)
    return block


def _cache_block(analysis: Mapping[str, Any] | None) -> dict:
    engine = (analysis or {}).get("engine") or {}
    pose_cache = engine.get("pose_cache") or {}
    windows = engine.get("club_windows") or {}
    return {
        "pose_cache": pose_cache.get("status"),
        "club_windows": windows.get("mode"),
        "club_candidate_cache": (windows.get("candidate_cache") or {}).get("hits") if windows.get("candidate_cache") else None,
    }


def _timings_block(timings: Mapping[str, Any] | None, engine: Mapping[str, Any] | None,
                   work: Path | str | None) -> dict:
    out = dict(timings or {})
    if engine:
        out["engine"] = dict(engine)
    if work is not None:
        durations = progress.stage_durations_ms(work)
        if durations:
            # Real start->done durations; a cached/skipped stage stays null.
            out["stage_durations_ms"] = durations
    return out


def environment_note() -> str:
    """One line for batch reports: where the numbers came from."""

    return f"python {sys.version.split()[0]}"


__all__ = ["CATEGORIES", "CATEGORY_STAGE", "COVERAGE_LANDMARKS", "COVERAGE_THRESHOLD", "EXPECTED_PHASES",
           "INTERNAL_FAILURE", "MIN_PHASES_FOR_SWING", "SCHEMA", "build", "club_coverage", "environment_note",
           "failure_block", "person_confidence", "phase_confidence", "pose_coverage", "sanitize", "unavailable",
           "video_block"]
