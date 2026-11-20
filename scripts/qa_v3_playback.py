"""Overlay v3 playback behaviour in a real browser: pause, resume, scrub, prev / next, speed, replay.

    python scripts/qa_v3_playback.py <bundle dir> [--out report.json]

Drives the viewer (``?embed=1&autoplay=1&overlay=3``) through headless Edge/Chrome with the same
controls a golfer uses, and asserts:

1. pause mid-reveal freezes the overlay exactly (reveal clock and SVG unchanged while paused);
2. play continues the reveal from the same point;
3. scrubbing clears the overlay and stops the reveal clock;
4. next / previous restart the reveal from 0 on the new check;
5. a playback-speed change leaves hold length and reveal timing unchanged (wall-clock holds);
6. the walkthrough reaches the end and every walkthrough check (scored + observations) was held;
7. "watch again" (replay) restarts from the first moment;
8. reduced motion shows each check complete at once.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import overlay_v3_qa as q  # noqa: E402

V = "window.__swingViewer"


def js(c, expr):
    out = c.evaluate(f"JSON.stringify({expr})")
    return None if out is None else json.loads(out)


def svg(c):
    return c.evaluate("document.getElementById('overlay').innerHTML")


def wait(c, cond, timeout=30.0, label=""):
    c.wait_for(cond, timeout=timeout, label=label)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    server, base = q.serve(Path(args.bundle))
    proc, c = q.open_browser()
    results: dict = {}

    def check(name, ok, detail=None):
        results[name] = {"ok": bool(ok), "detail": detail}
        print(("PASS " if ok else "FAIL ") + name + ("" if detail is None else f"  {detail}"), flush=True)

    try:
        c.send("Page.navigate", {"url": f"{base}?embed=1&autoplay=1&overlay=3"})
        wait(c, f"!!{V} && {V}.state === 'holding'", 60, "first hold")
        plan = js(c, f"{V}.checkIds")
        time.sleep(0.35)
        # 1. pause mid-reveal
        c.evaluate("document.getElementById('play').click()")
        time.sleep(0.05)
        a, s1 = js(c, f"{V}.v3"), svg(c)
        time.sleep(0.8)
        b, s2 = js(c, f"{V}.v3"), svg(c)
        check("pause freezes the reveal", js(c, f"{V}.state") == "paused" and a["elapsed"] == b["elapsed"]
              and not b["running"] and s1 == s2 and 0 < a["elapsed"] < a["end"], {"before": a, "after": b})
        # 2. play continues
        c.evaluate("document.getElementById('play').click()")
        time.sleep(0.3)
        cnt = js(c, f"{V}.v3")
        check("play continues from the same point", js(c, f"{V}.state") == "holding" and cnt["elapsed"] > b["elapsed"]
              and cnt["elapsed"] - b["elapsed"] < 600, {"resumed": cnt})
        # 4. next / previous restart the reveal
        c.evaluate("document.getElementById('next').click()")
        wait(c, f"{V}.state === 'paused' && {V}.checkId === {json.dumps(plan[1])}", 20, "next")
        time.sleep(0.15)
        n1 = js(c, f"{V}.v3")
        c.evaluate("document.getElementById('prev').click()")
        wait(c, f"{V}.checkId === {json.dumps(plan[0])}", 20, "prev")
        time.sleep(0.15)
        p1 = js(c, f"{V}.v3")
        check("next / previous restart the reveal", n1["elapsed"] < 400 and p1["elapsed"] < 400, {"next": n1, "prev": p1})
        time.sleep(1.8)
        check("a check reached by prev / next still completes its reveal", js(c, f"{V}.v3")["elapsed"] == js(c, f"{V}.v3")["end"])
        # 3. scrub clears
        c.evaluate("var s=document.getElementById('seek'); s.value='600'; s.dispatchEvent(new Event('input'))")
        time.sleep(0.4)
        # The checkpoint overlay must be gone; the faint Address reference lines (.persist, shown between
        # checkpoints once Address has passed and the camera was still) are allowed to stay.
        empty = c.evaluate("Array.from(document.querySelectorAll('#overlay path[d]:not([d=\"\"]), #overlay text'))"
                           ".filter(function (e) { return !e.closest('.persist'); }).length")
        persist = c.evaluate("document.querySelectorAll('#overlay .persist').length")
        check("scrubbing clears the checkpoint overlay and stops the clock", empty == 0 and not js(c, f"{V}.v3")["running"],
              {"checkpoint_elements": empty, "address_reference_groups": persist})
        # 5 + 6. speed change: hold timing in wall-clock; full walkthrough from the start at 1/2x
        c.evaluate("document.getElementById('speed').click()")             # 1x -> 1/2x
        rate = c.evaluate("document.getElementById('video').playbackRate")
        c.evaluate(f"{V}.goToCheck({json.dumps(plan[0])})")
        wait(c, f"{V}.checkId === {json.dumps(plan[0])}", 20, "back to start")
        c.evaluate("document.getElementById('play').click()")
        held, holds, t0, started = [], {}, time.time(), None
        while time.time() - t0 < 120:
            st, cid = js(c, f"{V}.state"), js(c, f"{V}.checkId")
            if st == "holding" and cid:
                if cid not in holds:
                    holds[cid] = time.time()
                    held.append(cid)
            if st == "ended":
                break
            time.sleep(0.05)
        durations = [round(holds[held[i + 1]] - holds[held[i]], 2) for i in range(len(held) - 1)]
        check("walkthrough at 1/2x reaches the end holding every check", js(c, f"{V}.state") == "ended"
              and held == plan, {"rate": rate, "held": len(held), "plan": len(plan)})
        check("hold timing is unaffected by playback speed (same-moment switches)",
              all(1.4 <= d <= 3.2 for d, a1, b1 in zip(durations, held, held[1:]) if a1.split(":")[0] == b1.split(":")[0]),
              {"hold_to_hold_s": durations})
        # 7. replay
        c.evaluate("document.getElementById('play').click()")
        wait(c, f"{V}.state === 'holding'", 30, "replay")
        check("watch again restarts from the first moment", js(c, f"{V}.checkId") == plan[0])
        # 8. reduced motion
        c.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
        c.send("Page.navigate", {"url": f"{base}?embed=1&check={plan[0]}&overlay=3"})
        wait(c, f"!!{V} && {V}.checkId === {json.dumps(plan[0])}", 60, "reduced")
        time.sleep(0.2)
        r = js(c, f"{V}.v3")
        check("reduced motion shows the check complete at once", r["elapsed"] == r["end"] and not r["running"], r)
    finally:
        proc.terminate()
        server.shutdown()
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [k for k, v in results.items() if not v["ok"]]
    print(json.dumps({"passed": len(results) - len(failed), "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
