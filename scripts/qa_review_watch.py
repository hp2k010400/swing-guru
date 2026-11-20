"""Browser QA of the human review watch pages (the real guided player, autoplaying).

    .venv\\Scripts\\python.exe scripts\\qa_review_watch.py 01 04 12 --out var\\qa\\guided_review_qa\\watch

Needs the review server (scripts/serve_review.py) on port 8770. For each swing
and each width (desktop 1440x960, phone 390x844) it opens
``/qa/human_review/<swing>/watch.html`` without touching any control, then
follows the embedded player: the walkthrough must start by itself, hold every
guided check, never move backwards, load the video once and reach the results.
It screenshots every hold (the whole page, so the overlay is seen on the real
video inside the review page).
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

BASE = "http://127.0.0.1:8770/qa/human_review"
STATE = """(() => { const f = document.getElementById('player'); if (!f || !f.contentWindow) return null;
  const v = f.contentWindow.__swingViewer; if (!v) return null;
  const doc = f.contentWindow.document;
  const svg = doc.getElementById('overlay');
  return {state: v.state, screen: v.screen, t: v.currentTime, id: v.checkId, ids: v.checkIds, stats: v.stats,
          overlay: v.overlay, error: v.error, dashed: svg ? svg.querySelectorAll('[stroke-dasharray]').length : 0,
          skeleton: doc.querySelectorAll('.skeleton').length,
          card: (doc.getElementById('card-phase').textContent + ' | ' + doc.getElementById('card-title').textContent + ' | '
                 + doc.getElementById('card-value').textContent + ' | ' + doc.getElementById('card-detail').textContent),
          frame: f.getBoundingClientRect().width + 'x' + f.getBoundingClientRect().height,
          stage: (() => { const r = doc.getElementById('stage').getBoundingClientRect(); return [r.width, r.height]; })()}; })()"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("swings", nargs="+")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "guided_review_qa" / "watch"))
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    folders = {p.name[:2]: p.name for p in (ROOT / "var" / "qa" / "human_review").iterdir() if p.is_dir()}
    log, failures = {}, []

    def record(name, ok, detail):
        print(("PASS " if ok else "FAIL ") + name + ": " + detail, flush=True)
        if not ok:
            failures.append(name)

    port = ui.free_port()
    # No autoplay flag: the page must start the (muted) video on its own, as in a normal browser.
    process = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="sg-watch-")), extra_args=["--mute-audio"])
    try:
        ui.wait_for_devtools(port)
        client = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        for key in args.swings:
            slug = folders[key]
            for label, (w, h, mobile) in {"desktop": (1440, 960, False), "phone": (390, 844, True)}.items():
                name = f"{key}-{label}"
                client.viewport(w, h, mobile=mobile)
                client.send("Page.navigate", {"url": f"{BASE}/{slug}/watch.html"})
                started, holds, samples, cards, last = None, [], [], {}, None
                deadline = time.monotonic() + 90
                began = time.monotonic()
                while time.monotonic() < deadline:
                    s = client.evaluate(STATE)
                    if not s:
                        time.sleep(0.1)
                        continue
                    last = s
                    if s.get("error"):
                        break
                    samples.append(s["t"])
                    if started is None and s["state"] in ("playing", "holding"):
                        started = round(time.monotonic() - began, 2)
                    if s["state"] == "holding" and s.get("id") and s["id"] not in cards:
                        cards[s["id"]] = s
                        holds.append((s["id"], s["t"]))
                        time.sleep(0.45)  # let the 150-250 ms overlay draw finish
                        shot = client.evaluate(STATE) or s
                        cards[s["id"]] = shot
                        if label == "phone":
                            client.evaluate("document.getElementById('player').scrollIntoView()")
                        client.screenshot(out / f"{name}-{len(holds):02d}-{s['id'].replace(':', '-')}.png",
                                          max_height=h if label == "phone" else 960)
                    if s.get("screen") == "results":
                        break
                    time.sleep(0.03)
                stats = (last or {}).get("stats") or {}
                backwards = sum(1 for a, b in zip(samples, samples[1:]) if b < a - 1e-4)
                ids = (last or {}).get("ids") or []
                record(f"{name}: starts by itself", started is not None, f"after {started} s without any click")
                record(f"{name}: every guided check held", len(holds) == len(ids) and bool(ids), f"{len(holds)}/{len(ids)}")
                record(f"{name}: no seek between same-moment checks", stats.get("sameMomentSeeks") == 0, str(stats.get("sameMomentSeeks")))
                record(f"{name}: never moves backwards", backwards == 0 and stats.get("backwardHoldSeeks") == 0,
                       f"{backwards} backward samples, {stats.get('backwardHoldSeeks')} backward hold seeks, "
                       f"largest forward hold nudge {stats.get('maxHoldSeekFrames', 0):.2f} frames")
                record(f"{name}: one video load", stats.get("loadstarts") == 1 and stats.get("sourceSets") == 1,
                       f"loadstarts {stats.get('loadstarts')}")
                record(f"{name}: reaches the results", (last or {}).get("screen") == "results", str((last or {}).get("screen")))
                record(f"{name}: overlays drawn on the video", all(c.get("overlay", {}).get("primitives", 0) > 0 for c in cards.values()),
                       json.dumps({k: c.get("overlay", {}).get("primitives") for k, c in cards.items()}))
                pelvis = [c for k, c in cards.items() if k.endswith(":pelvis")]
                log[name] = {"holds": holds, "started_after_s": started, "stats": stats, "frame": (last or {}).get("frame"),
                             "stage": (last or {}).get("stage"),
                             "cards": {k: c.get("card") for k, c in cards.items()},
                             "pelvis_dashed": [c.get("dashed") for c in pelvis]}
                print("    player", (last or {}).get("frame"), "video stage", (last or {}).get("stage"), flush=True)
        client.close()
    finally:
        process.terminate()
        (out / "watch-log.json").write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")
    print("failures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
