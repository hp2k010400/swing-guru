// Replay (#/replay/<id>[/<phase:kind>]) — the guided viewer, unchanged, full screen inside the app.
// It plays the cached bundle (analysis.json + swing.mp4); nothing is re-analysed.
import { back } from "../nav.js";
import { esc } from "../ui.js";

export const title = "Replay";
export const tabs = false;

export function render({ args, dev }) {
  const [id, check] = args;
  const params = new URLSearchParams({ embed: "1" });
  if (check) params.set("check", decodeURIComponent(check));
  else params.set("autoplay", "1");
  if (dev) params.set("dev", "1");
  return `<div class="replay-host"><iframe class="replay-frame" title="Guided replay"
    src="/swings/${esc(id)}/index.html?${params}" allow="autoplay; fullscreen"></iframe></div>`;
}

export function mount(view, { args }) {
  const [id] = args;
  const summary = `#/swing/${id}`;
  const onMessage = (event) => {
    const data = event.data || {};
    if (data.source !== "swing-viewer" || (data.type !== "close" && data.type !== "ended")) return;
    back(summary, summary);
  };
  window.addEventListener("message", onMessage);
  return () => window.removeEventListener("message", onMessage);
}
