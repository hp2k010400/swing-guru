"""Run the whole engine with different pose providers and compare end to end.

    .venv\\Scripts\\python.exe scripts\\research\\engine_compare.py VIDEO

For each provider: pose -> automatic moments -> guided checks (measurement
validity) -> club tracking, on the same clip. Prints and saves a table.
"""

from __future__ import annotations

import json
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402

from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.club import ClassicalClubTracker  # noqa: E402
from swingguru.club.metrics import phase_shaft_metrics  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import PHASE_LABELS, ManualPhaseProvider, Phase  # noqa: E402
from swingguru.pipeline import PipelineConfig, process_video  # noqa: E402
from swingguru.pose_rtm import RTMPoseAdapter  # noqa: E402
from swingguru.ui.timeline import build_timeline  # noqa: E402

KEY = (Phase.ADDRESS, Phase.TAKEAWAY, Phase.MID_BACK, Phase.TOP, Phase.DOWNSWING, Phase.IMPACT)
TRUTH_ANGLE = {"Address": -56.0, "Takeaway": -162.0, "Mid-Back": 140.0, "Top": 105.0, "Mid-Down": 124.0, "Impact": -70.0}
HUMAN_MOMENTS = {"Address": 108, "Takeaway": 116, "Mid-Back": 120, "Top": 127, "Mid-Down": 132, "Impact": 134}


def run(video: Path, label: str, provider) -> dict:
    cache = ROOT / "var" / "research" / f"records_{label}.pkl"
    started = time.monotonic()
    if cache.exists() and provider is not None:
        records = pickle.loads(cache.read_bytes())
    elif provider is None:
        records = pickle.loads((ROOT / "var" / "real_probe" / "records.pkl").read_bytes())
    else:
        result = process_video(
            video,
            ROOT / "var" / "research" / f"run_{label}",
            config=PipelineConfig(measurement=MeasurementConfig(), write_annotated=False),
            pose_provider=provider,
            phase_provider=ManualPhaseProvider(frames={}, derive_reference_from_address=False),
        )
        records = list(result.records)
        cache.write_bytes(pickle.dumps(records))
    pose_seconds = time.monotonic() - started

    moments = estimate_moments(records)
    frames_by_phase = moments.frames()
    timeline = build_timeline(
        records, moments, fps=30.0, measurement_config=MeasurementConfig(),
        reference_frame=frames_by_phase.get(Phase.ADDRESS),
    )
    checks = timeline.checks
    capture = cv2.VideoCapture(str(video))
    frames = []
    while len(frames) < len(records):
        ok, frame = capture.read()
        if not ok:
            break
        frames.append(frame)
    track = ClassicalClubTracker().track_sequence(frames, records)
    metrics = phase_shaft_metrics(track, moments, fps=30.0)

    rows = {}
    for phase in KEY:
        name = PHASE_LABELS[phase]
        frame = frames_by_phase.get(phase)
        shaft = metrics["per_phase"][name]
        raw = shaft.get("raw_angle_deg")
        error = None if raw is None else round(abs(((raw - TRUTH_ANGLE[name]) + 180) % 360 - 180), 1)
        rows[name] = {
            "moment_frame": frame,
            "moment_error": None if frame is None else frame - HUMAN_MOMENTS[name],
            "club_level": shaft.get("level"),
            "club_angle": raw,
            "club_error_deg": error,
            "club_reported": shaft.get("status") == "reported",
        }
    window = [track.frames[i] for i in range(108, 135) if i < len(track.frames)]
    return {
        "pose_seconds": round(pose_seconds, 1),
        "moments": rows,
        "checks_valid": f"{sum(1 for c in checks if c.valid)}/{len(checks)}",
        "club_medium_plus_address_to_impact": f"{sum(1 for s in window if s.level in ('HIGH', 'MEDIUM') and not s.interpolated)}/{len(window)}",
        "top_comparison": metrics["top_comparison"],
    }


def main() -> int:
    video = Path(sys.argv[1])
    providers = {
        "mediapipe": None,
        "rtmw-balanced": RTMPoseAdapter("wholebody", "balanced"),
        "rtmw-lightweight": RTMPoseAdapter("wholebody", "lightweight"),
    }
    table = {}
    for label, provider in providers.items():
        table[label] = run(video, label, provider)
        print(f"== {label}  pose {table[label]['pose_seconds']}s  checks valid {table[label]['checks_valid']}  "
              f"club MEDIUM+ {table[label]['club_medium_plus_address_to_impact']}")
        for name, row in table[label]["moments"].items():
            print(f"   {name:9} moment={row['moment_frame']} (err {row['moment_error']})  club={row['club_level']} "
                  f"{row['club_angle']} err={row['club_error_deg']} reported={row['club_reported']}")
        top = table[label]["top_comparison"]
        print(f"   top: hand {top['hand_based_frame']} club {top['club_based'].get('frame')}")
    (ROOT / "var" / "research" / "engine_compare.json").write_text(json.dumps(table, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
