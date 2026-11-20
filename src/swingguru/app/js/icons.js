// Stroke icons on a 24px grid (1.8-2px strokes set by CSS). Drawn for Swing Guru; no icon library.

const P = {
  home: '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/>',
  library: '<rect x="3.5" y="6.5" width="17" height="13" rx="2.5"/><path d="M6.5 3.5h11"/><path d="m10.5 10.3 4 2.7-4 2.7z"/>',
  drills: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/>',
  profile: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.8 20c.9-3.6 3.8-5.6 7.2-5.6s6.3 2 7.2 5.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  play: '<path d="M8 5.8v12.4c0 .7.8 1.2 1.4.8l9.6-6.2c.5-.4.5-1.2 0-1.6L9.4 5c-.6-.4-1.4.1-1.4.8z"/>',
  chevron: '<path d="m9.5 6 6 6-6 6"/>',
  back: '<path d="m14.5 6-6 6 6 6"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  camera: '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2.2l1.5-2h5.6l1.5 2h2.2A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/><circle cx="12" cy="12.8" r="3.3"/>',
  upload: '<path d="M12 15V4.5M7.5 9 12 4.5 16.5 9"/><path d="M4.5 14.5v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3"/>',
  tips: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1 2V16h5.2v-.2c0-.8.4-1.5 1-2A6 6 0 0 0 12 3z"/>',
  flame: '<path d="M12 22c4 0 7-2.8 7-6.8 0-3-1.8-5.2-3.3-6.8-.4 1.6-1.2 2.8-2.5 3.4.3-3.6-1.3-6.8-4.6-8.8.2 3.1-1.3 5.3-2.9 7.1C4.4 11.6 5 13.1 5 15.2 5 19.2 8 22 12 22z"/>',
  trophy: '<path d="M8 4.5h8v4.2a4 4 0 0 1-8 0z"/><path d="M8 6H5.2a2.8 2.8 0 0 0 3 3.8M16 6h2.8a2.8 2.8 0 0 1-3 3.8"/><path d="M12 12.7V16M8.8 19.5h6.4M10 16h4l.8 3.5H9.2z"/>',
  chart: '<path d="M4 19.5h16"/><path d="m5 15.5 4.5-4.5 3.5 3 6-6.5"/><path d="M15.5 7.5H19V11"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><path d="M12 3.5v3M12 17.5v3M3.5 12h3M17.5 12h3"/>',
  sparkle: '<path d="M12 3.5 13.9 9l5.6 1.9-5.6 1.9L12 18.5l-1.9-5.7-5.6-1.9L10.1 9z"/><path d="M18.5 3.5v3M17 5h3"/>',
  lock: '<rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8h.01"/>',
  check: '<path d="m5.5 12.5 4.2 4.2 8.8-9.2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/>',
  hand: '<path d="M8 12V6.5a1.5 1.5 0 0 1 3 0V11M11 10V5a1.5 1.5 0 0 1 3 0v5M14 10.5v-4a1.5 1.5 0 0 1 3 0V14c0 4-2.6 6.5-6.2 6.5-2.3 0-3.8-1-5-2.8l-2.2-3.4a1.4 1.4 0 0 1 2.2-1.7L8 14.5"/>',
  bell: '<path d="M6.5 16.5V11a5.5 5.5 0 0 1 11 0v5.5l1.5 2H5z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  crown: '<path d="m4 8 4 3.5L12 5l4 6.5L20 8l-1.6 10H5.6z"/><path d="M6 20.5h12"/>',
  restore: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.7M12 16.8h.01"/>',
  doc: '<path d="M7 3.5h7l4 4V20a.5.5 0 0 1-.5.5h-10A.5.5 0 0 1 7 20z"/><path d="M13.5 3.5v4.5H18M9.5 12.5h5M9.5 16h5"/>',
  user: '<circle cx="12" cy="9" r="3.5"/><circle cx="12" cy="12" r="8.5"/><path d="M6.7 18.3c1.2-2 3-3 5.3-3s4.1 1 5.3 3"/>',
  swing: '<circle cx="13.5" cy="4.8" r="1.8"/><path d="M13 7.5 11 13l2.5 3-1 4.5M11 13l-3 1.8-1.6 4.7M12.3 9.3 17 7.5 20 3.5"/>',
  wall: '<path d="M16.5 3.5v17"/><circle cx="10" cy="5.5" r="1.6"/><path d="M10 7.5v5l4.5 1M10 12.5l-2.5 7.5M14.5 13.5l-1 6.5"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  share: '<path d="M12 14.5V4M8 7.5 12 3.5l4 4"/><path d="M6.5 11H6a1.5 1.5 0 0 0-1.5 1.5v6A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5v-6A1.5 1.5 0 0 0 18 11h-.5"/>',
  replay: '<path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3"/><path d="M4.5 4.5v4h4"/><path d="m10.5 9.5 4 2.5-4 2.5z"/>',
  golf: '<path d="M7 20.5V3.5l9 3.8-9 3.9"/><ellipse cx="11" cy="20.5" rx="6" ry="1.2"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 13.5a7.6 7.6 0 0 0 0-3l2-1.5-2-3.4-2.3 1a7.6 7.6 0 0 0-2.6-1.5L14.2 2.6h-4l-.4 2.5a7.6 7.6 0 0 0-2.6 1.5l-2.3-1-2 3.4 2 1.5a7.6 7.6 0 0 0 0 3l-2 1.5 2 3.4 2.3-1a7.6 7.6 0 0 0 2.6 1.5l.4 2.5h4l.4-2.5a7.6 7.6 0 0 0 2.6-1.5l2.3 1 2-3.4z"/>',
};

const FILLED = new Set(["play"]);

export function icon(name, cls = "") {
  const body = P[name] || P.info;
  const fill = FILLED.has(name) ? ' fill="currentColor" stroke="none"' : "";
  return `<svg viewBox="0 0 24 24" aria-hidden="true" class="${cls}"${fill}>${body}</svg>`;
}

export const ICONS = Object.keys(P);
