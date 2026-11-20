// Tiny client for the app API (scripts/serve_app.py -> swingguru.app_api).

const cache = new Map();
// ?demo=0 in the page URL shows the production (no-stub) states.
const DEMO_PARAM = /[?&]demo=0/.test(location.search) ? "?demo=0" : "";

export async function get(path, { fresh = false } = {}) {
  if (!fresh && cache.has(path)) return cache.get(path);
  const request = fetch(`/api/${path}${DEMO_PARAM}`, { cache: "no-store" }).then((r) => {
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  });
  cache.set(path, request);
  request.catch(() => cache.delete(path));
  return request;
}

export const isStub = (obj) => !!obj && obj.source === "stub";

/** Forget cached responses (after a new swing or a club tag). */
export function invalidate() { cache.clear(); }

export async function post(path, body) {
  const r = await fetch(`/api/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const json = await r.json();
  if (!r.ok) throw new Error(json.error || `${path}: ${r.status}`);
  invalidate();
  return json;
}

/* ---------------------------------------------------------------- dates */

const DAY = 86400000;
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());

export function dayLabel(iso, now = new Date()) {
  const d = new Date(iso);
  const diff = Math.round((startOfDay(now) - startOfDay(d)) / DAY);
  const date = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  if (diff === 0) return `Today · ${date}`;
  if (diff === 1) return `Yesterday · ${date}`;
  return date;
}

export function shortDate(iso) {
  const d = new Date(iso);
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short" })} · ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}`;
}

export function inThisWeek(iso, now = new Date()) {
  const d = new Date(iso);
  const monday = startOfDay(new Date(now.getTime() - ((now.getDay() + 6) % 7) * DAY));
  return d >= monday;
}

export function greeting(now = new Date()) {
  const h = now.getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}
