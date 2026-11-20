// Design-system gallery (#/system): every token and component in one place, for review and QA.
import {
  badge, button, card, compactBar, emptyState, eyebrow, iconButton, largeTitle, list, listRow, progressRing,
  scorePill, scoreRing, section, segmented, stat, statusChip, statusText, tag,
} from "../ui.js";
import { ICONS, icon } from "../icons.js";

export const title = "Design system";
export const tab = "profile";

const sw = (name, value, dark = false) =>
  `<div><div class="swatch" style="background:${value};color:${dark ? "#fff" : "var(--ink)"}">${name}</div></div>`;

export function render() {
  return `${compactBar("Design system")}
  <div class="scroll">
    ${largeTitle("Design system")}
    ${section("Colour", `<div class="caption group-label" style="margin-top:0">Surfaces & ink</div>
      <div class="swatches">${sw("bg", "var(--bg)")}${sw("surface", "var(--surface)")}${sw("surface-2", "var(--surface-2)")}${sw("line", "var(--line)")}
        ${sw("ink", "var(--ink)", true)}${sw("ink-2", "var(--ink-2)", true)}${sw("ink-3", "var(--ink-3)", true)}${sw("page", "var(--page)")}</div>
      <div class="caption group-label">Brand & premium</div>
      <div class="swatches">${sw("green", "var(--green)", true)}${sw("deep", "var(--green-deep)", true)}${sw("soft", "var(--green-soft)")}${sw("tint", "var(--green-tint)")}
        ${sw("gold", "var(--gold)", true)}${sw("gold-soft", "var(--gold-soft)")}</div>
      <div class="caption group-label">Status</div>
      <div class="swatches">${sw("good", "var(--good)", true)}${sw("attention", "var(--attention)", true)}${sw("problem", "var(--problem)", true)}${sw("info", "var(--info)", true)}</div>`)}
    ${section("Type", `<div class="card stack">
      <div style="font:var(--t-large);letter-spacing:var(--track-tight)">Large title 34</div>
      <div style="font:var(--t-title1)">Title 1 · 28</div>
      <div style="font:var(--t-title2)">Title 2 · 22</div>
      <div class="headline">Headline · 17</div>
      <div style="font:var(--t-body)">Body · 16. Your hips pushed toward the ball through impact.</div>
      <div class="callout muted">Callout · 15 secondary</div>
      <div class="footnote muted">Footnote · 13</div>
      <div class="caption">Caption · 12 uppercase</div></div>`)}
    ${section("Buttons", `<div class="stack">
      ${button("Record a swing", { kind: "primary", icon: "camera", block: true })}
      <div class="row" style="flex-wrap:wrap">${button("Watch drill", { kind: "secondary", icon: "play" })}${button("Try Pro free", { kind: "premium", icon: "crown" })}${button("View all", { kind: "quiet" })}</div>
      <div class="row">${button("Small", { kind: "primary", size: "sm" })}${button("Ghost", { kind: "ghost", size: "sm" })}${iconButton("close", "Close")}${iconButton("share", "Share")}</div></div>`)}
    ${section("Chips & badges", `<div class="card stack">
      <div class="row" style="flex-wrap:wrap">${statusChip("Hip hinge good", "good")}${statusChip("Slightly shallow", "attention")}${statusChip("Early extension", "problem")}${statusChip("Hand position", "info")}</div>
      <div class="row" style="flex-wrap:wrap">${statusText("On plane", "good")}${statusText("Slight early extension", "attention")}</div>
      <div class="row" style="flex-wrap:wrap">${tag("Driver")}${tag("Impact")}${badge("Pro", { tone: "solid-gold", ico: "crown" })}${badge("PR", { ico: "trophy" })}${badge("New", { tone: "green" })}${badge("Free", { tone: "green" })}${scorePill(8.2)}${scorePill(6.1)}${scorePill(4.1)}</div></div>`)}
    ${section("Cards", `<div class="stack">
      ${card(`${eyebrow("Today's focus", "target")}<div class="card-title">Impact · Extension</div><p class="card-text">Keep your hips back as you swing through.</p>
        <div class="card-actions">${button("Watch drill", { kind: "secondary", size: "sm", icon: "play" })}${button("Record swing", { kind: "primary", size: "sm", icon: "camera" })}</div>`, { tone: "tint" })}
      ${card(`${eyebrow("Latest swing", "sparkle")}<div class="row"><div class="spacer"><div class="card-title">8 of 8 checkpoints scored</div><p class="card-text muted">Hero card on deep green.</p></div>${scoreRing(8.2, { size: "sm" })}</div>`, { tone: "hero" })}
      ${card(`<div class="row"><div class="spacer"><div class="row" style="gap:8px">${badge("Pro", { tone: "solid-gold", ico: "crown" })}</div>
        <div class="card-title" style="margin-top:10px">Build a plan around your swing</div><p class="card-text">Four-week programs and your full history.</p></div></div>
        <div class="card-actions">${button("Try free for 7 days", { kind: "premium", size: "sm" })}</div>`, { tone: "premium" })}
      ${card(`<div class="row">${progressRing(2, 3)}<div><div class="headline">Your improvement program</div><p class="footnote muted" style="margin-top:3px">1 more swing to unlock your trend</p></div></div>`, { tone: "tint" })}</div>`)}
    ${section("Score", `<div class="card">${scoreRing(7.6)}<p class="footnote muted" style="text-align:center;margin-top:10px">Based on 8 checkpoints</p></div>`)}
    ${section("Stats", `<div class="stats">${stat({ ico: "golf", value: "12", label: "Swings" })}${stat({ ico: "chart", value: "6.8", label: "Average" })}${stat({ ico: "trophy", value: "8.2", label: "Best" })}</div>`)}
    ${section("Rows", `${list([listRow({ ico: "hand", label: "Handedness", control: segmented("demo", [{ value: "a", label: "Auto" }, { value: "r", label: "Right" }, { value: "l", label: "Left" }], "r") }),
      listRow({ ico: "crown", label: "Swing Guru Pro", tone: "accent", action: "noop" }), listRow({ ico: "restore", label: "Clear cache", tone: "danger", action: "noop", chevron: false })], "Settings")}
      <div style="margin-top:12px"><button class="verdict-row"><span class="label">Hip hinge</span>${statusText("Hip hinge good", "good")}</button>
      <button class="verdict-row"><span class="label">Extension</span>${statusText("Early extension", "problem")}</button></div>`)}
    ${section("Drill card", `<button class="drill-card"><span class="drill-thumb">${icon("wall")}</span><span class="body"><span class="name">Butt-to-wall drill</span><span class="meta" style="display:block">Impact · Extension · 5 min</span></span>${button("Start", { kind: "secondary", size: "sm" })}</button>`)}
    ${section("Empty & loading", `${card(emptyState({ ico: "library", title: "No swings yet", text: "Every swing you analyze is saved here.", action: button("Analyze a swing", { kind: "primary", size: "sm" }) }))}
      <div class="stack" style="margin-top:12px"><div class="skeleton" style="height:18px;width:60%"></div><div class="skeleton" style="height:120px"></div></div>`)}
    ${section("Icons", `<div class="card" style="display:grid;grid-template-columns:repeat(6,1fr);gap:14px;color:var(--green)">
      ${ICONS.map((n) => `<div title="${n}" style="display:grid;place-items:center">${icon(n).replace("<svg", '<svg style="width:24px;height:24px;stroke:currentColor;stroke-width:1.8;fill:none;stroke-linecap:round;stroke-linejoin:round"')}</div>`).join("")}</div>`)}
  </div>`;
}
