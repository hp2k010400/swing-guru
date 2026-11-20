// Swing Guru UI components: small functions that return HTML strings, plus sheet/toast helpers.
import { icon } from "./icons.js";

export const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const attrs = (map = {}) =>
  Object.entries(map)
    .filter(([, v]) => v !== undefined && v !== null && v !== false)
    .map(([k, v]) => (v === true ? ` ${k}` : ` ${k}="${esc(v)}"`))
    .join("");

/* ---------------------------------------------------------------- layout */

export function largeTitle(title, right = "") {
  return `<header class="large-title"><h1>${esc(title)}</h1>${right}</header>`;
}

export function compactBar(title, { left = "", right = "", always = false } = {}) {
  return `<div class="compact-bar${always ? " always" : ""}"><div class="slot-left">${left}</div>
    <div class="title">${esc(title)}</div><div class="slot-right">${right}</div></div>`;
}

export function section(title, body, { link = null, id = null } = {}) {
  const action = link ? `<button class="link" data-go="${esc(link.href)}">${esc(link.label)}${icon("chevron")}</button>` : "";
  return `<section class="section"${id ? ` id="${esc(id)}"` : ""}>
    ${title ? `<div class="section-head"><h2>${esc(title)}</h2>${action}</div>` : ""}${body}</section>`;
}

export const groupLabel = (text, extra = "") => `<div class="caption group-label">${esc(text)}${extra}</div>`;

/* ---------------------------------------------------------------- buttons */

export function button(label, { kind = "primary", size = "", icon: ico = null, block = false, ...rest } = {}) {
  const cls = ["btn", `btn-${kind}`, size, block ? "block" : ""].filter(Boolean).join(" ");
  return `<button class="${cls}"${attrs(rest)}>${ico ? icon(ico) : ""}<span>${esc(label)}</span></button>`;
}

export const iconButton = (name, label, extra = {}) =>
  `<button class="icon-btn${extra.plain ? " plain" : ""}" aria-label="${esc(label)}"${attrs({ ...extra, plain: undefined })}>${icon(name)}</button>`;

/* ---------------------------------------------------------------- chips */

export const STATUS_WORD = { good: "In range", attention: "Slightly out", problem: "Clearly out", info: "Info" };

export function statusChip(label, status = "info") {
  return `<span class="chip ${esc(status)}"><span class="dot"></span>${esc(label)}</span>`;
}

export function statusText(label, status = "info") {
  return `<span class="status-text ${esc(status)}"><span class="dot"></span>${esc(label)}</span>`;
}

export const tag = (label) => `<span class="chip tag">${esc(label)}</span>`;

export function badge(label, { tone = "", ico = null } = {}) {
  return `<span class="badge${tone ? " " + tone : ""}">${ico ? icon(ico) : ""}${esc(label)}</span>`;
}

export const stubBadge = () => `<span class="badge stub dev-only">stub</span>`;

/** Wrap a stubbed value: invisible in production, a dashed outline + "STUB" label in ?dev=1. */
export function stub(html, note = "stub", { on = true, block = false } = {}) {
  if (!on) return html;
  const tagName = block ? "div" : "span";
  return `<${tagName} class="is-stub${block ? " block" : ""}" data-stub="${esc(note)}">${html}</${tagName}>`;
}

/* ---------------------------------------------------------------- cards */

export function card(body, { tone = "", tap = null, cls = "" } = {}) {
  const classes = ["card", tone, tap ? "tap" : "", cls].filter(Boolean).join(" ");
  return `<div class="${classes}"${tap ? ` data-go="${esc(tap)}" role="button" tabindex="0"` : ""}>${body}</div>`;
}

export function eyebrow(text, ico = null) {
  return `<div class="card-eyebrow">${ico ? icon(ico) : ""}<span class="caption" style="color:inherit;opacity:.9">${esc(text)}</span></div>`;
}

export function emptyState({ ico = "sparkle", title, text, action = "" }) {
  return `<div class="empty"><div class="glyph">${icon(ico)}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}

export function listRow({ ico = null, label, sub = "", value = "", href = null, action = null, tone = "", chevron = true, right = "", control = "" }) {
  const tagName = (href || action) && !control ? "button" : "div";
  const data = href ? ` data-go="${esc(href)}"` : action ? ` data-action="${esc(action)}"` : "";
  const classes = ["list-row", ico ? "" : "no-icon", tone, control ? "stacked" : ""].filter(Boolean).join(" ");
  return `<${tagName} class="${classes}"${data}>
    ${ico ? `<span class="lead">${icon(ico)}</span>` : ""}
    <span class="label">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</span>
    ${right}${value ? `<span class="value">${esc(value)}</span>` : ""}
    ${(href || action) && chevron && !control ? icon("chevron", "chev") : ""}
    ${control ? `<div class="control">${control}</div>` : ""}</${tagName}>`;
}

export function list(rows, title = "") {
  return `<div class="list">${title ? `<div class="list-title caption">${esc(title)}</div>` : ""}${rows.join("")}</div>`;
}

export function segmented(name, options, value) {
  return `<div class="segmented" role="group" data-segmented="${esc(name)}">${options
    .map((o) => `<button type="button" data-value="${esc(o.value)}" aria-pressed="${o.value === value}">${esc(o.label)}</button>`)
    .join("")}</div>`;
}

export function stat({ ico, value, label, href = null }) {
  return `<div class="stat"${href ? ` data-go="${esc(href)}" role="button" tabindex="0"` : ""}>
    <div class="stat-icon">${icon(ico)}</div><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`;
}

/* ---------------------------------------------------------------- rings */

export function scoreColour(score) {
  if (score == null) return "var(--info)";
  if (score >= 7.5) return "var(--good)";
  if (score >= 5) return "var(--attention)";
  return "var(--problem)";
}

export function scoreTone(score) {
  if (score == null) return "info";
  return score >= 7.5 ? "good" : score >= 5 ? "attention" : "problem";
}

export function scoreRing(score, { size = "", label = "/ 10" } = {}) {
  const r = 44, c = 2 * Math.PI * r;
  const frac = score == null ? 0 : Math.max(0, Math.min(1, score / 10));
  return `<div class="score-ring ${size}" role="img" aria-label="Score ${score == null ? "not available" : score.toFixed(1) + " out of 10"}">
    <svg viewBox="0 0 100 100"><circle class="track" cx="50" cy="50" r="${r}" fill="none" stroke-width="7"/>
      <circle class="arc" cx="50" cy="50" r="${r}" fill="none" stroke-width="7" stroke="${scoreColour(score)}"
        stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${(c * (1 - frac)).toFixed(1)}"/></svg>
    <div class="inner"><strong style="color:${scoreColour(score)}">${score == null ? "–" : score.toFixed(1)}</strong><span>${esc(label)}</span></div></div>`;
}

export function progressRing(done, total) {
  const r = 24, c = 2 * Math.PI * r, frac = total ? Math.min(1, done / total) : 0;
  return `<div class="ring"><svg viewBox="0 0 58 58"><circle class="track" cx="29" cy="29" r="${r}" fill="none" stroke-width="5"/>
    <circle class="arc" cx="29" cy="29" r="${r}" fill="none" stroke-width="5" stroke-dasharray="${c.toFixed(1)}"
      stroke-dashoffset="${(c * (1 - frac)).toFixed(1)}"/></svg><b>${done}/${total}</b></div>`;
}

export function scorePill(score) {
  if (score == null) return "";
  return `<span class="score-pill ${scoreTone(score)}">${score.toFixed(1)}</span>`;
}

/* ---------------------------------------------------------------- sheets & toast */

let openSheet = null;

export function showSheet(html, { onOpen } = {}) {
  closeSheet(true);
  const device = document.getElementById("device");
  const scrim = document.createElement("div");
  scrim.className = "scrim";
  const sheet = document.createElement("div");
  sheet.className = "sheet";
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.innerHTML = `<div class="grabber"></div><div class="sheet-body">${html}</div>`;
  device.append(scrim, sheet);
  scrim.addEventListener("click", () => closeSheet());
  sheet.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
  requestAnimationFrame(() => { scrim.classList.add("show"); sheet.classList.add("show"); });
  openSheet = { scrim, sheet };
  if (onOpen) onOpen(sheet);
  return sheet;
}

export function closeSheet(immediate = false) {
  if (!openSheet) return;
  const { scrim, sheet } = openSheet;
  openSheet = null;
  if (immediate) { scrim.remove(); sheet.remove(); return; }
  scrim.classList.remove("show");
  sheet.classList.remove("show");
  setTimeout(() => { scrim.remove(); sheet.remove(); }, 380);
}

let toastTimer = null;
export function toast(text) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");
    document.getElementById("device").append(el);
  }
  el.textContent = text;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

export { icon };
