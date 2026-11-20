"""Real-browser QA of the blind coach-review UI (Edge over CDP).

    .venv\\Scripts\\python.exe scripts\\coach_review_qa.py

Everything runs against a throwaway synthetic database under
``var/qa/coach_review/<run>/``: the real ``var/coach_review`` validation database
is never opened, and no rating is ever written to it. Three servers are started
(desktop batch of 32 swings, phone, and an edit-mode reviewer on a small batch) so
the script can check the locker, the score reveal, reviewer isolation, reloads,
skips, empty filters and post-reveal edits.

Each run collects page JavaScript errors and fails if any appear. Desktop
(1440x960) and phone (390x844) runs screenshot the real page.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import qa_ui_screenshots as ui  # noqa: E402
from swingguru.coach_review import config as C  # noqa: E402
from swingguru.coach_review import fixtures  # noqa: E402

BIG_BATCH = "qa-synthetic"
TOKEN = "qa-token"

READY = "document.documentElement.dataset.ready === '1'"
ERRORS = "(window.__qaErrors || []).slice()"
COLLECT_ERRORS = """
window.__qaErrors = [];
window.addEventListener('error', (event) => window.__qaErrors.push(String(event.message || event.type)));
window.addEventListener('unhandledrejection', (event) => window.__qaErrors.push(String(event.reason)));
"""


def pick_qa_video(explicit: str | None = None) -> Path:
    """A small real H.264 clip so the browser can actually decode and seek it."""

    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SystemExit(f"no such QA video: {path}")
        return path
    candidates = sorted((ROOT / "src" / "swingguru" / "ui" / "player_assets" / "generated").glob("*.mp4"),
                        key=lambda p: p.stat().st_size)
    for candidate in candidates:
        if 20_000 <= candidate.stat().st_size <= 2_000_000:
            return candidate
    for candidate in sorted((ROOT / "var" / "qa" / "test_swings").glob("*/bundle/swing.mp4")):
        if candidate.stat().st_size > 20_000:
            return candidate
    raise SystemExit("no real playable clip found for browser QA (checked player assets and var/qa/test_swings)")


def wait_for_server(port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.3)
    raise SystemExit(f"review server on port {port} did not become ready")


def start_server(db: Path, batch: str, reviewer: str, port: int, *, edit_mode: bool = False) -> subprocess.Popen:
    command = [sys.executable, str(ROOT / "scripts" / "coach_review_serve.py"), "--db", str(db),
               "--batch", batch, "--reviewer-id", reviewer, "--reviewer-name", reviewer,
               "--port", str(port), "--token", TOKEN, "--no-browser"]
    if edit_mode:
        command.append("--edit-mode")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wait_for_server(port)
    return process


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
        if not ok:
            self.failures.append(f"{name}: {detail}")
        return ok


PAGE_STATE = """(() => {
  const locked = document.getElementById('locked');
  const form = document.getElementById('form');
  const skipped = document.getElementById('skipped');
  const score = document.getElementById('score-panel');
  const error = document.getElementById('page-error');
  return {
    ordinal: document.getElementById('swing-ordinal').textContent,
    club: document.getElementById('swing-club').textContent,
    state: document.getElementById('swing-state').textContent,
    progress: document.getElementById('progress-value').textContent,
    position: document.getElementById('position').textContent,
    lockedVisible: !locked.hidden,
    formVisible: !form.hidden,
    skippedVisible: !skipped.hidden,
    lockedText: locked.innerText,
    scoreVisible: !score.hidden,
    scoreText: score.innerText,
    emptyVisible: !document.getElementById('empty').hidden,
    emptyTitle: document.getElementById('empty-title').textContent,
    pageError: error.hidden ? '' : error.textContent,
    message: document.getElementById('message').textContent,
    scoreMessage: document.getElementById('score-message').textContent,
    submitLabel: document.getElementById('submit').textContent,
    cancelVisible: !document.getElementById('cancel-edit').hidden,
    editVisible: !document.getElementById('edit').hidden,
    picks: Array.from(document.querySelectorAll('.rate.is-on')).map((n) => n.dataset.field + '=' + n.dataset.value),
    confidence: (Array.from(document.querySelectorAll('#confidence-chips .chip.is-on')).map((n) => n.dataset.level)[0]) || null,
    notes: document.getElementById('notes').value,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
    videoTop: Math.round(document.querySelector('.video-panel').getBoundingClientRect().top + window.scrollY),
    videoHeight: Math.round(document.querySelector('.video-panel').getBoundingClientRect().height),
    formTop: Math.round(document.getElementById('form').getBoundingClientRect().top + window.scrollY),
  };
})()"""


def open_page(client: ui.DevTools, port: int) -> None:
    client.send("Page.navigate", {"url": f"http://127.0.0.1:{port}/"})
    client.wait_for(READY, label="the review page")


def check_clean(client: ui.DevTools, checker: Checker, label: str) -> None:
    errors = client.evaluate(ERRORS) or []
    page_error = (client.evaluate(PAGE_STATE) or {}).get("pageError") or ""
    checker.check(f"{label}: no JavaScript errors", not errors and not page_error,
                  f"{errors} {page_error}".strip()[:160])


def run_flow(name: str, checker: Checker, out: Path, client: ui.DevTools, function, *args) -> dict:
    """Run one browser flow; a failure is recorded with the page state instead of aborting the run."""

    try:
        return function(*args)
    except Exception as error:  # noqa: BLE001 - the QA must report every problem it finds
        checker.check(f"{name}: flow completed", False, f"{type(error).__name__}: {error}")
        try:
            state = client.evaluate(PAGE_STATE)
            (out / f"{name}-failure.json").write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            client.screenshot(out / f"{name}-failure.png", max_height=1600)
        except Exception:  # noqa: BLE001 - the page may be gone
            pass
        return {"error": str(error)}


def rate(client: ui.DevTools, values: dict[str, int], level: str = "MEDIUM") -> None:
    """Click one rating at a time, like a person, and confirm each one registered."""

    client.wait_for("!document.getElementById('form').hidden && !document.querySelector('.rate').disabled",
                    label="the loaded, interactive rating form")
    for field, value in values.items():
        client.evaluate(f"document.querySelector('.rate[data-field=\"{field}\"][data-value=\"{value}\"]').click()")
        assert client.evaluate(
            f"document.querySelector('.rate[data-field=\"{field}\"][data-value=\"{value}\"]').classList.contains('is-on')"), \
            f"{field}={value} did not register"
    client.evaluate(f"document.querySelector('#confidence-chips .chip[data-level=\"{level}\"]').click()")


def submit(client: ui.DevTools) -> None:
    click(client, "submit")


def click_next(client: ui.DevTools) -> None:
    """Click Next and wait for the page to actually show a different swing."""

    before = client.evaluate("document.getElementById('swing-ordinal').textContent")
    click(client, "next")
    client.wait_for(f"document.getElementById('swing-ordinal').textContent !== {json.dumps(before)}",
                    label="the next swing")


def click(client: ui.DevTools, element_id: str, *, wait: float = 15.0) -> None:
    """Wait until the control is enabled (the page disables navigation while it is busy), then click it."""

    client.wait_for(f"(() => {{ const node = document.getElementById('{element_id}');"
                    f" return !!node && !node.disabled; }})()", timeout=wait, label=f"{element_id} to be enabled")
    client.evaluate(f"document.getElementById('{element_id}').click()")


def run_viewport(client: ui.DevTools, checker: Checker, out: Path, label: str, width: int, height: int,
                 mobile: bool, port: int) -> dict:
    client.viewport(width, height, mobile=mobile)
    open_page(client, port)
    log: dict = {"label": label, "steps": {}}

    blind_ok = client.evaluate("(async () => { const text = await (await fetch('/api/session')).text();"
                               " return !/score|analysis|coverage|%\\.mp4/i.test(text); })()")
    checker.check(f"{label}: session API carries no score", blind_ok is True, "no score/analysis strings")

    state = client.evaluate(PAGE_STATE)
    checker.check(f"{label}: score is hidden before submitting",
                  state["lockedVisible"] is False and state["formVisible"] is True and state["skippedVisible"] is False,
                  f"state={state['state']!r}")
    video_first = state["videoTop"] < state["formTop"] if mobile else state["videoTop"] <= state["formTop"]
    checker.check(f"{label}: the video comes before the form", video_first and state["videoHeight"] > 120,
                  f"video top {state['videoTop']}px, form top {state['formTop']}px")
    client.screenshot(out / f"{label}-01-loaded.png", max_height=height if mobile else 1600)
    log["steps"]["loaded"] = state

    played = client.evaluate("""(async () => {
      const video = document.getElementById('video');
      video.muted = true;
      try { await video.play(); } catch (error) { return 'play failed: ' + error.message; }
      await new Promise((resolve) => setTimeout(resolve, 700));
      const t = video.currentTime;
      video.pause();
      video.currentTime = 0;
      return t > 0.05 ? 'played' : 'no progress ' + t;
    })()""")
    checker.check(f"{label}: video plays and seeks", played == "played", str(played))

    rate(client, {"setup": 6, "backswing": 5, "top": 7, "downswing": 4, "overall": 6}, "MEDIUM")
    checker.check(f"{label}: ratings stay selected on the page",
                  client.evaluate("document.querySelectorAll('.rate.is-on').length") == 5,
                  str(client.evaluate("document.querySelectorAll('.rate.is-on').length")) + " ratings selected")
    client.screenshot(out / f"{label}-02-rated.png", max_height=height if mobile else 1600)
    submit(client)
    client.wait_for("!document.getElementById('locked').hidden", label="the locked panel")
    state = client.evaluate(PAGE_STATE)
    checker.check(f"{label}: submitting locks the ratings",
                  state["lockedVisible"] is True and state["formVisible"] is False, state["state"])
    checker.check(f"{label}: the saved values are shown",
                  "6 / 10" in state["lockedText"] and "MEDIUM" in state["lockedText"], "saved summary")
    client.screenshot(out / f"{label}-03-submitted.png", max_height=height if mobile else 1600)
    check_clean(client, checker, label)

    replay = client.evaluate("""(async () => {
      const token = document.querySelector('script[data-token]').dataset.token;
      const session = await (await fetch('/api/session')).json();
      const current = session.items.find((item) => item.state === 'submitted');
      const response = await fetch('/api/items/' + current.item_id + '/review', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Coach-Review-Token': token},
        body: JSON.stringify({ratings: {setup: 1, backswing: 1, top: 1, downswing: 1, overall: 1}, confidence: 'LOW'})});
      return response.status;
    })()""")
    checker.check(f"{label}: a blind submission cannot be overwritten", replay == 409, f"HTTP {replay}")

    click(client, "reveal")
    client.wait_for("!document.getElementById('score-panel').hidden", label="the score panel")
    state = client.evaluate(PAGE_STATE)
    unlocked = "/ 10" in state["scoreText"] or "No stored Swing Score" in state["scoreText"]
    checker.check(f"{label}: the score unlocks after submitting", state["scoreVisible"] and unlocked,
                  state["scoreText"][:60].replace("\n", " "))
    client.screenshot(out / f"{label}-04-revealed.png", max_height=height if mobile else 1600)
    check_clean(client, checker, label)

    click_next(client)
    state = client.evaluate(PAGE_STATE)
    checker.check(f"{label}: next keeps a coherent selection", state["formVisible"] is True, state["state"])
    client.screenshot(out / f"{label}-05-next.png", max_height=height if mobile else 1600)
    log["steps"]["next"] = state
    return log


def run_skip_flow(client: ui.DevTools, checker: Checker, out: Path, port: int) -> dict:
    """Skip a swing, leave it, come back through the Skipped filter and undo the skip."""

    client.viewport(1440, 960, mobile=False)
    open_page(client, port)
    click_next(client)
    skipped_ordinal = client.evaluate(PAGE_STATE)["ordinal"]
    click(client, "skip")
    client.wait_for("(async () => { const s = await (await fetch('/api/session')).json(); return s.batch.skipped >= 1; })()",
                    label="the recorded skip")
    client.evaluate("""(() => { document.querySelector('[data-filter="skipped"]').click(); })()""")
    client.wait_for("!document.getElementById('skipped').hidden", label="the skipped panel")
    state = client.evaluate(PAGE_STATE)
    checker.check("skip: the skipped swing is actionable with an undo button",
                  state["skippedVisible"] is True and state["formVisible"] is False, state["ordinal"])
    checker.check("skip: the skipped swing cannot reveal a score",
                  client.evaluate("document.getElementById('reveal') === null || "
                                  "document.getElementById('locked').hidden") is True, "no score panel")
    client.screenshot(out / "desktop-06-skipped.png", max_height=1600)
    click(client, "unskip")
    client.wait_for("document.getElementById('swing-state').textContent.indexOf('Not reviewed') === 0",
                    label="the undo skip")
    state = client.evaluate(PAGE_STATE)
    checker.check("skip: undo puts the swing back in the queue",
                  state["formVisible"] is True and state["ordinal"] == skipped_ordinal,
                  f"{state['ordinal']} / {state['state']}")
    client.evaluate("""(() => { document.querySelector('[data-filter="skipped"]').click(); })()""")
    client.wait_for("!document.getElementById('empty').hidden", label="the empty skipped filter")
    state = client.evaluate(PAGE_STATE)
    checker.check("skip: the empty skipped filter says so",
                  state["emptyVisible"] is True and "Nothing skipped" in state["emptyTitle"],
                  state["emptyTitle"])
    checker.check("skip: no score or form is left visible in the empty state",
                  state["lockedVisible"] is False and client.evaluate(
                      "document.getElementById('stage').hidden") is True, "stage hidden")
    client.screenshot(out / "desktop-07-empty-filter.png", max_height=1600)
    click(client, "empty-all")
    client.wait_for("!document.getElementById('stage').hidden", label="the all filter")
    check_clean(client, checker, "skip")
    return state


def run_reload_flow(client: ui.DevTools, checker: Checker, out: Path, port: int) -> dict:
    """Reload as the reviewer who already rated a swing, then walk 30+ swings."""

    client.viewport(1440, 960, mobile=False)
    open_page(client, port)
    client.wait_for("document.getElementById('swing-state').textContent.indexOf('Submitted') === 0",
                    label="the previously submitted swing after reload")
    state = client.evaluate(PAGE_STATE)
    checker.check("reload: a saved submission renders after a reload",
                  state["lockedVisible"] is True and "6 / 10" in state["lockedText"], state["state"])
    client.screenshot(out / "desktop-08-reload-saved.png", max_height=1600)
    check_clean(client, checker, "reload")

    seen = 0
    for _ in range(40):  # walk the whole 32-swing batch and then some
        blocked = client.evaluate("document.getElementById('next').disabled")
        if blocked:
            break
        click_next(client)
        seen += 1
    state = client.evaluate(PAGE_STATE)
    checker.check("reload: navigation walks 30+ swings without a page error", seen >= 30 and not state["pageError"],
                  f"{seen} swings visited, position {state['position']}")
    check_clean(client, checker, "reload-walk")
    client.screenshot(out / "desktop-09-walked-batch.png", max_height=1600)
    return {"visited": seen, "position": state["position"]}


def run_tiny_batch(client: ui.DevTools, checker: Checker, out: Path, port: int, total: int) -> dict:
    """Rate the batch down to one unreviewed swing, then submit that last one in the UI."""

    client.viewport(1440, 960, mobile=False)
    open_page(client, port)
    # Rate every swing but the last through the API (this flow is about the last one in the UI).
    filled = client.evaluate("""(async () => {
      const token = document.querySelector('script[data-token]').dataset.token;
      const session = await (await fetch('/api/session')).json();
      const rest = session.items.slice(0, -1);
      for (const item of rest) {
        await fetch('/api/items/' + item.item_id + '/review', {
          method: 'POST', headers: {'Content-Type': 'application/json', 'X-Coach-Review-Token': token},
          body: JSON.stringify({ratings: {setup: 7, backswing: 7, top: 7, downswing: 7, overall: 7}, confidence: 'HIGH'})});
      }
      return rest.length;
    })()""")
    checker.check("tiny: the batch was filled down to the last swing", filled == total - 1, f"{filled} rated")
    open_page(client, port)
    client.evaluate("""(() => { document.querySelector('[data-filter="unreviewed"]').click(); })()""")
    client.wait_for("!document.getElementById('stage').hidden", label="the unreviewed filter")
    rate(client, {"setup": 7, "backswing": 7, "top": 7, "downswing": 7, "overall": 7}, "HIGH")
    submit(client)
    client.wait_for("!document.getElementById('empty').hidden", label="the completed state")
    state = client.evaluate(PAGE_STATE)
    checker.check("tiny: submitting the last unreviewed swing shows the completed state",
                  state["emptyVisible"] is True and "reviewed" in state["emptyTitle"].lower(),
                  state["emptyTitle"])
    checker.check("tiny: the stage is not left half-visible", client.evaluate(
        "document.getElementById('stage').hidden") is True, "stage hidden")
    client.evaluate("""(() => { document.querySelector('[data-filter="reviewed"]').click(); })()""")
    client.wait_for("!document.getElementById('locked').hidden", label="the reviewed filter")
    checker.check("tiny: the reviewed filter still shows the last swing",
                  client.evaluate(PAGE_STATE)["lockedVisible"] is True, "locked panel")
    client.screenshot(out / "desktop-10-completed.png", max_height=1600)
    check_clean(client, checker, "tiny")
    return state


def run_edit_mode(client: ui.DevTools, checker: Checker, out: Path, port: int) -> dict:
    client.viewport(1440, 960, mobile=False)
    open_page(client, port)
    visible = client.evaluate("document.body.innerText.indexOf('edit mode on') >= 0")
    checker.check("edit mode: the page says edit mode", visible is True, "banner text")

    rate(client, {"setup": 5, "backswing": 5, "top": 5, "downswing": 5, "overall": 5}, "HIGH")
    submit(client)
    client.wait_for("!document.getElementById('locked').hidden", label="the first submission")
    checker.check("edit mode: a blind submission still locks", client.evaluate(
        "document.getElementById('form').hidden") is True, "form hidden")
    click(client, "reveal")
    client.wait_for("!document.getElementById('score-panel').hidden", label="the score panel")

    click(client, "edit")
    client.wait_for("!document.getElementById('form').hidden", label="the edit form")
    state = client.evaluate(PAGE_STATE)
    checker.check("edit mode: Edit enters edit mode with the saved values prefilled",
                  state["formVisible"] is True and "overall=5" in state["picks"] and state["submitLabel"] == "Save new revision",
                  f"picks {state['picks']} / {state['submitLabel']}")
    checker.check("edit mode: Cancel edit is offered", state["cancelVisible"] is True, "cancel visible")
    client.screenshot(out / "desktop-11-edit-prefilled.png", max_height=1600)

    client.evaluate("document.querySelector('.rate[data-field=\"overall\"][data-value=\"2\"]').click()")
    click(client, "cancel-edit")
    state = client.evaluate(PAGE_STATE)
    checker.check("edit mode: cancel restores the saved values and leaves the revision alone",
                  state["lockedVisible"] is True and "overall=5" in state["picks"] and "revision 2" not in state["lockedText"],
                  f"picks {state['picks']}")

    click(client, "edit")
    client.wait_for("!document.getElementById('form').hidden", label="the edit form again")
    client.evaluate("document.querySelector('.rate[data-field=\"overall\"][data-value=\"2\"]').click()")
    submit(client)
    client.wait_for("document.getElementById('locked-when').textContent.indexOf('revision 2') >= 0", label="revision 2")
    state = client.evaluate(PAGE_STATE)
    checker.check("edit mode: an explicit save stores revision 2 as a post-reveal edit",
                  "revision 2" in state["lockedText"] and "post-reveal" in state["lockedText"]
                  and "2 / 10" in state["lockedText"], "revision 2 saved")
    client.screenshot(out / "desktop-12-edit-saved.png", max_height=1600)
    check_clean(client, checker, "edit mode")

    open_page(client, port)
    client.evaluate("""(() => { document.querySelector('[data-filter="reviewed"]').click(); })()""")
    client.wait_for("document.getElementById('locked-when').textContent.indexOf('revision 2') >= 0",
                    label="revision 2 after a reload")
    click(client, "edit")
    client.wait_for("!document.getElementById('form').hidden", label="the edit form after a reload")
    state = client.evaluate(PAGE_STATE)
    checker.check("edit mode: a reload keeps the edit and prefills it",
                  state["formVisible"] is True and "overall=2" in state["picks"], f"picks {state['picks']}")
    check_clean(client, checker, "edit mode reload")
    return {"picks": state["picks"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(C.DEFAULT_QA_DIR))
    parser.add_argument("--video", default=None, help="override the real clip used for browser QA")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)

    db = out / "review.sqlite3"
    clip = pick_qa_video(args.video)
    print(f"QA clip: {clip}")
    # 32 swings, each with its own copy of the clip (byte-identical media is refused by
    # the importer), a mix of usable scores, unavailable snapshots and no snapshot at all.
    clip_bytes = clip.read_bytes()
    big = []
    for index in range(1, 33):
        if index in (5, 17):
            score = None
        elif index % 3 == 2:
            score = fixtures.synthetic_score(available=False, excluded_ids=["impact.head", "top.posture"])
        else:
            score = fixtures.synthetic_score(total=8.6 if index % 3 == 0 else 6.1)
        # Distinct bytes per swing: the importer refuses byte-identical clips, and a
        # trailing marker keeps the real, playable clip intact for the browser.
        big.append({"swing_id": f"qa-{index:02d}", "club": "Driver", "score": score,
                    "video_bytes": clip_bytes + b"\nQA-SWING-" + f"{index:02d}".encode("ascii")})
    fixtures.build_db(db, BIG_BATCH, big, root=out / "media")

    checker = Checker()
    logs: dict = {"started_at": stamp, "batch": BIG_BATCH, "swings": len(big), "db": str(db),
                  "qa_clip": str(clip), "viewports": {}, "flows": {}}
    ports = {"desktop": ui.free_port(), "phone": ui.free_port(), "tiny": ui.free_port(), "editor": ui.free_port()}
    servers: list[subprocess.Popen] = []
    browser = None
    # The managed sandbox denies writes to the system temp directory, so the browser
    # profile lives inside this run's workspace folder.
    profile = out / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        servers.append(start_server(db, BIG_BATCH, "qa-desktop", ports["desktop"]))
        servers.append(start_server(db, BIG_BATCH, "qa-phone", ports["phone"]))
        servers.append(start_server(db, BIG_BATCH, "qa-tiny", ports["tiny"]))
        servers.append(start_server(db, BIG_BATCH, "qa-editor", ports["editor"], edit_mode=True))
        devtools_port = ui.free_port()
        browser = ui.launch_browser(ui.find_browser(), devtools_port, profile,
                                    extra_args=["--mute-audio", "--autoplay-policy=no-user-gesture-required"])
        ui.wait_for_devtools(devtools_port)
        client = ui.DevTools(ui.first_page_target(devtools_port)["webSocketDebuggerUrl"])
        client.send("Page.enable")
        client.send("Runtime.enable")
        client.send("Page.addScriptToEvaluateOnNewDocument", {"source": COLLECT_ERRORS})
        for label, width, height, mobile in (("desktop", 1440, 960, False), ("phone", 390, 844, True)):
            logs["viewports"][label] = run_flow(f"viewport-{label}", checker, out, client, run_viewport,
                                                client, checker, out, label, width, height, mobile, ports[label])
        logs["flows"]["skip"] = run_flow("skip", checker, out, client, run_skip_flow, client, checker, out,
                                         ports["desktop"])
        # Same server and reviewer as the desktop pass: this is a genuine reload of a saved session.
        logs["flows"]["reload"] = run_flow("reload", checker, out, client, run_reload_flow, client, checker, out,
                                           ports["desktop"])
        logs["flows"]["tiny"] = run_flow("tiny", checker, out, client, run_tiny_batch, client, checker, out,
                                         ports["tiny"], len(big))
        logs["flows"]["edit_mode"] = run_flow("edit-mode", checker, out, client, run_edit_mode, client, checker, out,
                                              ports["editor"])
        client.close()
    finally:
        for process in (browser, *servers):
            if process is not None:
                process.terminate()
        shutil.rmtree(profile, ignore_errors=True)

    logs["failures"] = checker.failures
    logs["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out / "qa-log.json").write_text(json.dumps(logs, indent=2, default=str), encoding="utf-8")
    summary = ["# Coach review browser QA", "",
               f"Run {stamp} against the synthetic batch `{BIG_BATCH}` ({len(big)} swings) in `{db}`. "
               "The real validation database was not used.", "",
               f"Screenshots and `qa-log.json`: {out}", "", "## Checks", ""]
    summary += [f"* FAIL {failure}" for failure in checker.failures] or ["* All checks passed."]
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"\n{'PASS' if not checker.failures else 'FAIL'}: {len(checker.failures)} failure(s)")
    print(f"artifacts: {out}")
    return 1 if checker.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
