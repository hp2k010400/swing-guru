// Library — every analysed swing, newest first, grouped by day.
import { dayLabel, get, inThisWeek, shortDate } from "../api.js";
import { swingCard } from "../parts.js";
import { button, compactBar, emptyState, esc, icon, largeTitle } from "../ui.js";

export const title = "Library";

let state = { filter: "all", query: "" };

export async function render() {
  const { swings } = await get("swings");
  if (!swings.length) {
    return `${compactBar("Library")}<div class="scroll">${largeTitle("Library")}${emptyState({
      ico: "library", title: "No swings yet",
      text: "Every swing you analyze is saved here, so you can see what's changing.",
      action: button("Analyze a swing", { kind: "primary", icon: "camera", "data-action": "analyze" }),
    })}</div>`;
  }
  const clubs = [...new Set(swings.map((s) => s.club).filter(Boolean))];
  const filters = [["all", `All · ${swings.length}`], ["week", "This week"], ...clubs.map((c) => [`club:${c}`, c])];
  return `${compactBar("Library")}
  <div class="scroll">
    ${largeTitle("Library", `<span class="footnote muted" style="padding-bottom:6px">${swings.length} swings</span>`)}
    <label class="search">${icon("search")}<input type="search" data-search placeholder="Search by club or date" aria-label="Search swings" value="${esc(state.query)}" /></label>
    <div class="filters" style="margin-top:12px">${filters.map(([id, label]) =>
      `<button class="filter" data-filter="${esc(id)}" aria-pressed="${state.filter === id}">${esc(label)}</button>`).join("")}</div>
    ${clubs.length ? "" : `<p class="footnote faint" style="margin-top:10px">Tag the club on a swing to filter by club.</p>`}
    <div data-results></div>
  </div>`;
}

function results(swings) {
  const q = state.query.trim().toLowerCase();
  const shown = swings.filter((s) => {
    if (state.filter === "week" && !inThisWeek(s.date)) return false;
    if (state.filter.startsWith("club:") && s.club !== state.filter.slice(5)) return false;
    if (!q) return true;
    return `${s.club || "swing"} ${shortDate(s.date)} ${dayLabel(s.date)}`.toLowerCase().includes(q);
  });
  if (!shown.length) {
    return emptyState({ ico: "search", title: "No swings match", text: "Try another club or date, or clear the filter." });
  }
  const groups = new Map();
  for (const s of shown) {
    const key = dayLabel(s.date);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  }
  return [...groups].map(([day, items]) =>
    `<div class="caption group-label">${esc(day)} · ${items.length}</div>
     <div class="swing-grid">${items.map((s) => swingCard(s)).join("")}</div>`).join("");
}

export async function mount(view) {
  const { swings } = await get("swings");
  const host = view.querySelector("[data-results]");
  if (!host) return;
  const paint = () => { host.innerHTML = results(swings); };
  paint();
  view.querySelectorAll("[data-filter]").forEach((btn) => btn.addEventListener("click", () => {
    state.filter = btn.dataset.filter;
    view.querySelectorAll("[data-filter]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    paint();
  }));
  view.querySelector("[data-search]").addEventListener("input", (e) => { state.query = e.target.value; paint(); });
}
