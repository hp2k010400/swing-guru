"""Build the human review pack for the robustness batch (read-only; never changes the engine).

    .venv\\Scripts\\python.exe scripts\\human_review_pack.py
    .venv\\Scripts\\python.exe scripts\\serve_review.py      # then open the printed URL

Reads the batch output in ``var/qa/test_swings/`` and writes ``var/qa/human_review/``:

* ``index.html`` - one card per real down-the-line swing, plus the excluded files;
* ``<swing>/watch.html`` - the PRIMARY review: the real guided analysis player
  (the engine's viewer, large, starting by itself) with the review form beside it;
* ``<swing>/index.html`` - details: measurements, camera check and the engineering
  frame review (six stills with the debug skeleton - QA only, not the golfer's view);
* ``<swing>/review_sheet.jpg`` - Address .. Impact with body, shaft and
  tush-line overlays, confidence and camera validity on every frame.

Answers are saved by ``serve_review.py`` to ``var/qa/human_review/reviews.json``.
Nothing reads that file back into the engine.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import batch_validate as bv  # noqa: E402

VAR = ROOT / "var"
BATCH = VAR / "qa" / "test_swings"
OUT = VAR / "qa" / "human_review"
# Files in the batch that are not a golfer's own down-the-line swing (see FINDINGS_baseline.md).
EXCLUDED = {
    "03": "Not a usable swing: mostly an empty bay, the golfer is at or beyond the left edge of the picture.",
    "05": "Not a usable swing: a screen recording of an edited social-media montage of a different player.",
}
PELVIS_PHASES = ("top", "downswing", "impact")
CAMERA_COLOUR = {"VALID": (70, 170, 70), "UNCERTAIN": (40, 170, 230), "INVALID": (60, 60, 220)}


def url(path: Path) -> str:
    """Server URL for a file under ``var`` (the review server's root)."""

    return "/" + quote(path.resolve().relative_to(VAR.resolve()).as_posix())


def load(folder: Path) -> dict | None:
    report_path, analysis_path = folder / "report.json", folder / "bundle" / "analysis.json"
    if not report_path.exists() or not analysis_path.exists():
        return None
    window = json.loads((folder / "swing_window.json").read_text(encoding="utf-8"))["window"]
    return {
        "folder": folder,
        "report": json.loads(report_path.read_text(encoding="utf-8")),
        "analysis": json.loads(analysis_path.read_text(encoding="utf-8")),
        "window": window,
        "video": (folder / "window.mp4") if window.get("trimmed") else (VAR / "test_swings" / window["analysed_file"]),
        "source": VAR / "test_swings" / json.loads(report_path.read_text(encoding="utf-8"))["video"],
    }


# ---------------------------------------------------------------------------
# Plain-language facts shared by the sheet and the page
# ---------------------------------------------------------------------------


def camera_summary(analysis: dict) -> tuple[str, str]:
    """(status, one plain sentence)."""

    check = analysis["camera_check"]
    status = check["status"]
    motion = (check.get("signals") or {}).get("camera_motion") or {}
    if not check.get("signals") or "address" in check.get("signals", {}):
        return status, "Camera could not be checked (no Address moment was found)."
    if motion.get("status") == "INVALID":
        worst = max((c.get("shift_px") or 0) for c in motion.get("comparisons", {}).values()) if motion.get("comparisons") else 0
        return status, f"Camera moved during the swing (about {worst:.0f} px). Position measurements are withheld."
    if motion.get("status") == "UNCERTAIN":
        return status, "Camera movement could not be measured reliably."
    others = [n for n, s in check["signals"].items() if s.get("status") != "VALID" and n != "camera_motion"]
    if others:
        return status, "Camera did not move, but: " + ", ".join(n.replace("_", " ") for n in others) + " not fully valid."
    return status, "Camera did not move; the view looks like a usable down-the-line view."


def withheld(analysis: dict) -> list[str]:
    return [f"{h['phase'].replace('downswing', 'mid-down').replace('_', '-').title()}: "
            f"{h.get('title', h['kind'])} - {h['reason']}" for h in analysis.get("hidden", [])]


# ---------------------------------------------------------------------------
# Review sheet
# ---------------------------------------------------------------------------


def review_sheet(item: dict, out: Path) -> None:
    analysis, report = item["analysis"], item["report"]
    width, height = int(analysis["width"]), int(analysis["height"])
    records = {int(r.index): r for r in bv.load_records_any(item["folder"] / "bundle" / "pose_records.pkl")} \
        if hasattr(bv, "load_records_any") else None
    if records is None:
        from swingguru.pose_cache import load_records_any

        records = {int(r.index): r for r in load_records_any(item["folder"] / "bundle" / "pose_records.pkl")}
    by_phase = {p["phase"]: p for p in analysis["phases"]}
    frames = bv.read_frames(item["video"], {p["frame_index"] for p in analysis["phases"] if p["frame_index"] is not None})
    depth = analysis.get("pelvis_depth") or {}
    tush_x = depth.get("tush_line_x")
    camera_status, camera_text = camera_summary(analysis)
    moved = ((analysis["camera_check"].get("signals") or {}).get("camera_motion") or {}).get("status") == "INVALID"
    tile_h = 760
    scale = tile_h / height
    tile_w = int(round(width * scale))
    tiles = []
    for key, label in bv.PHASES:
        phase = by_phase.get(key, {})
        index = phase.get("frame_index")
        image = frames.get(index) if index is not None else None
        if image is None:
            tile = np.full((tile_h, tile_w, 3), 45, np.uint8)
            cv2.putText(tile, f"{label}: not found", (12, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
            tiles.append(tile)
            continue
        if image.shape[:2] != (height, width):
            image = cv2.resize(image, (width, height))
        tile = cv2.resize(image, (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        record = records.get(index)

        def p(point):
            return int(round(float(point[0]) * scale)), int(round(float(point[1]) * scale))

        usable_groups = 0
        if record is not None:
            pixels = record.points.pixels
            for a, b in bv.SKELETON:
                if a in pixels and b in pixels:
                    ok = bv.joint_usable(record, a, width, height) and bv.joint_usable(record, b, width, height)
                    cv2.line(tile, p(pixels[a]), p(pixels[b]), (90, 220, 90) if ok else (60, 60, 230), 2, cv2.LINE_AA)
            for names in bv.JOINT_GROUPS.values():
                for name in names:
                    if name in pixels:
                        ok = bv.joint_usable(record, name, width, height)
                        cv2.circle(tile, p(pixels[name]), 4, (90, 220, 90) if ok else (60, 60, 230), -1, cv2.LINE_AA)
            usable_groups = sum(1 for ok in bv.pose_table(record, width, height).values() if ok)
        shaft = next((c for c in phase.get("checks", []) if c["kind"] == "shaft"), None)
        if shaft:
            line = next(g for g in shaft["geometry"] if g.get("role") == "shaft")
            (x1, y1), (x2, y2) = line["points"]
            colour = (0, 200, 255) if shaft.get("source") == "classical" else (255, 110, 255)
            cv2.line(tile, (int(x1 * tile_w), int(y1 * tile_h)), (int(x2 * tile_w), int(y2 * tile_h)), colour, 3, cv2.LINE_AA)
        rear_text = None
        if tush_x is not None and key in ("address",) + PELVIS_PHASES:
            x = int(round(tush_x * scale))
            for y in range(0, tile_h, 14):
                cv2.line(tile, (x, y), (x, y + 7), (255, 255, 255), 2, cv2.LINE_AA)
            rear = depth["address"] if key == "address" else (depth["phases"].get(key) or {}).get("rear")
            if rear and rear.get("point"):
                cv2.circle(tile, p(rear["point"]), 8, (30, 140, 255), -1, cv2.LINE_AA)
                cv2.circle(tile, p(rear["point"]), 7, (255, 255, 255), 1, cv2.LINE_AA)
            if key in PELVIS_PHASES:
                info = depth["phases"].get(key) or {}
                pct = info.get("displacement_pct_height")
                rear_text = ("Rear pelvis: withheld (camera moved)" if moved else
                             f"Rear pelvis {pct:+.1f}% ({info.get('level')})" if pct is not None else "Rear pelvis: not measured")
            else:
                rear_text = "Tush line set here" if (depth.get("address") or {}).get("available") else "Tush line: not set"
        estimate = report["phases"][key]
        club = report["club"][key]
        lines = [
            f"{label}  {estimate['time_s']}s  (frame {estimate['source_frame']})",
            f"Moment confidence {estimate['confidence']:.2f}" if estimate.get("confidence") is not None else "Moment confidence -",
            (f"Shaft {club['angle_deg']:.0f} deg  {club['level']}  ({'AI fallback' if club['source'] == 'owlv2-fallback' else 'classic'})"
             if club["detected"] else "Shaft: not detected"),
            f"Body: {usable_groups}/7 joint groups tracked",
        ]
        if rear_text:
            lines.append(rear_text)
        top = 34 + 28 * len(lines)
        cv2.rectangle(tile, (0, 0), (tile_w, top), (25, 25, 25), -1)
        colour = CAMERA_COLOUR.get(camera_status, (120, 120, 120))
        cv2.rectangle(tile, (0, 0), (tile_w, 30), colour, -1)
        cv2.putText(tile, f"CAMERA {camera_status}" + ("  (moved)" if moved else ""), (10, 21),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        for i, text in enumerate(lines):
            cv2.putText(tile, text, (10, 56 + 28 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.62 if i == 0 else 0.55,
                        (255, 255, 255), 2 if i == 0 else 1, cv2.LINE_AA)
        tiles.append(tile)
    sheet = np.vstack([np.hstack(tiles[:3]), np.hstack(tiles[3:])])
    header = np.full((96, sheet.shape[1], 3), 248, np.uint8)
    w = item["window"]
    cv2.putText(header, report["video"], (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)
    cv2.putText(header, f"Swing window {w['start_s']:.2f}-{w['end_s']:.2f} s of {report['general']['duration_s']} s"
                f" ({'trimmed automatically' if w.get('trimmed') else 'whole clip'})  |  {camera_text}",
                (12, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    cv2.putText(header, f"{len(withheld(analysis))} measurement(s) withheld", (12, 86),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (40, 40, 40), 1, cv2.LINE_AA)
    legend = np.full((44, sheet.shape[1], 3), 248, np.uint8)
    x = 12
    for colour, text, kind in (((90, 220, 90), "body joint tracked", "dot"), ((60, 60, 230), "body joint unsure", "dot"),
                               ((0, 200, 255), "shaft (classic)", "line"), ((255, 110, 255), "shaft (AI fallback)", "line"),
                               ((120, 120, 120), "tush line (dashed)", "dash"), ((30, 140, 255), "back of pelvis", "dot")):
        if kind == "dot":
            cv2.circle(legend, (x + 8, 22), 7, colour, -1, cv2.LINE_AA)
        elif kind == "line":
            cv2.line(legend, (x, 22), (x + 22, 22), colour, 4, cv2.LINE_AA)
        else:
            for dx in (0, 12):
                cv2.line(legend, (x + dx, 22), (x + dx + 7, 22), colour, 3, cv2.LINE_AA)
        cv2.putText(legend, text, (x + 28, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (40, 40, 40), 1, cv2.LINE_AA)
        x += 46 + 10 * len(text)
    cv2.imwrite(str(out), np.vstack([header, sheet, legend]), [cv2.IMWRITE_JPEG_QUALITY, 90])


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

STYLE = """
:root { --bg:#f6f4ef; --card:#fff; --ink:#1f2421; --muted:#5d665f; --line:#e3dfd6; --accent:#2f6b4f;
        --ok:#2f7d4f; --warn:#b7791f; --bad:#b83a3a; }
@media (prefers-color-scheme: dark) { :root { --bg:#161917; --card:#202522; --ink:#eef1ee; --muted:#a4ada6;
        --line:#343b36; --accent:#6fbf94; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:16px/1.5 system-ui, -apple-system, Segoe UI, sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; } h2 { font-size: 19px; margin: 28px 0 10px; }
p.lead { color: var(--muted); margin: 0 0 20px; }
a { color: var(--accent); }
.grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden;
        text-decoration:none; color:inherit; display:flex; flex-direction:column; }
.card img { width:100%; aspect-ratio: 4/3; object-fit: cover; object-position: top; background:#000; }
.card .body { padding: 12px 14px 14px; display:flex; flex-direction:column; gap:6px; }
.card h3 { font-size: 16px; margin:0; }
.pill { display:inline-block; font-size:12px; font-weight:600; padding:2px 8px; border-radius:999px; border:1px solid var(--line); }
.VALID, .PASS, .correct { color: var(--ok); } .UNCERTAIN, .PARTIAL { color: var(--warn); } .INVALID, .FAIL, .wrong { color: var(--bad); }
.reviewed { background: var(--accent); color:#fff; border-color: var(--accent); }
.meta { color: var(--muted); font-size: 14px; }
.panel { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:16px; margin-top:16px; }
video { width:100%; max-height:70vh; background:#000; border-radius:10px; }
.sheet img { width:100%; border-radius:10px; border:1px solid var(--line); }
table { width:100%; border-collapse: collapse; font-size: 14px; }
td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); vertical-align: top; }
.two { display:grid; grid-template-columns: 1fr 1fr; gap:16px; } @media (max-width: 800px) { .two { grid-template-columns: 1fr; } }
fieldset { border:1px solid var(--line); border-radius:10px; margin: 0 0 12px; padding: 10px 12px; }
legend { font-weight: 600; padding: 0 6px; }
fieldset label { display:inline-flex; align-items:center; gap:6px; margin: 4px 14px 4px 0; cursor:pointer; }
.phases label { font-size: 14px; }
textarea { width:100%; min-height: 90px; font: inherit; padding: 8px; border-radius: 8px; border:1px solid var(--line);
           background: var(--bg); color: var(--ink); }
button { font: inherit; font-weight:600; padding: 10px 18px; border-radius: 10px; border: 0; background: var(--accent);
         color: #fff; cursor:pointer; } button.secondary { background: transparent; color: var(--accent); border:1px solid var(--accent); }
.status { margin-left: 12px; color: var(--muted); }
.links a { display:inline-block; margin: 0 16px 8px 0; font-weight: 600; }
.help { color: var(--muted); font-size: 14px; margin: 2px 0 8px; }
"""

REVIEW_JS = """
const API = '/qa/human_review/api/reviews';
async function loadAll() {
  try { const r = await fetch(API, {cache: 'no-store'}); if (r.ok) return await r.json(); } catch (e) {}
  try { return JSON.parse(localStorage.getItem('swingguru-reviews') || '{}'); } catch (e) { return {}; }
}
async function saveOne(slug, review) {
  const all = await loadAll(); all[slug] = review;
  try { localStorage.setItem('swingguru-reviews', JSON.stringify(all)); } catch (e) {}
  try {
    const r = await fetch(API, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({slug, review})});
    return r.ok;
  } catch (e) { return false; }
}
"""


OVERALL = {"trust": ("PASS", "I would trust this analysis"),
           "mostly": ("PARTIAL", "I mostly trust it"),
           "not": ("FAIL", "I would not trust it")}
VIEWER_FILES = ("index.html", "app.css", "app.js")

EXTRA_STYLE = """
.card .thumb { display:block; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:6px; }
.btn { display:inline-block; font-weight:600; padding:10px 16px; border-radius:10px; text-decoration:none;
       background:var(--accent); color:#fff; } .btn.secondary { background:transparent; color:var(--accent); border:1px solid var(--accent); }
.btn.big { font-size:18px; padding:14px 22px; }
.watch { display:grid; grid-template-columns: 440px minmax(320px, 1fr); gap:24px; align-items:start; }
.watch iframe { width:100%; height: calc(100vh - 120px); min-height: 640px; border:0; border-radius:18px; background:#f4f2ed;
                box-shadow: 0 1px 2px rgba(0,0,0,.05), 0 10px 30px rgba(0,0,0,.07); }
.watch .side { position: sticky; top: 12px; max-height: calc(100vh - 24px); overflow:auto; }
@media (max-width: 900px) { .watch { grid-template-columns: 1fr; } .watch iframe { height: 100svh; min-height: 560px; }
  main.wide h1 { font-size: 21px; } main.wide p.lead { font-size: 14px; }
                            .watch .side { position: static; max-height:none; } }
main.wide { max-width: 1500px; }
.qa-note { background: var(--bg); border:1px dashed var(--line); border-radius:10px; padding:8px 12px; font-size:14px; color:var(--muted); }
"""


def viewer_url(item: dict, autoplay: bool = True) -> str:
    return url(item["folder"] / "bundle" / "index.html") + "?review=1" + ("&autoplay=1" if autoplay else "")


def card(item: dict) -> str:
    r, a = item["report"], item["analysis"]
    slug = r["slug"]
    camera_status, camera_text = camera_summary(a)
    w = item["window"]
    return f"""
<div class="card" data-slug="{html.escape(slug)}">
  <a class="thumb" href="{html.escape(slug)}/watch.html"><img src="{html.escape(slug)}/thumb.jpg"
     alt="Swing {slug[:2]} at Address" loading="lazy"></a>
  <div class="body">
    <h3>Swing {slug[:2]} <span class="pill review-state">Not reviewed</span></h3>
    <div class="meta">{html.escape(r['video'])} · swing at {w['start_s']:.1f}-{w['end_s']:.1f} s</div>
    <div><span class="pill {camera_status}">Camera {camera_status}</span>
         <span class="pill">{r['phases_found']}/6 phases</span>
         <span class="pill">{len(withheld(a))} withheld</span></div>
    <div class="meta">{html.escape(camera_text)}</div>
    <div class="actions"><a class="btn" href="{html.escape(slug)}/watch.html">&#9654; Watch Swing Guru analysis</a>
      <a class="btn secondary" href="{html.escape(slug)}/index.html">Details &amp; engineering frames</a></div>
  </div>
</div>"""


def index_page(items: list[dict], excluded: list[tuple[str, str, str]]) -> str:
    cards = "\n".join(card(item) for item in items)
    skipped = "".join(f"<li><b>Swing {k}</b> ({html.escape(name)}): {html.escape(reason)}</li>" for k, name, reason in excluded)
    labels = json.dumps({k: v[1] for k, v in OVERALL.items()})
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Swing Guru review</title>
<style>{STYLE}{EXTRA_STYLE}</style></head><body><main>
<h1>Swing Guru - your review</h1>
<p class="lead">{len(items)} real down-the-line swings. For each one press <b>Watch Swing Guru analysis</b>: the real swing
video plays and pauses by itself at each moment, with the measurement drawn on the golfer and the result card updating -
exactly what a golfer would see. Then answer a few questions next to the player. Your answers are saved to a file on this
computer; nothing changes in Swing Guru.</p>
<p class="meta"><span id="progress">0</span> of {len(items)} reviewed ·
<a href="/qa/human_review/api/reviews" download="reviews.json">Download all my answers (JSON)</a></p>
<div class="grid">{cards}</div>
<h2>Not included</h2><ul class="meta">{skipped}</ul>
<h2>About the "engineering frames"</h2>
<p class="meta">The six-frame sheets (card pictures and the Details page) are an engineering check, not the golfer's view:
green body lines = joint tracked, red = unsure; yellow = club shaft (classic method), pink = club shaft (AI fallback); dashed
white = tush line (back of the pelvis at Address), orange dot = back of the pelvis in that frame. Camera VALID = the phone
stayed still; INVALID (moved) = it moved, so hip, head and tush-line numbers are held back.</p>
</main><script>{REVIEW_JS}
const LABELS = {labels};
loadAll().then(all => {{
  let n = 0;
  document.querySelectorAll('.card').forEach(c => {{
    const r = all[c.dataset.slug]; const pill = c.querySelector('.review-state');
    if (r && r.overall) {{ n++; pill.textContent = 'Reviewed: ' + (LABELS[r.overall] || r.overall); pill.classList.add('reviewed'); }}
  }});
  document.getElementById('progress').textContent = n;
}});
</script></body></html>"""


def radio(name: str, options: list[tuple[str, str]]) -> str:
    return "".join(f'<label><input type="radio" name="{name}" value="{v}"> {html.escape(t)}</label>' for v, t in options)


def checks(name: str, options: list[tuple[str, str]]) -> str:
    return "".join(f'<label><input type="checkbox" name="{name}" value="{v}"> {html.escape(t)}</label>' for v, t in options)


def phase_boxes(name: str) -> str:
    return '<div class="phases help">Which moments? ' + checks(name, list(bv.PHASES)) + "</div>"


def review_form(slug: str) -> str:
    """The same form on the watch page and the details page (one saved answer per swing)."""

    overall = json.dumps({k: v[0] for k, v in OVERALL.items()})
    return f"""
<form class="panel" id="review" style="margin-top:0">
  <h2 style="margin-top:0">Your review</h2>
  <p class="help">Judge only what you can see. Saved on this computer; it does not change Swing Guru.</p>
  <fieldset><legend>1. Guided playback</legend>
    {checks('guided', [('correct', 'Correct'), ('timing_wrong', 'Timing wrong'), ('overlay_wrong', 'Overlay wrong'),
                       ('jerky', 'Playback jerky')])}
    <div class="help">Tick every problem you saw, or just Correct.</div></fieldset>
  <fieldset><legend>2. Phase frames - is each pause the right moment?</legend>
    {radio('phases', [('correct', 'Correct'), ('wrong', 'Wrong')])}{phase_boxes('phases_wrong')}</fieldset>
  <fieldset><legend>3. Shaft line - on the club?</legend>
    {radio('shaft', [('correct', 'Correct'), ('wrong', 'Wrong'), ('missing', 'Missing')])}{phase_boxes('shaft_frames')}</fieldset>
  <fieldset><legend>4. Body tracking - lines on the right body parts?</legend>
    {radio('body', [('correct', 'Correct'), ('wrong', 'Wrong')])}{phase_boxes('body_wrong')}</fieldset>
  <fieldset><legend>5. Tush line - dashed line at the back of the pelvis at Address?</legend>
    {radio('tush', [('correct', 'Correct'), ('wrong', 'Wrong'), ('unavailable', 'Unavailable / not shown')])}</fieldset>
  <fieldset><legend>6. Overall</legend>
    {radio('overall', [(k, v[1]) for k, v in OVERALL.items()])}</fieldset>
  <fieldset><legend>Notes</legend><textarea name="notes" placeholder="Anything you noticed"></textarea></fieldset>
  <button type="submit">Save my review</button><span class="status" id="status"></span>
</form>
<script>{REVIEW_JS}
const SLUG = {json.dumps(slug)};
const OVERALL_STATUS = {overall};
const form = document.getElementById('review');
function read() {{
  const d = new FormData(form); const out = {{}};
  for (const k of ['phases', 'shaft', 'body', 'tush', 'overall']) out[k] = d.get(k);
  for (const k of ['guided', 'phases_wrong', 'shaft_frames', 'body_wrong']) out[k] = d.getAll(k);
  out.overall_status = OVERALL_STATUS[out.overall] || null;
  out.notes = d.get('notes') || ''; out.saved_at = new Date().toISOString(); return out;
}}
function fill(r) {{
  if (!r) return;
  for (const [k, v] of Object.entries(r)) {{
    if (Array.isArray(v)) v.forEach(x => {{ const el = form.querySelector(`input[name="${{k}}"][value="${{x}}"]`); if (el) el.checked = true; }});
    else if (k === 'notes') form.notes.value = v;
    else {{ const el = form.querySelector(`input[name="${{k}}"][value="${{v}}"]`); if (el) el.checked = true; }}
  }}
  document.getElementById('status').textContent = r.saved_at ? 'Last saved ' + new Date(r.saved_at).toLocaleString() : '';
}}
loadAll().then(all => fill(all[SLUG]));
form.addEventListener('submit', async e => {{
  e.preventDefault();
  const review = read();
  if (!review.overall) {{ document.getElementById('status').textContent = 'Please choose an overall answer (question 6).'; return; }}
  const ok = await saveOne(SLUG, review);
  document.getElementById('status').textContent = ok ? 'Saved \\u2713' : 'Saved in this browser only (review server not running)';
}});
</script>"""


def nav(previous: str | None, following: str | None, page: str) -> str:
    return " · ".join(x for x in (
        f'<a href="../{previous}/{page}">&larr; previous swing</a>' if previous else "",
        '<a href="../index.html">all swings</a>',
        f'<a href="../{following}/{page}">next swing &rarr;</a>' if following else "") if x)


def watch_page(item: dict, previous: str | None, following: str | None) -> str:
    r, a, w = item["report"], item["analysis"], item["window"]
    slug = r["slug"]
    camera_status, camera_text = camera_summary(a)
    held = withheld(a)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Swing {slug[:2]} - watch the analysis</title>
<style>{STYLE}{EXTRA_STYLE}</style></head><body><main class="wide">
<p class="meta">{nav(previous, following, 'watch.html')} · <a href="index.html">details &amp; engineering frames</a></p>
<h1>Swing {slug[:2]} - Swing Guru analysis</h1>
<p class="lead">{html.escape(r['video'])} · swing found at {w['start_s']:.2f}-{w['end_s']:.2f} s
({'trimmed automatically' if w.get('trimmed') else 'whole clip'}) · <span class="{camera_status}"><b>Camera {camera_status}</b></span>
- {html.escape(camera_text)}{f' {len(held)} measurement(s) held back.' if held else ''}</p>
<div class="watch">
  <div>
    <iframe id="player" src="{viewer_url(item)}" title="Swing Guru guided analysis" allow="autoplay; fullscreen"></iframe>
    <div class="actions">
      <button type="button" id="restart">&#8635; Restart the analysis</button>
      <a class="btn secondary" href="{viewer_url(item)}" target="_blank">Open the player on its own</a>
    </div>
    <p class="help">It starts by itself. Space or the play button pauses; the arrows step between checks; the list button
    opens the results. Nothing here is a still image - it is the real video, paused at each moment.</p>
  </div>
  <div class="side">{review_form(slug)}</div>
</div>
<script>
// Fit the player to the window below the header, so the controls are always on screen.
function fit() {{
  const f = document.getElementById('player');
  if (window.innerWidth <= 900) {{ f.style.height = ''; return; }}
  f.style.height = Math.max(560, window.innerHeight - f.getBoundingClientRect().top - window.scrollY - 12) + 'px';
}}
window.addEventListener('resize', fit); fit();
document.getElementById('restart').addEventListener('click', () => {{
  const f = document.getElementById('player'); f.src = f.src;
}});
</script>
</main></body></html>"""


def swing_page(item: dict, previous: str | None, following: str | None) -> str:
    r, a, w = item["report"], item["analysis"], item["window"]
    slug = r["slug"]
    camera_status, camera_text = camera_summary(a)
    rows = []
    for phase in a["phases"]:
        for c in phase["checks"]:
            source = {"owlv2-fallback": " (AI fallback)", "classical": " (classic)"}.get(c.get("source", ""), "")
            rows.append(f"<tr><td>{html.escape(phase['label'])}</td><td>{html.escape(c['title'])}</td>"
                        f"<td>{html.escape(c['value'])} {html.escape(c.get('qualifier') or '')}</td>"
                        f"<td>{html.escape(c.get('summary') or '')}</td><td>{c['level']}{source}</td></tr>")
    hidden_rows = "".join(f"<li>{html.escape(t)}</li>" for t in withheld(a)) or "<li>Nothing withheld.</li>"
    interp = a.get("interpretations") or {}
    said = [x["user_text"] for x in interp.get("results", []) if x["status"] in ("described", "observed", "fired") and x.get("user_text")]
    said_html = "".join(f"<li>{html.escape(t)}</li>" for t in said) or "<li>No statements.</li>"
    signals = "".join(f"<tr><td>{html.escape(n.replace('_', ' '))}</td><td class='{s.get('status')}'>{s.get('status')}</td>"
                      f"<td>{html.escape(str(s.get('value', s.get('note', ''))))}</td></tr>"
                      for n, s in (a["camera_check"].get("signals") or {}).items())
    phases_found = ", ".join(f"{p['label']} {r['phases'][p['phase']]['time_s']} s" for p in a["phases"]
                             if p["frame_index"] is not None)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Swing {slug[:2]} - details</title>
<style>{STYLE}{EXTRA_STYLE}</style></head><body><main>
<p class="meta">{nav(previous, following, 'index.html')}</p>
<h1>Swing {slug[:2]}</h1>
<p class="lead">{html.escape(r['video'])} · swing found at {w['start_s']:.2f}-{w['end_s']:.2f} s of
{r['general']['duration_s']} s ({'trimmed automatically' if w.get('trimmed') else 'whole clip used'})
· <span class="{camera_status}"><b>Camera {camera_status}</b></span> - {html.escape(camera_text)}</p>
<p><a class="btn big" href="watch.html">&#9654; Watch Swing Guru analysis</a></p>

<div class="two">
  <div class="panel"><b>Moments found</b><p class="meta">{html.escape(phases_found) or 'None'}</p>
    <b>What Swing Guru would say</b><ul>{said_html}</ul></div>
  <div class="panel"><b>Held back</b><ul class="meta">{hidden_rows}</ul></div>
</div>

<h2>Engineering frame review</h2>
<p class="qa-note">For engineering QA only - this is <b>not</b> what a golfer sees. Six still frames with the full debug body
skeleton and every overlay at once. The golfer's experience is the guided player above.</p>
<div class="sheet panel"><a href="review_sheet.jpg" target="_blank"><img src="review_sheet.jpg"
  alt="Engineering frames: Address, Takeaway, Mid-Back, Top, Mid-Down and Impact with debug overlays"></a></div>
<div class="links"><a href="{url(item['video'])}" target="_blank">Raw swing video (no analysis){' - trimmed' if w.get('trimmed') else ''}</a>
  <a href="{url(item['source'])}" target="_blank">Original clip</a>
  <a href="{viewer_url(item, autoplay=False)}&amp;dev=1" target="_blank">Player in developer mode</a></div>

{review_form(slug)}

<h2>Current measurements</h2>
<div class="panel"><table><tr><th>Moment</th><th>Measurement</th><th>Value</th><th>Detail</th><th>Confidence</th></tr>
{''.join(rows) or '<tr><td colspan=5>No measurements.</td></tr>'}</table></div>
<h2>Camera check</h2>
<div class="panel"><table><tr><th>Signal</th><th>Status</th><th>Value</th></tr>{signals}</table></div>
<p class="meta">{nav(previous, following, 'index.html')}</p>
</main></body></html>"""


def thumbnail(item: dict, out: Path) -> None:
    """A plain Address frame from the swing video (no overlays) for the index card."""

    address = next((p["frame_index"] for p in item["analysis"]["phases"] if p["phase"] == "address"), None)
    frames = bv.read_frames(item["video"], {address if address is not None else 0})
    image = next(iter(frames.values()), None)
    if image is None:
        return
    h, w = image.shape[:2]
    band = min(h, int(w * 0.75))  # a 4:3 band around the golfer's middle
    top = max(0, min(h - band, int(h * 0.55) - band // 2))
    cv2.imwrite(str(out), cv2.resize(image[top:top + band], (480, 360), interpolation=cv2.INTER_AREA),
                [cv2.IMWRITE_JPEG_QUALITY, 85])


def refresh_viewer(item: dict) -> None:
    """Copy the current player into the bundle (its analysis.json is untouched)."""

    import shutil

    viewer = ROOT / "src" / "swingguru" / "viewer"
    for name in VIEWER_FILES:
        shutil.copyfile(viewer / name, item["folder"] / "bundle" / name)
    # Golfer-facing verdicts are a presentation layer; refresh them in place.
    from refresh_verdicts import refresh

    item["analysis"] = refresh(item["folder"] / "bundle" / "analysis.json")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    items, excluded = [], []
    for folder in sorted(p for p in BATCH.iterdir() if p.is_dir()):
        key = folder.name[:2]
        report_path = folder / "report.json"
        if key in EXCLUDED:
            name = json.loads(report_path.read_text(encoding="utf-8"))["video"] if report_path.exists() else folder.name
            excluded.append((key, name, EXCLUDED[key]))
            continue
        item = load(folder)
        if item is None:
            excluded.append((key, folder.name, "No analysis output found."))
            continue
        items.append(item)
    slugs = [item["report"]["slug"] for item in items]
    for i, item in enumerate(items):
        slug = slugs[i]
        page_dir = OUT / slug
        page_dir.mkdir(parents=True, exist_ok=True)
        refresh_viewer(item)
        thumbnail(item, page_dir / "thumb.jpg")
        if "--pages-only" not in sys.argv:
            review_sheet(item, page_dir / "review_sheet.jpg")
        previous, following = (slugs[i - 1] if i else None), (slugs[i + 1] if i + 1 < len(slugs) else None)
        (page_dir / "index.html").write_text(swing_page(item, previous, following), encoding="utf-8")
        (page_dir / "watch.html").write_text(watch_page(item, previous, following), encoding="utf-8")
        print("built", slug, flush=True)
    (OUT / "index.html").write_text(index_page(items, excluded), encoding="utf-8")
    print(f"{len(items)} swings, {len(excluded)} excluded -> {OUT / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
