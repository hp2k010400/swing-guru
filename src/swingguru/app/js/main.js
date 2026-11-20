// Swing Guru app shell: routing, tab bar, the Analyze sheet and screen mounting.
import { icon } from "./icons.js";
import { badge, closeSheet, esc, showSheet, statusChip, toast } from "./ui.js";
import * as home from "./screens/home.js";
import * as library from "./screens/library.js";
import * as drills from "./screens/drills.js";
import * as profile from "./screens/profile.js";
import * as system from "./screens/system.js";
import * as swing from "./screens/summary.js";
import * as replay from "./screens/replay.js";
import * as processing from "./screens/processing.js";
import { invalidate } from "./api.js";
import { rememberScroll, scrollFor, visited } from "./nav.js";

export const DEV = /[?&]dev=1/.test(location.search);
if (DEV) document.documentElement.classList.add("dev");

const TABS = [
  { id: "home", label: "Home", ico: "home" },
  { id: "library", label: "Library", ico: "library" },
  { id: "analyze", fab: true, label: "Analyze a swing" },
  { id: "drills", label: "Drills", ico: "drills" },
  { id: "profile", label: "Profile", ico: "profile" },
];

const ROUTES = { home, library, drills, profile, system, swing, replay, processing };

const device = document.getElementById("device");
const viewHost = document.getElementById("views");
const tabbar = document.getElementById("tabbar");

function renderTabbar(active) {
  tabbar.innerHTML = TABS.map((t) =>
    t.fab
      ? `<button class="tab-fab" data-action="analyze" aria-label="${esc(t.label)}">${icon("plus")}</button>`
      : `<button class="tab" data-go="#/${t.id}" ${t.id === active ? 'aria-current="page"' : ""}>${icon(t.ico)}<span>${esc(t.label)}</span></button>`
  ).join("");
}

function parse() {
  const [name = "home", ...args] = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { name: ROUTES[name] ? name : "home", args };
}

let current = null;
let currentHash = null;
let routing = 0;

async function route() {
  const { name, args } = parse();
  const screen = ROUTES[name];
  const hash = location.hash || "#/home";
  const ticket = ++routing;
  closeSheet(true);
  if (current && currentHash) {
    const scroller = current.querySelector(".scroll");
    if (scroller) rememberScroll(currentHash, scroller.scrollTop);
    if (current._unmount) current._unmount();
  }
  const view = document.createElement("section");
  view.className = "view view-enter";
  view.dataset.screen = name;
  let html;
  try {
    html = await screen.render({ args, dev: DEV });
  } catch (error) {
    html = `<div class="scroll"><div class="empty" style="margin-top:120px"><h3>Something went wrong</h3>
      <p>${esc(error.message || error)}</p><button class="btn btn-primary" data-go="#/home">Back to Home</button></div></div>`;
  }
  if (ticket !== routing) return;  // a newer navigation won
  view.innerHTML = html;
  const tabs = screen.tabs !== false;
  view.classList.toggle("no-tabs", !tabs);
  tabbar.hidden = !tabs;
  renderTabbar(screen.tab || name);
  if (current) current.remove();
  viewHost.append(view);
  current = view;
  currentHash = hash;
  visited(hash);
  wireScroll(view);
  const scroller = view.querySelector(".scroll");
  if (scroller) scroller.scrollTop = scrollFor(hash);
  if (screen.mount) view._unmount = await screen.mount(view, { args, dev: DEV }) || null;
  document.title = `${screen.title || "Swing Guru"} · Swing Guru`;
}

/* The Analyze sheet hands us a File: send it to the pipeline, then watch it process. */
document.addEventListener("sg:upload", async (event) => {
  const file = event.detail;
  closeSheet();
  if (!file.type.startsWith("video/") && !/\.(mp4|mov|m4v|avi)$/i.test(file.name)) {
    toast("Please choose a video file");
    return;
  }
  toast("Uploading your swing…");
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      body: file,
      headers: { "X-File-Name": encodeURIComponent(file.name), "X-File-Modified": String(file.lastModified || "") },
    });
    const job = await response.json();
    if (job.id) {
      invalidate();
      location.hash = `#/processing/${job.id}`;
    } else {
      location.hash = `#/processing/error-${encodeURIComponent(job.error || "failed")}`;
    }
  } catch (error) {
    location.hash = "#/processing/error-failed";
  }
});

function wireScroll(view) {
  const scroller = view.querySelector(".scroll");
  const bar = view.querySelector(".compact-bar:not(.always)");
  if (!scroller || !bar) return;
  const onScroll = () => bar.classList.toggle("show", scroller.scrollTop > 46);
  scroller.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
}

export function openAnalyzeSheet() {
  showSheet(`
    <h2>Analyze a swing</h2>
    <p class="sheet-sub">Film down the line, from behind your hands. One swing per clip works best.</p>
    <div style="margin-top:20px">
      <label class="option primary" role="button" tabindex="0">
        <span class="ico">${icon("camera")}</span>
        <span><span class="t">Record a swing</span><span class="d">Use your camera · 60 fps if your phone offers it</span></span>
        ${icon("chevron", "chev")}
        <input type="file" accept="video/*" capture="environment" hidden data-upload />
      </label>
      <label class="option" role="button" tabindex="0">
        <span class="ico">${icon("upload")}</span>
        <span><span class="t">Upload a video</span><span class="d">From your camera roll · the original file, not a chat copy</span></span>
        ${icon("chevron", "chev")}
        <input type="file" accept="video/*" hidden data-upload />
      </label>
      <button class="option" data-action="tips">
        <span class="ico">${icon("tips")}</span>
        <span><span class="t">Filming tips</span><span class="d">Get the most accurate analysis</span></span>
        ${icon("chevron", "chev")}
      </button>
    </div>`, {
    onOpen(sheet) {
      sheet.querySelectorAll("[data-upload]").forEach((input) =>
        input.addEventListener("change", () => {
          if (input.files && input.files[0]) document.dispatchEvent(new CustomEvent("sg:upload", { detail: input.files[0] }));
        }));
    },
  });
}

export function openFilmingTips() {
  const tip = (ico, t, d) => `<div class="row" style="align-items:flex-start;gap:14px;padding:12px 0">
      <span class="drill-thumb" style="width:44px;height:44px;border-radius:13px">${icon(ico)}</span>
      <span><span class="headline">${esc(t)}</span><span class="footnote muted" style="display:block;margin-top:3px">${esc(d)}</span></span></div>`;
  showSheet(`
    <h2>Filming tips</h2>
    <p class="sheet-sub">Two minutes of set-up gives a much sharper analysis.</p>
    <div style="margin-top:14px">
      ${tip("camera", "Behind your hands, down the line", "3–4 m back, lens at hip height, looking along your target line.")}
      ${tip("profile", "Whole body, whole club", "Head and feet in frame all swing, with room for the club at the top.")}
      ${tip("clock", "60 fps and the original file", "Higher frame rates keep the club sharp. Chat apps shrink the video, so upload the original.")}
      ${tip("sparkle", "Good light, steady phone", "Daylight or bright floodlights. A tripod or propped phone is best; handheld still works.")}
    </div>
    <button class="btn btn-primary block" data-close style="margin-top:18px">Got it</button>`);
}

export function openScoreHelp() {
  showSheet(`
    <h2>How your swing is measured</h2>
    <p class="sheet-sub">Every checkpoint is measured from your video: angles and positions at six moments of the swing.</p>
    <div style="margin-top:18px" class="stack">
      <div class="row">${statusChip("In range", "good")}<span class="footnote muted">within the usual range for that checkpoint</span></div>
      <div class="row">${statusChip("Slightly out", "attention")}<span class="footnote muted">a little outside it</span></div>
      <div class="row">${statusChip("Clearly out", "problem")}<span class="footnote muted">well outside it</span></div>
    </div>
    <div class="card flat" style="margin-top:20px;padding:16px">
      <div class="headline">About the Swing Score</div>
      <p class="footnote muted" style="margin-top:6px">We're calibrating the Swing Score before it goes live, so it isn't calculated yet.
        The ranges behind the colours are still provisional too; each checkpoint shows why it got its colour.</p></div>
    <button class="btn btn-primary block" data-close style="margin-top:18px">Got it</button>`);
}

export function openPro() {
  showSheet(`
    <div class="row" style="gap:8px">${badge("Pro", { tone: "solid-gold", ico: "crown" })}</div>
    <h2 style="margin-top:12px">Swing Guru Pro</h2>
    <p class="sheet-sub">Everything you need to keep improving, week after week.</p>
    <div class="stack" style="margin-top:18px">
      ${["Guided four-week programs built on your checkpoints", "Your full swing history and comparisons", "Priority analysis"]
        .map((t) => `<div class="row">${icon("check", "ico-inline")}<span class="callout">${esc(t)}</span></div>`).join("")}
    </div>
    <button class="btn btn-primary block" data-close style="margin-top:22px">Try free for 7 days</button>
    <p class="footnote faint" style="text-align:center;margin-top:10px">Pro isn't available in the preview yet. <span class="badge stub dev-only">stub</span></p>`);
}

document.addEventListener("click", (event) => {
  const go = event.target.closest("[data-go]");
  if (go) {
    event.preventDefault();
    const href = go.getAttribute("data-go");
    if (href.startsWith("#")) location.hash = href; else location.href = href;
    return;
  }
  const action = event.target.closest("[data-action]");
  if (!action) return;
  const name = action.getAttribute("data-action");
  if (name === "analyze") openAnalyzeSheet();
  if (name === "tips") openFilmingTips();
  if (name === "score-help") openScoreHelp();
  if (name === "upgrade") openPro();
  if (["restore", "signin", "support"].includes(name)) toast("Coming soon");
  document.dispatchEvent(new CustomEvent("sg:action", { detail: { name, el: action } }));
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSheet();
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[role=button][data-go]")) event.target.click();
});

window.addEventListener("hashchange", route);
route();
