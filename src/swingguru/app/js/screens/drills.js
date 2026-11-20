// Drills — the priority drill, drills for this swing's out-of-range checkpoints, and the library by phase.
import { get } from "../api.js";
import { NEUTRAL_FOCUS, drillCard, rememberDrills } from "../parts.js";
import { badge, button, card, compactBar, esc, eyebrow, icon, largeTitle, section, statusChip, stub } from "../ui.js";

export const title = "Drills";

export async function render() {
  const data = await get("drills");
  data.groups.forEach((g) => rememberDrills(g.drills));
  const priority = data.priority;
  const focus = priority && priority.focus;
  let priorityCard;
  if (focus && focus.available && priority.drills.length) {
    const d = priority.drills[0];
    rememberDrills(priority.drills);
    priorityCard = stub(card(`${eyebrow("Your priority drill", "target")}
      <div class="row" style="align-items:flex-start;gap:14px">
        <span class="drill-thumb" style="background:rgba(255,255,255,.14);color:#fff">${icon(d.icon)}</span>
        <div class="spacer"><div class="card-title">${esc(d.name)}</div>
          <p class="card-text muted">${esc(focus.title)} · ${d.minutes} min</p></div></div>
      <div class="card-actions">${button("Start drill", { kind: "light", size: "sm", icon: "play", "data-drill": d.id })}</div>`,
    { tone: "hero" }), "priority", { on: focus.source === "stub", block: true });
  } else {
    priorityCard = card(`${eyebrow("Your priority drill", "target")}<p class="callout muted">${esc(NEUTRAL_FOCUS)}</p>`, { tone: "tint" });
  }

  const forSwing = data.for_your_swing.length
    ? data.for_your_swing.map((f) => drillCard(f.drill, { status: f.checkpoint.status, label: `${f.checkpoint.name}: ${f.checkpoint.label}` })).join("")
    : card(`<p class="callout muted">Every checkpoint in your latest swing was in range.</p>`, { tone: "flat" });

  return `${compactBar("Drills")}
  <div class="scroll">
    ${largeTitle("Drills")}
    ${priorityCard}
    ${section("For your latest swing", `<p class="footnote muted" style="margin:-4px 0 12px">One drill for each checkpoint that was out of range.</p>${forSwing}`)}
    ${section("Drill library", data.groups.map((g) =>
      `<div class="caption group-label">${esc(g.label)}</div>${g.drills.map((d) => drillCard(d)).join("")}`).join(""))}
    ${section("", card(`<div class="row" style="gap:8px">${badge("Pro", { tone: "solid-gold", ico: "crown" })}</div>
      <div class="card-title" style="margin-top:10px">Guided programs</div>
      <p class="card-text">A week-by-week plan that picks your drills and checks them on your next swings.</p>
      <div class="locked"><div class="lock-line">${icon("lock")}Coming with Swing Guru Pro</div></div>`, { tone: "premium" }))}
    <p class="footnote faint" style="margin-top:18px;text-align:center">Drills are in preview and haven't been reviewed by a coach yet.
      <span class="badge stub dev-only">${esc(data.status)}</span></p>
  </div>`;
}
