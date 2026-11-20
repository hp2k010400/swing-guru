// Product pieces shared by several screens. Real vs stub is decided by the API's `source` fields.
import { isStub, shortDate } from "./api.js";
import {
  badge, button, esc, eyebrow, icon, scorePill, scoreRing, showSheet, statusChip, stub, tag, toast,
} from "./ui.js";

export const NEUTRAL_FOCUS = "Your focus will appear here as Swing Guru learns your swing.";

const STATUS_DOT = { good: "var(--good)", attention: "var(--attention)", problem: "var(--problem)" };

/* ---------------------------------------------------------------- swings */

export function inRange(counts) {
  return `${counts.good} of ${counts.total} in range`;
}

export function swingCard(s, { showPr = true } = {}) {
  const score = s.score || {};
  const pill = score.available ? stub(scorePill(score.total), "score", { on: isStub(score) }) : "";
  const pr = showPr && s.pr && s.pr.value ? stub(badge("PR", { ico: "trophy" }), "PR", { on: s.pr.source === "stub" }) : "<span></span>";
  const worst = s.counts.problem ? "problem" : s.counts.attention ? "attention" : "good";
  return `<div class="swing-card" data-go="#/swing/${esc(s.id)}" role="button" tabindex="0" aria-label="${esc(s.club || "Swing")} ${esc(shortDate(s.date))}">
    <img src="${esc(s.thumb)}" alt="" loading="lazy" />
    <div class="top">${pr}${pill}</div>
    <div class="bottom">
      <div class="club">${esc(s.club || "Swing")}</div>
      <div class="date">${esc(shortDate(s.date))}</div>
      <div class="issue"><span class="dot" style="background:${STATUS_DOT[worst]}"></span>${s.counts.good}/${s.counts.total} in range</div>
    </div>
    <button class="play" data-go="${esc(s.replay)}" aria-label="Replay the analysis">${icon("play")}</button>
  </div>`;
}

export function scoreBlock(score, { size = "sm" } = {}) {
  if (!score || !score.available) {
    return `<div class="score-soon"><strong>Score</strong><span>coming soon</span></div>`;
  }
  return stub(scoreRing(score.total, { size }), "score", { on: isStub(score) });
}

/* ---------------------------------------------------------------- start here */

export function startHereCard(focus, { compact = false, record = true } = {}) {
  if (!focus || !focus.available) {
    return `<div class="card tint">${eyebrow("Start here", "target")}
      <p class="callout muted">${esc(NEUTRAL_FOCUS)}</p>
      ${record ? `<div class="card-actions">${button("Record a swing", { kind: "primary", size: "sm", icon: "camera", "data-action": "analyze" })}</div>` : ""}</div>`;
  }
  const drill = (focus.drills || [])[0];
  rememberDrills(focus.drills || []);
  const body = `${eyebrow("Start here", "target")}
    <div class="card-title">${esc(focus.title)}</div>
    <div style="margin-top:8px">${statusChip(focus.label, focus.status)}</div>
    <p class="card-text">${esc(focus.message)}</p>
    ${compact ? "" : `<div class="card-actions">
      ${drill ? button("Start drill", { kind: "secondary", size: "sm", icon: "play", "data-drill": drill.id }) : ""}
      ${record ? button("Record swing", { kind: "primary", size: "sm", icon: "camera", "data-action": "analyze" }) : ""}</div>`}`;
  return stub(`<div class="card tint">${body}</div>`, "start here", { on: isStub(focus), block: true });
}

/* ---------------------------------------------------------------- consistency */

export function consistencyRow(row) {
  const pct = (n) => (row.total ? (100 * n) / row.total : 0);
  return `<div class="cons-row">
    <div class="cons-head"><span class="cons-name">${esc(row.name)}<small>${esc(row.phase_label)}</small></span>
      <span class="cons-count">${row.good}/${row.total} in range</span></div>
    <div class="cons-bar" role="img" aria-label="${row.good} in range, ${row.attention} slightly out, ${row.problem} clearly out">
      <span style="width:${pct(row.good)}%;background:var(--good)"></span>
      <span style="width:${pct(row.attention)}%;background:var(--attention)"></span>
      <span style="width:${pct(row.problem)}%;background:var(--problem)"></span></div></div>`;
}

/* ---------------------------------------------------------------- trend */

export function trendChart(trend) {
  const points = (trend && trend.points) || [];
  if (points.length < 2) {
    return `<p class="callout muted">${esc((trend && trend.reason) || "Your trend appears after a few swings.")}</p>`;
  }
  const w = 320, h = 120, pad = 10;
  const xs = points.map((_, i) => pad + (i * (w - 2 * pad)) / (points.length - 1));
  const ys = points.map((p) => h - pad - ((p.total / 10) * (h - 2 * pad)));
  const line = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const area = `${line} L${xs[xs.length - 1].toFixed(1)},${h - pad} L${xs[0].toFixed(1)},${h - pad} Z`;
  const grid = [0, 5, 10].map((v) => {
    const y = h - pad - (v / 10) * (h - 2 * pad);
    return `<line x1="0" x2="${w}" y1="${y}" y2="${y}" stroke="var(--line)" stroke-dasharray="3 4"/><text x="0" y="${y - 3}" fill="var(--ink-3)" font-size="9">${v}</text>`;
  }).join("");
  const svg = `<svg viewBox="0 0 ${w} ${h}" class="trend" aria-label="Score trend">
    <defs><linearGradient id="tg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="var(--green)" stop-opacity=".18"/><stop offset="1" stop-color="var(--green)" stop-opacity="0"/></linearGradient></defs>
    ${grid}<path d="${area}" fill="url(#tg)"/><path d="${line}" fill="none" stroke="var(--green)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${xs[xs.length - 1]}" cy="${ys[ys.length - 1]}" r="4.5" fill="#fff" stroke="var(--green)" stroke-width="2.4"/></svg>`;
  return stub(svg, "trend", { on: trend.source === "stub", block: true });
}

/* ---------------------------------------------------------------- drills */

const DRILL_BY_ID = new Map();
export const rememberDrills = (list) => list.forEach((d) => DRILL_BY_ID.set(d.id, d));

export function drillCard(d, { status = null, label = null } = {}) {
  rememberDrills([d]);
  return `<button class="drill-card" data-drill="${esc(d.id)}">
    <span class="drill-thumb">${icon(d.icon || "target")}</span>
    <span class="body"><span class="name" style="display:block">${esc(d.name)}</span>
      <span class="meta" style="display:block">${esc(d.phase_label)} · ${esc(d.checkpoint)} · ${d.minutes} min</span>
      ${status ? `<span style="display:block;margin-top:7px">${statusChip(label, status)}</span>` : ""}</span>
    ${icon("chevron", "chev-sm")}</button>`;
}

export function openDrill(id) {
  const d = DRILL_BY_ID.get(id);
  if (!d) return;
  showSheet(`
    <div class="row" style="gap:14px"><span class="drill-thumb">${icon(d.icon || "target")}</span>
      <div><h2>${esc(d.name)}</h2><div class="row" style="gap:6px;margin-top:6px">${tag(d.phase_label)}${tag(d.checkpoint)}${tag(`${d.minutes} min`)}</div></div></div>
    <p class="callout" style="margin-top:18px">${esc(d.why)}</p>
    <div class="caption" style="margin:22px 0 10px">How to do it</div>
    <ol class="steps">${d.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
    <div class="card flat" style="margin-top:16px;padding:14px 16px"><div class="row">${icon("clock", "ico-inline")}<span class="subhead">${esc(d.reps)}</span></div></div>
    <p class="footnote faint" style="margin-top:14px">Preview drill · not yet reviewed by a coach · video coming soon
      <span class="badge stub dev-only">${esc(d.status)}</span></p>
    <button class="btn btn-primary block" data-close data-drill-done="${esc(d.id)}" style="margin-top:16px">Done — I practised this</button>`);
}

document.addEventListener("click", (event) => {
  const done = event.target.closest("[data-drill-done]");
  if (done) { toast("Nice work. Film a swing to check it."); return; }
  const trigger = event.target.closest("[data-drill]");
  if (trigger) openDrill(trigger.getAttribute("data-drill"));
});
