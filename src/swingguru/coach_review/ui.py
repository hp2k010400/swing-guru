"""The review page: one HTML file, one stylesheet, one script. No inline script.

Video first, warm golf palette, responsive from phone width up. The page knows
only what the API gives it: an opaque item id, its position, the club and the
review state. Selecting a swing fetches that swing's detail (which carries the
saved rating once there is one); the score arrives only after a submission.
"""

from __future__ import annotations

import html
import json

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Swing review - __BATCH__</title>
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header class="bar">
  <div class="bar-main">
    <div>
      <p class="eyebrow">Swing Guru &middot; blind coach review</p>
      <h1>__BATCH__</h1>
      <p class="sub">Reviewer <strong>__REVIEWER__</strong>__EDITMODE__</p>
    </div>
    <div class="progress">
      <span class="progress-label">Reviewed</span>
      <span class="progress-value" id="progress-value">0 / 0</span>
      <span class="progress-meta" id="progress-meta"></span>
    </div>
  </div>
  <div class="bar-filters">
    <div class="segmented" role="group" aria-label="Filter swings">
      <button type="button" data-filter="all" class="is-on">All</button>
      <button type="button" data-filter="unreviewed">Unreviewed</button>
      <button type="button" data-filter="reviewed">Reviewed</button>
      <button type="button" data-filter="skipped">Skipped</button>
    </div>
    <div class="nav">
      <button type="button" id="prev" title="Previous swing (left arrow)">&#8592; Previous</button>
      <span class="position" id="position"></span>
      <button type="button" id="next" title="Next swing (right arrow)">Next &#8594;</button>
    </div>
  </div>
</header>

<main>
  <section class="stage" id="stage" aria-live="polite">
    <div class="panel video-panel">
      <div class="video-head">
        <span class="pill" id="swing-ordinal">Swing</span>
        <span class="pill club" id="swing-club"></span>
        <span class="pill state" id="swing-state"></span>
      </div>
      <video id="video" controls playsinline preload="metadata"></video>
      <p class="video-help">Watch the whole swing, replay it as often as you like, then rate it from memory of the
      movement - not from the swing positions the app measures.</p>
    </div>

    <div class="side">
      <form class="panel form" id="form" autocomplete="off">
        <h2>Your ratings</h2>
        <p class="help">Whole numbers 1-10. Nothing is pre-filled.</p>
        <div id="rating-rows"></div>

        <fieldset>
          <legend>Confidence in your ratings</legend>
          <div class="chips" id="confidence-chips" role="radiogroup" aria-label="Confidence"></div>
        </fieldset>

        <label class="notes-label" for="notes">Notes <span class="optional">(optional)</span></label>
        <textarea id="notes" maxlength="2000" rows="4" placeholder="Anything you noticed about this swing"></textarea>

        <div class="actions">
          <button type="submit" class="primary" id="submit">Submit ratings</button>
          <button type="button" id="cancel-edit" hidden>Cancel edit</button>
          <button type="button" id="skip">Skip this swing</button>
        </div>
        <p class="message" id="message" role="status"></p>
      </form>

      <section class="panel locked" id="locked" hidden>
        <h2>Submitted</h2>
        <p class="help" id="locked-when"></p>
        <div class="locked-values" id="locked-values"></div>
        <div class="actions">
          <button type="button" class="primary" id="reveal">Show Swing Score</button>
          <button type="button" id="edit" hidden>Edit submission</button>
        </div>
        <p class="message" id="score-message"></p>
        <div id="score-panel" hidden></div>
      </section>

      <section class="panel skipped" id="skipped" hidden>
        <h2>Skipped</h2>
        <p class="help">This swing is out of the current queue. The score stays hidden until you rate it.</p>
        <div class="actions">
          <button type="button" class="primary" id="unskip">Put back in the queue</button>
        </div>
      </section>
    </div>
  </section>

  <section class="panel" id="empty" hidden>
    <h2 id="empty-title">Nothing here</h2>
    <p id="empty-text" class="help"></p>
    <div class="actions">
      <button type="button" id="empty-all">Show all swings</button>
      <button type="button" id="empty-unreviewed">Show unreviewed</button>
    </div>
  </section>

  <p class="message" id="page-error" role="alert" hidden></p>
</main>

<script src="/app.js" data-batch="__BATCH__" data-reviewer="__REVIEWER__" data-token="__TOKEN__"
        data-edit-mode="__EDITMODE_FLAG__"></script>
</body>
</html>
"""

CSS = """
:root {
  --bg: #f9fafa; --surface: #ffffff; --surface2: #f1f3f2; --line: #e6e9e7; --line-strong: #d4d9d6;
  --ink: #141a17; --ink2: #5b635e; --ink3: #9aa19d; --green: #2f6f4e; --green-deep: #1f4d36;
  --green-soft: #e7f0ea; --gold: #b98b2f; --gold-soft: #f6eedb; --problem: #bd4f39; --problem-soft: #fbe8e3;
  --radius: 12px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { background: var(--bg); color: var(--ink); font: 16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size: 22px; margin: 2px 0 0; }
h2 { font-size: 17px; margin: 0 0 10px; }
p { margin: 0 0 8px; }
.bar { background: var(--surface); border-bottom: 1px solid var(--line); padding: 14px 20px 12px; position: sticky; top: 0; z-index: 5; }
.bar-main { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; justify-content: space-between; max-width: 1180px; margin: 0 auto; }
.eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .7px; text-transform: uppercase; color: var(--green); margin: 0; }
.sub { color: var(--ink2); font-size: 14px; margin: 4px 0 0; }
.progress { text-align: right; }
.progress-label { display: block; font-size: 12px; font-weight: 600; letter-spacing: .6px; text-transform: uppercase; color: var(--ink2); }
.progress-value { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.progress-meta { display: block; font-size: 13px; color: var(--ink2); }
.bar-filters { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; max-width: 1180px; margin: 12px auto 0; }
.segmented { display: inline-flex; border: 1px solid var(--line-strong); border-radius: 999px; overflow: hidden; background: var(--surface); }
.segmented button { border: 0; background: transparent; color: var(--ink2); font: inherit; font-size: 14px; padding: 7px 14px; cursor: pointer; }
.segmented button + button { border-left: 1px solid var(--line); }
.segmented button.is-on { background: var(--green); color: #fff; font-weight: 600; }
.nav { display: flex; align-items: center; gap: 10px; }
.nav button, .actions button, .chip, .rate { font: inherit; cursor: pointer; border-radius: 10px; border: 1px solid var(--line-strong); background: var(--surface); color: var(--ink); padding: 8px 14px; }
.nav button:hover:not(:disabled), .actions button:hover:not(:disabled) { border-color: var(--green); color: var(--green-deep); }
button:disabled { opacity: .45; cursor: not-allowed; }
.position { font-size: 13px; color: var(--ink2); font-variant-numeric: tabular-nums; min-width: 72px; text-align: center; }
main { max-width: 1180px; margin: 0 auto; padding: 20px 20px 64px; }
.stage { display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(320px, 1fr); gap: 18px; align-items: start; }
.side { display: flex; flex-direction: column; gap: 18px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; margin-bottom: 18px; }
.side .panel { margin-bottom: 0; }
.video-head { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
.pill { display: inline-block; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; background: var(--surface2); color: var(--ink2); border: 1px solid var(--line); }
.pill.club { background: var(--green-soft); color: var(--green-deep); border-color: var(--green-soft); }
.pill.state.submitted { background: var(--green); color: #fff; border-color: var(--green); }
.pill.state.skipped { background: var(--gold-soft); color: #7a5a13; border-color: var(--gold-soft); }
video { width: 100%; max-height: 62vh; background: #0d1210; border-radius: 10px; display: block; }
.video-help, .help { color: var(--ink2); font-size: 13px; }
.video-help { margin: 10px 0 0; }
.rate-row { display: grid; grid-template-columns: 1fr; gap: 6px; padding: 10px 0; border-top: 1px solid var(--line); }
.rate-row:first-child { border-top: 0; }
.rate-row .label { font-weight: 600; }
.rate-row .scale { color: var(--ink3); font-weight: 400; font-size: 13px; }
.rates { display: grid; grid-template-columns: repeat(10, minmax(0, 1fr)); gap: 4px; }
.rate { padding: 7px 0; text-align: center; font-variant-numeric: tabular-nums; border-radius: 8px; }
.rate.is-on { background: var(--green); border-color: var(--green); color: #fff; font-weight: 700; }
fieldset { border: 0; margin: 14px 0 0; padding: 0; }
legend { font-weight: 600; padding: 0; margin-bottom: 6px; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip.is-on { background: var(--green); border-color: var(--green); color: #fff; font-weight: 600; }
.notes-label { display: block; font-weight: 600; margin: 14px 0 6px; }
.optional { color: var(--ink3); font-weight: 400; }
textarea { width: 100%; font: inherit; padding: 9px; border-radius: 10px; border: 1px solid var(--line-strong); background: var(--bg); color: var(--ink); }
.actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
.actions button.primary { background: var(--green); border-color: var(--green); color: #fff; font-weight: 600; padding-left: 20px; padding-right: 20px; }
.actions button.primary:disabled { background: var(--line-strong); border-color: var(--line-strong); color: #fff; }
.message { margin-top: 10px; font-size: 14px; color: var(--ink2); min-height: 20px; }
.message.bad { color: var(--problem); }
#page-error { max-width: 1180px; margin: 0 auto 24px; padding: 10px 14px; border: 1px solid var(--problem); background: var(--problem-soft); border-radius: 10px; }
.locked-values { display: grid; gap: 6px; margin-top: 6px; }
.locked-values div { display: flex; justify-content: space-between; gap: 12px; border-bottom: 1px dashed var(--line); padding: 4px 0; font-size: 15px; }
.locked-values strong { text-align: right; }
.score-total { font-size: 30px; font-weight: 700; color: var(--green-deep); font-variant-numeric: tabular-nums; }
.score-head { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; }
.score-meta { color: var(--ink2); font-size: 13px; }
.score-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
.score-table th, .score-table td { text-align: left; padding: 5px 6px; border-bottom: 1px solid var(--line); }
.score-note { color: var(--ink2); font-size: 13px; margin-top: 10px; }
button:focus-visible, .chip:focus-visible, .rate:focus-visible, textarea:focus-visible, video:focus-visible,
.segmented button:focus-visible, .nav button:focus-visible { outline: 2px solid var(--green-deep); outline-offset: 2px; }
@media (max-width: 900px) {
  .stage { grid-template-columns: 1fr; }
  main { padding: 14px 12px 80px; }
  .bar { padding: 12px 12px 10px; }
  .progress { text-align: left; }
  .rates { gap: 3px; }
  .rate { padding: 9px 0; }
}
@media (max-width: 380px) { .rate { font-size: 14px; } }
"""

JS = r"""
"use strict";
const meta = document.currentScript;
const BATCH = meta.dataset.batch;
const REVIEWER = meta.dataset.reviewer;
const TOKEN = meta.dataset.token;
const EDIT_MODE = meta.dataset.editMode === "1";

const RATING_FIELDS = ["setup", "backswing", "top", "downswing", "overall"];
const state = {
  session: null, detail: null, itemId: null, filter: "all",
  drafts: {}, confidence: {}, notes: {}, saved: {}, editing: {},
  revealed: {}, busy: false, seq: 0,
};

const $ = (id) => document.getElementById(id);

function reportPageError(message) {
  const node = $("page-error");
  node.hidden = false;
  node.textContent = "Something went wrong on this page: " + message;
}

window.addEventListener("error", (event) => reportPageError(event.message || "script error"));
window.addEventListener("unhandledrejection", (event) => reportPageError(String(event.reason)));

async function api(path, options) {
  const response = await fetch(path, Object.assign({cache: "no-store"}, options || {}));
  let payload = null;
  try { payload = await response.json(); } catch (error) { payload = null; }
  if (!response.ok) {
    const message = (payload && payload.error) || ("Request failed (" + response.status + ")");
    throw new Error(message);
  }
  return payload;
}

function setMessage(text, bad) {
  const node = $("message");
  node.textContent = text || "";
  node.classList.toggle("bad", Boolean(bad));
}

function currentItem() {
  return state.session && state.session.items.find((item) => item.item_id === state.itemId) || null;
}

function visibleItems() {
  if (!state.session) return [];
  const items = state.session.items;
  if (state.filter === "unreviewed") return items.filter((i) => i.state === "new");
  if (state.filter === "reviewed") return items.filter((i) => i.state === "submitted");
  if (state.filter === "skipped") return items.filter((i) => i.state === "skipped");
  return items;
}

function draftState(itemId) {
  const draft = state.drafts[itemId] || {};
  const ratings = {};
  RATING_FIELDS.forEach((field) => {
    if (typeof draft[field] === "number") ratings[field] = draft[field];
  });
  return JSON.stringify({ratings, confidence: state.confidence[itemId] || null,
                         notes: state.notes[itemId] || ""});
}

function isDirty(itemId) {
  if (!itemId || !(itemId in state.drafts)) return false;
  return draftState(itemId) !== state.saved[itemId];
}

function anyDirty() {
  return Object.keys(state.drafts).some((itemId) => isDirty(itemId));
}

function snapshot(itemId) {
  state.saved[itemId] = draftState(itemId);
}

function hydrate(item, detail) {
  const itemId = item.item_id;
  if (itemId in state.drafts) return;
  const review = detail && detail.review ? detail.review : null;
  const draft = {};
  RATING_FIELDS.forEach((field) => { draft[field] = review && typeof review[field] === "number" ? review[field] : null; });
  state.drafts[itemId] = draft;
  state.confidence[itemId] = review ? review.confidence : null;
  state.notes[itemId] = review ? (review.notes || "") : "";
  snapshot(itemId);
}

function allPicked(itemId) {
  const draft = state.drafts[itemId] || {};
  return RATING_FIELDS.every((field) => typeof draft[field] === "number") && Boolean(state.confidence[itemId]);
}

function setBusy(value) {
  state.busy = Boolean(value);
  $("prev").disabled = state.busy || $("prev").dataset.blocked === "1";
  $("next").disabled = state.busy || $("next").dataset.blocked === "1";
  document.querySelectorAll("[data-filter], .rate, .chip, #notes, #reveal, #edit, #cancel-edit, #skip, #unskip").forEach((node) => {
    node.disabled = state.busy;
  });
  $("submit").disabled = state.busy || !allPicked(state.itemId);
}

function buildRatingRows() {
  const host = $("rating-rows");
  host.innerHTML = "";
  state.session.ratings.forEach((field) => {
    const row = document.createElement("div");
    row.className = "rate-row";
    const label = document.createElement("div");
    label.className = "label";
    label.innerHTML = field.label + ' <span class="scale">' + field.min + "-" + field.max + "</span>";
    const group = document.createElement("div");
    group.className = "rates";
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", field.label);
    for (let value = field.min; value <= field.max; value += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "rate";
      button.dataset.field = field.id;
      button.dataset.value = String(value);
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", "false");
      button.textContent = String(value);
      group.appendChild(button);
    }
    row.append(label, group);
    host.appendChild(row);
  });
  const chips = $("confidence-chips");
  chips.innerHTML = "";
  state.session.confidence_levels.forEach((level) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.level = level;
    chip.setAttribute("role", "radio");
    chip.setAttribute("aria-checked", "false");
    chip.textContent = level.charAt(0) + level.slice(1).toLowerCase();
    chips.appendChild(chip);
  });
}

function applyPicks(itemId) {
  const draft = state.drafts[itemId] || {};
  document.querySelectorAll(".rate").forEach((button) => {
    const on = draft[button.dataset.field] === Number(button.dataset.value);
    button.classList.toggle("is-on", on);
    button.setAttribute("aria-checked", String(on));
  });
  const level = state.confidence[itemId] || null;
  document.querySelectorAll("#confidence-chips .chip").forEach((chip) => {
    const on = chip.dataset.level === level;
    chip.classList.toggle("is-on", on);
    chip.setAttribute("aria-checked", String(on));
  });
  $("notes").value = state.notes[itemId] || "";
}

function renderHeader() {
  const {batch, items} = state.session;
  $("progress-value").textContent = batch.reviewed + " / " + batch.items;
  if (batch.items > 0 && batch.reviewed === batch.items) $("progress-meta").textContent = "All swings reviewed";
  else if (batch.skipped) $("progress-meta").textContent = batch.skipped + " skipped";
  else $("progress-meta").textContent = "no skips yet";
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.classList.toggle("is-on", button.dataset.filter === state.filter);
  });
  const visible = visibleItems();
  const at = visible.findIndex((item) => item.item_id === state.itemId);
  $("position").textContent = visible.length ? (at >= 0 ? (at + 1) : 0) + " of " + visible.length : "0 shown";
  $("prev").dataset.blocked = (!visible.length || at <= 0) ? "1" : "0";
  $("next").dataset.blocked = (!visible.length || at < 0 || at >= visible.length - 1) ? "1" : "0";
  setBusy(state.busy);
}

function paintLocked(item, detail) {
  const review = detail.review;
  const when = $("locked-when");
  when.textContent = "First submitted " + review.first_submitted_at +
    (review.revision > 1 ? " - revision " + review.revision + " (post-reveal edit)" : " - the original blind rating");
  const host = $("locked-values");
  host.innerHTML = "";
  state.session.ratings.forEach((field) => {
    const line = document.createElement("div");
    const name = document.createElement("span");
    name.textContent = field.label;
    const value = document.createElement("strong");
    value.textContent = review[field.id] + " / 10";
    line.append(name, value);
    host.appendChild(line);
  });
  const confidence = document.createElement("div");
  confidence.innerHTML = "<span>Confidence</span><strong></strong>";
  confidence.querySelector("strong").textContent = review.confidence;
  host.appendChild(confidence);
  if (review.notes) {
    const notes = document.createElement("div");
    notes.innerHTML = "<span>Notes</span><strong></strong>";
    notes.querySelector("strong").textContent = review.notes;
    host.appendChild(notes);
  }
  const revealed = state.revealed[item.item_id];
  $("score-panel").hidden = !revealed;
  $("score-panel").innerHTML = "";
  $("score-message").textContent = "";
  if (revealed) renderScore(revealed);
  $("edit").hidden = !EDIT_MODE;
}

function paint() {
  const item = currentItem();
  if (!item || !state.detail) return;
  const detail = state.detail;
  const submitted = detail.state === "submitted";
  const skipped = detail.state === "skipped";
  const editing = Boolean(state.editing[item.item_id]);

  const badge = $("swing-state");
  badge.className = "pill state " + detail.state;
  badge.textContent = submitted ? "Submitted" : (skipped ? "Skipped" : "Not reviewed yet");
  $("swing-ordinal").textContent = "Swing " + item.ordinal + " of " + state.session.batch.items;
  $("swing-club").textContent = item.club ? item.club : "Club: not given";

  $("form").hidden = skipped || (submitted && !(EDIT_MODE && editing));
  $("locked").hidden = !submitted || (EDIT_MODE && editing);
  $("skipped").hidden = !skipped;
  $("skip").hidden = detail.state !== "new";
  $("cancel-edit").hidden = !(EDIT_MODE && submitted && editing);
  $("submit").textContent = submitted && editing ? "Save new revision" : "Submit ratings";
  $("submit").disabled = !allPicked(item.item_id);

  applyPicks(item.item_id);
  if (submitted && !editing) paintLocked(item, detail);
  setMessage("");
  renderHeader();
}

function paintEmpty() {
  const {batch} = state.session;
  const visible = visibleItems();
  $("stage").hidden = true;
  $("empty").hidden = false;
  const title = $("empty-title");
  const text = $("empty-text");
  if (!state.session.items.length) {
    title.textContent = "No swings in this batch";
    text.textContent = "Import a batch with scripts/coach_review_import.py and reload.";
  } else if (state.filter === "unreviewed" && batch.reviewed === batch.items) {
    title.textContent = "Every swing is reviewed";
    text.textContent = "All " + batch.items + " swings have a first blind rating. Show all swings to review or edit one.";
  } else if (state.filter === "skipped" && !visible.length) {
    title.textContent = "Nothing skipped";
    text.textContent = "No swing is currently skipped.";
  } else if (state.filter === "reviewed" && !batch.reviewed) {
    title.textContent = "Nothing reviewed yet";
    text.textContent = "Rate a swing and it will appear here.";
  } else {
    title.textContent = "Nothing in this filter";
    text.textContent = "Every swing is in a different state. Show all swings to continue.";
  }
}

function paintStage() {
  if (!state.session) return;
  const visible = visibleItems();
  if (!visible.length || !currentItem() || !visible.some((item) => item.item_id === state.itemId)) {
    if (!visible.length) { paintEmpty(); return; }
    select(visible[0].item_id, {force: true});
    return;
  }
  $("empty").hidden = true;
  $("stage").hidden = false;
}

async function select(itemId, options) {
  if (state.busy) return;
  const item = state.session.items.find((i) => i.item_id === itemId);
  if (!item) return;
  if (!(options && options.force) && state.itemId && state.itemId !== itemId && isDirty(state.itemId)) {
    if (!window.confirm("You have unsaved ratings on the previous swing. Leave them behind?")) return;
  }
  state.itemId = itemId;
  state.detail = null;
  $("empty").hidden = true;
  $("stage").hidden = false;
  const video = $("video");
  if (video.dataset.src !== item.video) {
    video.dataset.src = item.video;
    video.src = item.video;
    try { video.pause(); } catch (error) { /* ignore */ }
  }
  const seq = ++state.seq;
  setBusy(true);
  try {
    const detail = await api("/api/items/" + itemId);
    if (seq !== state.seq) return;      // a later selection won: drop this response
    state.detail = detail;
    hydrate(item, detail);
    paint();
  } catch (error) {
    if (seq === state.seq) setMessage(error.message, true);
  } finally {
    if (seq === state.seq) setBusy(false);
  }
}

function step(delta) {
  if (state.busy || !state.session) return;
  const visible = visibleItems();
  if (!visible.length) return;
  const at = visible.findIndex((item) => item.item_id === state.itemId);
  const base = at < 0 ? (delta > 0 ? -1 : visible.length) : at;
  const target = visible[base + delta];
  if (target) select(target.item_id);
  else if (delta > 0) paintEmpty();
}

function stepAfterSubmit() {
  const visible = visibleItems();
  const at = visible.findIndex((item) => item.item_id === state.itemId);
  const target = visible[at + 1] || visible[at - 1];
  if (target) select(target.item_id, {force: true});
  else paintEmpty();
}

function setFilter(filter) {
  if (state.busy) return;
  state.filter = filter;
  const visible = visibleItems();
  if (!visible.length) { renderHeader(); paintEmpty(); return; }
  if (!visible.some((item) => item.item_id === state.itemId)) {
    const all = state.session.items;
    const at = all.findIndex((item) => item.item_id === state.itemId);
    const next = visible.find((item) => all.indexOf(item) >= at) || visible[0];
    select(next.item_id, {force: true});
  } else {
    $("empty").hidden = true;
    $("stage").hidden = false;
    paint();
  }
}

function refreshCounts() {
  const items = state.session.items;
  state.session.batch.reviewed = items.filter((item) => item.state === "submitted").length;
  state.session.batch.skipped = items.filter((item) => item.state === "skipped").length;
}

async function save() {
  if (state.busy) return;
  const item = currentItem();
  if (!item) return;
  const itemId = item.item_id;
  if (!allPicked(itemId)) { setMessage("Give all five ratings and a confidence before submitting.", true); return; }
  const isEdit = item.state === "submitted" && EDIT_MODE && Boolean(state.editing[itemId]);
  const draft = state.drafts[itemId] || {};
  const ratings = {};
  RATING_FIELDS.forEach((field) => {
    if (typeof draft[field] === "number") ratings[field] = draft[field];
  });
  setBusy(true);
  try {
    const payload = await api("/api/items/" + itemId + "/review", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Coach-Review-Token": TOKEN},
      body: JSON.stringify({ratings, confidence: state.confidence[itemId], notes: state.notes[itemId] || "",
                            edit: isEdit}),
    });
    item.state = "submitted";
    state.detail = Object.assign({}, state.detail, {state: "submitted", locked: !EDIT_MODE, review: payload.review});
    snapshot(itemId);
    state.editing[itemId] = false;
    state.revealed[itemId] = null;
    refreshCounts();
    paint();
    setMessage(isEdit ? "Saved as a new revision (post-reveal edit)."
                      : "Saved. Ratings are locked for this swing.");
    if (!isEdit) stepAfterSubmit();
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setBusy(false);
    paintStage();
  }
}

async function skip() {
  const item = currentItem();
  if (!item || state.busy) return;
  setBusy(true);
  try {
    await api("/api/items/" + item.item_id + "/skip", {method: "POST", headers: {"X-Coach-Review-Token": TOKEN}});
    item.state = "skipped";
    if (state.detail) state.detail.state = "skipped";
    refreshCounts();
    paint();
    setMessage("Skipped. Skipping does not reveal the score - submit ratings to unlock it.");
    step(1);
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setBusy(false);
    paintStage();
  }
}

async function unskip() {
  const item = currentItem();
  if (!item || state.busy) return;
  setBusy(true);
  try {
    await api("/api/items/" + item.item_id + "/unskip", {method: "POST", headers: {"X-Coach-Review-Token": TOKEN}});
    item.state = "new";
    if (state.detail) state.detail.state = "new";
    refreshCounts();
    paint();
    setMessage("Skip removed - this swing is back in the queue.");
  } catch (error) {
    setMessage(error.message, true);
  } finally {
    setBusy(false);
  }
}

function startEdit() {
  const item = currentItem();
  if (!item || state.busy) return;
  state.editing[item.item_id] = true;
  paint();
  $("form").scrollIntoView({block: "nearest"});
  setMessage("Edit mode: change the ratings, then Save new revision. Cancel edit keeps the saved values.");
}

function cancelEdit() {
  const item = currentItem();
  if (!item) return;
  const detail = state.detail;
  if (detail && detail.review) {
    const draft = {};
    RATING_FIELDS.forEach((field) => { draft[field] = detail.review[field]; });
    state.drafts[item.item_id] = draft;
    state.confidence[item.item_id] = detail.review.confidence;
    state.notes[item.item_id] = detail.review.notes || "";
    snapshot(item.item_id);
  }
  state.editing[item.item_id] = false;
  paint();
  setMessage("Edit cancelled - the saved rating is unchanged.");
}

function renderScore(payload) {
  const host = $("score-panel");
  host.innerHTML = "";
  host.hidden = false;
  const score = payload.score;
  if (!score) {
    const line = document.createElement("p");
    line.className = "score-note";
    line.textContent = "No stored Swing Score was captured for this swing at import time, so there is nothing to compare.";
    host.appendChild(line);
    return;
  }
  const head = document.createElement("div");
  head.className = "score-head";
  const total = document.createElement("span");
  total.className = "score-total";
  total.textContent = score.available && typeof score.total === "number" ? score.total.toFixed(1) + " / 10" : "No score";
  const meta = document.createElement("span");
  meta.className = "score-meta";
  meta.textContent = "Model " + (score.version || payload.model_version || "unknown") +
    " \u00b7 " + score.measured + " of " + score.eligible + " checks measured" +
    (score.confidence && score.confidence.label ? " \u00b7 " + score.confidence.label + " confidence" : "");
  head.append(total, meta);
  host.appendChild(head);

  const table = document.createElement("table");
  table.className = "score-table";
  table.innerHTML = "<thead><tr><th>Check</th><th>Phase</th><th>Points</th><th>Weight</th></tr></thead>";
  const body = document.createElement("tbody");
  const checks = score.checks || [];
  const weightSum = checks.reduce((sum, check) => sum + (typeof check.weight === "number" ? check.weight : 0), 0);
  checks.forEach((check) => {
    const share = weightSum > 0 && typeof check.weight === "number" ? check.weight / weightSum : null;
    const row = document.createElement("tr");
    [check.name, check.phase, (typeof check.points === "number" ? check.points.toFixed(1) : "-"),
     (share === null ? "-" : share.toFixed(2))].forEach((text) => {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  table.appendChild(body);
  host.appendChild(table);
  if ((score.excluded || []).length) {
    const note = document.createElement("p");
    note.className = "score-note";
    note.textContent = "Not measured: " + score.excluded.map((e) => e.name + " (" + e.reason + ")").join("; ");
    host.appendChild(note);
  }
  if (score.reason) {
    const reason = document.createElement("p");
    reason.className = "score-note";
    reason.textContent = score.reason;
    host.appendChild(reason);
  }
}

async function reveal() {
  const item = currentItem();
  if (!item || state.busy) return;
  setBusy(true);
  try {
    const payload = await api("/api/items/" + item.item_id + "/score");
    state.revealed[item.item_id] = payload;
    $("score-message").textContent = "";
    renderScore(payload);
  } catch (error) {
    $("score-message").textContent = error.message;
  } finally {
    setBusy(false);
  }
}

async function boot() {
  try {
    state.session = await api("/api/session");
  } catch (error) {
    $("stage").hidden = true;
    $("empty").hidden = false;
    $("empty-title").textContent = "Could not load the session";
    $("empty-text").textContent = error.message;
    document.documentElement.dataset.ready = "1";
    return;
  }
  buildRatingRows();
  renderHeader();
  const first = visibleItems()[0] || state.session.items[0];
  if (first) await select(first.item_id, {force: true});
  else paintEmpty();
  document.documentElement.dataset.ready = "1";
}

document.addEventListener("click", (event) => {
  if (state.busy || !state.session) return;
  const rate = event.target.closest(".rate");
  if (rate) {
    const itemId = state.itemId;
    if (!itemId) return;
    const field = rate.dataset.field;
    const value = Number(rate.dataset.value);
    state.drafts[itemId] = state.drafts[itemId] || {};
    if (state.drafts[itemId][field] === value) delete state.drafts[itemId][field];
    else state.drafts[itemId][field] = value;
    applyPicks(itemId);
    $("submit").disabled = !allPicked(itemId);
    return;
  }
  const chip = event.target.closest("#confidence-chips .chip");
  if (chip) {
    const itemId = state.itemId;
    if (!itemId) return;
    state.confidence[itemId] = chip.dataset.level;
    applyPicks(itemId);
    $("submit").disabled = !allPicked(itemId);
    return;
  }
  const filter = event.target.closest("[data-filter]");
  if (filter) { setFilter(filter.dataset.filter); return; }
  if (event.target.closest("#prev")) { step(-1); return; }
  if (event.target.closest("#next")) { step(1); return; }
  if (event.target.closest("#skip")) { skip(); return; }
  if (event.target.closest("#unskip")) { unskip(); return; }
  if (event.target.closest("#reveal")) { reveal(); return; }
  if (event.target.closest("#edit")) { startEdit(); return; }
  if (event.target.closest("#cancel-edit")) { cancelEdit(); return; }
  if (event.target.closest("#empty-all")) { setFilter("all"); return; }
  if (event.target.closest("#empty-unreviewed")) { setFilter("unreviewed"); return; }
});

$("form").addEventListener("submit", (event) => { event.preventDefault(); save(); });
$("notes").addEventListener("input", () => {
  if (!state.itemId) return;
  state.notes[state.itemId] = $("notes").value;
});
$("video").addEventListener("error", () => setMessage("This swing video could not be loaded.", true));
document.addEventListener("keydown", (event) => {
  if (event.target.closest("textarea, input")) return;
  if (event.key === "ArrowLeft") { step(-1); }
  if (event.key === "ArrowRight") { step(1); }
});
window.addEventListener("beforeunload", (event) => {
  if (anyDirty()) { event.preventDefault(); event.returnValue = ""; }
});

boot();
"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(*, batch: str, reviewer: str, token: str, edit_mode: bool) -> str:
    edit_note = " &middot; edit mode on" if edit_mode else ""
    return (PAGE.replace("__BATCH__", _escape(batch))
                .replace("__REVIEWER__", _escape(reviewer))
                .replace("__EDITMODE__", edit_note)
                .replace("__TOKEN__", _escape(token))
                .replace("__EDITMODE_FLAG__", "1" if edit_mode else "0"))


def session_payload(session: dict) -> dict:
    """Exactly what the browser may see: no scores, no paths, no analysis metadata."""

    return json.loads(json.dumps(session))


__all__ = ["CSS", "JS", "PAGE", "page", "session_payload"]
