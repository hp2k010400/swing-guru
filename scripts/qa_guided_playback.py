"""Real-browser evidence for the guided playback interaction.

Drives the installed Edge in headless mode over the Chrome DevTools Protocol
(reusing the helpers in ``qa_ui_screenshots.py``) and records *timing* evidence,
not just screenshots:

* the video's ``currentTime`` really advances between moments;
* two different checks are held at the same video time;
* pausing during a hold freezes the hold instead of racing on;
* playback resumes after the last check of a moment;
* 0.25x/0.5x/1x change both ``playbackRate`` and the observed traversal;
* the scrubber seeks the real video, Prev/Next step through checks, Replay
  resets the run;
* a portrait clip stays aligned and nothing overflows at 390px or 1440px.

Usage::

    .venv\\Scripts\\python.exe scripts\\qa_guided_playback.py ^
        --url http://127.0.0.1:8501 ^
        --out var\\qa\\guided-playback
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402

PLAYER_STATE_JS = """
(() => {
  const frames = Array.from(document.querySelectorAll('iframe'));
  for (const frame of frames) {
    try {
      const win = frame.contentWindow;
      if (win && win.__swingPlayer) {
        const api = win.__swingPlayer;
        const doc = win.document;
        const video = doc.getElementById('sg-video');
        const overlay = doc.getElementById('sg-overlay');
        return {
          state: api.state,
          currentTime: api.currentTime,
          checkIndex: api.checkIndex,
          checkId: api.checkId,
          dwelled: api.dwelled,
          eventTimes: api.eventTimes,
          renders: api.renders,
          videoSrc: api.videoSrc,
          readyState: api.readyState,
          lastSeek: api.lastSeek,
          lastSeeked: api.lastSeeked,
          seekable: api.seekable,
          speed: api.speed,
          rate: video ? video.playbackRate : null,
          duration: video ? video.duration : null,
          paused: video ? video.paused : null,
          videoWidth: video ? video.videoWidth : null,
          videoHeight: video ? video.videoHeight : null,
          error: api.error,
          phase: (doc.getElementById('sg-phase') || {}).textContent || null,
          label: (doc.getElementById('sg-label') || {}).textContent || null,
          value: (doc.getElementById('sg-value') || {}).textContent || null,
          provenance: (doc.getElementById('sg-provenance') || {}).textContent || null,
          overlayCount: overlay ? overlay.childElementCount : null
        };
      }
    } catch (error) {
      return { frameError: String(error) };
    }
  }
  return null;
})()
"""


def _component_call(client: "ui.DevTools", body: str):
    """Run ``body`` inside the player iframe with win/doc in scope."""

    expression = (
        "(() => { const frames = Array.from(document.querySelectorAll('iframe'));"
        " for (const frame of frames) { try { const win = frame.contentWindow;"
        " if (win && win.__swingPlayer) { const doc = win.document;"
        " return (function (win, doc) { " + body + " })(win, doc); }"
        " } catch (error) { return { frameError: String(error) }; } }"
        " return null; })()"
    )
    return client.evaluate(expression)


def player_state(client: "ui.DevTools") -> dict | None:
    value = client.evaluate(PLAYER_STATE_JS)
    return value if isinstance(value, dict) else None


def click_control(client: "ui.DevTools", element_id: str) -> bool:
    return bool(
        _component_call(
            client,
            f"const el = doc.getElementById('{element_id}');"
            " if (!el) return false; el.click(); return true;",
        )
    )


def click_selector(client: "ui.DevTools", selector: str) -> bool:
    return bool(
        _component_call(
            client,
            f"const el = doc.querySelector({json.dumps(selector)});"
            " if (!el) return false; el.click(); return true;",
        )
    )


def wait_for(
    client: "ui.DevTools",
    predicate,
    *,
    timeout: float = 45.0,
    label: str = "player state",
):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = player_state(client)
        if last and predicate(last):
            return last
        if last and last.get("error"):
            raise SystemExit(f"player error while waiting for {label}: {last['error']}")
        time.sleep(0.12)
    raise SystemExit(f"timed out waiting for {label}; last state: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8501")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "guided-playback"))
    parser.add_argument("--keep-browser", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    binary = ui.find_browser()
    port = ui.free_port()
    profile = Path(__import__("tempfile").mkdtemp(prefix="swingguru-guided-"))
    log: dict[str, object] = {
        "url": args.url,
        "browser": str(binary),
        "checks": [],
        "captures": [],
        "measurements": {},
        "failures": [],
    }

    def record(name: str, ok: bool, detail: str, data: object = None) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        if data is not None:
            entry["data"] = data
        log["checks"].append(entry)
        if not ok:
            log["failures"].append(entry)
        print(("PASS " if ok else "FAIL ") + name + ": " + detail)

    # Headless Edge blocks programmatic playback without a user gesture; the
    # real page plays from a click, so the harness lifts that policy rather
    # than faking the gesture.
    process = ui.launch_browser(
        binary,
        port,
        profile,
        extra_args=[
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
        ],
    )
    client: "ui.DevTools" | None = None
    try:
        version = ui.wait_for_devtools(port)
        log["browser_version"] = version.get("Browser")
        target = ui.first_page_target(port)
        client = ui.DevTools(target["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Page.navigate", {"url": args.url})
        client.wait_for(ui.page_has("Analyse swing"), timeout=120, label="upload panel")

        def capture(name: str, note: str, *, scroll: str | None = None) -> None:
            if scroll is not None:
                client.evaluate(ui.scroll_to(scroll))
                time.sleep(0.5)
            path = out_dir / f"{name}.png"
            size = client.screenshot(path)
            overflow = client.evaluate(ui.overflow_pixels())
            state = player_state(client)
            log["captures"].append(
                {
                    "name": name,
                    "file": path.name,
                    "note": note,
                    "size": size,
                    "overflow_px": overflow,
                    "player": state,
                }
            )
            print(f"captured {path.name}: {size} overflow={overflow}px")

        # ---------------------------------------------------------- start
        client.viewport(1440, 2200)
        time.sleep(0.8)
        if not client.evaluate(ui.open_expander("Developer details")):
            raise SystemExit("could not open Developer details")
        client.wait_for(ui.page_has("Guided playback sample"), label="source options")
        if not client.evaluate(ui.click_label("Guided playback sample")):
            raise SystemExit("could not select the guided playback sample")
        client.wait_for(
            ui.page_has("Create guided fixture and analyse"), label="guided fixture button"
        )
        client.evaluate(ui.click_text("Create guided fixture and analyse"))
        wait_for(
            client,
            lambda state: state.get("state") in {"ready", "playing", "paused", "holding"},
            timeout=240,
            label="the guided player to receive its timeline",
        )
        ready = wait_for(
            client,
            lambda state: state.get("state") == "ready"
            and (state.get("duration") or 0) > 0,
            timeout=60,
            label="the player to be ready with loaded metadata",
        )
        record(
            "player_ready",
            True,
            f"player ready with {ready.get('duration')}s preview at {ready.get('videoWidth')}x{ready.get('videoHeight')}",
            ready,
        )
        capture("01-ready-desktop", "guided player ready, desktop", scroll="Swing Guru")

        # ------------------------------------------------- play and first hold
        click_control(client, "sg-play")
        hold1 = wait_for(
            client,
            lambda state: state.get("state") == "holding" and state.get("checkId"),
            timeout=60,
            label="the first held check",
        )
        record(
            "first_hold",
            bool(hold1.get("checkId", "").startswith("address:")),
            f"holding {hold1.get('checkId')} at t={hold1.get('currentTime')}",
            hold1,
        )
        record(
            "geometry_on_hold",
            bool(hold1.get("overlayCount", 0) > 0),
            f"{hold1.get('overlayCount')} geometry primitives drawn",
        )
        capture("02-address-check1-desktop", "first Address check held")

        hold2 = wait_for(
            client,
            lambda state: state.get("state") == "holding"
            and state.get("checkId")
            and state.get("checkId") != hold1.get("checkId"),
            timeout=30,
            label="the second check of the same moment",
        )
        same_time = abs(float(hold2.get("currentTime")) - float(hold1.get("currentTime")))
        record(
            "same_held_time_two_checks",
            same_time <= 0.002 and hold2.get("checkId") != hold1.get("checkId"),
            f"{hold1.get('checkId')} and {hold2.get('checkId')} both at "
            f"t={hold2.get('currentTime')} (delta {same_time:.4f}s)",
            {"first": hold1, "second": hold2},
        )
        capture("03-address-check2-desktop", "second Address check, same held frame")

        # ------------------------------------------------- pause freezes hold
        click_control(client, "sg-play")  # toggle to Pause
        paused = wait_for(
            client,
            lambda state: state.get("state") == "paused",
            timeout=15,
            label="pause during a hold",
        )
        time.sleep(1.2)
        still = player_state(client)
        record(
            "pause_freezes_hold",
            abs(float(still.get("currentTime")) - float(paused.get("currentTime"))) < 0.002
            and still.get("checkId") == paused.get("checkId")
            and still.get("dwelled") == paused.get("dwelled"),
            f"held at t={still.get('currentTime')} for 1.2s with the same check and no new dwell",
            {"paused": paused, "after": still},
        )
        capture("04-paused-hold-desktop", "hold frozen by Pause")

        # ------------------------------------------------- resume and play on
        click_control(client, "sg-play")
        wait_for(
            client,
            lambda state: state.get("state") in {"holding", "playing"},
            timeout=15,
            label="resume",
        )
        playing = wait_for(
            client,
            lambda state: state.get("state") == "playing",
            timeout=30,
            label="playback to resume after the last check",
        )
        first_time = float(playing.get("currentTime"))
        next_event = next(
            (
                float(item)
                for item in (playing.get("eventTimes") or [])
                if float(item) > first_time + 0.05
            ),
            None,
        )
        window = 0.9 if next_event is None else min(0.9, max(0.15, next_event - first_time - 0.05))
        time.sleep(window)
        later = player_state(client)
        advanced = float(later.get("currentTime")) - first_time
        record(
            "current_time_advances",
            advanced > 0.08,
            f"currentTime advanced {advanced:.2f}s in {window:.2f}s of wall clock "
            "after the hold (the next moment bounds the free window)",
            {"from": first_time, "to": later.get("currentTime")},
        )
        capture("05-playing-after-address-desktop", "playing again after the Address checks")

        # ------------------------------------------------- a later moment
        later_hold = wait_for(
            client,
            lambda state: state.get("state") == "holding"
            and state.get("checkId")
            and not state.get("checkId", "").startswith("address:"),
            timeout=60,
            label="a later moment to hold",
        )
        record(
            "later_moment_holds",
            True,
            f"later moment {later_hold.get('checkId')} held automatically at "
            f"t={later_hold.get('currentTime')}",
            later_hold,
        )
        capture("06-later-moment-desktop", "a later moment held automatically")

        # ------------------------------------------------- speed controls
        click_selector(client, "button[data-speed='0.5']")
        speed_half = wait_for(
            client,
            lambda state: abs(float(state.get("speed") or 0) - 0.5) < 1e-6,
            timeout=10,
            label="half speed",
        )
        record(
            "speed_button_sets_rate",
            abs(float(speed_half.get("rate") or 0) - 0.5) < 1e-6,
            f"playbackRate is {speed_half.get('rate')} at 0.5x",
        )

        # ------------------------------------------------- seek
        duration = float(player_state(client).get("duration") or 0)
        _component_call(
            client,
            "const el = doc.getElementById('sg-seek'); el.value = '600';"
            " el.dispatchEvent(new win.Event('input', {bubbles: true})); return true;",
        )
        seeked = wait_for(
            client,
            lambda state: state.get("state") == "paused",
            timeout=20,
            label="the scrubber to seek",
        )
        target = 0.6 * duration
        record(
            "scrubber_seeks_video",
            abs(float(seeked.get("currentTime")) - target) < 0.3,
            f"scrub to 60% landed at t={seeked.get('currentTime')} (target {target:.2f}s)",
            seeked,
        )
        capture("07-seeked-desktop", "scrubbed to 60% and paused")

        # ------------------------------------------------- prev / next
        click_control(client, "sg-next")
        first = wait_for(
            client,
            lambda state: state.get("state") == "paused" and state.get("checkId"),
            timeout=20,
            label="next check",
        )
        click_control(client, "sg-next")
        second = wait_for(
            client,
            lambda state: state.get("state") == "paused"
            and state.get("checkId")
            and state.get("checkId") != first.get("checkId"),
            timeout=20,
            label="the following check",
        )
        click_control(client, "sg-prev")
        back = wait_for(
            client,
            lambda state: state.get("state") == "paused"
            and state.get("checkId") == first.get("checkId"),
            timeout=20,
            label="the previous check",
        )
        record(
            "prev_next_navigates_checks",
            True,
            f"Next moved {first.get('checkId')} -> {second.get('checkId')} and Prev "
            f"returned to {back.get('checkId')}",
        )
        capture("08-check-navigation-desktop", "check navigation via Next/Prev")

        # ------------------------------------------------- replay
        click_control(client, "sg-replay")
        replayed = wait_for(
            client,
            lambda state: state.get("state") in {"playing", "holding"},
            timeout=20,
            label="replay to start",
        )
        record(
            "replay_resets",
            float(replayed.get("currentTime")) < 0.6
            and replayed.get("dwelled") == []
            and replayed.get("checkIndex", -1) <= 0,
            f"replay restarted at t={replayed.get('currentTime')} with "
            f"{len(replayed.get('dwelled') or [])} completed checks",
            replayed,
        )

        # ------------------------------------------------- mobile viewport
        client.viewport(390, 844, mobile=True)
        time.sleep(1.2)
        capture("09-player-mobile", "player at 390px mobile", scroll="Swing Guru")
        mobile_overflow = log["captures"][-1]["overflow_px"]
        record(
            "no_mobile_overflow",
            mobile_overflow <= 1,
            f"390px viewport horizontal overflow {mobile_overflow}px",
        )

        # ------------------- speed on a run with no moments to interrupt it
        client.viewport(1440, 2200)
        time.sleep(0.8)
        client.evaluate(ui.scroll_to("Developer details"))
        client.evaluate(ui.open_expander("Developer details"))
        client.wait_for(ui.page_has("Sample clip"), label="the sample source option")
        client.evaluate(ui.click_label("Sample clip"))
        client.wait_for(
            ui.page_has("Create sample clip and analyse"), label="the sample button"
        )
        client.evaluate(ui.click_text("Create sample clip and analyse"))
        plain = wait_for(
            client,
            lambda state: state.get("state") == "ready" and not state.get("eventTimes"),
            timeout=180,
            label="a run with no moments",
        )
        record(
            "sample_run_has_no_moments",
            not plain.get("eventTimes"),
            "the monotonic sample sweep produced "
            f"{len(plain.get('eventTimes') or [])} moments, so playback is free",
            plain,
        )

        def measure_traversal(seconds: float, speed: float) -> float:
            click_selector(client, f"button[data-speed='{speed:g}']")
            wait_for(
                client,
                lambda state: abs(float(state.get("speed") or 0) - speed) < 1e-6,
                timeout=10,
                label=f"{speed}x speed",
            )
            click_control(client, "sg-replay")
            wait_for(
                client,
                lambda state: state.get("state") == "playing",
                timeout=20,
                label="replay before the traversal measure",
            )
            wait_for(
                client,
                lambda state: float(state.get("currentTime") or 0) > 0.03,
                timeout=5,
                label="the video to be moving before the traversal measure",
            )
            moving = player_state(client)
            log["measurements"][f"rate_at_{speed:g}x"] = moving.get("rate")
            record(
                f"rate_held_at_{speed:g}x",
                abs(float(moving.get("rate") or 0) - speed) < 1e-6,
                f"playbackRate is {moving.get('rate')} while measuring {speed}x",
            )
            start = float(moving.get("currentTime"))
            time.sleep(seconds)
            end = float(player_state(client).get("currentTime"))
            return end - start

        half_delta = measure_traversal(0.6, 0.5)
        full_delta = measure_traversal(0.6, 1.0)
        log["measurements"]["speed_half_delta"] = round(half_delta, 3)
        log["measurements"]["speed_full_delta"] = round(full_delta, 3)
        record(
            "speed_changes_traversal",
            0.15 <= half_delta <= 0.55 and 0.4 <= full_delta <= 1.0 and full_delta > half_delta * 1.4,
            f"0.6s of wall clock advanced {half_delta:.2f}s at 0.5x and "
            f"{full_delta:.2f}s at 1x",
        )

        # ------------------------------------------------- portrait clip
        client.viewport(1440, 2200)
        time.sleep(0.8)
        client.evaluate(ui.scroll_to("Developer details"))
        if not client.evaluate(ui.open_expander("Developer details")):
            raise SystemExit("could not reopen Developer details")
        client.wait_for(
            ui.page_has("Guided playback sample (portrait)"), label="portrait option"
        )
        client.evaluate(ui.click_label("Guided playback sample (portrait)"))
        client.wait_for(
            ui.page_has("Create portrait guided fixture and analyse"),
            label="portrait button",
        )
        client.evaluate(ui.click_text("Create portrait guided fixture and analyse"))
        portrait = wait_for(
            client,
            lambda state: state.get("videoHeight")
            and state.get("videoWidth")
            and state.get("videoHeight") > state.get("videoWidth")
            and state.get("state") == "ready",
            timeout=240,
            label="the portrait preview",
        )
        record(
            "portrait_video_loaded",
            True,
            f"portrait preview {portrait.get('videoWidth')}x{portrait.get('videoHeight')}",
            portrait,
        )
        click_control(client, "sg-play")
        portrait_hold = wait_for(
            client,
            lambda state: state.get("state") == "holding" and state.get("checkId"),
            timeout=60,
            label="a held check in the portrait clip",
        )
        record(
            "portrait_overlay_drawn",
            bool(portrait_hold.get("overlayCount", 0) > 0),
            f"{portrait_hold.get('overlayCount')} primitives on the portrait frame",
            portrait_hold,
        )
        capture(
            "10-portrait-moment-desktop",
            "portrait clip holding a check",
            scroll="Swing Guru",
        )
        client.viewport(390, 844, mobile=True)
        time.sleep(1.0)
        capture(
            "11-portrait-moment-mobile",
            "portrait clip holding a check at 390px",
            scroll="Swing Guru",
        )
        portrait_overflow = log["captures"][-1]["overflow_px"]
        record(
            "no_portrait_mobile_overflow",
            portrait_overflow <= 1,
            f"portrait mobile overflow {portrait_overflow}px",
        )

        log["ok"] = not log["failures"]
    finally:
        if client is not None:
            client.close()
        if not args.keep_browser:
            process.terminate()
            try:
                process.wait(timeout=15)
            except Exception:  # noqa: BLE001 - best effort cleanup
                process.kill()
            __import__("shutil").rmtree(profile, ignore_errors=True)
        (out_dir / "capture-log.json").write_text(
            json.dumps(log, indent=2), encoding="utf-8"
        )
        print(f"log written to {out_dir / 'capture-log.json'}")
    return 0 if log.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
