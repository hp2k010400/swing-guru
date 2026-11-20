"""End-to-end QA of the Swing Guru app flow in headless Edge (phone viewport).

    .venv\\Scripts\\python.exe scripts\\qa_app_flow.py [--swings 08,12,07] [--upload VIDEO] [--errors a.mp4,b.mp4]
        [--out var\\qa\\app\\phase4]

Checks, for each existing swing: Library -> Summary (verdicts match the API) ->
tap a checkpoint -> Replay opens paused on that check -> back returns to the
same Summary at the same scroll -> drill sheet -> club tag saves and shows in
Library/Home. One full replay must hand back to the Summary on its own.
With --upload: the real file input -> processing -> new Summary -> Library +1 ->
Home latest; the same file again must not be analysed twice. With --errors:
each video must end on an error screen with guidance, and never reach Library.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402

BASE = "http://127.0.0.1:8780/"
results: list[dict] = []


def api(path: str) -> dict:
    with urllib.request.urlopen(BASE + "api/" + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"{'PASS' if ok else 'FAIL'} {name}{' - ' + detail if detail else ''}", flush=True)


class App:
    def __init__(self, client: ui.DevTools, out: Path) -> None:
        self.c, self.out = client, out

    def go(self, hash_: str, wait: str, timeout: float = 30) -> None:
        self.c.evaluate(f"location.hash = {json.dumps(hash_)}")
        self.c.wait_for(wait, timeout=timeout, label=hash_)
        time.sleep(0.8)

    def js(self, expr: str):
        return self.c.evaluate(expr)

    def click(self, selector: str) -> bool:
        return bool(self.js(f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return false; el.scrollIntoView({{block:'center'}}); el.click(); return true; }})()"))

    def shot(self, name: str) -> None:
        self.c.screenshot(self.out / f"{name}.png")

    def viewer(self, prop: str):
        return self.js(f"(() => {{ const f = document.querySelector('.replay-frame'); const v = f && f.contentWindow && f.contentWindow.__swingViewer; return v ? v.{prop} : null; }})()")

    def upload(self, path: Path) -> None:
        self.click("[data-action=analyze]")
        time.sleep(0.8)
        root = self.c.send("DOM.getDocument", {"depth": -1})["root"]["nodeId"]
        nodes = self.c.send("DOM.querySelectorAll", {"nodeId": root, "selector": "input[data-upload]"})["nodeIds"]
        self.c.send("DOM.setFileInputFiles", {"nodeId": nodes[-1], "files": [str(path)]})


def existing_swing(app: App, swing_id: str, tag: str, full_replay: bool) -> None:
    s = api(f"swings/{swing_id}")
    app.go("#/library", "!!document.querySelector('.swing-card')")
    app.click(f".swing-card[data-go='#/swing/{swing_id}']")
    app.c.wait_for("location.hash.startsWith('#/swing/') && !!document.querySelector('.card.result')", timeout=20, label="summary")
    time.sleep(0.8)
    app.shot(f"{tag}_summary_0")
    shown = app.js("[...document.querySelectorAll('.verdict-row:not(.withheld) .status-text')].map(e => e.textContent.trim())")
    expected = [c["label"] for c in s["checkpoints"]]
    check(f"{tag}: summary verdicts are the API's real verdicts", shown == expected, f"{shown}")
    groups = app.js("[...document.querySelectorAll('.summary .group-label')].map(e => e.textContent.trim())")
    check(f"{tag}: no Takeaway group (unsupported)", "Takeaway" not in groups, f"{groups}")
    withheld_rows = app.js("document.querySelectorAll('.verdict-row.withheld').length")
    check(f"{tag}: withheld checks shown as 'Not measured'", withheld_rows == len(s["withheld"]), f"{withheld_rows} rows")
    check(f"{tag}: headline counts", app.js("document.querySelector('.card.result').innerText").count(str(s["counts"]["good"])) > 0)
    # Scroll so the breakdown is in view, remember where we are, and tap the last supported check.
    app.js("document.querySelector('.view .scroll').scrollTop = document.querySelector('.verdict-row').offsetTop - 200")
    time.sleep(0.4)
    app.shot(f"{tag}_summary_1")
    target = s["checkpoints"][-1]["id"].replace(".", ":", 1)
    row = json.dumps(f".verdict-row[data-go$='/{target}']")
    app.js(f"document.querySelector({row}).scrollIntoView({{block: 'center'}})")
    time.sleep(0.4)
    before = app.js("document.querySelector('.view .scroll').scrollTop")
    app.js(f"document.querySelector({row}).click()")
    app.c.wait_for("!!document.querySelector('.replay-frame')", timeout=10, label="replay")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and app.viewer("checkId") != target:
        time.sleep(0.3)
    time.sleep(0.8)
    app.shot(f"{tag}_replay_check")
    check(f"{tag}: checkpoint deep link opens the replay on {target}", app.viewer("checkId") == target,
          f"viewer on {app.viewer('checkId')}, state {app.viewer('state')}")
    # Back button inside the viewer returns to the same Summary at the same scroll.
    app.js("document.querySelector('.replay-frame').contentDocument.getElementById('to-results').click()")
    app.c.wait_for(f"location.hash === '#/swing/{swing_id}' && !!document.querySelector('.card.result')", timeout=10, label="back")
    time.sleep(0.6)
    after = app.js("document.querySelector('.view .scroll').scrollTop")
    check(f"{tag}: back returns to the same Summary position", abs(after - before) < 4, f"{before} -> {after}")
    # Drill sheet.
    if app.click(".summary .drill-card"):
        time.sleep(0.8)
        app.shot(f"{tag}_drill")
        check(f"{tag}: drill sheet opens", "Preview drill" in (app.js("document.querySelector('.sheet') && document.querySelector('.sheet').innerText") or ""))
        app.js("document.querySelector('.scrim').click()")
        time.sleep(0.6)
    if full_replay:
        app.click(".replay-cta")
        app.c.wait_for("!!document.querySelector('.replay-frame')", timeout=10, label="replay")
        time.sleep(2.5)
        app.shot(f"{tag}_replay_playing")
        check(f"{tag}: Replay analysis plays the full walkthrough", app.viewer("state") in ("playing", "holding"), f"{app.viewer('state')}")
        try:
            app.c.wait_for(f"location.hash === '#/swing/{swing_id}'", timeout=90, label="walkthrough end")
            check(f"{tag}: the finished walkthrough returns to the Summary", True)
        except TimeoutError:
            check(f"{tag}: the finished walkthrough returns to the Summary", False, "timed out")


def tag_club(app: App, swing_id: str, club: str) -> None:
    app.go(f"#/swing/{swing_id}", "!!document.querySelector('.card.result')")
    had_prompt = app.js("!!document.querySelector('.club-prompt')")
    check("untagged swing asks for its club", had_prompt)
    app.click(f"[data-club='{club}']")
    time.sleep(1.2)
    check("club tag saves", api(f"swings/{swing_id}")["club"] == club)
    app.shot("club_tagged_summary")
    app.go("#/library", "!!document.querySelector('.swing-card')")
    chips = app.js("[...document.querySelectorAll('[data-filter]')].map(b => b.textContent)")
    check("Library shows a filter for the tagged club", club in chips, f"{chips}")
    app.click(f"[data-filter='club:{club}']")
    time.sleep(0.5)
    count = app.js("document.querySelectorAll('.swing-card').length")
    check("club filter shows only that club", count == sum(1 for s in api('swings')['swings'] if s['club'] == club), f"{count}")
    app.shot("library_club_filter")
    app.go("#/home", "!!document.querySelector('.carousel')")
    check("Home recent swings show the club", club in app.js("document.querySelector('.carousel').innerText"))


def upload_flow(app: App, video: Path, tag: str = "upload") -> str | None:
    before = len(api("swings")["swings"])
    app.go("#/home", "!!document.querySelector('.view')")
    app.upload(video)
    app.c.wait_for("location.hash.startsWith('#/processing/')", timeout=120, label="processing screen")
    started = time.monotonic()
    shots = 0
    while time.monotonic() - started < 900:
        hash_ = app.js("location.hash")
        if hash_.startswith("#/swing/"):
            break
        if app.js("!!document.querySelector('.proc-orb.error')"):
            break
        if shots < 3 and time.monotonic() - started > shots * 12:
            app.shot(f"{tag}_processing_{shots}")
            shots += 1
        time.sleep(1.0)
    hash_ = app.js("location.hash")
    ok = hash_.startswith("#/swing/") and hash_.endswith("/new")
    check("upload -> processing -> new Summary", ok, f"{hash_} after {time.monotonic() - started:.0f}s")
    if not ok:
        app.shot(f"{tag}_failed")
        return None
    time.sleep(1.2)
    app.shot(f"{tag}_summary_new")
    swing_id = hash_.split("/")[2]
    after = api("swings")["swings"]
    check("Library gained the new swing", len(after) == before + 1 and any(s["id"] == swing_id for s in after))
    check("new swing is the latest on Home", api("home")["latest"]["id"] == swing_id)
    stages = api(f"jobs/{swing_id[1:]}")["stages"]
    check("every processing stage completed", all(s["state"] == "done" for s in stages), f"{[s['state'] for s in stages]}")
    # Same file again: no second analysis.
    app.go("#/home", "!!document.querySelector('.view')")
    app.upload(video)
    app.c.wait_for("location.hash.startsWith('#/swing/')", timeout=120, label="duplicate")
    time.sleep(1.0)
    check("the same video isn't analysed twice", app.js("location.hash").endswith("/existing")
          and len(api("swings")["swings"]) == len(after), app.js("location.hash"))
    app.shot(f"{tag}_duplicate")
    return swing_id


def error_flow(app: App, video: Path, name: str) -> None:
    before = len(api("swings")["swings"])
    app.go("#/home", "!!document.querySelector('.view')")
    app.upload(video)
    try:
        app.c.wait_for("!!document.querySelector('.proc-orb.error') || location.hash.startsWith('#/swing/')", timeout=900,
                       label=f"error {name}")
    except TimeoutError:
        check(f"error video {name}: ends in an error state", False, "timed out")
        return
    time.sleep(0.6)
    app.shot(f"error_{name}")
    text = app.js("document.querySelector('.processing') ? document.querySelector('.processing').innerText : location.hash")
    ok = app.js("!!document.querySelector('.proc-orb.error')") and len(api("swings")["swings"]) == before
    leak = any(w in text for w in ("RTMW", "OWL", "pose", "segmentation", "cache"))
    check(f"error video {name}: clear guidance, nothing saved", ok and not leak, text.replace("\n", " | ")[:160])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--swings", default="08,12,07")
    parser.add_argument("--full-replay", default="08")
    parser.add_argument("--club", default="Driver")
    parser.add_argument("--upload", default=None)
    parser.add_argument("--errors", default="")
    parser.add_argument("--out", default=str(ROOT / "var" / "qa" / "app" / "phase4"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    port = ui.free_port()
    process = ui.launch_browser(ui.find_browser(), port, Path(tempfile.mkdtemp(prefix="swingguru-flow-")),
                                extra_args=["--autoplay-policy=no-user-gesture-required"])
    try:
        ui.wait_for_devtools(port)
        client = ui.DevTools(ui.first_page_target(port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("DOM.enable")
        client.viewport(390, 844, mobile=True)
        client.send("Page.navigate", {"url": BASE + "#/home"})
        client.wait_for("!!document.querySelector('.view')", timeout=30, label="app")
        app = App(client, out)
        ids = {s["id"][:2]: s["id"] for s in api("swings")["swings"]}
        for key in [k for k in args.swings.split(",") if k]:
            existing_swing(app, ids[key], key, full_replay=(key == args.full_replay))
        untagged = next((s["id"] for s in api("swings")["swings"] if not s["club"]), None)
        if untagged:
            tag_club(app, untagged, args.club)
        for index, video in enumerate([Path(v) for v in (args.upload or "").split(",") if v]):
            new_id = upload_flow(app, video, tag=f"upload{index}")
            if new_id:
                notice = api(f"swings/{new_id}").get("notice")
                print(f"     notice for {video.name}: {notice}", flush=True)
        for video in [Path(v) for v in args.errors.split(",") if v]:
            error_flow(app, video, video.stem[-8:].replace(" ", "_").replace(".", "-"))
    finally:
        process.terminate()
    (out / "flow_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

