"""Load the guided player on its own, outside Streamlit, for fast visual checks.

Builds a payload from the deterministic guided fixture (or a supplied JSON
payload), writes a harness page next to a copy of the player assets, opens it
in headless Edge, steps onto a check and reports computed styles plus a
screenshot. Used to debug overlay rendering without a full analysis run.

    .venv\\Scripts\\python.exe scripts\\player_harness.py --out var\\qa\\harness
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402

import qa_ui_screenshots as ui  # noqa: E402
from swingguru.auto_moments import estimate_moments  # noqa: E402
from swingguru.measurements import MeasurementConfig  # noqa: E402
from swingguru.phases import Phase  # noqa: E402
from swingguru.pipeline import build_record  # noqa: E402
from swingguru.synthetic import joints_to_observation  # noqa: E402
from swingguru.ui.guided_fixture import GuidedSwingClip, guided_joints, make_guided_clip  # noqa: E402
from swingguru.ui.timeline import build_timeline, timeline_payload  # noqa: E402

PROBE_JS = """
(() => {
  const api = window.__swingPlayer;
  const overlay = document.getElementById('sg-overlay');
  const pick = (selector) => Array.from(overlay.querySelectorAll(selector)).map(node => ({
    tag: node.tagName, cls: node.getAttribute('class'),
    opacity: getComputedStyle(node).opacity,
    stroke: node.getAttribute('stroke'), fill: node.getAttribute('fill')
  }));
  return {state: api.state, check: api.checkId, overlayClass: overlay.getAttribute('class'),
          overlayOpacity: getComputedStyle(overlay).opacity,
          nodes: pick('path, circle, rect, text, g')};
})()
"""


def build_payload(work: Path) -> dict:
    clip = GuidedSwingClip(width=360, height=640)
    video = make_guided_clip(work / "fixture.mp4", clip=clip, fourcc="avc1")
    records = []
    for index in range(clip.frame_count):
        records.append(
            build_record(
                index,
                index * 1000.0 / clip.fps,
                np.zeros((clip.height, clip.width, 3), dtype=np.uint8),
                joints_to_observation(guided_joints(index, clip=clip)),
                config=MeasurementConfig(),
            )
        )
    moments = estimate_moments(records)
    timeline = build_timeline(
        records,
        moments,
        fps=clip.fps,
        measurement_config=MeasurementConfig(),
        reference_frame=moments.get(Phase.ADDRESS).frame_index,
    )
    payload = timeline_payload(
        timeline, run_key="harness", video_src=video.name, hold_seconds=1.8
    )
    payload["width"], payload["height"] = clip.width, clip.height
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "harness"))
    parser.add_argument("--width", type=int, default=420)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--steps", type=int, default=1, help="checks to step through")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="sg-harness-"))
    for item in (ROOT / "src" / "swingguru" / "ui" / "player_assets").iterdir():
        if item.is_file():
            shutil.copy(item, work / item.name)
    payload = build_payload(work)
    page = (work / "index.html").read_text(encoding="utf-8").replace(
        '<script src="./player.js"></script>',
        '<script src="./player.js"></script>\n<script>window.postMessage({type: "streamlit:render", args: {data: '
        + json.dumps(payload)
        + "}}, '*');</script>",
    )
    (work / "harness.html").write_text(page, encoding="utf-8")

    port = ui.free_port()
    process = ui.launch_browser(
        ui.find_browser(),
        port,
        Path(tempfile.mkdtemp(prefix="sg-harness-profile-")),
        extra_args=["--autoplay-policy=no-user-gesture-required", "--allow-file-access-from-files"],
    )
    try:
        ui.wait_for_devtools(port)
        client = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.viewport(args.width, args.height)
        client.send("Page.navigate", {"url": (work / "harness.html").as_uri()})
        time.sleep(2.5)
        for step in range(args.steps):
            client.evaluate("document.getElementById('sg-next').click()")
            time.sleep(1.2)
            probe = client.evaluate(PROBE_JS)
            (out / f"probe-{step}.json").write_text(json.dumps(probe, indent=2))
            client.screenshot(out / f"check-{step}.png", max_height=args.height)
            print(json.dumps({k: probe[k] for k in ("state", "check", "overlayClass", "overlayOpacity")}))
            for node in probe["nodes"]:
                if node["cls"]:
                    print("  ", node)
        client.close()
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
