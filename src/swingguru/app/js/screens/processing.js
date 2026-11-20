// Processing (#/processing/<job>) — the real pipeline's stages as they complete, then the Summary.
import { get, invalidate } from "../api.js";
import { replace } from "../nav.js";
import { button, esc, icon } from "../ui.js";

export const title = "Analysing";
export const tabs = false;

const TIPS = [
  "Film from behind your hands, looking along your target line.",
  "Keep your head and feet in the frame for the whole swing.",
  "60 fps keeps the club sharper through the fastest part of the swing.",
  "Upload the original video. Chat apps shrink it and blur the club.",
  "A propped-up phone gives the steadiest analysis.",
];

const ERROR_COPY = {  // for failures before a job exists (upload rejected)
  unreadable: ["We couldn't read this video", "Try exporting it again as an MP4 or MOV, then upload the original file."],
  too_large: ["This video is too large", "Trim it to one swing, or record at 1080p."],
  failed: ["We couldn't upload this video", "Check your connection and try again."],
};

export function render({ args }) {
  const [job] = args;
  if (job && job.startsWith("error-")) {
    const code = decodeURIComponent(job.slice(6));
    const [t, a] = ERROR_COPY[code] || ERROR_COPY.failed;
    return errorView(t, a);
  }
  return `<div class="scroll processing">
    <div class="proc-hero">
      <div class="proc-orb"><span></span>${icon("swing")}</div>
      <h1>Analysing your swing</h1>
      <p class="callout muted" data-proc-sub>This usually takes a minute or two.</p>
    </div>
    <ol class="proc-stages" data-stages></ol>
    <p class="footnote faint proc-elapsed" data-elapsed></p>
    <div class="card flat proc-tip"><div class="caption">Tip</div><p class="callout" data-tip>${esc(TIPS[0])}</p></div>
  </div>`;
}

function errorView(titleText, advice) {
  return `<div class="scroll processing">
    <div class="proc-hero">
      <div class="proc-orb error">${icon("info")}</div>
      <h1>${esc(titleText)}</h1>
      <p class="callout muted">${esc(advice)}</p>
    </div>
    <div class="stack" style="margin-top:26px">
      ${button("Try another video", { kind: "primary", block: true, icon: "upload", "data-action": "analyze" })}
      ${button("Filming tips", { kind: "secondary", block: true, icon: "tips", "data-action": "tips" })}
      ${button("Back to Home", { kind: "quiet", block: true, "data-go": "#/home" })}
    </div></div>`;
}

export function mount(view, { args }) {
  const [job] = args;
  if (!job || job.startsWith("error-")) return null;
  let tip = 0, timer = 0, tipTimer = 0, stopped = false;
  const stagesEl = view.querySelector("[data-stages]");
  const elapsedEl = view.querySelector("[data-elapsed]");
  tipTimer = setInterval(() => {
    tip = (tip + 1) % TIPS.length;
    const el = view.querySelector("[data-tip]");
    if (el) { el.classList.remove("fade-in"); void el.offsetWidth; el.textContent = TIPS[tip]; el.classList.add("fade-in"); }
  }, 6000);

  const paint = (j) => {
    stagesEl.innerHTML = j.stages.map((s) => `<li class="stage ${s.state}">
      <span class="mark">${s.state === "done" ? icon("check") : s.state === "active" ? "<i></i>" : ""}</span>
      <span>${esc(s.label)}</span></li>`).join("");
    const secs = Math.round(j.elapsed_s || 0);
    elapsedEl.textContent = secs >= 5 ? `Still working · ${secs}s` : "";
    if (secs > 120) view.querySelector("[data-proc-sub]").textContent = "Longer clips take a little more time. Hang tight.";
  };

  const poll = async () => {
    if (stopped) return;
    let j;
    try { j = await get(`jobs/${job}`, { fresh: true }); } catch (e) { timer = setTimeout(poll, 1500); return; }
    if (j.state === "done") {
      paint(j);
      invalidate();
      setTimeout(() => replace(`#/swing/${j.swing_id}/${j.duplicate ? "existing" : "new"}`), j.duplicate ? 200 : 600);
      return;
    }
    if (j.state === "error") {
      view.innerHTML = errorView(j.error_title, j.error_advice);
      return;
    }
    paint(j);
    timer = setTimeout(poll, 1000);
  };
  poll();
  return () => { stopped = true; clearTimeout(timer); clearInterval(tipTimer); };
}
