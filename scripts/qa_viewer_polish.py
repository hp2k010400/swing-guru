"""Visual QA captures of the consumer viewer at phone width (design review).

    .venv\\Scripts\\python.exe scripts\\qa_viewer_polish.py var\\qa\\test_swings\\01_...\\bundle --out var\\qa\\ui_polish\\final

Serves the bundle, opens it at 390x844 (a phone), presses play once and captures:
every held check, active playback between moments, a paused checkpoint, the
switch between two checks on the same frame (overlay leaving / arriving), and
the results screen. Also writes the stage-to-viewport ratio and timings.
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

import qa_ui_screenshots as ui  # noqa: E402
from qa_viewer import STATE, serve  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server, port = serve(Path(args.bundle))
    browser_port = ui.free_port()
    process = ui.launch_browser(ui.find_browser(), browser_port, Path(tempfile.mkdtemp(prefix="sg-polish-")),
                                extra_args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
    log: dict = {"captures": []}
    try:
        ui.wait_for_devtools(browser_port)
        c = ui.DevTools(ui.first_page_target(browser_port)["webSocketDebuggerUrl"])
        c.send("Page.enable")
        c.send("Runtime.enable")
        c.viewport(args.width, args.height, mobile=True)
        c.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/index.html"})
        for _ in range(300):
            s = c.evaluate(STATE)
            if s and s.get("state") == "ready":
                break
            time.sleep(0.05)
        time.sleep(0.6)

        def shot(name, note=""):
            path = out / f"{len(log['captures']):02d}-{name}.png"
            c.screenshot(path, max_height=args.height)
            log["captures"].append({"file": path.name, "note": note})

        shot("ready", "before play")
        log["stage_ratio"] = c.evaluate("document.getElementById('stage').getBoundingClientRect().height / window.innerHeight")
        log["stage_width_ratio"] = c.evaluate("document.getElementById('stage').getBoundingClientRect().width / window.innerWidth")
        c.evaluate("document.getElementById('play').click()")
        seen, got_playing, got_pause, got_switch = [], False, False, False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            s = c.evaluate(STATE) or {}
            if s.get("state") == "playing" and seen and not got_playing and \
                    c.evaluate("document.getElementById('phase-pill').textContent") == "Takeaway":
                shot("takeaway-playback", f"playing through Takeaway at t={s.get('t')} (no checkpoint there)")
                got_playing = True
            if s.get("state") == "holding" and s.get("id") and s["id"] not in seen:
                seen.append(s["id"])
                time.sleep(0.4)  # overlay draw-in done (180-240 ms)
                # Every overlay element and the label must sit inside the picture.
                spill = c.evaluate("""(() => { const svg = document.getElementById('overlay'), v = window.__swingViewer;
                  const b = v.overlay.box; if (!b) return null; let worst = 0;
                  svg.querySelectorAll('path,circle,rect').forEach(el => { const r = el.getBBox();
                    worst = Math.max(worst, b.x - r.x, b.y - r.y, r.x + r.width - (b.x + b.width), r.y + r.height - (b.y + b.height)); });
                  return worst; })()""")
                log.setdefault("overlay_spill_px", {})[s["id"]] = None if spill is None else round(spill, 1)
                shot("hold-" + s["id"].replace(":", "-"), f"held at t={s.get('t')}")
                if s["id"].startswith("top:") and not got_pause:
                    c.evaluate("document.getElementById('play').click()")  # pause on the checkpoint
                    time.sleep(0.35)
                    shot("paused-checkpoint", "user paused while a check is held")
                    c.evaluate("document.getElementById('play').click()")
                    got_pause = True
                if s["id"] == "address:torso_angle" and not got_switch:
                    # Wait for the switch to the next Address check on the same frame.
                    t_frame = s.get("t")
                    while time.monotonic() < deadline:
                        leaving = c.evaluate("document.getElementById('overlay').classList.contains('leaving')")
                        if leaving:
                            shot("switch-leaving", "same frame: old overlay fading out, no seek")
                            break
                        time.sleep(0.01)
                    time.sleep(0.12)
                    after = c.evaluate(STATE) or {}
                    shot("switch-arriving", f"same frame t={after.get('t')} (was {t_frame}): new overlay drawing in")
                    log["switch_same_frame"] = after.get("t") == t_frame
                    got_switch = True
            if s.get("screen") == "results":
                break
            time.sleep(0.02)
        time.sleep(0.7)
        shot("results", "results screen (mobile)")
        stats = (c.evaluate(STATE) or {}).get("stats") or {}
        log.update({"held": seen, "stats": stats})
        c.close()
    finally:
        process.terminate()
        server.shutdown()
        (out / "polish-log.json").write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    spill = log.get("overlay_spill_px") or {}
    log["max_overlay_spill_px"] = max((v for v in spill.values() if v is not None), default=None)
    print(json.dumps({k: log[k] for k in ("stage_ratio", "switch_same_frame", "max_overlay_spill_px", "held") if k in log}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
