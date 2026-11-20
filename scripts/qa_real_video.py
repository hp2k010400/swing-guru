"""End-to-end browser check of guided playback on a real uploaded swing video.

Uploads VIDEO through the page's own file input (as a person would), presses
Analyse swing, waits for MediaPipe and the automatic moments, presses Play and
watches the player until the walkthrough ends, then verifies:

* every moment held, in order, with the card text and geometry recorded;
* checks of one moment held at one video time with **no seek** between them;
* the video never jumped backwards while playing into a hold, and every hold
  needed at most a two-frame forward step (frame timestamps in a re-encoded
  preview are not exact multiples of 1/fps);
* no source reload and no Streamlit message during playback;
* the walkthrough starts at the swing rather than at frame 0;
* the overlay anchor lands on the check's own normalised point before and
  after resizing to a 390 px phone width.

Usage::

    .venv\\Scripts\\python.exe scripts\\qa_real_video.py VIDEO ^
        --url http://127.0.0.1:8501 --out var\\qa\\real-video
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_guided_playback as guided  # noqa: E402
import qa_ui_screenshots as ui  # noqa: E402

HOOKS_JS = """
(() => {
  for (const frame of Array.from(document.querySelectorAll('iframe'))) {
    try {
      const win = frame.contentWindow;
      if (win && win.__swingPlayer) {
        const api = win.__swingPlayer;
        const stage = win.document.getElementById('sg-stage').getBoundingClientRect();
        const card = win.document.getElementById('sg-card').getBoundingClientRect();
        const frameRect = frame.getBoundingClientRect();
        return {
          stats: api.stats, overlay: api.overlay, segment: api.segment,
          renders: api.renders, videoSrc: api.videoSrc,
          stage: {top: frameRect.top + stage.top, bottom: frameRect.top + stage.bottom,
                  width: stage.width, height: stage.height},
          cardBottom: frameRect.top + card.bottom,
          viewport: {width: window.innerWidth, height: window.innerHeight}
        };
      }
    } catch (error) { return {error: String(error)}; }
  }
  return null;
})()
"""


def hooks(client: "ui.DevTools") -> dict:
    value = client.evaluate(HOOKS_JS)
    return value if isinstance(value, dict) else {}


def upload(client: "ui.DevTools", video: Path) -> None:
    document = client.send("DOM.getDocument", {"depth": -1, "pierce": True})
    node = client.send(
        "DOM.querySelector",
        {"nodeId": document["root"]["nodeId"], "selector": "input[type=file]"},
    )
    if not node.get("nodeId"):
        raise SystemExit("no file input on the page")
    client.send(
        "DOM.setFileInputFiles",
        {"nodeId": node["nodeId"], "files": [str(video.resolve())]},
    )


def anchor_error(info: dict) -> float | None:
    overlay = info.get("overlay") or {}
    box = overlay.get("box")
    callout = overlay.get("callout")
    expected = overlay.get("expectedAnchor")
    if not box or not callout or not expected:
        return None
    ax, ay = callout["anchor"]
    nx = (ax - box["x"]) / box["width"]
    ny = (ay - box["y"]) / box["height"]
    return max(abs(nx - expected[0]), abs(ny - expected[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video")
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "real-video"))
    parser.add_argument("--speed", default="1")
    args = parser.parse_args()

    video = Path(args.video)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    log: dict[str, object] = {"video": video.name, "holds": [], "checks": [], "failures": []}

    def record(name: str, ok: bool, detail: str) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        log["checks"].append(entry)
        if not ok:
            log["failures"].append(entry)
        print(("PASS " if ok else "FAIL ") + name + ": " + detail)

    binary = ui.find_browser()
    port = ui.free_port()
    profile = Path(tempfile.mkdtemp(prefix="swingguru-real-"))
    process = ui.launch_browser(
        binary,
        port,
        profile,
        extra_args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
    )
    client = None
    try:
        ui.wait_for_devtools(port)
        client = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("DOM.enable")
        client.viewport(1440, 900)
        client.send("Page.navigate", {"url": args.url})
        client.wait_for(ui.page_has("Analyse swing"), timeout=120, label="upload panel")
        time.sleep(1.0)

        upload(client, video)
        client.wait_for(ui.page_has(video.name[:20]), timeout=60, label="uploaded file name")
        time.sleep(1.0)
        started = time.monotonic()
        if not client.evaluate(ui.click_text("Analyse swing")):
            raise SystemExit("could not press Analyse swing")

        ready = guided.wait_for(
            client,
            lambda s: s.get("state") == "ready" and (s.get("duration") or 0) > 0,
            timeout=600,
            label="the guided player to be ready for the real video",
        )
        time.sleep(1.2)  # let the prime seek to the segment start settle
        analyse_s = time.monotonic() - started
        before = hooks(client)
        event_times = ready.get("eventTimes") or []
        log["ready"] = ready
        log["hooks_ready"] = before
        record(
            "real_video_player_ready",
            True,
            f"analysed in {analyse_s:.1f}s; preview {ready.get('duration'):.2f}s "
            f"{ready.get('videoWidth')}x{ready.get('videoHeight')}; moment times {event_times}; "
            f"segment {before.get('segment')}",
        )
        client.evaluate(ui.scroll_to("Swing Guru"))
        time.sleep(0.4)
        client.screenshot(out_dir / "00-ready.png", max_height=900)

        stage = before.get("stage") or {}
        viewport = before.get("viewport") or {}
        record(
            "desktop_video_fits_viewport",
            stage.get("height", 99999) <= viewport.get("height", 0) * 0.72,
            f"stage {stage.get('width', 0):.0f}x{stage.get('height', 0):.0f} in a "
            f"{viewport.get('width')}x{viewport.get('height')} window",
        )

        if args.speed != "1":
            guided.click_selector(client, f"button[data-speed='{args.speed}']")
        renders_before = before.get("renders")
        guided.click_control(client, "sg-play")
        seen: dict[str, dict] = {}
        order: list[str] = []
        samples: list[tuple[float, str]] = []
        deadline = time.monotonic() + 240
        last: dict = {}
        while time.monotonic() < deadline:
            state = guided.player_state(client) or {}
            last = state
            if state.get("error"):
                record("no_player_error", False, str(state["error"]))
                break
            mode = state.get("state")
            if state.get("currentTime") is not None:
                samples.append((float(state["currentTime"]), str(mode)))
            check_id = state.get("checkId")
            if mode == "holding" and check_id and check_id not in seen:
                seen[check_id] = state
                order.append(check_id)
                time.sleep(0.5)  # let the draw-in animation finish
                name = f"{len(order):02d}-{check_id.replace(':', '-')}.png"
                client.screenshot(out_dir / name, max_height=900)
                info = hooks(client)
                log["holds"].append(
                    {
                        "check": check_id,
                        "time_s": state.get("currentTime"),
                        "phase": state.get("phase"),
                        "label": state.get("label"),
                        "value": state.get("value"),
                        "provenance": state.get("provenance"),
                        "overlay_primitives": (info.get("overlay") or {}).get("primitives"),
                        "anchor_error": anchor_error(info),
                        "screenshot": name,
                    }
                )
                print(
                    f"hold {check_id:45} t={state.get('currentTime'):.3f} "
                    f"{state.get('phase')} | {state.get('label')} = {state.get('value')}"
                )
            if mode == "ended":
                break
            time.sleep(0.03)

        after = hooks(client)
        stats = after.get("stats") or {}
        log["stats"] = stats
        phases: list[str] = []
        for check_id in order:
            phase = check_id.split(":")[0]
            if phase not in phases:
                phases.append(phase)
        expected_order = ["address", "takeaway", "mid_back", "top", "downswing", "impact", "finish"]
        record(
            "moments_played_in_order",
            phases == [p for p in expected_order if p in phases],
            f"held moments: {phases}",
        )
        record("all_seven_moments_held", len(phases) == 7, f"{len(phases)} of 7 moments held")
        by_phase: dict[str, set[float]] = {}
        for check_id in order:
            by_phase.setdefault(check_id.split(":")[0], set()).add(
                round(float(seen[check_id]["currentTime"]), 4)
            )
        record(
            "checks_share_their_moment_time",
            all(len(times) == 1 for times in by_phase.values()),
            json.dumps({k: sorted(v) for k, v in by_phase.items()}),
        )
        record(
            "no_seek_between_same_moment_checks",
            stats.get("sameMomentSeeks") == 0,
            f"sameMomentSeeks={stats.get('sameMomentSeeks')}",
        )
        record(
            "holds_never_seek_backwards",
            stats.get("backwardHoldSeeks") == 0 and (stats.get("maxHoldSeekFrames") or 0) <= 2.05,
            f"holds={stats.get('holds')} holdSeeks={stats.get('holdSeeks')} "
            f"backward={stats.get('backwardHoldSeeks')} "
            f"largest step={stats.get('maxHoldSeekFrames', 0):.2f} frames "
            f"(frame callbacks: {stats.get('frameCallbacks')})",
        )
        backwards = [
            (a, b)
            for (a, mode_a), (b, mode_b) in zip(samples, samples[1:])
            if b < a - 1e-4
        ]
        record(
            "current_time_never_went_backwards",
            not backwards,
            f"{len(samples)} samples, {len(backwards)} backward steps {backwards[:3]}",
        )
        record(
            "no_source_reload_during_playback",
            stats.get("loadstarts") == (before.get("stats") or {}).get("loadstarts")
            and stats.get("sourceSets") == 1
            and after.get("videoSrc") == before.get("videoSrc"),
            f"loadstarts {stats.get('loadstarts')} (before {(before.get('stats') or {}).get('loadstarts')}), "
            f"source sets {stats.get('sourceSets')}",
        )
        during = [
            entry
            for entry in (stats.get("renderLog") or [])
            if entry.get("mode") in ("playing", "holding", "seeking", "paused")
        ]
        record(
            "no_streamlit_rerun_during_playback",
            stats.get("streamlitMessages") == 0 and not during,
            f"messages posted {stats.get('streamlitMessages')}, renders {renders_before} -> {after.get('renders')}, "
            f"height posts {stats.get('heightPosts')}, render log {stats.get('renderLog')}",
        )
        segment = before.get("segment") or [0, 0]
        first_play = next((t for t, mode in samples if mode == "playing"), None)
        record(
            "walkthrough_starts_at_the_swing",
            first_play is not None and first_play >= float(segment[0]) - 0.05 and float(segment[0]) > 1.0,
            f"segment {segment}, first playing time {first_play}",
        )
        record("reached_end", last.get("state") == "ended", f"final state {last.get('state')}")
        errors = [h["anchor_error"] for h in log["holds"] if h["anchor_error"] is not None]
        record(
            "overlay_anchor_aligned_desktop",
            bool(errors) and max(errors) < 0.004,
            f"max anchor error {max(errors) if errors else None} (fraction of frame)",
        )

        # Step onto a check, then resize to a phone and re-verify alignment.
        guided.click_control(client, "sg-next")
        guided.wait_for(client, lambda s: s.get("state") == "paused" and s.get("checkId"), timeout=20,
                        label="a paused check")
        time.sleep(0.6)
        desktop = hooks(client)
        client.viewport(390, 844, mobile=True)
        time.sleep(1.5)
        client.evaluate(ui.scroll_to("Swing Guru"))
        time.sleep(0.6)
        phone = hooks(client)
        client.screenshot(out_dir / "98-mobile-check.png", max_height=844)
        record(
            "overlay_realigned_after_resize",
            anchor_error(phone) is not None and anchor_error(phone) < 0.004
            and (phone.get("overlay") or {}).get("box", {}).get("width") != (desktop.get("overlay") or {}).get("box", {}).get("width"),
            f"desktop box {(desktop.get('overlay') or {}).get('box')}, phone box {(phone.get('overlay') or {}).get('box')}, "
            f"phone anchor error {anchor_error(phone)}",
        )
        overflow = client.evaluate(ui.overflow_pixels())
        record("mobile_no_overflow", (overflow or 0) <= 1, f"overflow {overflow}px at 390px")
        client.screenshot(out_dir / "99-mobile-full.png")
    finally:
        if client is not None:
            client.close()
        process.terminate()
        (out_dir / "capture-log.json").write_text(json.dumps(log, indent=2, default=str))
    return 1 if log["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
