"""Browser QA for the standalone analysis viewer.

    .venv\\Scripts\\python.exe scripts\\qa_viewer.py var\\analysis\\real --out var\\qa\\viewer

Serves the bundle on a local port, drives headless Edge through the whole
walkthrough (desktop device frame, then a 390 px phone), screenshots every
held check and the results screen, replays a check from the results, and
verifies: no backward jumps, no seek between same-moment checks, no source
reload, overlay anchor on the check's own point before and after a resize.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402

STATE = """(() => { const v = window.__swingViewer; if (!v) return null;
  return {state: v.state, screen: v.screen, t: v.currentTime, id: v.checkId, check: v.check,
          stats: v.stats, overlay: v.overlay, segment: v.segment, ids: v.checkIds, error: v.error}; })()"""


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *args, **kwargs: None  # type: ignore[assignment]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def anchor_error(info: dict) -> float | None:
    overlay = info.get("overlay") or {}
    box, callout, check = overlay.get("box"), overlay.get("callout"), info.get("check") or {}
    if not box or not callout or not check.get("anchor"):
        return None
    ax, ay = callout["anchor"]
    ex, ey = check["anchor"]
    return max(abs((ax - box["x"]) / box["width"] - ex), abs((ay - box["y"]) / box["height"] - ey))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "viewer"))
    args = parser.parse_args()
    bundle, out = Path(args.bundle), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server, port = serve(bundle)
    url = f"http://127.0.0.1:{port}/index.html"
    log: dict = {"url": url, "runs": {}, "checks": [], "failures": []}

    def record(name: str, ok: bool, detail: str) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        log["checks"].append(entry)
        if not ok:
            log["failures"].append(entry)
        print(("PASS " if ok else "FAIL ") + name + ": " + detail)

    process = ui.launch_browser(
        ui.find_browser(), (port_b := ui.free_port()), Path(tempfile.mkdtemp(prefix="sg-viewer-")),
        extra_args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
    )
    client = None
    try:
        ui.wait_for_devtools(port_b)
        client = ui.DevTools(ui.first_page_target(port_b)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")

        def wait(predicate, timeout=60.0, label="state"):
            deadline = time.monotonic() + timeout
            last = None
            while time.monotonic() < deadline:
                last = client.evaluate(STATE)
                if last and predicate(last):
                    return last
                if last and last.get("error"):
                    raise SystemExit(f"viewer error: {last['error']}")
                time.sleep(0.05)
            raise SystemExit(f"timed out waiting for {label}: {last}")

        for label, (width, height, mobile) in {"desktop": (1440, 960, False), "phone": (390, 844, True)}.items():
            client.viewport(width, height, mobile=mobile)
            client.send("Page.navigate", {"url": url})
            ready = wait(lambda s: s["state"] == "ready", 60, "ready")
            time.sleep(0.8)
            client.screenshot(out / f"{label}-00-ready.png", max_height=height)
            client.evaluate("document.getElementById('play').click()")
            seen, order, samples, errors = {}, [], [], []
            deadline = time.monotonic() + 120
            last = ready
            while time.monotonic() < deadline:
                s = client.evaluate(STATE) or {}
                last = s
                samples.append((s.get("t"), s.get("state")))
                if s.get("state") == "holding" and s.get("id") and s["id"] not in seen:
                    seen[s["id"]] = s["t"]
                    order.append(s["id"])
                    time.sleep(0.75)  # let the overlay draw in
                    after = client.evaluate(STATE) or {}
                    if after.get("id") == s["id"]:
                        errors.append(anchor_error(after) or 0.0)
                        client.screenshot(out / f"{label}-{len(order):02d}-{s['id'].replace(':', '-')}.png", max_height=height)
                if s.get("screen") == "results":
                    break
                time.sleep(0.03)
            time.sleep(0.6)
            client.screenshot(out / f"{label}-90-results.png", max_height=height)
            stats = last.get("stats") or {}
            backwards = [(a, b) for (a, _), (b, _) in zip(samples, samples[1:]) if a is not None and b is not None and b < a - 1e-4]
            by_phase = {}
            for check_id in order:
                by_phase.setdefault(check_id.split(":")[0], set()).add(round(seen[check_id], 4))
            log["runs"][label] = {"order": order, "stats": stats, "anchor_errors": errors}
            record(f"{label}: all shown checks held", len(order) == len(ready["ids"]), f"{len(order)}/{len(ready['ids'])} held")
            record(f"{label}: checks share their moment's frame", all(len(v) == 1 for v in by_phase.values()),
                   json.dumps({k: sorted(v) for k, v in by_phase.items()}))
            record(f"{label}: no seek between same-moment checks", stats.get("sameMomentSeeks") == 0, str(stats.get("sameMomentSeeks")))
            record(f"{label}: holds never jump back", stats.get("backwardHoldSeeks") == 0 and stats.get("maxHoldSeekFrames", 0) <= 2.05,
                   f"backward {stats.get('backwardHoldSeeks')}, largest forward step {stats.get('maxHoldSeekFrames', 0):.2f} frames")
            record(f"{label}: video time never went backwards", not backwards, f"{len(samples)} samples, {len(backwards)} backward")
            record(f"{label}: single source load", stats.get("loadstarts") == 1 and stats.get("sourceSets") == 1,
                   f"loadstarts {stats.get('loadstarts')}, sources {stats.get('sourceSets')}")
            record(f"{label}: overlay anchored", bool(errors) and max(errors) < 0.004, f"max anchor error {max(errors) if errors else None}")
            record(f"{label}: results screen shown after Impact", last.get("screen") == "results", str(last.get("screen")))
            client.evaluate("document.querySelector('.results-scroll').scrollTop = 99999")
            time.sleep(0.4)
            client.screenshot(out / f"{label}-91-results-bottom.png", max_height=height)

        # Replay from the results screen, then resize with the check on screen.
        client.evaluate("document.querySelector('.results-scroll').scrollTop = 0")
        client.evaluate("document.querySelector('.focus button, .replay').click()")
        replayed = wait(lambda s: s["screen"] == "analysis" and s["state"] == "paused" and s.get("id"), 20, "replay")
        time.sleep(0.6)
        replayed = client.evaluate(STATE)
        client.screenshot(out / "phone-92-replayed.png", max_height=844)
        record("replay from results lands on the check", bool(replayed.get("id")), str(replayed.get("id")))
        phone_error = anchor_error(replayed)
        client.viewport(1440, 960)
        time.sleep(1.0)
        desktop = client.evaluate(STATE)
        record("overlay realigned after resize", phone_error is not None and anchor_error(desktop) is not None
               and anchor_error(desktop) < 0.004 and desktop["overlay"]["box"]["width"] != replayed["overlay"]["box"]["width"],
               f"phone box {replayed['overlay']['box']['width']:.0f}px err {phone_error}, desktop box {desktop['overlay']['box']['width']:.0f}px err {anchor_error(desktop)}")
        client.screenshot(out / "desktop-93-after-resize.png", max_height=960)
        overflow = client.evaluate(ui.overflow_pixels())
        record("no horizontal overflow", (overflow or 0) <= 1, f"{overflow}px")

        # Video share of the phone viewport, and a results-only check replayed.
        client.viewport(390, 844, mobile=True)
        time.sleep(1.0)
        ratio = client.evaluate("document.getElementById('stage').getBoundingClientRect().height / window.innerHeight")
        record("phone video share of viewport", ratio is not None and ratio >= 0.6, f"{ratio:.2f}")
        guided = set(client.evaluate(STATE)["ids"])
        all_ids = client.evaluate("(() => { const out = []; document.querySelectorAll('.row').forEach(() => {}); return null; })()")
        extra = next((cid for cid in ("mid_back:posture", "top:head", "impact:head") if cid not in guided), None)
        if extra:
            client.evaluate(f"window.__swingViewer.goToCheck('{extra}')")
            shown = wait(lambda s: s["state"] == "paused" and s.get("id") == extra, 20, "results-only check")
            time.sleep(0.6)
            client.screenshot(out / f"phone-94-results-only-{extra.replace(':', '-')}.png", max_height=844)
            record("results-only check replays on the video", shown.get("id") == extra, extra)
    finally:
        if client is not None:
            client.close()
        process.terminate()
        server.shutdown()
        (out / "qa-log.json").write_text(json.dumps(log, indent=2, default=str))
    return 1 if log["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
