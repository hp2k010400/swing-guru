// Home — "what should I do today?": Start here, latest swing, checkpoints over time, trend, recent swings.
import { get, greeting, shortDate } from "../api.js";
import { STUBS } from "../data/stubs.js";
import { consistencyRow, drillCard, inRange, scoreBlock, startHereCard, swingCard, trendChart } from "../parts.js";
import {
  badge, button, card, compactBar, emptyState, esc, eyebrow, icon, progressRing, section, stub,
} from "../ui.js";

export const title = "Home";

export async function render() {
  const data = await get("home");
  const user = STUBS.user;
  const initials = user.name.split(" ").map((p) => p[0]).join("").slice(0, 2);
  const header = `<header class="row home-head">
      ${stub(`<div class="avatar">${esc(initials)}</div>`, "user")}
      <div class="spacer"><div class="footnote muted">${esc(greeting())},</div>
        <div class="headline" style="font-size:22px">${esc(user.firstName)}</div></div>
      ${data.streak_weeks ? `<span class="streak">${icon("flame")}${data.streak_weeks} week${data.streak_weeks > 1 ? "s" : ""}</span>` : ""}
    </header>`;

  if (!data.count) return `${compactBar("Home")}<div class="scroll">${header}${firstRun()}</div>`;

  const latest = data.latest;
  const latestCard = card(`
    ${eyebrow("Latest swing", "sparkle")}
    <div class="latest">
      <div class="thumb"><img src="${esc(latest.thumb)}" alt="" /></div>
      <div class="info">
        <div class="card-title">${esc(latest.club || "Swing")}</div>
        <div class="footnote muted" style="margin-top:3px">${esc(shortDate(latest.date))}</div>
        <div class="subhead" style="margin-top:10px">${esc(inRange(latest.counts))}</div>
      </div>
      ${scoreBlock(latest.score)}
    </div>
    <div class="card-actions">
      ${button("View analysis", { kind: "light", size: "sm", "data-go": `#/swing/${latest.id}` })}
      ${button("Replay", { kind: "on-hero", size: "sm", icon: "play", "data-go": latest.replay })}
    </div>`, { tone: "hero", tap: null });

  // One drill tied to the latest swing: the Start-here drill when there is one,
  // otherwise the drill mapped to its first out-of-range check (a mapping, not a priority).
  const rec = latest.focus.available && (latest.focus.drills || []).length
    ? null
    : (latest.recommended || [])[0];
  const drillSection = rec ? section("Your next drill",
    drillCard(rec.drill, { status: rec.checkpoint.status, label: `${rec.checkpoint.name}: ${rec.checkpoint.label}` }),
    { link: { label: "All drills", href: "#/drills" } }) : "";

  const program = data.program;
  const programCard = card(`<div class="row">${progressRing(Math.min(program.swings, program.unlock_after), program.unlock_after)}
      <div class="spacer"><div class="headline">Your improvement program</div>
        <p class="footnote muted" style="margin-top:3px">${program.swings >= program.unlock_after
          ? "Unlocked · a 4-week plan built on your checkpoints"
          : `${program.unlock_after - program.swings} more swing${program.unlock_after - program.swings > 1 ? "s" : ""} to unlock your plan`}</p></div>
      ${badge("Pro", { tone: "solid-gold", ico: "crown" })}</div>`, { tone: "tint" });

  return `${compactBar("Home")}
  <div class="scroll">
    ${header}
    <div class="dev-banner">Dev: violet dashed items are stubs (score, Start here, trend, PR, program, user).</div>
    ${startHereCard(latest.focus)}
    <div style="margin-top:14px">${latestCard}</div>
    ${drillSection}
    ${section(`Across your ${data.count} swing${data.count > 1 ? "s" : ""}`, card(`
      <p class="footnote muted" style="margin:-4px 0 16px">How often each checkpoint was in range.</p>
      ${data.consistency.map(consistencyRow).join("")}`), { link: { label: "History", href: "#/library" } })}
    ${section("Score trend", card(trendChart(data.trend)))}
    <div style="margin-top:14px">${stub(programCard, "program", { block: true })}</div>
    ${section("Recent swings", `<div class="carousel">${data.recent.map((s) => swingCard(s)).join("")}</div>`,
      { link: { label: "View all", href: "#/library" } })}
    ${section("", proCard())}
  </div>`;
}

function firstRun() {
  return card(emptyState({
    ico: "swing",
    title: "Analyze your first swing",
    text: "Film one swing down the line. You'll see every checkpoint, and a drill for anything out of range.",
    action: button("Analyze a swing", { kind: "primary", icon: "camera", "data-action": "analyze" }),
  }));
}

function proCard() {
  return card(`<div class="row" style="gap:8px">${badge("Pro", { tone: "solid-gold", ico: "crown" })}</div>
    <div class="card-title" style="margin-top:10px">Practise with a plan</div>
    <p class="card-text">Four-week programs built on your checkpoints, and your full swing history.</p>
    <div class="card-actions">${button("Try Pro free for 7 days", { kind: "premium", size: "sm", "data-action": "upgrade" })}</div>`,
  { tone: "premium" });
}
