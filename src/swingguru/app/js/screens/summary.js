// Swing Summary (#/swing/<id>[/new]) — how the swing went, what looked good, what needs
// attention, what to do next. Every verdict here is real; score / Start here follow the
// production-vs-stub rules in swingguru/app_model.py.
import { get, isStub, post, shortDate } from "../api.js";
import { back } from "../nav.js";
import { NEUTRAL_FOCUS, drillCard, rememberDrills, startHereCard } from "../parts.js";
import {
  button, compactBar, esc, icon, iconButton, scoreRing, showSheet, closeSheet, statusText, stub, toast,
} from "../ui.js";

export const title = "Swing analysis";
export const tabs = false;

const viewerId = (id) => id.replace(".", ":");  // "impact.pelvis" -> the viewer's "impact:pelvis"

export async function render({ args }) {
  const [id, flag] = args;
  const s = await get(`swings/${id}`);
  const isNew = flag === "new";
  return `${compactBar("Swing analysis", { left: iconButton("close", "Close", { plain: true, "data-summary-close": "" }) })}
  <div class="scroll summary">
    <header class="summary-top">
      ${iconButton("close", "Close", { "data-summary-close": "" })}
      <div class="summary-when"><div class="subhead">${esc(shortDate(s.date))}</div>${s.club ? clubChip(s) : ""}</div>
    </header>
    ${isNew ? `<div class="saved-note">${icon("check", "ico-inline")}Saved to your Library</div>` : ""}
    ${flag === "existing" ? `<div class="saved-note">${icon("library", "ico-inline")}Already in your Library, so we didn't analyse it again</div>` : ""}
    ${s.notice ? `<div class="notice">${icon("info", "ico-inline")}<span>${esc(s.notice)}</span></div>` : ""}
    ${s.club ? "" : clubPrompt(s)}
    ${resultHeader(s)}
    <p class="summary-text">${esc(s.summary_text)}</p>
    ${s.camera.moved && s.camera.corrected_phases.length ? `<div class="notice" style="margin-top:12px">${icon("info", "ico-inline")}
      <span>Your phone moved during the swing. We corrected for it where we could.</span></div>` : ""}
    <div style="margin-top:20px">${startHereCard(s.focus, { record: false })}</div>
    <button class="btn btn-primary block replay-cta" data-go="#/replay/${esc(s.id)}">${icon("play")}<span>Replay analysis</span></button>
    ${breakdown(s)}
    ${practice(s)}
    <button class="btn btn-quiet block" data-summary-done style="margin-top:22px">Done</button>
  </div>`;
}

/* ------------------------------------------------------------ header */

function resultHeader(s) {
  const h = s.headline;
  const bar = h.total ? `<div class="result-bar" role="img" aria-label="${h.good} in range, ${h.attention} slightly out, ${h.problem} clearly out">
      ${["good", "attention", "problem"].map((k) => `<span style="flex:${h[k]};background:var(--${k})"></span>`).join("")}</div>` : "";
  const counts = `<div class="result-counts">
      <span>${statusText(`${h.good} in range`, "good")}</span>
      ${h.attention ? `<span>${statusText(`${h.attention} slightly out`, "attention")}</span>` : ""}
      ${h.problem ? `<span>${statusText(`${h.problem} clearly out`, "problem")}</span>` : ""}</div>`;
  if (s.score.available) {
    // The Swing Score slot: a calibrated model will fill it; in demo it is a flagged stub.
    return `<div class="card result">
      <div class="row" style="gap:18px">${stub(scoreRing(s.score.total, { size: "md" }), "score", { on: isStub(s.score) })}
        <div class="spacer"><div class="caption">Swing Score</div>
          <div class="result-title">${h.good} of ${h.total} checks in range</div>
          <div class="footnote muted">${h.phases_analysed} phases analysed</div></div></div>
      ${bar}${counts}</div>`;
  }
  return `<div class="card result">
    <div class="caption">Swing analysis</div>
    <div class="result-big"><strong>${h.good}</strong><span>of ${h.total} checks in range</span></div>
    <div class="footnote muted">${h.phases_analysed} phases analysed</div>
    ${bar}${counts}</div>`;
}

/* ------------------------------------------------------------ club */

function clubChip(s) {
  return `<button class="chip ${s.club ? "tag" : "outline"} club-chip" data-club-sheet>${icon("golf", "ico-chip")}${esc(s.club || "Add club")}</button>`;
}

function clubPrompt(s) {
  return `<div class="club-prompt"><div class="subhead">Which club was this?</div>
    <div class="filters" style="margin-top:10px">${s.clubs.map((c) => `<button class="filter" data-club="${esc(c)}">${esc(c)}</button>`).join("")}</div></div>`;
}

/* ------------------------------------------------------------ breakdown */

function breakdown(s) {
  const groups = s.breakdown.map((g) => `
    <div class="caption group-label">${esc(g.label)}</div>
    ${g.rows.map((r) => row(s, r)).join("")}`).join("");
  return `<section class="section"><div class="section-head"><h2>Breakdown</h2></div>${groups}
    <p class="footnote faint" style="margin-top:12px">Takeaway is in the replay but isn't scored yet. Tap a check to see it on your swing.</p></section>`;
}

function row(s, r) {
  if (r.kind === "withheld") {
    return `<div class="verdict-row withheld"><span class="label">${esc(r.name)}<small>${esc(r.reason)}</small></span>
      <span class="status-text info"><span class="dot"></span>Not measured</span></div>`;
  }
  const value = r.value ? `<span class="value">${esc(r.value)}</span>` : "";
  return `<button class="verdict-row" data-go="#/replay/${esc(s.id)}/${esc(viewerId(r.id))}" aria-label="Replay ${esc(r.name)}: ${esc(r.label)}">
    <span class="label">${esc(r.name)}${r.kind === "info" ? `<small>${esc(r.support || "")}</small>` : ""}</span>
    ${r.kind === "info" ? value : `${statusText(r.label, r.status)}`}
    ${icon("play", "row-play")}</button>`;
}

/* ------------------------------------------------------------ practice */

function practice(s) {
  const fromFocus = s.focus.available && (s.focus.drills || []).length;
  const items = fromFocus
    ? [{ drill: s.focus.drills[0], checkpoint: { name: s.focus.title, label: s.focus.label, status: s.focus.status } }]
    : s.recommended;
  if (!items.length) {
    return `<section class="section"><div class="section-head"><h2>Practice</h2></div>
      <div class="card flat"><p class="callout muted">Every check we measured was in range. Film another swing to keep it that way.</p></div></section>`;
  }
  items.forEach((i) => rememberDrills([i.drill]));
  const cards = items.map((i) => drillCard(i.drill, { status: i.checkpoint.status, label: `${i.checkpoint.name}: ${i.checkpoint.label}` })).join("");
  const body = fromFocus ? stub(cards, "start here drill", { on: isStub(s.focus), block: true }) : cards;
  return `<section class="section"><div class="section-head"><h2>Practice</h2>
      <button class="link" data-go="#/drills">All drills${icon("chevron")}</button></div>
    <p class="footnote muted" style="margin:-4px 0 12px">${fromFocus ? "The drill for where to start." : "Drills for the checks that were out of range."}</p>
    ${cards ? body : ""}
    <p class="footnote faint" style="margin-top:10px">Preview drills · not yet reviewed by a coach</p></section>`;
}

/* ------------------------------------------------------------ behaviour */

export function mount(view, { args }) {
  const [id] = args;
  view.querySelectorAll("[data-summary-close]").forEach((b) => b.addEventListener("click", () => back("#/library")));
  view.querySelector("[data-summary-done]")?.addEventListener("click", () => { location.hash = "#/home"; });
  const tag = async (club) => {
    try {
      await post(`swings/${id}/meta`, { club });
      toast(club ? `Tagged as ${club}` : "Club removed");
      closeSheet();
      window.dispatchEvent(new HashChangeEvent("hashchange"));  // re-render with the tag
    } catch (error) { toast("Couldn't save the club"); }
  };
  view.querySelectorAll("[data-club]").forEach((b) => b.addEventListener("click", () => tag(b.dataset.club)));
  view.querySelector("[data-club-sheet]")?.addEventListener("click", async () => {
    const s = await get(`swings/${id}`);
    showSheet(`<h2>Which club?</h2><p class="sheet-sub">Tag the club so you can filter your swings by it.</p>
      <div class="club-grid">${s.clubs.map((c) => `<button class="filter" aria-pressed="${c === s.club}" data-pick="${esc(c)}">${esc(c)}</button>`).join("")}</div>
      ${s.club ? `<button class="btn btn-quiet block" data-pick="" style="margin-top:12px">Remove tag</button>` : ""}`, {
      onOpen(sheet) { sheet.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => tag(b.dataset.pick || null))); },
    });
  });
}

export { NEUTRAL_FOCUS };
