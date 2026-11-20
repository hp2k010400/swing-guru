/* Swing Guru analysis viewer (standalone, no dependencies).
 *
 * Reads analysis.json produced by scripts/build_analysis.py and plays the real
 * swing video between moments. Just before a moment's frame is presented the
 * video pauses and steps forward onto it (never back), one measurement overlay
 * draws in, and the card shows its result. Several checks at one moment change
 * without moving the video. After Impact the results screen appears.
 *
 * All measurement logic lives in Python; this file only draws what it is given.
 */
(function () {
  "use strict";

  var HOLD_MS = 1800;  // per check; 1.5-2.0 s reads as a guided pause, not a slideshow
  var FADE_MS = 170;
  var SWITCH_MS = 130;
  // Overlay palette (matches --sg-mark in app.css). No dark halos: the SVG has
  // one soft drop shadow, so lines read on sky and on mat without looking technical.
  var GREEN = "#72c89a";
  var WHITE = "rgba(255,255,255,0.95)";
  var PILL = "rgba(16,19,17,0.62)";
  var SVG_NS = "http://www.w3.org/2000/svg";
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var data = null;
  var events = [];
  var flat = [];
  var allChecks = [];
  var DEV = /[?&]dev=1/.test(location.search);
  // ?autoplay=1 starts the guided walkthrough as soon as the video is ready
  // (the video is muted, so browsers allow it); ?review=1 is the large layout
  // used by the human review pack.
  var AUTOPLAY = /[?&]autoplay=1/.test(location.search);
  if (/[?&]review=1/.test(location.search)) document.documentElement.classList.add("review");
  // ?embed=1: running inside the Swing Guru app. The top-left button returns to
  // the app's summary, and the end of the walkthrough hands back to the app
  // instead of opening this viewer's own results screen.
  // ?check=<phase:kind> opens the walkthrough paused on that check.
  var EMBED = /[?&]embed=1/.test(location.search);
  // Overlay v2 (1.0.1 / staging only): requested explicitly with ?overlay=2. Without it the viewer
  // draws exactly as before (drawOverlay), so builds that never ask for it are unaffected.
  var OVERLAY_V2 = /[?&]overlay=2(&|$)/.test(location.search);
  // Overlay v3 (development / staging only): requested explicitly with ?overlay=3. Every v3 change in
  // the shared code below is guarded by OVERLAY_V3, so v1 and v2 run exactly as before.
  var OVERLAY_V3 = /[?&]overlay=3(&|$)/.test(location.search);
  // Video export (staging only, scripts/export_replay_video.py): v3 plus ?capture=1 lays the replay
  // out as a clean 9:16 frame and ends on the stored Swing Score. Never set by any app build.
  var CAPTURE = OVERLAY_V3 && /[?&]capture=1(&|$)/.test(location.search);
  if (CAPTURE) document.documentElement.classList.add("capture");
  // 1.0.1 app builds only (both opt-in, so earlier builds render exactly as before):
  // ?theme=dark|light follows the app's appearance setting; ?cards=simple shows each card as the
  // status dot and one plain sentence (no labels, numbers or jargon; the analysis is unchanged).
  var THEME = (location.search.match(/[?&]theme=(dark|light)(&|$)/) || [])[1];
  if (THEME) document.documentElement.setAttribute("data-theme", THEME);
  var SIMPLE_CARDS = /[?&]cards=simple(&|$)/.test(location.search);
  if (SIMPLE_CARDS) document.documentElement.classList.add("simple-cards");
  var DEEP_CHECK = (location.search.match(/[?&]check=([^&]+)/) || [])[1];
  DEEP_CHECK = DEEP_CHECK ? decodeURIComponent(DEEP_CHECK) : null;
  if (EMBED) document.documentElement.classList.add("embed");
  // ?analysis=<url>&video=<url>: load a stored analysis (the mobile app passes
  // short-lived signed URLs) instead of the bundle next to this page.
  function param(name) {
    var m = location.search.match(new RegExp("[?&]" + name + "=([^&]+)"));
    return m ? decodeURIComponent(m[1]) : null;
  }
  function notifyApp(type) {
    var c = active();
    var message = { source: "swing-viewer", type: type, checkId: c ? c.id : null };
    try {
      // Inside the mobile app's WebView the native bridge carries the message.
      var bridge = window.ReactNativeWebView;
      if (bridge) bridge.postMessage(JSON.stringify(message)); else window.parent.postMessage(message, "*");
    } catch (e) { /* ignore */ }
  }
  var els = {};
  var objectUrl = null;
  var state = {
    mode: "loading",
    checkIndex: -1,
    pendingEvent: 0,
    dwellRemaining: 0,
    awaitingSeek: null,
    raf: 0,
    frameCallback: 0,
    transition: 0,
    lastTick: 0,
    speed: 1,
    screen: "analysis",
    endTimer: 0,
    layoutKey: "",
    overlayBox: null,
    overlayAnchor: null,
    error: "",
    adhoc: null,
    stats: { seeks: 0, holds: 0, holdSeeks: 0, backwardHoldSeeks: 0, maxHoldSeekFrames: 0, sameMomentSeeks: 0, loadstarts: 0, sourceSets: 0 }
  };

  function $(id) { return document.getElementById(id); }
  function now() { return window.performance ? performance.now() : Date.now(); }

  /* ------------------------------------------------------------ helpers */

  function fps() { return data && data.fps > 0 ? data.fps : 30; }
  function step() { return 1 / fps(); }
  function frameTime(t) { return Number(t) + 0.5 * step(); }
  function segStart() { return data ? Number(data.segment_start_s) || 0 : 0; }
  function segEnd() {
    var d = els.video && isFinite(els.video.duration) ? els.video.duration : Infinity;
    return Math.min(data ? Number(data.segment_end_s) || d : d, d);
  }
  function active() { return state.adhoc || (state.checkIndex >= 0 ? flat[state.checkIndex] : null); }
  function eventOf(check) {
    for (var i = 0; i < events.length; i++) if (events[i].checks.indexOf(check) >= 0) return i;
    return -1;
  }
  function firstEventAtOrAfter(t) {
    for (var i = 0; i < events.length; i++) if (frameTime(events[i].time_s) >= t - 0.25 * step()) return i;
    return events.length;
  }
  function clearTransition() { if (state.transition) { clearTimeout(state.transition); state.transition = 0; } }
  // How long a check holds: HOLD_MS for v1 / v2 (unchanged); v3 adds time for its sequenced reveal.
  function holdFor(check) { return OVERLAY_V3 ? v3HoldMs(check) : HOLD_MS; }
  function wrap(a) { var w = ((a + 180) % 360 + 360) % 360 - 180; return w === -180 ? 180 : w; }

  /* --------------------------------------------------------------- load */

  function fetchBlob(url) {
    return fetch(url).then(function (r) { if (!r.ok) throw new Error("video " + r.status); return r.blob(); });
  }

  function load() {
    // When the app passes both URLs the video downloads alongside the analysis
    // instead of after it (the replay's longest wait).
    var early = param("video") ? fetchBlob(param("video")) : null;
    if (early) early.catch(function () { /* reported when awaited below */ });
    fetch(param("analysis") || "analysis.json", { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("analysis.json " + r.status); return r.json(); })
      .then(function (json) {
        data = json;
        // The walkthrough shows the guided checks; the results screen shows all.
        allChecks = [];
        (data.phases || []).forEach(function (p) { p.checks.forEach(function (c) { c.phaseLabel = p.label; allChecks.push(c); }); });
        events = (data.phases || []).map(function (p) {
            return { phase: p.phase, label: p.label, time_s: p.time_s,
                     checks: p.checks.filter(function (c) { return c.guided !== false || (OVERLAY_V3 && !!c.observation); }) };
          })
          .filter(function (e) { return e.checks.length && e.time_s !== null; });
        if (OVERLAY_V3) {
          // A check measured on a neighbouring frame (engine plane_neighbour) holds on that frame, so
          // its line sits on the club in the picture; it becomes its own stop next to its moment.
          var split = [];
          events.forEach(function (e) {
            var own = e.checks.filter(function (c) { return c.measured_frame && c.time_s !== e.time_s; });
            var rest = e.checks.filter(function (c) { return own.indexOf(c) < 0; });
            if (rest.length) split.push({ phase: e.phase, label: e.label, time_s: e.time_s, checks: rest });
            own.forEach(function (c) { split.push({ phase: e.phase, label: e.label, time_s: c.time_s, checks: [c] }); });
          });
          events = split;
        }
        events.sort(function (a, b) { return a.time_s - b.time_s; });
        flat = [];
        events.forEach(function (e) { e.checks.forEach(function (c) { c.phaseLabel = e.label; flat.push(c); }); });
        buildPhaseChips();
        buildResults();
        return early || fetchBlob(data.video);
      })
      .then(function (blob) {
        objectUrl = URL.createObjectURL(blob);
        state.stats.sourceSets += 1;
        els.video.src = objectUrl;
        els.video.load();
      })
      .catch(function (error) { fail("Could not load this analysis (" + error.message + ")."); });
  }

  /* ---------------------------------------------------- frame-true clock */

  function watchFrames() {
    var v = els.video;
    if (v && !state.frameCallback && typeof v.requestVideoFrameCallback === "function") {
      state.frameCallback = v.requestVideoFrameCallback(onFrame);
    }
  }
  function unwatchFrames() {
    var v = els.video;
    if (v && state.frameCallback && v.cancelVideoFrameCallback) v.cancelVideoFrameCallback(state.frameCallback);
    state.frameCallback = 0;
  }
  function onFrame(_t, meta) {
    state.frameCallback = 0;
    if (state.mode !== "playing") return;
    upcoming(Number(meta && meta.mediaTime), false);
    if (state.mode === "playing") watchFrames();
  }
  function upcoming(t, polling) {
    if (!isFinite(t)) return;
    var s = step();
    if (t >= segEnd() - 0.5 * s) { finishWalkthrough(); return; }
    var i = state.pendingEvent;
    if (i < 0 || i >= events.length) return;
    var look = polling ? s + 0.017 * (els.video.playbackRate || 1) : s;
    if (t + look >= Number(events[i].time_s) - 0.25 * s) beginHold(i);
  }
  function ensureLoop() { if (!state.raf) { state.lastTick = now(); state.raf = requestAnimationFrame(tick); } }
  function tick(stamp) {
    state.raf = 0;
    var dt = Math.max(0, stamp - (state.lastTick || stamp));
    state.lastTick = stamp;
    if (state.mode === "holding" && !state.transition) {
      state.dwellRemaining -= dt;
      if (state.dwellRemaining <= 0) advance();
    } else if (state.mode === "playing" && !state.frameCallback) {
      upcoming(Number(els.video.currentTime), true);
    }
    renderProgress();
    if (state.mode === "playing" || state.mode === "holding" || state.mode === "seeking") state.raf = requestAnimationFrame(tick);
  }

  /* ----------------------------------------------------------- playback */

  function seek(t, kind) {
    state.stats.seeks += 1;
    try { els.video.currentTime = t; } catch (e) { fail("Could not seek in the video."); }
    state.lastSeek = { to: t, kind: kind };
  }

  function beginHold(i) {
    var v = els.video;
    v.pause();
    unwatchFrames();
    state.stats.holds += 1;
    var target = frameTime(events[i].time_s);
    var offset = target - Number(v.currentTime);
    if (Math.abs(offset) > 0.25 * step()) {
      state.stats.holdSeeks += 1;
      if (offset < 0) state.stats.backwardHoldSeeks += 1;
      state.stats.maxHoldSeekFrames = Math.max(state.stats.maxHoldSeekFrames, Math.abs(offset) / step());
      state.mode = "seeking";
      state.awaitingSeek = { kind: "hold", event: i };
      seek(target, "hold");
      render();
      ensureLoop();
      return;
    }
    enterHold(i);
  }

  function enterHold(i) {
    state.mode = "holding";
    state.pendingEvent = i + 1;
    state.checkIndex = flat.indexOf(events[i].checks[0]);
    state.dwellRemaining = holdFor(active());
    render(true);
    ensureLoop();
  }

  function advance() {
    var check = active();
    var i = eventOf(check);
    var e = events[i];
    var pos = e ? e.checks.indexOf(check) : -1;
    if (e && pos + 1 < e.checks.length) {
      var before = state.stats.seeks;
      fade(SWITCH_MS, function () {
        state.checkIndex = flat.indexOf(e.checks[pos + 1]);
        state.dwellRemaining = holdFor(active());
        state.stats.sameMomentSeeks += state.stats.seeks - before;
        render(true);
      });
      return;
    }
    fade(FADE_MS, function () {
      state.checkIndex = -1;
      state.mode = "playing";
      state.pendingEvent = Math.max(state.pendingEvent, firstEventAtOrAfter(Number(els.video.currentTime) + step()));
      render();
      resume();
      ensureLoop();
    });
  }

  function fade(ms, next) {
    clearTransition();
    if (reducedMotion || !els.overlay.firstChild) { next(); return; }
    els.overlay.classList.add("leaving");
    state.transition = setTimeout(function () {
      state.transition = 0;
      els.overlay.classList.remove("leaving");
      next();
    }, ms);
  }

  function resume() {
    watchFrames();
    var p = els.video.play();
    if (p && p.catch) p.catch(function (err) {
      if (err && err.name === "AbortError") { setTimeout(function () { if (state.mode === "playing") resume(); }, 80); return; }
      fail("Playback could not start.");
    });
  }

  function finishWalkthrough() {
    els.video.pause();
    unwatchFrames();
    state.mode = "ended";
    state.checkIndex = -1;
    render();
    clearTimeout(state.endTimer);
    state.endTimer = setTimeout(function () {
      if (state.mode !== "ended") return;
      if (CAPTURE) { captureScore(); return; }
      if (EMBED) notifyApp("ended"); else showScreen("results");
    }, 900);
  }

  /* Capture only: the checkpoint card sits at the top of the frame (usually sky in a down-the-line clip).
   * reserveCaptureCard measures it there before the overlay is drawn, so the verdict bubble keeps below
   * it; placeCaptureCard then moves it to the bottom only when the top would cover more of what this
   * check draws (its measured lines and joints, the golfer's head, the bubble) than the bottom would. */
  function stageRect(el) {
    var r = el.getBoundingClientRect(), s = els.stage.getBoundingClientRect();
    return { x: r.left - s.left, y: r.top - s.top, w: r.width, h: r.height };
  }

  function reserveCaptureCard(check) {
    els.card.classList.remove("bottom");
    state.captureReserve = 0;
    if (!check) return;
    var r = stageRect(els.card);
    state.captureReserve = r.y + r.h + 8;
  }

  function placeCaptureCard(check) {
    var box = state.overlayBox;
    if (!check || !box || !check.geometry) return;
    function px(p) { return [box.x + p[0] * box.width, box.y + p[1] * box.height]; }
    var pts = [];
    (check.geometry || []).forEach(function (g) {
      var q = (g.points || []).map(px);
      if (q.length === 1) pts.push(q[0]);
      for (var i = 1; i < q.length; i++) {
        for (var k = 0; k <= 8; k++) pts.push([q[i - 1][0] + (q[i][0] - q[i - 1][0]) * k / 8, q[i - 1][1] + (q[i][1] - q[i - 1][1]) * k / 8]);
      }
    });
    var body = bodyPoints(check) || {};
    ["nose", "left_ear", "right_ear"].forEach(function (n) { if (body[n]) { var h = px(body[n]); pts.push(h, [h[0], h[1] - 18]); } });
    var pill = state.overlayAnchor && state.overlayAnchor.pill;
    if (pill) pts.push([pill[0], pill[1]], [pill[0] + pill[2], pill[1]], [pill[0], pill[1] + pill[3]], [pill[0] + pill[2], pill[1] + pill[3]]);
    var top = stageRect(els.card), stageH = els.stage.getBoundingClientRect().height;
    var bottom = { x: top.x, y: stageH - 16 - top.h, w: top.w, h: top.h };
    function hits(r) {
      return pts.filter(function (p) { return p[0] > r.x - 6 && p[0] < r.x + r.w + 6 && p[1] > r.y - 6 && p[1] < r.y + r.h + 6; }).length;
    }
    var atTop = hits(top);
    state.captureCard = { top: atTop, bottom: atTop ? hits(bottom) : null };
    if (atTop && hits(bottom) < atTop) els.card.classList.add("bottom");
  }

  // Capture only: the stored Swing Score as the closing frame (ring fill + count-up, then a hold).
  function captureScore() {
    var s = (data && data.swing_score) || {};
    var box = document.createElement("div");
    box.className = "capture-score";
    var available = s.available && s.total !== null && s.total !== undefined;
    var r = 74, c = 2 * Math.PI * r;
    box.innerHTML = "<div class='label'>Swing Score</div>" + (available ?
      "<div class='ring'><svg viewBox='0 0 168 168'><circle cx='84' cy='84' r='" + r + "' fill='none' stroke='rgba(255,255,255,0.14)' stroke-width='10'/>" +
      "<circle class='fill' cx='84' cy='84' r='" + r + "' fill='none' stroke='" + STATUS_STROKE.good + "' stroke-width='10' stroke-linecap='round'" +
      " stroke-dasharray='" + c.toFixed(1) + "' stroke-dashoffset='" + c.toFixed(1) + "'/></svg><div class='num'><span>0.0</span><small>/10</small></div></div>" : "") +
      "<div class='sub'></div><div class='brand'>Swing Guru</div>";
    box.querySelector(".sub").textContent = (s.measured != null ? s.measured + " of " + (s.eligible || 8) + " score checks measured" : "") ||
                                            (s.reason || "");
    els.stage.appendChild(box);
    requestAnimationFrame(function () { box.classList.add("in"); });
    var done = function () { window.__captureDone = true; };
    if (!available) { setTimeout(done, 2600); return; }
    var fill = box.querySelector(".fill"), num = box.querySelector(".num span"), target = Number(s.total);
    var start = null, dur = reducedMotion ? 0 : 1300;
    function frame(t) {
      if (start === null) start = t;
      var p = dur ? Math.min(1, (t - start) / dur) : 1, e = 1 - Math.pow(1 - p, 3);
      fill.setAttribute("stroke-dashoffset", (c * (1 - e * target / 10)).toFixed(1));
      num.textContent = (target * e).toFixed(1);
      if (p < 1) requestAnimationFrame(frame); else setTimeout(done, 2200);
    }
    setTimeout(function () { requestAnimationFrame(frame); }, 350);
  }

  function play() {
    var v = els.video;
    if (state.mode === "loading" || state.mode === "error") return;
    if (state.adhoc) {
      // A results-screen check outside the walkthrough: carry on from here.
      state.adhoc = null;
      state.checkIndex = -1;
      state.mode = "playing";
      state.pendingEvent = firstEventAtOrAfter(Number(v.currentTime) + step());
      render();
      resume();
      ensureLoop();
      return;
    }
    if (state.mode === "paused" && state.checkIndex >= 0) {
      state.mode = "holding";
      if (state.dwellRemaining <= 0) state.dwellRemaining = holdFor(active());
      render();
      if (OVERLAY_V3) v3Run();   // the reveal continues from where it was frozen
      ensureLoop();
      return;
    }
    var t = Number(v.currentTime);
    if (state.mode === "ended" || t < segStart() - step() || t >= segEnd() - step()) { replay(); return; }
    state.mode = "playing";
    state.checkIndex = -1;
    state.pendingEvent = firstEventAtOrAfter(t);
    render();
    resume();
    ensureLoop();
  }

  function pause() {
    if (OVERLAY_V3 && (state.mode === "holding" || state.mode === "playing")) v3Stop();   // freeze the reveal
    if (state.mode === "holding") state.mode = "paused";
    else if (state.mode === "playing") { els.video.pause(); unwatchFrames(); state.mode = "paused"; }
    else return;
    render();
  }

  function toggle() { (state.mode === "playing" || state.mode === "holding") ? pause() : play(); }

  function replay() {
    clearTransition();
    clearTimeout(state.endTimer);
    els.video.pause();
    unwatchFrames();
    state.checkIndex = -1;
    state.pendingEvent = 0;
    state.mode = "seeking";
    state.awaitingSeek = { kind: "replay" };
    seek(segStart(), "replay");
    render();
    ensureLoop();
  }

  function goToCheck(index, autoplay) {
    if (index < 0 || index >= flat.length) return;
    state.adhoc = null;
    clearTransition();
    clearTimeout(state.endTimer);
    els.video.pause();
    unwatchFrames();
    var check = flat[index];
    var target = frameTime(check.time_s);
    state.pendingEvent = eventOf(check) + 1;
    if (Math.abs(els.video.currentTime - target) <= 0.25 * step()) {
      state.checkIndex = index;
      state.mode = autoplay ? "holding" : "paused";
      state.dwellRemaining = holdFor(check);
      render(true);
      if (autoplay) ensureLoop();
      return;
    }
    state.mode = "seeking";
    state.checkIndex = -1;
    state.awaitingSeek = { kind: "check", index: index, autoplay: !!autoplay };
    seek(target, "check");
    render();
    ensureLoop();
  }

  function stepCheck(delta) {
    if (!flat.length) return;
    var i = state.checkIndex;
    if (i < 0) i = delta > 0 ? flat.indexOf(events[Math.min(state.pendingEvent, events.length - 1)].checks[0]) : flat.length - 1;
    else i = Math.max(0, Math.min(flat.length - 1, i + delta));
    goToCheck(i, false);
  }

  function onSeeked() {
    var pending = state.awaitingSeek;
    state.awaitingSeek = null;
    if (!pending) {
      renderProgress();
      if (DEEP_CHECK && state.mode === "ready") { var id = DEEP_CHECK; DEEP_CHECK = null; AUTOPLAY = false; replayCheck(id); return; }
      if (AUTOPLAY && state.mode === "ready") { AUTOPLAY = false; play(); }
      return;
    }
    if (pending.kind === "hold") { enterHold(pending.event); return; }
    if (pending.kind === "adhoc") {
      state.adhoc = pending.check;
      state.checkIndex = -1;
      state.mode = "paused";
      render(true);
      return;
    }
    if (pending.kind === "check") {
      state.checkIndex = pending.index;
      state.dwellRemaining = holdFor(active());
      state.mode = pending.autoplay ? "holding" : "paused";
      render(true);
      if (pending.autoplay) ensureLoop();
      return;
    }
    if (pending.kind === "replay") { state.mode = "playing"; state.pendingEvent = 0; render(); resume(); ensureLoop(); return; }
    state.pendingEvent = firstEventAtOrAfter(Number(els.video.currentTime));
    state.mode = "paused";
    render();
  }

  function onScrub() {
    var start = segStart(), end = segEnd();
    if (!isFinite(end) || end <= start) return;
    clearTransition();
    els.video.pause();
    unwatchFrames();
    state.mode = "seeking";
    state.checkIndex = -1;
    state.awaitingSeek = { kind: "scrub" };
    seek(start + (Number(els.seek.value) / 1000) * (end - start), "scrub");
    render();
    ensureLoop();
  }

  function setSpeed(speed) {
    state.speed = speed;
    try { els.video.defaultPlaybackRate = speed; els.video.playbackRate = speed; } catch (e) { /* ignore */ }
    var labels = { 1: "1×", 0.5: "½×", 0.25: "¼×" };
    $("speed").textContent = labels[speed] || speed + "×";
    $("speed").setAttribute("aria-label", "Playback speed " + (labels[speed] || speed + "×"));
  }

  function cycleSpeed() {
    var order = [1, 0.5, 0.25];
    setSpeed(order[(order.indexOf(state.speed) + 1) % order.length]);
  }

  function fail(message) {
    state.error = message;
    state.mode = "error";
    els.loading.hidden = false;
    els.loading.textContent = message;
    render();
  }

  /* ------------------------------------------------------------- layout */

  function aspect() {
    var v = els.video;
    if (v && v.videoWidth) return v.videoWidth / v.videoHeight;
    return data && data.width ? data.width / data.height : 9 / 16;
  }

  function layout() {
    var wrap = els.stageWrap;
    wrap.style.flex = "";  // measure the full space available to the video
    var w = wrap.clientWidth, h = wrap.clientHeight;
    if (!w || !h) return;
    var a = aspect();
    var height = Math.min(h, w / a);
    els.stage.style.height = Math.round(height) + "px";
    els.stage.style.width = Math.round(height * a) + "px";
    // Then hug the video, so video, controls and result sit together with no
    // dead band between them (the screen centres the group).
    wrap.style.flex = "0 0 " + Math.round(height) + "px";
    var box = contentBox();
    var key = box ? [box.x, box.y, box.width, box.height].map(Math.round).join("|") : "";
    if (key !== state.layoutKey) { state.layoutKey = key; drawOverlay(active(), false); }
  }

  function contentBox() {
    var v = els.video;
    var bw = v.clientWidth, bh = v.clientHeight;
    var sw = v.videoWidth || (data && data.width), sh = v.videoHeight || (data && data.height);
    if (!bw || !bh || !sw || !sh) return null;
    var s = Math.min(bw / sw, bh / sh);
    return { x: (bw - sw * s) / 2, y: (bh - sh * s) / 2, width: sw * s, height: sh * s, boxWidth: bw, boxHeight: bh };
  }

  /* ------------------------------------------------------------- render */

  function render(animate) {
    document.body.setAttribute("data-mode", state.mode);
    var check = active();
    renderCard(check);
    if (CAPTURE) reserveCaptureCard(check);
    drawOverlay(check, !!animate);
    if (CAPTURE) placeCaptureCard(check);
    renderChips(check);
    renderTicks(check);
    renderProgress();
    els.phasePill.textContent = whereLabel(check);
    els.play.setAttribute("aria-label", state.mode === "playing" || state.mode === "holding" ? "Pause" : "Play");
  }

  // The phase shown on the video: the held moment, else the last one passed.
  function whereLabel(check) {
    if (check) return check.phaseLabel;
    if (state.mode === "ended") return "Complete";
    if (state.mode === "playing" && data) {
      // Every phase the video has reached, including ones with no checkpoint (Takeaway).
      var t = Number(els.video.currentTime), label = null;
      (data.phases || []).forEach(function (p) {
        if (p.time_s !== null && frameTime(p.time_s) <= t + 0.25 * step()) label = p.label;
      });
      if (label) return label;
    }
    return "Your swing";
  }

  function renderCard(check) {
    if (check) state.lastCheck = check;
    // While the video plays between moments the card keeps the last result,
    // softened, so nothing jumps; it lights up again at the next moment.
    var past = !check && state.mode === "playing" && state.lastCheck;
    els.card.classList.toggle("past", !!past);
    if (past) check = state.lastCheck;
    if (!check) {
      els.card.setAttribute("data-status", "none");
      els.cardPhase.textContent = state.mode === "ended" ? "Complete" : "Swing analysis";
      els.cardTitle.textContent = state.mode === "ended" ? "Opening your results" : "Press play";
      els.cardValue.textContent = "";
      els.cardDetail.textContent = state.mode === "ended" ? "" :
        "We'll pause at " + events.length + " moments of your swing and show one check at a time.";
      els.badge.hidden = true;
      els.explain.hidden = true;
      els.devMapping.hidden = true;
      els.dots.innerHTML = "";
      return;
    }
    // Golfer-facing order: phase, verdict, one support line, the measured value.
    var e = events[eventOf(check)] || { checks: [check] };
    var v = verdictOf(check);
    els.card.setAttribute("data-status", v.status);
    els.cardPhase.textContent = check.phaseLabel + (check.checkpoint ? " · " + check.checkpoint : "");
    els.cardTitle.textContent = v.label;
    els.cardValue.textContent = v.value || "";
    if (v.qualifier) {
      var q = document.createElement("small");
      q.textContent = v.qualifier;
      els.cardValue.appendChild(q);
    }
    els.cardDetail.textContent = v.support || "";
    els.badge.hidden = !check.estimated;
    els.explain.hidden = false;
    els.explain.open = false;
    els.explainTitle.textContent = check.title;
    els.explainText.textContent = check.explain;
    var mapping = v.mapping || {};
    els.explainNote.textContent = mapping.validation === "validated_rule" ? "" :
      "This label is an early guide based on the measurement, not yet a calibrated verdict.";
    if (OVERLAY_V3) {
      // Scored checkpoint vs additional observation, told apart on the card itself.
      var obs = isObservation(check);
      els.card.classList.toggle("observation", obs);
      els.badge.textContent = obs ? "Not scored" : "Estimated";
      if (obs) {
        els.badge.hidden = false;
        els.cardPhase.textContent = check.phaseLabel + " · " + check.observation.name + " · Observation";
        els.explainNote.textContent = check.observation.note;
      } else if (check.measured_frame) {
        // Measured on the clear frame beside the moment (the club was blurred on the moment itself).
        var off = check.measured_frame.offset;
        els.explainNote.textContent = "Measured one frame " + (off > 0 ? "after" : "before") + " " + check.phaseLabel +
          ", where the club was clearly visible. Counted as an estimate.";
      }
    }
    // Developer mode shows exactly how the label was produced.
    els.devMapping.hidden = !DEV;
    if (DEV) {
      els.devMapping.textContent = [
        "check  " + check.id + "  (" + check.level + (check.source ? ", " + check.source : "") + ")",
        "value  " + check.value + " " + (check.qualifier || "") + "   detail: " + check.detail,
        "map    " + (mapping.id || "-") + "  v" + (mapping.version || "?") + "  [" + (mapping.validation || "-") + "]",
        mapping.measurement ? "input  " + mapping.input + " " + mapping.unit + "  = " + mapping.measurement : "reason " + (mapping.reason || ""),
        mapping.bands ? "bands  " + mapping.bands.map(function (b) {
          return (b.from === null ? "" : b.from) + ".." + (b.to === null ? "" : b.to) + " " + b.status + " \"" + b.label + "\"";
        }).join("; ") : "",
      ].filter(Boolean).join("\n");
    }
    if (SIMPLE_CARDS) {
      els.cardTitle.textContent = simpleSentence(check, v);
      els.badge.hidden = true;
      els.explain.hidden = true;
    }
    els.dots.innerHTML = "";
    var pos = e.checks.indexOf(check);
    e.checks.forEach(function (c, i) {
      var dot = document.createElement("span");
      dot.className = "dot" + (i < pos ? " done" : "") + (i === pos ? " active" : "");
      dot.setAttribute("data-status", verdictOf(c).status);
      els.dots.appendChild(dot);
    });
  }

  // ?cards=simple: one plain sentence per verdict (keyed by verdict_id from swingguru/verdicts.py),
  // never a number. Plane names its moment; top posture says which way it changed.
  var SIMPLE = {
    "address.hip_hinge.too_upright": "You're standing too tall. Tilt more from your hips.",
    "address.hip_hinge.slightly_upright": "You're standing a little tall. Tilt a touch more from your hips.",
    "address.hip_hinge.hip_hinge_good": "You have a good, athletic tilt from your hips.",
    "address.hip_hinge.slightly_bent_over": "You're bent over a little too far.",
    "address.hip_hinge.too_bent_over": "You're bent over too far. Stand a little taller.",
    "address.knee_bend.too_straight": "Your legs are nearly straight. Soften your knees.",
    "address.knee_bend.slightly_straight": "Your knees could flex a touch more.",
    "address.knee_bend.knee_bend_good": "Your knees have a soft, athletic flex.",
    "address.knee_bend.slightly_flexed": "Your knees are bent a little more than you need.",
    "address.knee_bend.too_much_knee_bend": "You're sitting down too much. Straighten your legs a little.",
    "address.arm_hang.too_crowded": "Your hands are too close to your body. Give your arms room.",
    "address.arm_hang.slightly_crowded": "Your hands are a little close to your body.",
    "address.arm_hang.arm_hang_good": "Your arms hang naturally from your shoulders.",
    "address.arm_hang.slightly_reaching": "You're reaching slightly. Let your arms hang.",
    "address.arm_hang.reaching": "You're reaching for the ball. Let your arms hang down.",
    "swing.plane_vs_address_shaft.too_shallow": "Your club is too flat {when}.",
    "swing.plane_vs_address_shaft.slightly_shallow": "Your club is a little flat {when}.",
    "swing.plane_vs_address_shaft.on_plane": "Your club is on plane {when}.",
    "swing.plane_vs_address_shaft.slightly_steep": "Your club is a little steep {when}.",
    "swing.plane_vs_address_shaft.too_steep": "Your club is too steep {when}.",
    "top.posture.posture_maintained": "You held your posture to the top.",
    "impact.early_extension.posture_maintained": "Your hips stayed back through impact.",
    "impact.early_extension.slight_early_extension": "Your hips moved slightly toward the ball at impact.",
    "impact.early_extension.early_extension": "Your hips pushed toward the ball at impact.",
    "impact.head.head_steady": "Your head stayed steady through the swing.",
    "impact.head.slight_head_movement": "Your head moved a little during the swing.",
    "impact.head.excessive_head_movement": "Your head moved a lot during the swing."
  };
  // ?cards=simple: the short label on the video beside the measurement (no numbers, no jargon).
  var SIMPLE_LABEL = {
    "address.hip_hinge.too_upright": "Too upright", "address.hip_hinge.slightly_upright": "A little upright",
    "address.hip_hinge.hip_hinge_good": "Hip tilt good", "address.hip_hinge.slightly_bent_over": "A little bent over",
    "address.hip_hinge.too_bent_over": "Too bent over",
    "address.knee_bend.too_straight": "Knees too straight", "address.knee_bend.slightly_straight": "Knees a bit straight",
    "address.knee_bend.knee_bend_good": "Knee flex good", "address.knee_bend.slightly_flexed": "Knees a bit bent",
    "address.knee_bend.too_much_knee_bend": "Too much knee bend",
    "address.arm_hang.too_crowded": "Hands too close", "address.arm_hang.slightly_crowded": "Hands a bit close",
    "address.arm_hang.arm_hang_good": "Arm hang good", "address.arm_hang.slightly_reaching": "Reaching slightly",
    "address.arm_hang.reaching": "Reaching for the ball",
    "swing.plane_vs_address_shaft.too_shallow": "Club too flat", "swing.plane_vs_address_shaft.slightly_shallow": "Club a little flat",
    "swing.plane_vs_address_shaft.on_plane": "On plane", "swing.plane_vs_address_shaft.slightly_steep": "Club a little steep",
    "swing.plane_vs_address_shaft.too_steep": "Club too steep",
    "top.posture.posture_maintained": "Posture looks good",
    "impact.early_extension.posture_maintained": "Hips stayed back",
    "impact.early_extension.slight_early_extension": "Hips moved a little", "impact.early_extension.early_extension": "Hips moved in",
    "impact.head.head_steady": "Head steady", "impact.head.slight_head_movement": "Head moved a little",
    "impact.head.excessive_head_movement": "Head moved too much"
  };

  function simpleLabel(check, v) {
    if (isObservation(check)) return check.observation.name;
    if (v.verdict_id && /^top\.posture\.(slight_)?loss_of_posture$/.test(v.verdict_id)) {
      var slight = v.verdict_id.indexOf("slight_") >= 0, up = ((v.mapping || {}).input || 0) < 0;
      return up ? (slight ? "Rose a little" : "Stood up") : (slight ? "Dipped a little" : "Dipped down");
    }
    return SIMPLE_LABEL[v.verdict_id] || v.label || "";
  }

  // The text of the on-video pill: the verdict label, or in simple mode the short plain label.
  function pillText(check, v) { return SIMPLE_CARDS ? simpleLabel(check, v) : (v.label || check.callout); }

  var SIMPLE_OBSERVATION = { mid_back: "Here's your posture halfway back.", top: "Here's where your head is at the top.",
                             downswing: "Here's your posture coming down." };

  function simpleSentence(check, v) {
    if (isObservation(check)) return SIMPLE_OBSERVATION[check.phase] || "Here's this moment of your swing.";
    var mapping = v.mapping || {};
    if (v.verdict_id && /^top\.posture\.(slight_)?loss_of_posture$/.test(v.verdict_id)) {
      var slight = v.verdict_id.indexOf("slight_") >= 0, up = (mapping.input || 0) < 0;
      return up ? (slight ? "You rose slightly out of your posture at the top." : "You stood up out of your posture at the top.")
                : (slight ? "You dipped slightly at the top." : "You dipped down at the top.");
    }
    var text = SIMPLE[v.verdict_id];
    if (text) return text.replace("{when}", check.phase === "mid_back" ? "halfway back" : "coming down");
    return v.support_before_direction || v.support || v.label || "";
  }

  // The presentation verdict for a check (added by swingguru/verdicts.py). Old
  // bundles without one fall back to a neutral, information-only label.
  function verdictOf(check) {
    // v3 observations: measured value and a plain description only, never a verdict colour or band.
    if (isObservation(check)) {
      return { label: check.observation.label || check.callout, status: "info", support: check.detail || "",
               value: check.value, qualifier: check.qualifier || "", mapping: { validation: "observation" } };
    }
    if (check && check.verdict) return check.verdict;
    return { label: check ? check.title : "", status: "info", support: check ? check.detail : "",
             value: check ? check.value : "", qualifier: check ? check.qualifier : "", mapping: { validation: "none" } };
  }

  // One dot per swing moment: passed ones filled, the held one in green.
  function renderChips(check) {
    if (!els.phases) return;
    var current = check ? check.phase : null;
    var t = Number(els.video.currentTime);
    var ended = state.mode === "ended";
    Array.prototype.forEach.call(els.phases.children, function (btn) {
      var phase = btn.getAttribute("data-phase");
      var time = Number(btn.getAttribute("data-time"));
      // The held moment shows the colour of the check on screen.
      if (phase === current) btn.setAttribute("data-status", verdictOf(check).status);
      else btn.setAttribute("data-status", btn.getAttribute("data-phase-status") || "info");
      btn.classList.toggle("active", phase === current);
      btn.classList.toggle("done", phase !== current && (ended || (isFinite(time) && frameTime(time) <= t + 0.25 * step())));
    });
  }

  function renderTicks(check) {
    var start = segStart(), end = segEnd(), span = end - start;
    var activeEvent = check ? eventOf(check) : -1;
    var key = [start, end, events.length, activeEvent].join("|");
    if (els.ticks.getAttribute("data-key") === key || !isFinite(span) || span <= 0) return;
    els.ticks.setAttribute("data-key", key);
    els.ticks.innerHTML = "";
    events.forEach(function (e, i) {
      var t = document.createElement("span");
      t.className = "tick" + (i === activeEvent ? " active" : "");
      t.style.left = (((frameTime(e.time_s) - start) / span) * 100).toFixed(2) + "%";
      els.ticks.appendChild(t);
    });
  }

  function renderProgress() {
    var v = els.video, start = segStart(), end = segEnd();
    var f = isFinite(end) && end > start ? Math.min(1, Math.max(0, (v.currentTime - start) / (end - start))) : 0;
    els.progress.style.width = (f * 100).toFixed(2) + "%";
    if (document.activeElement !== els.seek) els.seek.value = String(Math.round(f * 1000));
    if (state.mode === "playing") {  // the phase name and dots follow the video
      var where = whereLabel(null);
      if (els.phasePill.textContent !== where) { els.phasePill.textContent = where; renderChips(null); }
    }
  }

  /* ------------------------------------------------------------ overlay */

  function svg(tag, attrs, parent) {
    var n = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) if (Object.prototype.hasOwnProperty.call(attrs, k) && attrs[k] !== null) n.setAttribute(k, String(attrs[k]));
    (parent || els.overlay).appendChild(n);
    return n;
  }
  function d(points) { return points.map(function (p, i) { return (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" "); }

  // Status colours drawn on video (brighter than the card colours so they read
  // on sky, mat and shadow). Information checks draw in soft white.
  // Same values as --sg-mark-* in app.css.
  var STATUS_STROKE = { good: "#3ee08a", attention: "#ffc24a", problem: "#ff6a5c", info: "rgba(255,255,255,0.92)" };
  // Body guides: the joints a measurement is about, drawn under it in soft white.
  var GUIDE_STROKE = "rgba(255,255,255,0.62)";
  // Overlay choreography (ms): guides, then references, then the measured line, joints, label.
  var T = { guide: 0, guideDur: 320, ref: 140, line: 220, lineDur: 460, joint: 620, label: 680 };

  // Which body chains help explain each measurement. "far:" = the side away from the camera;
  // a bare joint name ("shoulder") is resolved to the near side, a full name is used as is.
  var GUIDES = {
    torso_angle: [["ear", "shoulder", "hip", "knee", "ankle"]],
    trail_knee: [["shoulder", "hip"], ["heel", "ankle", "foot_index"]],
    lead_knee: [["shoulder", "hip"], ["heel", "ankle", "foot_index"]],
    arm_hang: [["shoulder", "elbow", "wrist"], ["shoulder", "hip"]],
    shaft: [["hip", "shoulder", "elbow", "wrist"]],
    posture: [["ear", "shoulder", "hip", "knee"]],
    pelvis: [["shoulder", "hip", "knee", "ankle"]],
    head: [["ear", "shoulder", "hip"]]
  };

  function timing(el, delay, duration) {
    el.style.setProperty("--d", delay + "ms");
    if (duration) el.style.setProperty("--t", duration + "ms");
  }

  // The tracked body points for this moment (absent on swings analysed before overlay v1).
  function bodyPoints(check) {
    var phases = data && data.overlay && data.overlay.phases;
    var entry = phases && phases[check.phase];
    return entry && entry.points ? entry.points : null;
  }

  // The near side is the one whose joints sit on the check's own measured geometry.
  function nearSide(points, geometry, px) {
    var anchors = [];
    geometry.forEach(function (g) {
      if (g.role === "reference" || g.role === "origin") return;
      (g.points || []).forEach(function (p) { anchors.push(px(p)); });
    });
    var best = null;
    ["left", "right"].forEach(function (side) {
      var total = 0, seen = 0;
      ["shoulder", "hip", "knee", "wrist", "elbow"].forEach(function (joint) {
        var p = points[side + "_" + joint];
        if (!p || !anchors.length) return;
        var q = px(p), near = Infinity;
        anchors.forEach(function (a) { near = Math.min(near, Math.hypot(a[0] - q[0], a[1] - q[1])); });
        total += near; seen += 1;
      });
      var score = seen ? total / seen : Infinity;
      if (!best || score < best.score) best = { side: side, score: score };
    });
    return best && isFinite(best.score) ? best.side : "right";
  }

  function drawGuides(check, group, geometry, px, frame, animated, obstacles) {
    var points = bodyPoints(check);
    var chains = GUIDES[check.kind];
    if (!points || !chains) return;
    var near = nearSide(points, geometry, px), far = near === "left" ? "right" : "left";
    function resolve(name) {
      if (name.indexOf("far:") === 0) return far + "_" + name.slice(4);
      return /^(left|right)_/.test(name) ? name : near + "_" + name;
    }
    var joints = {};
    chains.forEach(function (chain) {
      // Break the chain where a joint was not tracked reliably: never bridge a gap.
      var run = [];
      var flush = function () {
        if (run.length >= 2) {
          var path = clippedPath(run, frame);
          if (path) {
            var el = svg("path", { d: path, fill: "none", stroke: GUIDE_STROKE, "stroke-width": 1.35,
                                   "stroke-linecap": "round", "stroke-linejoin": "round" }, group);
            if (animated) { el.setAttribute("class", "draw guide"); timing(el, T.guide, T.guideDur); }
            for (var i = 1; i < run.length; i++) {
              var seg = clipSegment(run[i - 1], run[i], frame);
              if (seg) { seg.weight = 0.5; obstacles.push(seg); }
            }
          }
        }
        run = [];
      };
      chain.forEach(function (name) {
        var p = points[resolve(name)];
        if (!p) { flush(); return; }
        var q = px(p);
        run.push(q);
        joints[resolve(name)] = q;
      });
      flush();
    });
    Object.keys(joints).forEach(function (name) {
      var q = joints[name];
      if (q[0] < frame.x0 || q[0] > frame.x1 || q[1] < frame.y0 || q[1] > frame.y1) return;
      var dot = svg("circle", { cx: q[0], cy: q[1], r: 2.2, fill: "#fff", opacity: 0.8 }, group);
      if (animated) { dot.setAttribute("class", "pop"); timing(dot, T.joint - 120); }
    });
  }

  /* ---- geometry helpers: keep every line inside the visible frame ---- */

  // Liang-Barsky: the part of segment a-b inside rect r, or null.
  function clipSegment(a, b, r) {
    var dx = b[0] - a[0], dy = b[1] - a[1], t0 = 0, t1 = 1;
    var p = [-dx, dx, -dy, dy], q = [a[0] - r.x0, r.x1 - a[0], a[1] - r.y0, r.y1 - a[1]];
    for (var i = 0; i < 4; i++) {
      if (p[i] === 0) { if (q[i] < 0) return null; continue; }
      var t = q[i] / p[i];
      if (p[i] < 0) { if (t > t1) return null; if (t > t0) t0 = t; }
      else { if (t < t0) return null; if (t < t1) t1 = t; }
    }
    return [[a[0] + t0 * dx, a[1] + t0 * dy], [a[0] + t1 * dx, a[1] + t1 * dy]];
  }

  // The infinite line through a and b, as a long segment (clipped by the caller).
  function extendLine(a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1], n = Math.hypot(dx, dy) || 1, k = 5000 / n;
    return [[a[0] - dx * k, a[1] - dy * k], [b[0] + dx * k, b[1] + dy * k]];
  }

  // A polyline clipped to the frame, as an SVG path ("" when fully outside).
  function clippedPath(points, r) {
    var out = "", last = null;
    for (var i = 1; i < points.length; i++) {
      var seg = clipSegment(points[i - 1], points[i], r);
      if (!seg) { last = null; continue; }
      var start = seg[0], end = seg[1];
      if (!last || Math.abs(last[0] - start[0]) > 0.5 || Math.abs(last[1] - start[1]) > 0.5) out += "M" + start[0].toFixed(1) + " " + start[1].toFixed(1);
      out += "L" + end[0].toFixed(1) + " " + end[1].toFixed(1);
      last = end;
    }
    return out;
  }

  function drawOverlay(check, animate) {
    if (OVERLAY_V3) return drawOverlayV3(check, animate);
    if (OVERLAY_V2) return drawOverlayV2(check, animate);
    var o = els.overlay;
    while (o.firstChild) o.removeChild(o.firstChild);
    o.classList.remove("in");
    state.overlayBox = null;
    state.overlayAnchor = null;
    var box = contentBox();
    if (!box) return;
    o.setAttribute("viewBox", "0 0 " + box.boxWidth + " " + box.boxHeight);
    state.overlayBox = box;
    if (!check || !check.geometry) return;
    // The hip check also draws the Address tush line and the current back of
    // the pelvis when that was measured (it is absent when the camera moved).
    var extension = check.kind === "pelvis" && check.pelvis_depth && check.dev_geometry;
    var geometry;
    if (extension && !DEV) {
      // Extension: only the Address tush line and the back of the pelvis.
      geometry = check.dev_geometry;
    } else {
      geometry = check.geometry.concat(DEV && check.dev_geometry ? check.dev_geometry : []);
    }
    var animated = animate && !reducedMotion;
    var cls = function (name) { return animated ? name : null; };
    function px(p) { return [box.x + p[0] * box.width, box.y + p[1] * box.height]; }
    var status = verdictOf(check).status;
    var colour = STATUS_STROKE[status] || STATUS_STROKE.info;
    // Lines stop just inside the picture (rounded ends never touch the edge).
    var inset = 5;
    var frame = { x0: box.x + inset, y0: box.y + inset, x1: box.x + box.width - inset, y1: box.y + box.height - inset };
    // One stroke system for every checkpoint: the measurement as a fine line in
    // its (softened) verdict colour, references as small round white dots.
    var w = Math.max(1.6, Math.min(2.2, box.width * 0.0055));
    var ref = 1.9;       // diameter of a reference dot
    var refGap = 5.2;    // spacing of reference dots
    var guides = svg("g", { "class": "guides" });
    var shapes = svg("g", {});
    var drawn = [];
    // Plane view: only where the Address reference is known to sit correctly in
    // this frame (tripod, or a confirmed camera correction).
    var planeView = check.kind === "shaft" && (check.phase === "mid_back" || check.phase === "downswing") &&
                    check.reference_frame !== "unaligned";
    var obstacles = [];  // drawn segments the label should not sit on

    geometry.forEach(function (g) {
      var pts = (g.points || []).map(px);
      if (!pts.length) return;
      var role = g.role;
      if (g.kind === "line" || g.kind === "polyline") {
        var isRef = role === "reference";
        if (planeView && pts.length === 2 && (isRef || role === "shaft")) {
          // Plane checks: both lines run across the useful frame (then clipped),
          // so current shaft vs Address reference reads at a glance.
          var full = extendLine(pts[0], pts[1]);
          if (role === "shaft") {
            var shaftLine = clippedPath(full, frame);
            if (shaftLine) svg("path", { d: shaftLine, fill: "none", stroke: colour, "stroke-width": ref, "stroke-linecap": "round",
                                   "stroke-dasharray": "0.01 " + refGap, opacity: 0.55, "class": cls("fade-early") }, shapes);
          } else {
            pts = full;
          }
        }
        var path = clippedPath(pts, frame);
        if (!path) return;
        for (var i = 1; i < pts.length; i++) {
          var seg = clipSegment(pts[i - 1], pts[i], frame);
          if (seg) { if (isRef) seg.weight = 0.25; obstacles.push(seg); }  // a faint reference matters less
        }
        // Keep the label off the body beyond a measured line too: past the
        // upper end of the torso line is the head, past the grip the arms.
        if (!isRef && role !== "accent" && pts.length >= 2) {
          var a = pts[0], b = pts[pts.length - 1];
          var hi = a[1] < b[1] ? a : b, lo = hi === a ? b : a;
          var ext = [hi[0] + (hi[0] - lo[0]) * 0.8, hi[1] + (hi[1] - lo[1]) * 0.8];
          var extSeg = clipSegment(hi, ext, frame);
          if (extSeg) obstacles.push(extSeg);
        }
        // The extension "accent" is the measured shift, so it takes the verdict colour.
        var measured = !isRef && (role !== "accent" || extension);
        var width = measured ? w : ref;
        var stroke = measured ? colour : WHITE;
        if (measured && role !== "shaft") {
          // A soft halo of the same colour under the measured line: richer on busy backgrounds.
          var glow = svg("path", { d: path, fill: "none", stroke: stroke, "stroke-width": (width * 3.4).toFixed(2),
                                   "stroke-linecap": "round", "stroke-linejoin": "round", opacity: 0.22 }, shapes);
          drawn.push(glow);
        }
        var el = svg("path", { d: path, fill: "none", stroke: stroke, "stroke-width": width, "stroke-linecap": "round",
                               "stroke-linejoin": "round", opacity: measured ? 0.98 : 0.8 }, shapes);
        if (isRef || role === "accent" && !extension) {
          el.setAttribute("stroke-dasharray", "0.01 " + refGap);  // round dots
          if (animated) el.setAttribute("class", "fade-early");
        } else if (role === "shaft") {
          // The club: short rounded dashes in the verdict colour (fades in).
          el.setAttribute("stroke-dasharray", "4.5 " + (4.5 + w).toFixed(1));
          if (animated) el.setAttribute("class", "fade-early");
        } else drawn.push(el);
      } else if (g.kind === "ring") {
        var r = Math.max(8, Number(g.radius) * box.height);
        var isRefRing = role === "reference";
        obstacles.push([[pts[0][0] - r, pts[0][1]], [pts[0][0] + r, pts[0][1]]]);
        var ring = svg("circle", { cx: pts[0][0], cy: pts[0][1], r: r, fill: "none", "stroke-linecap": "round",
                                   stroke: isRefRing ? WHITE : colour, "stroke-width": isRefRing ? ref : w,
                                   opacity: isRefRing ? 0.78 : 0.96 }, shapes);
        if (isRefRing) { ring.setAttribute("stroke-dasharray", "0.01 " + (refGap * 0.8).toFixed(1)); if (animated) ring.setAttribute("class", "fade-early"); }
        else drawn.push(ring);
      } else if (g.kind === "arc") {
        // Angle arcs are left out: the line and its dashed reference already
        // show the angle, and the value is on the card.
        return;
      } else if (g.kind === "dot") {
        var dp = [Math.min(frame.x1, Math.max(frame.x0, pts[0][0])), Math.min(frame.y1, Math.max(frame.y0, pts[0][1]))];
        if (role === "origin") {  // where the clubhead sat at Address
          if (planeView) svg("circle", { cx: dp[0], cy: dp[1], r: 4.2, fill: "none", stroke: "#fff", "stroke-width": 1.3,
                                         opacity: 0.85, "class": cls("fade-early") }, shapes);
          return;
        }
        var joint = svg("circle", { cx: dp[0], cy: dp[1], r: 2.6, fill: "#fff", opacity: 0.98,
                                    stroke: colour, "stroke-width": 1.2, "class": cls("pop") }, shapes);
        if (animated) timing(joint, T.joint);
      }
    });

    drawGuides(check, guides, geometry, px, frame, animated, obstacles);

    var anchor = check.anchor;
    if (extension && !DEV) {  // the label points at the back of the pelvis
      var rear = check.dev_geometry.filter(function (g) { return g.kind === "dot"; })[0];
      if (rear) anchor = rear.points[0];
    }
    // Other measured points at this moment (e.g. the nose from the head check,
    // mid-torso from posture) mark the golfer's body: keep the label off them.
    (data.phases || []).forEach(function (p) {
      if (p.phase !== check.phase) return;
      p.checks.forEach(function (c) {
        if (c === check || !c.anchor) return;
        // Their measured lines too (e.g. the club at Address): hands and club stay clear.
        (c.geometry || []).forEach(function (g) {
          if ((g.kind === "line" || g.kind === "polyline") && g.role !== "reference") {
            var gp = g.points.map(px);
            for (var k = 1; k < gp.length; k++) {
              var s = clipSegment(gp[k - 1], gp[k], frame);
              if (s) obstacles.push(s);
            }
          }
        });
        // A head-sized zone; for the head check (anchored on the nose) it
        // reaches up over the whole head.
        var q = px(c.anchor), r = Math.max(18, box.height * 0.04), up = c.kind === "head" ? 2.2 * r : r;
        obstacles.push([[q[0] - r, q[1]], [q[0] + r, q[1]]]);
        obstacles.push([[q[0] - r, q[1] - up * 0.6], [q[0] + r, q[1] - up * 0.6]]);
        obstacles.push([[q[0], q[1] - up], [q[0], q[1] + r]]);
      });
    });
    state.anchorNorm = anchor || null;
    if (anchor) {
      var v = verdictOf(check);
      callout(px(anchor), pillText(check, v), status, box, w, animated, labelSide(check), obstacles);
    }

    // Guides, references, measured line, joints, label: one deliberate sequence (T).
    var guideLines = Array.prototype.slice.call(guides.querySelectorAll("path"));
    drawn = drawn.concat(guideLines);
    drawn.forEach(function (shape) {
      var len = 0;
      try { len = shape.getTotalLength(); } catch (e) { len = 0; }
      if (!animated || len <= 0) return;
      var guide = guideLines.indexOf(shape) >= 0;
      if (!guide) shape.setAttribute("class", "draw");
      // Web Animations, not a CSS transition: the draw-on runs regardless of when the browser
      // computes styles for the freshly created path. The final state is the plain line.
      shape.style.strokeDasharray = len.toFixed(1);
      shape.style.strokeDashoffset = "0";
      if (typeof shape.animate === "function") {
        shape.animate([{ strokeDashoffset: len.toFixed(1) }, { strokeDashoffset: "0" }], {
          duration: guide ? T.guideDur : T.lineDur, delay: guide ? T.guide : T.line,
          easing: "cubic-bezier(0.22, 0.7, 0.2, 1)", fill: "backwards" });
      }
    });
    if (animated) {
      Array.prototype.forEach.call(shapes.querySelectorAll(".fade-early"), function (el, i) {
        timing(el, T.ref + Math.min(i, 3) * 40);
      });
      Array.prototype.forEach.call(o.querySelectorAll("g.fade"), function (el) { timing(el, T.label); });
      void o.getBoundingClientRect();
      requestAnimationFrame(function () { o.classList.add("in"); });
    } else {
      o.classList.add("in");
    }
  }

  /* ---------------------------------------------------------------- overlay v2 (?overlay=2)
   * Same measured geometry as v1 (nothing is re-measured here), drawn with one stroke system
   * scaled to the golfer: widths and marker sizes follow the golfer's torso length on screen, not
   * the video box. Every line sits on a thin dark under-line so it reads on sky, mat and shadow.
   * Hierarchy: faint body guides < dashed white references < the measured line in its verdict
   * colour < joint markers < angle arc + value < the verdict label. */

  var UNDER = "rgba(6,10,8,0.5)";

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // The golfer's torso length on screen (shoulder centre to hip centre) at this moment.
  function torsoPx(check, px) {
    var p = bodyPoints(check);
    if (!p) return null;
    function mid(a, b) {
      var A = p[a], B = p[b];
      if (A && B) return [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2];
      return A || B || null;
    }
    var s = mid("left_shoulder", "right_shoulder"), h = mid("left_hip", "right_hip");
    if (!s || !h) return null;
    var a = px(s), b = px(h);
    return Math.hypot(a[0] - b[0], a[1] - b[1]) || null;
  }

  // A path with its dark under-line (drawn first, slightly wider).
  function stroked(parent, d, colour, width, opts) {
    opts = opts || {};
    var attrs = { d: d, fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" };
    var under = null;
    if (opts.under !== false) {
      under = svg("path", Object.assign({}, attrs, { stroke: UNDER, "stroke-width": (width + (opts.underPad || 2.2)).toFixed(2),
                                                      opacity: opts.underOpacity || 0.55 }), parent);
      if (opts.dash) under.setAttribute("stroke-dasharray", opts.dash);
    }
    var line = svg("path", Object.assign({}, attrs, { stroke: colour, "stroke-width": width.toFixed(2),
                                                     opacity: opts.opacity == null ? 1 : opts.opacity }), parent);
    if (opts.dash) line.setAttribute("stroke-dasharray", opts.dash);
    return { line: line, under: under };
  }

  // A joint marker: white disc, verdict-coloured rim, dark outer ring.
  function marker(parent, q, r, colour, cls) {
    var g = svg("g", { "class": cls || null }, parent);
    svg("circle", { cx: q[0], cy: q[1], r: (r + 1.4).toFixed(2), fill: UNDER }, g);
    svg("circle", { cx: q[0], cy: q[1], r: r.toFixed(2), fill: "#fff", stroke: colour,
                    "stroke-width": Math.max(1.1, r * 0.38).toFixed(2) }, g);
    return g;
  }

  function polar(c, r, deg) { var t = deg * Math.PI / 180; return [c[0] + r * Math.cos(t), c[1] + r * Math.sin(t)]; }

  // The angle a check measures, as an arc at its vertex with the check's own value beside it.
  // Only for checks whose geometry is a reference ray and a measured ray from one vertex.
  var ARC_KINDS = { torso_angle: true, trail_knee: true, lead_knee: true, arm_hang: true };

  function angleArc(check, geometry, px, parent, colour, sizes, obstacles, frame) {
    if (!ARC_KINDS[check.kind]) return null;
    var ref = geometry.filter(function (g) { return g.role === "reference" && (g.points || []).length === 2; })[0];
    var measured = geometry.filter(function (g) { return (g.kind === "line" || g.kind === "polyline") && g.role === "primary"; })[0];
    if (!ref || !measured) return null;
    var v = px(ref.points[0]), r1 = px(ref.points[1]);
    var mp = measured.points.map(px), k = 0, best = Infinity;
    mp.forEach(function (q, i) { var dd = Math.hypot(q[0] - v[0], q[1] - v[1]); if (dd < best) { best = dd; k = i; } });
    var other = mp[k + 1] || mp[k - 1];
    if (!other) return null;
    var a1 = Math.atan2(r1[1] - v[1], r1[0] - v[0]) * 180 / Math.PI;
    var a2 = Math.atan2(other[1] - v[1], other[0] - v[0]) * 180 / Math.PI;
    var sweep = ((a2 - a1 + 540) % 360) - 180;              // shortest signed sweep
    if (Math.abs(sweep) < 2) return null;
    var r = sizes.arc, start = polar(v, r, a1), end = polar(v, r, a1 + sweep);
    var d = "M" + start[0].toFixed(1) + " " + start[1].toFixed(1) + "A" + r.toFixed(1) + " " + r.toFixed(1) +
            " 0 0 " + (sweep > 0 ? 1 : 0) + " " + end[0].toFixed(1) + " " + end[1].toFixed(1);
    var arc = stroked(parent, d, colour, sizes.arcWidth, { opacity: 0.95 });
    // Wide angles: the value sits just outside the arc on its bisector. Narrow ones (a knee): inside
    // the wedge it would sit on the lines, so it goes just outside the measured line instead.
    var mid;
    if (Math.abs(sweep) >= 35) {
      mid = polar(v, r + sizes.valueGap, a1 + sweep / 2);
    } else {
      var along = polar(v, r * 0.9, a1 + sweep);
      var out = (a1 + sweep + (sweep > 0 ? 90 : -90)) * Math.PI / 180;
      mid = [along[0] + Math.cos(out) * (sizes.valueGap + 8), along[1] + Math.sin(out) * (sizes.valueGap + 8)];
    }
    if (mid[0] < frame.x0 + 14 || mid[0] > frame.x1 - 14 || mid[1] < frame.y0 + 10 || mid[1] > frame.y1 - 10) return arc;
    var text = svg("text", { x: mid[0].toFixed(1), y: mid[1].toFixed(1), "class": "value-text", "text-anchor": "middle",
                             "dominant-baseline": "central", "font-size": sizes.valueFont.toFixed(1) }, parent);
    text.textContent = check.value;
    obstacles.push([[mid[0] - 16, mid[1]], [mid[0] + 16, mid[1]]]);
    return arc;
  }

  function drawGuidesV2(check, group, geometry, px, frame, sizes, obstacles) {
    var points = bodyPoints(check);
    var chains = GUIDES[check.kind];
    if (!points || !chains) return;
    var near = nearSide(points, geometry, px), far = near === "left" ? "right" : "left";
    function resolve(name) {
      if (name.indexOf("far:") === 0) return far + "_" + name.slice(4);
      return /^(left|right)_/.test(name) ? name : near + "_" + name;
    }
    var joints = {};
    chains.forEach(function (chain) {
      var run = [];
      var flush = function () {   // never bridge an untracked joint
        if (run.length >= 2) {
          var path = clippedPath(run, frame);
          if (path) {
            stroked(group, path, "rgba(255,255,255,0.78)", sizes.guide, { opacity: 0.7, underPad: 1.6, underOpacity: 0.4 });
            for (var i = 1; i < run.length; i++) {
              var seg = clipSegment(run[i - 1], run[i], frame);
              if (seg) { seg.weight = 0.5; obstacles.push(seg); }
            }
          }
        }
        run = [];
      };
      chain.forEach(function (name) {
        var p = points[resolve(name)];
        if (!p) { flush(); return; }
        var q = px(p);
        run.push(q);
        joints[resolve(name)] = q;
      });
      flush();
    });
    Object.keys(joints).forEach(function (name) {
      var q = joints[name];
      if (q[0] < frame.x0 || q[0] > frame.x1 || q[1] < frame.y0 || q[1] > frame.y1) return;
      svg("circle", { cx: q[0], cy: q[1], r: (sizes.guideJoint + 1).toFixed(2), fill: UNDER, opacity: 0.6 }, group);
      svg("circle", { cx: q[0], cy: q[1], r: sizes.guideJoint.toFixed(2), fill: "#fff", opacity: 0.85 }, group);
    });
  }

  function drawOverlayV2(check, animate) {
    var o = els.overlay;
    while (o.firstChild) o.removeChild(o.firstChild);
    o.classList.remove("in");
    state.overlayBox = null;
    state.overlayAnchor = null;
    var box = contentBox();
    if (!box) return;
    o.setAttribute("viewBox", "0 0 " + box.boxWidth + " " + box.boxHeight);
    state.overlayBox = box;
    if (!check || !check.geometry) return;
    var extension = check.kind === "pelvis" && check.pelvis_depth && check.dev_geometry;
    var geometry = extension && !DEV ? check.dev_geometry : check.geometry.concat(DEV && check.dev_geometry ? check.dev_geometry : []);
    var animated = animate && !reducedMotion;
    var cls = function (name) { return animated ? name : null; };
    function px(p) { return [box.x + p[0] * box.width, box.y + p[1] * box.height]; }
    var status = verdictOf(check).status;
    var colour = STATUS_STROKE[status] || STATUS_STROKE.info;
    var inset = 5;
    var frame = { x0: box.x + inset, y0: box.y + inset, x1: box.x + box.width - inset, y1: box.y + box.height - inset };
    // Sizes follow the golfer, within readable limits.
    var torso = torsoPx(check, px) || box.height * 0.2;
    var sizes = {
      line: clamp(torso * 0.021, 1.7, 3.2),
      ref: clamp(torso * 0.013, 1.3, 2.1),
      guide: clamp(torso * 0.011, 1.0, 1.7),
      joint: clamp(torso * 0.026, 2.6, 4.6),
      guideJoint: clamp(torso * 0.013, 1.5, 2.6),
      arc: clamp(torso * 0.3, 16, 64),
      arcWidth: clamp(torso * 0.014, 1.3, 2.2),
      valueGap: clamp(torso * 0.09, 9, 16),
      valueFont: clamp(torso * 0.085, 10.5, 13)
    };
    state.overlaySizes = sizes;   // diagnostics (QA screenshots)
    var dash = "0.01 " + (sizes.ref * 3.6).toFixed(1);     // round reference dots, spaced to the stroke
    var guides = svg("g", { "class": "guides" + (animated ? " fade-early" : "") });
    var refs = svg("g", { "class": animated ? "fade-early" : null });
    var shapes = svg("g", {});
    var marks = svg("g", {});
    var drawn = [];
    var obstacles = [];
    var planeView = check.kind === "shaft" && (check.phase === "mid_back" || check.phase === "downswing") &&
                    check.reference_frame !== "unaligned";

    geometry.forEach(function (g) {
      var pts = (g.points || []).map(px);
      if (!pts.length) return;
      var role = g.role;
      if (g.kind === "line" || g.kind === "polyline") {
        var isRef = role === "reference";
        if (planeView && pts.length === 2 && isRef) pts = extendLine(pts[0], pts[1]);   // full-length Address reference
        var path = clippedPath(pts, frame);
        if (!path) return;
        for (var i = 1; i < pts.length; i++) {
          var seg = clipSegment(pts[i - 1], pts[i], frame);
          if (seg) { if (isRef) seg.weight = 0.25; obstacles.push(seg); }
        }
        if (!isRef && role !== "accent" && pts.length >= 2) {   // keep the label off the head / arms beyond a line
          var a = pts[0], b = pts[pts.length - 1];
          var hi = a[1] < b[1] ? a : b, lo = hi === a ? b : a;
          var extSeg = clipSegment(hi, [hi[0] + (hi[0] - lo[0]) * 0.8, hi[1] + (hi[1] - lo[1]) * 0.8], frame);
          if (extSeg) obstacles.push(extSeg);
        }
        if (isRef || (role === "accent" && !extension)) {
          stroked(refs, path, WHITE, sizes.ref, { dash: dash, opacity: 0.9, underPad: 1.4, underOpacity: 0.45 });
        } else if (role === "shaft") {
          if (planeView) {
            // The current shaft line across the frame as faint dots (to compare with the Address
            // reference), the club itself solid.
            var across = clippedPath(extendLine(pts[0], pts[1]), frame);
            if (across) stroked(refs, across, colour, sizes.ref, { dash: dash, opacity: 0.6, under: false });
          }
          drawn.push(stroked(shapes, path, colour, sizes.line, {}).line);
        } else {
          drawn.push(stroked(shapes, path, colour, sizes.line, {}).line);
        }
      } else if (g.kind === "ring") {
        var r = Math.max(sizes.joint * 2.4, Number(g.radius) * box.height);
        var isRefRing = role === "reference";
        obstacles.push([[pts[0][0] - r, pts[0][1]], [pts[0][0] + r, pts[0][1]]]);
        var c = "M" + (pts[0][0] - r).toFixed(1) + " " + pts[0][1].toFixed(1) + "a" + r.toFixed(1) + " " + r.toFixed(1) +
                " 0 1 0 " + (2 * r).toFixed(1) + " 0a" + r.toFixed(1) + " " + r.toFixed(1) + " 0 1 0 " + (-2 * r).toFixed(1) + " 0";
        if (isRefRing) stroked(refs, c, WHITE, sizes.ref, { dash: dash, opacity: 0.9, underPad: 1.4, underOpacity: 0.45 });
        else drawn.push(stroked(shapes, c, colour, sizes.line, {}).line);
      } else if (g.kind === "dot") {
        var dp = [clamp(pts[0][0], frame.x0, frame.x1), clamp(pts[0][1], frame.y0, frame.y1)];
        if (role === "origin") {
          if (planeView) {
            svg("circle", { cx: dp[0], cy: dp[1], r: (sizes.joint + 2.6).toFixed(2), fill: "none", stroke: UNDER,
                            "stroke-width": 3, opacity: 0.5 }, refs);
            svg("circle", { cx: dp[0], cy: dp[1], r: (sizes.joint + 2.6).toFixed(2), fill: "none", stroke: "#fff",
                            "stroke-width": 1.4 }, refs);
          }
          return;
        }
        var m = marker(marks, dp, sizes.joint, colour, cls("pop"));
        if (animated) timing(m, T.joint);
      }
      // Engine "arc" geometry is replaced by angleArc below (one consistent style for every angle).
    });

    drawGuidesV2(check, guides, geometry, px, frame, sizes, obstacles);
    var arc = angleArc(check, geometry, px, marks, colour, sizes, obstacles, frame);
    if (arc) drawn.push(arc.line);

    var anchor = check.anchor;
    if (extension && !DEV) {
      var rear = check.dev_geometry.filter(function (g) { return g.kind === "dot"; })[0];
      if (rear) anchor = rear.points[0];
    }
    (data.phases || []).forEach(function (p) {
      if (p.phase !== check.phase) return;
      p.checks.forEach(function (c) {
        if (c === check || !c.anchor) return;
        (c.geometry || []).forEach(function (g) {
          if ((g.kind === "line" || g.kind === "polyline") && g.role !== "reference") {
            var gp = g.points.map(px);
            for (var k = 1; k < gp.length; k++) {
              var s = clipSegment(gp[k - 1], gp[k], frame);
              if (s) obstacles.push(s);
            }
          }
        });
        var q = px(c.anchor), rr = Math.max(18, torso * 0.35), up = c.kind === "head" ? 2.2 * rr : rr;
        obstacles.push([[q[0] - rr, q[1]], [q[0] + rr, q[1]]]);
        obstacles.push([[q[0] - rr, q[1] - up * 0.6], [q[0] + rr, q[1] - up * 0.6]]);
        obstacles.push([[q[0], q[1] - up], [q[0], q[1] + rr]]);
      });
    });
    state.anchorNorm = anchor || null;
    if (anchor) {
      var vv = verdictOf(check);
      callout(px(anchor), pillText(check, vv), status, box, sizes.line, animated, labelSide(check), obstacles,
              { v2: true });
    }

    drawn.forEach(function (shape) {
      var len = 0;
      try { len = shape.getTotalLength(); } catch (e) { len = 0; }
      if (!animated || len <= 0 || typeof shape.animate !== "function") return;
      shape.animate([{ strokeDasharray: len.toFixed(1) + " " + len.toFixed(1), strokeDashoffset: len.toFixed(1) },
                     { strokeDasharray: len.toFixed(1) + " " + len.toFixed(1), strokeDashoffset: "0" }],
                    { duration: T.lineDur, delay: T.line, easing: "cubic-bezier(0.22, 0.7, 0.2, 1)", fill: "backwards" });
    });
    if (animated) {
      Array.prototype.forEach.call(o.querySelectorAll(".fade-early"), function (el, i) { timing(el, T.ref + Math.min(i, 3) * 40); });
      Array.prototype.forEach.call(o.querySelectorAll("g.fade"), function (el) { timing(el, T.label); });
      void o.getBoundingClientRect();
      requestAnimationFrame(function () { o.classList.add("in"); });
    } else {
      o.classList.add("in");
    }
  }

  /* ---------------------------------------------------------------- overlay v3 (?overlay=3)
   * Development only (staging / explicitly versioned test URLs). Same measured geometry, same colours,
   * same dark verdict bubble and torso-scaled stroke system as v2; what changes is the presentation:
   *
   *   joint focus -> reference line(s) -> measured segments one by one, each drawn outward from the
   *   measured joint -> angle arc sweep -> value -> verdict bubble; body guides dim once the
   *   measurement has landed.
   *
   * Every element's state is a pure function of one reveal clock (v3.elapsed, ms), so the overlay
   * freezes exactly where it is on pause, continues on play, restarts on replay / prev / next and is
   * cleared on seek or scrub. Holds and reveals run on wall-clock time: playback speed changes the
   * video between moments, never the hold. Reduced motion shows the final state at once.
   * Additional observations (analysis.observations, engine profile candidate) are walked through
   * here too, drawn in neutral white with no verdict colour, and are never scored. */

  var V3_HOLD_MS = 2300;        // a scored check: reveal (~1.3 s) plus reading time
  var V3_OBSERVATION_MS = 1600; // an observation: shorter, it is supporting detail
  var T3 = { focus: 0, focusDur: 340, guide: 40, guideDur: 300, ref: 150, refDur: 380, seg: 380, segDur: 300,
             segStep: 230, arcDur: 320, valueDur: 200, labelGap: 110, labelDur: 240, dimDur: 360 };
  var v3 = { elapsed: 0, end: 0, running: false, raf: 0, last: 0, items: [], checkId: null };

  function isObservation(check) { return !!(OVERLAY_V3 && check && check.observation); }

  function v3HoldMs(check) {
    if (isObservation(check)) return V3_OBSERVATION_MS;
    return Math.max(HOLD_MS, V3_HOLD_MS);
  }

  function ease(p) { p = clamp(p, 0, 1); return 1 - Math.pow(1 - p, 3); }

  function v3Progress(item) { return item.dur > 0 ? ease((v3.elapsed - item.start) / item.dur) : (v3.elapsed >= item.start ? 1 : 0); }

  function v3Apply() {
    v3.items.forEach(function (it) {
      var p = v3Progress(it);
      if (it.type === "seg") {
        var b = [it.a[0] + (it.b[0] - it.a[0]) * p, it.a[1] + (it.b[1] - it.a[1]) * p];
        var dd = p <= 0 ? "" : "M" + it.a[0].toFixed(1) + " " + it.a[1].toFixed(1) + "L" + b[0].toFixed(1) + " " + b[1].toFixed(1);
        it.els.forEach(function (el) { el.setAttribute("d", dd); });
      } else if (it.type === "arc") {
        it.els.forEach(function (el) { el.setAttribute("d", p <= 0 ? "" : arcPath(it.c, it.r, it.a0, it.sweep * p)); });
      } else if (it.type === "fade") {
        if (it.chain && v3.elapsed < it.start) return;   // a later fade on the same element waits its turn
        it.el.setAttribute("opacity", (it.from + (it.to - it.from) * p).toFixed(3));
        if (it.dy) it.el.setAttribute("transform", "translate(0 " + ((1 - p) * it.dy).toFixed(2) + ")");
      } else if (it.type === "pop") {
        var s = p <= 0 ? 0.001 : 0.35 + 0.65 * p + Math.sin(p * Math.PI) * 0.18;
        it.el.setAttribute("opacity", p <= 0 ? "0" : "1");
        it.el.setAttribute("transform", "translate(" + it.c[0].toFixed(1) + " " + it.c[1].toFixed(1) + ") scale(" +
                           s.toFixed(3) + ") translate(" + (-it.c[0]).toFixed(1) + " " + (-it.c[1]).toFixed(1) + ")");
      } else if (it.type === "pulse") {
        var raw = clamp((v3.elapsed - it.start) / it.dur, 0, 1);
        it.el.setAttribute("r", (it.r1 + (it.r0 - it.r1) * (1 - ease(raw))).toFixed(2));
        it.el.setAttribute("opacity", (raw <= 0 || raw >= 1 ? 0 : 0.9 * Math.sin(raw * Math.PI)).toFixed(3));
      }
    });
  }

  function v3Frame(stamp) {
    v3.raf = 0;
    if (!v3.running) return;
    var dt = Math.max(0, Math.min(100, stamp - (v3.last || stamp)));   // a background tab never jumps ahead
    v3.last = stamp;
    v3.elapsed = Math.min(v3.end, v3.elapsed + dt);
    v3Apply();
    if (v3.elapsed < v3.end) v3.raf = requestAnimationFrame(v3Frame);
    else v3.running = false;
  }

  function v3Run() {
    if (reducedMotion) { v3.elapsed = v3.end; v3.running = false; v3Apply(); return; }
    v3.running = v3.elapsed < v3.end;
    if (v3.running && !v3.raf) { v3.last = now(); v3.raf = requestAnimationFrame(v3Frame); }
  }

  function v3Stop() { v3.running = false; if (v3.raf) { cancelAnimationFrame(v3.raf); v3.raf = 0; } }

  function arcPath(c, r, a0, sweep) {
    var s = polar(c, r, a0), e = polar(c, r, a0 + sweep);
    return "M" + s[0].toFixed(1) + " " + s[1].toFixed(1) + "A" + r.toFixed(1) + " " + r.toFixed(1) + " 0 " +
           (Math.abs(sweep) > 180 ? 1 : 0) + " " + (sweep > 0 ? 1 : 0) + " " + e[0].toFixed(1) + " " + e[1].toFixed(1);
  }

  function deg(a, b) { return Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI; }
  function dist(a, b) { return Math.hypot(a[0] - b[0], a[1] - b[1]); }

  // An empty stroked path pair (under-line + line) whose geometry the clock writes.
  function v3Path(parent, colour, width, opts) {
    var s = stroked(parent, "", colour, width, opts || {});
    return [s.under, s.line].filter(Boolean);
  }

  // The angle a check shows, as [vertex, reference direction point, measured direction point], or null.
  function v3Angle(check, geometry, px) {
    var lines = geometry.filter(function (g) { return (g.kind === "line" || g.kind === "polyline") && (g.points || []).length >= 2; });
    var ref = lines.filter(function (g) { return g.role === "reference"; })[0];
    var measured = lines.filter(function (g) { return g.role === "primary"; })[0];
    if (!ref || !measured) return null;
    if (ARC_KINDS[check.kind]) {
      var v = px(ref.points[0]), r1 = px(ref.points[1]);
      var mp = measured.points.map(px), k = 0, best = Infinity;
      mp.forEach(function (q, i) { var dd = dist(q, v); if (dd < best) { best = dd; k = i; } });
      var other = mp[k + 1] || mp[k - 1];
      return other ? { v: v, ref: r1, meas: other } : null;
    }
    if (check.kind === "posture") {
      // Address hip -> shoulder line, carried to the current hip; the current hip -> shoulder line.
      var a0 = px(ref.points[0]), a1 = px(ref.points[1]), h = px(measured.points[0]), s = px(measured.points[1]);
      return { v: h, ref: [h[0] + (a1[0] - a0[0]), h[1] + (a1[1] - a0[1])], meas: s };
    }
    return null;
  }

  function v3ValueText(parent, at, text, sizes, frame, obstacles) {
    if (!text || at[0] < frame.x0 + 14 || at[0] > frame.x1 - 14 || at[1] < frame.y0 + 10 || at[1] > frame.y1 - 10) return null;
    var t = svg("text", { x: at[0].toFixed(1), y: at[1].toFixed(1), "class": "value-text", "text-anchor": "middle",
                          "dominant-baseline": "central", "font-size": sizes.valueFont.toFixed(1), opacity: 0 }, parent);
    t.textContent = text;
    obstacles.push([[at[0] - 16, at[1]], [at[0] + 16, at[1]]]);
    return t;
  }

  function signedDeg(value) {
    var n = Math.round(Number(value));
    return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n) + "°";
  }

  /* Address references between checkpoints (v3): once the Address moment has been shown, the Address
   * club line (extended: the plane line) and the Address spine line stay faintly on screen while the
   * video plays, so the overlay never goes blank and the swing can be read against the set-up. They
   * are the engine's own Address geometry (nothing new is measured) and only drawn when the camera
   * check found the phone still: if it moved, Address lines would sit in the wrong place. */
  var PERSIST_OPACITY = 0.55, PERSIST_HOLD_OPACITY = 0.3;

  function addressCheck(kind) {
    var p = (data && data.phases || []).filter(function (q) { return q.phase === "address"; })[0];
    return p ? p.checks.filter(function (c) { return c.kind === kind && c.geometry; })[0] : null;
  }

  function persistAllowed() {
    var motion = ((data && data.camera_check || {}).signals || {}).camera_motion || {};
    var a = (data && data.phases || []).filter(function (q) { return q.phase === "address"; })[0];
    return motion.status === "VALID" && a && a.time_s !== null &&
           Number(els.video.currentTime) >= frameTime(a.time_s) - 0.25 * step();
  }

  // The Address reference lines (returns the drawn group, or null). skip: {plane, spine}.
  function drawAddressPersist(box, frame, skip) {
    if (!persistAllowed()) return null;
    function px(p) { return [box.x + p[0] * box.width, box.y + p[1] * box.height]; }
    var g = svg("g", { "class": "persist" });
    var torsoCheck = addressCheck("torso_angle");
    var torso = (torsoCheck && torsoPx(torsoCheck, px)) || box.height * 0.2;
    var w = clamp(torso * 0.012, 1.1, 1.9), dash = "0.01 " + (w * 3.8).toFixed(1);
    var opts = { dash: dash, opacity: 0.95, underPad: 1.2, underOpacity: 0.35 };
    var shaft = addressCheck("shaft"), drawn = 0;
    if (shaft && !skip.plane) {
      var s = shaft.geometry.filter(function (gm) { return gm.role === "shaft" && (gm.points || []).length === 2; })[0];
      if (s) {
        var a = px(s.points[0]), b = px(s.points[1]), full = clippedPath(extendLine(a, b), frame);
        if (full) { stroked(g, full, WHITE, w, opts); drawn += 1; }
      }
    }
    if (torsoCheck && !skip.spine) {
      var line = torsoCheck.geometry.filter(function (gm) { return gm.role === "primary" && (gm.kind === "line"); })[0];
      if (line) {
        var hip = px(line.points[0]), sh = px(line.points[1]);
        var up = [sh[0] + (sh[0] - hip[0]) * 0.6, sh[1] + (sh[1] - hip[1]) * 0.6];   // on toward the head
        var path = clippedPath([hip, up], frame);
        if (path) { stroked(g, path, WHITE, w, opts); drawn += 1; }
      }
    }
    if (!drawn) { g.parentNode.removeChild(g); return null; }
    return g;
  }

  function drawOverlayV3(check, animate) {
    var o = els.overlay;
    while (o.firstChild) o.removeChild(o.firstChild);
    o.classList.add("v3");
    state.overlayBox = null;
    state.overlayAnchor = null;
    var same = check && v3.checkId === check.id;
    v3.items = [];
    v3.checkId = check ? check.id : null;
    var box = contentBox();
    if (!box || !check || !check.geometry) {
      v3Stop(); v3.elapsed = 0;
      if (box) {
        o.setAttribute("viewBox", "0 0 " + box.boxWidth + " " + box.boxHeight); state.overlayBox = box;
        // Between checkpoints: the Address references, faded in once per pass (then just shown).
        var pf = { x0: box.x + 5, y0: box.y + 5, x1: box.x + box.width - 5, y1: box.y + box.height - 5 };
        var persist = state.mode !== "ended" ? drawAddressPersist(box, pf, {}) : null;
        if (persist) {
          persist.setAttribute("opacity", state.v3PersistShown ? PERSIST_OPACITY : 0);
          if (!state.v3PersistShown && !reducedMotion) {
            void persist.getBoundingClientRect();
            requestAnimationFrame(function () { persist.setAttribute("opacity", PERSIST_OPACITY); });
          } else persist.setAttribute("opacity", PERSIST_OPACITY);
          state.v3PersistShown = true;
        } else if (!persistAllowed()) state.v3PersistShown = false;
      }
      o.classList.add("in"); return;
    }
    o.setAttribute("viewBox", "0 0 " + box.boxWidth + " " + box.boxHeight);
    state.overlayBox = box;
    o.classList.add("in");
    if (check.phase !== "address") {
      // The Address references stay underneath, dimmer, unless this check draws the same reference itself.
      var under = drawAddressPersist(box, { x0: box.x + 5, y0: box.y + 5, x1: box.x + box.width - 5, y1: box.y + box.height - 5 },
                                     { plane: check.kind === "shaft", spine: check.kind === "posture" });
      if (under) under.setAttribute("opacity", PERSIST_HOLD_OPACITY);
    }
    var observation = isObservation(check);
    var extension = check.kind === "pelvis" && check.pelvis_depth && check.dev_geometry;
    var geometry = extension && !DEV ? check.dev_geometry : check.geometry.concat(DEV && check.dev_geometry ? check.dev_geometry : []);
    function px(p) { return [box.x + p[0] * box.width, box.y + p[1] * box.height]; }
    var status = verdictOf(check).status;
    var colour = observation ? STATUS_STROKE.info : (STATUS_STROKE[status] || STATUS_STROKE.info);
    var inset = 5;
    var frame = { x0: box.x + inset, y0: box.y + inset, x1: box.x + box.width - inset, y1: box.y + box.height - inset };
    var torso = torsoPx(check, px) || box.height * 0.2;
    var sizes = {
      line: clamp(torso * 0.021, 1.7, 3.2), ref: clamp(torso * 0.013, 1.3, 2.1), guide: clamp(torso * 0.011, 1.0, 1.7),
      joint: clamp(torso * 0.026, 2.6, 4.6), guideJoint: clamp(torso * 0.013, 1.5, 2.6), arc: clamp(torso * 0.3, 16, 64),
      arcWidth: clamp(torso * 0.014, 1.3, 2.2), valueGap: clamp(torso * 0.09, 9, 16), valueFont: clamp(torso * 0.085, 10.5, 13)
    };
    state.overlaySizes = sizes;
    var dash = "0.01 " + (sizes.ref * 3.6).toFixed(1);
    var guides = svg("g", { opacity: 0 });
    var refs = svg("g", {});
    var shapes = svg("g", {});
    var marks = svg("g", {});
    var focus = svg("g", {});
    var obstacles = [];
    var items = v3.items;
    var planeView = check.kind === "shaft" && (check.phase === "mid_back" || check.phase === "downswing") &&
                    check.reference_frame !== "unaligned";
    var angle = v3Angle(check, geometry, px);

    // The measured joint: where the eye is led first.
    var vertex = angle ? angle.v : null;
    if (!vertex) {
      var dot = geometry.filter(function (g) { return g.kind === "dot" && g.role === "primary"; })[0];
      vertex = dot ? px(dot.points[0]) : (check.anchor ? px(check.anchor) : null);
    }
    if (vertex && (vertex[0] < frame.x0 || vertex[0] > frame.x1 || vertex[1] < frame.y0 || vertex[1] > frame.y1)) vertex = null;
    if (vertex) {
      var ring = svg("circle", { cx: vertex[0].toFixed(1), cy: vertex[1].toFixed(1), r: 0, fill: "none", stroke: colour,
                                 "stroke-width": Math.max(1.2, sizes.arcWidth).toFixed(2), opacity: 0 }, focus);
      items.push({ type: "pulse", el: ring, start: T3.focus, dur: T3.focusDur, r0: sizes.joint * 4.2, r1: sizes.joint * 1.6 });
    }

    // Segments run outward from the measured joint (or from the Address clubhead for the plane reference).
    function oriented(a, b, from) {
      if (!from) return [a, b];
      return dist(a, from) <= dist(b, from) ? [a, b] : [b, a];
    }
    function addSeg(parent, a, b, colourOf, width, opts, start, dur) {
      var seg = clipSegment(a, b, frame);
      if (!seg) return null;
      var item = { type: "seg", els: v3Path(parent, colourOf, width, opts), a: seg[0], b: seg[1], start: start, dur: dur };
      items.push(item);
      return item;
    }

    var refEnd = T3.ref, measuredSegs = [], markers = [];
    var origin = null;
    geometry.forEach(function (g) {
      if (g.kind === "dot" && g.role === "origin" && planeView) origin = px(g.points[0]);
    });
    geometry.forEach(function (g) {
      var pts = (g.points || []).map(px);
      if (!pts.length) return;
      var role = g.role;
      if (g.kind === "line" || g.kind === "polyline") {
        var isRef = role === "reference";
        for (var i = 1; i < pts.length; i++) {
          var cs = clipSegment(pts[i - 1], pts[i], frame);
          if (cs) { if (isRef) cs.weight = 0.25; obstacles.push(cs); }
        }
        if (!isRef && role !== "accent" && pts.length >= 2) {
          var a = pts[0], b = pts[pts.length - 1];
          var hi = a[1] < b[1] ? a : b, lo = hi === a ? b : a;
          var extSeg = clipSegment(hi, [hi[0] + (hi[0] - lo[0]) * 0.8, hi[1] + (hi[1] - lo[1]) * 0.8], frame);
          if (extSeg) obstacles.push(extSeg);
        }
        if (isRef || (role === "accent" && !extension)) {
          var refOpts = { dash: dash, opacity: 0.9, underPad: 1.4, underOpacity: 0.45 };
          if (planeView && isRef && pts.length === 2) {
            // Full-length Address reference, drawn from the Address clubhead outward both ways.
            var full = extendLine(pts[0], pts[1]), from = origin || pts[1];
            addSeg(refs, from, full[0], WHITE, sizes.ref, refOpts, T3.ref, T3.refDur + 120);
            addSeg(refs, from, full[1], WHITE, sizes.ref, refOpts, T3.ref, T3.refDur + 120);
            refEnd = Math.max(refEnd, T3.ref + T3.refDur + 120);
          } else {
            for (var j = 1; j < pts.length; j++) {
              var ab = oriented(pts[j - 1], pts[j], role === "accent" ? null : vertex);
              addSeg(refs, ab[0], ab[1], WHITE, sizes.ref, refOpts, T3.ref, T3.refDur);
            }
            refEnd = Math.max(refEnd, T3.ref + T3.refDur);
          }
        } else {
          // The measured line in its verdict colour (an observation: neutral white), segment by segment.
          if (role === "shaft" && pts.length === 2) {
            measuredSegs.push({ a: pts[0], b: pts[1], shaft: true });   // grip -> clubhead
          } else {
            var segs = [];
            for (var k = 1; k < pts.length; k++) segs.push(oriented(pts[k - 1], pts[k], vertex));
            if (vertex) segs.sort(function (s1, s2) { return dist(s1[0], vertex) - dist(s2[0], vertex); });
            segs.forEach(function (s) { measuredSegs.push({ a: s[0], b: s[1] }); });
          }
        }
      } else if (g.kind === "ring") {
        var r = Math.max(sizes.joint * 2.4, Number(g.radius) * box.height);
        obstacles.push([[pts[0][0] - r, pts[0][1]], [pts[0][0] + r, pts[0][1]]]);
        var ringRef = role === "reference";
        var rp = ringRef ? v3Path(refs, WHITE, sizes.ref, { dash: dash, opacity: 0.9, underPad: 1.4, underOpacity: 0.45 })
                         : v3Path(shapes, colour, sizes.line, {});
        items.push({ type: "arc", els: rp, c: pts[0], r: r, a0: -90, sweep: 359.9,
                     start: ringRef ? T3.ref : T3.seg, dur: ringRef ? T3.refDur : T3.segDur + 80 });
        if (ringRef) refEnd = Math.max(refEnd, T3.ref + T3.refDur);
        else measuredSegs.push({ ring: true });
      } else if (g.kind === "dot" && role !== "origin") {
        markers.push([clamp(pts[0][0], frame.x0, frame.x1), clamp(pts[0][1], frame.y0, frame.y1)]);
      }
    });

    if (origin) {
      var og = svg("g", {}, refs);
      svg("circle", { cx: origin[0], cy: origin[1], r: (sizes.joint + 2.6).toFixed(2), fill: "none", stroke: UNDER, "stroke-width": 3, opacity: 0.5 }, og);
      svg("circle", { cx: origin[0], cy: origin[1], r: (sizes.joint + 2.6).toFixed(2), fill: "none", stroke: "#fff", "stroke-width": 1.4 }, og);
      items.push({ type: "pop", el: og, c: origin, start: T3.ref - 60, dur: 220 });
    }

    // Measured segments in sequence, each starting once the references are under way.
    var t = Math.max(T3.seg, refEnd - 160), measuredEnd = t, drawnSegs = [];
    measuredSegs.forEach(function (s) {
      if (s.ring) { measuredEnd = Math.max(measuredEnd, T3.seg + T3.segDur + 80); return; }
      var item = addSeg(shapes, s.a, s.b, colour, sizes.line, {}, t, T3.segDur);
      if (item) {
        drawnSegs.push(item);
        measuredEnd = Math.max(measuredEnd, t + T3.segDur);
        t += T3.segStep;
      }
      if (s.shaft && planeView) {
        // The current shaft line across the frame as faint dots (to compare with the Address line).
        var across = extendLine(s.a, s.b);
        addSeg(refs, s.a, across[1], colour, sizes.ref, { dash: dash, opacity: 0.55, under: false }, t, T3.refDur);
        addSeg(refs, s.a, across[0], colour, sizes.ref, { dash: dash, opacity: 0.55, under: false }, t, T3.refDur);
      }
    });
    // A joint marker pops when the first measured segment touches it: at its start (the measured
    // joint) or as the line arrives (the far joint). Markers on no segment land with the measurement.
    markers.forEach(function (m) {
      var at = null;
      drawnSegs.forEach(function (it) {
        if (dist(m, it.a) < 2) at = at === null ? it.start : Math.min(at, it.start);
        if (dist(m, it.b) < 2) at = at === null ? it.start + it.dur - 60 : Math.min(at, it.start + it.dur - 60);
      });
      var mk = marker(marks, m, sizes.joint, colour, null);
      mk.setAttribute("opacity", 0);
      items.push({ type: "pop", el: mk, c: m, start: at === null ? measuredEnd - 120 : at, dur: 260 });
    });

    // Angle arc + value (body angles, posture), or the plane wedge at the grip.
    var valueAt = measuredEnd, valueEl = null;
    if (angle) {
      var a1 = deg(angle.v, angle.ref), a2 = deg(angle.v, angle.meas);
      var sweep = ((a2 - a1 + 540) % 360) - 180;
      if (Math.abs(sweep) >= 2) {
        var ra = sizes.arc;
        items.push({ type: "arc", els: v3Path(marks, colour, sizes.arcWidth, { opacity: 0.95 }), c: angle.v, r: ra, a0: a1, sweep: sweep,
                     start: measuredEnd - 40, dur: T3.arcDur });
        var mid;
        if (Math.abs(sweep) >= 35) {
          mid = polar(angle.v, ra + sizes.valueGap, a1 + sweep / 2);
        } else {
          var along = polar(angle.v, ra * 0.9, a1 + sweep);
          var out = (a1 + sweep + (sweep > 0 ? 90 : -90)) * Math.PI / 180;
          mid = [along[0] + Math.cos(out) * (sizes.valueGap + 8), along[1] + Math.sin(out) * (sizes.valueGap + 8)];
        }
        valueEl = v3ValueText(marks, mid, check.value, sizes, frame, obstacles);
        valueAt = measuredEnd - 40 + T3.arcDur - 80;
      }
    } else if (planeView && check.raw && check.raw.change_vs_address_deg != null &&
               Math.abs(Number(check.raw.change_vs_address_deg)) >= 2) {
      // Plane wedge at the grip: the Address direction (dashed) against the current shaft, with the
      // engine's own change value.
      var shaftG = geometry.filter(function (g) { return g.role === "shaft"; })[0];
      var refG = geometry.filter(function (g) { return g.role === "reference"; })[0];
      if (shaftG && refG) {
        var grip = px(shaftG.points[0]), tip = px(shaftG.points[1]);
        var rd = [px(refG.points[1])[0] - px(refG.points[0])[0], px(refG.points[1])[1] - px(refG.points[0])[1]];
        var sd = [tip[0] - grip[0], tip[1] - grip[1]];
        if (rd[0] * sd[0] + rd[1] * sd[1] < 0) rd = [-rd[0], -rd[1]];      // the reference direction on the clubhead side
        var rw = sizes.arc * 1.35, rn = Math.hypot(rd[0], rd[1]) || 1;
        var rayEnd = [grip[0] + rd[0] / rn * rw * 1.25, grip[1] + rd[1] / rn * rw * 1.25];
        addSeg(marks, grip, rayEnd, WHITE, sizes.ref, { dash: dash, opacity: 0.9, underPad: 1.4, underOpacity: 0.45 }, measuredEnd - 60, 220);
        var w1 = deg(grip, rayEnd), w2 = deg(grip, tip), ws = ((w2 - w1 + 540) % 360) - 180;
        if (Math.abs(ws) >= 1.5) {
          items.push({ type: "arc", els: v3Path(marks, colour, sizes.arcWidth, { opacity: 0.95 }), c: grip, r: rw, a0: w1, sweep: ws,
                       start: measuredEnd + 80, dur: T3.arcDur });
          var wm = polar(grip, rw + sizes.valueGap + 6, w1 + ws / 2);
          valueEl = v3ValueText(marks, wm, signedDeg(check.raw.change_vs_address_deg), sizes, frame, obstacles);
          valueAt = measuredEnd + 80 + T3.arcDur - 80;
        }
      }
    }
    if (valueEl) items.push({ type: "fade", el: valueEl, from: 0, to: 1, dy: 3, start: valueAt, dur: T3.valueDur });

    // Body guides: faint while the measurement draws, dimmer once it has landed (less clutter).
    drawGuidesV2(check, guides, geometry, px, frame, sizes, obstacles);
    if (guides.firstChild) {
      items.push({ type: "fade", el: guides, from: 0, to: 0.95, start: T3.guide, dur: T3.guideDur });
      items.push({ type: "fade", el: guides, from: 0.95, to: 0.5, start: measuredEnd, dur: T3.dimDur, chain: true });
    }

    // Other measured points at this moment keep the bubble off the golfer (as v2).
    var anchor = check.anchor;
    if (extension && !DEV) {
      var rear = check.dev_geometry.filter(function (g) { return g.kind === "dot"; })[0];
      if (rear) anchor = rear.points[0];
    }
    (data.phases || []).forEach(function (p) {
      if (p.phase !== check.phase) return;
      p.checks.forEach(function (c) {
        if (c === check || !c.anchor) return;
        (c.geometry || []).forEach(function (g) {
          if ((g.kind === "line" || g.kind === "polyline") && g.role !== "reference") {
            var gp = g.points.map(px);
            for (var q = 1; q < gp.length; q++) { var s2 = clipSegment(gp[q - 1], gp[q], frame); if (s2) obstacles.push(s2); }
          }
        });
        var cq = px(c.anchor), rr = Math.max(18, torso * 0.35), up = c.kind === "head" ? 2.2 * rr : rr;
        obstacles.push([[cq[0] - rr, cq[1]], [cq[0] + rr, cq[1]]]);
        obstacles.push([[cq[0] - rr, cq[1] - up * 0.6], [cq[0] + rr, cq[1] - up * 0.6]]);
        obstacles.push([[cq[0], cq[1] - up], [cq[0], cq[1] + rr]]);
      });
    });
    state.anchorNorm = anchor || null;
    var labelStart = Math.max(valueAt + (valueEl ? T3.valueDur : 0), measuredEnd) + T3.labelGap;
    if (anchor) {
      var vv = verdictOf(check);
      callout(px(anchor), pillText(check, vv), observation ? "info" : status, box, sizes.line, false, labelSide(check),
              obstacles, { v2: true });
      var bubble = o.lastChild;
      bubble.setAttribute("opacity", 0);
      items.push({ type: "fade", el: bubble, from: 0, to: 1, dy: 4, start: labelStart, dur: T3.labelDur });
    }
    v3.end = labelStart + T3.labelDur;
    if (animate) v3.elapsed = 0;
    else if (!same) v3.elapsed = v3.end;            // shown without its reveal (e.g. a results-screen jump)
    v3.elapsed = Math.min(v3.elapsed, v3.end);
    state.v3 = { end: v3.end, items: items.length };   // diagnostics (QA)
    v3Apply();
    if (animate) v3Run();
    else if (v3.running && !v3.raf) v3Run();          // a relayout mid-reveal keeps going
  }

  // Body measurements prefer their label behind the golfer (away from the
  // ball), where it cannot sit on the hands or the club; shaft labels have no
  // preference. +1 = right, -1 = left, 0 = none.
  function labelSide(check) {
    var ctx = data && data.geometry_context;
    if (check.kind === "shaft" || check.kind === "arm_hang" || !ctx || !ctx.toward_ball_known || !ctx.toward_ball) return 0;
    return ctx.toward_ball > 0 ? -1 : 1;
  }

  // Area of rect a outside rect b.
  function outside(a, b) {
    var ix = Math.max(0, Math.min(a.x + a.w, b.x1) - Math.max(a.x, b.x0));
    var iy = Math.max(0, Math.min(a.y + a.h, b.y1) - Math.max(a.y, b.y0));
    return a.w * a.h - ix * iy;
  }

  // How much of the drawn geometry a label rect would cover (sampled).
  function covers(rect, segments) {
    var hits = 0, pad = 9;  // a little breathing room around hands, club and lines
    segments.forEach(function (s) {
      for (var k = 0; k <= 12; k++) {
        var t = k / 12, x = s[0][0] + t * (s[1][0] - s[0][0]), y = s[0][1] + t * (s[1][1] - s[0][1]);
        if (x > rect.x - pad && x < rect.x + rect.w + pad && y > rect.y - pad && y < rect.y + rect.h + pad) hits += s.weight || 1;
      }
    });
    return hits;
  }

  // The verdict pill: anchored to the measured point, placed at the best of a
  // few preferred positions, always inside the safe frame, never on the
  // measurement it describes, with a short straight leader.
  function callout(anchor, text, status, box, w, animated, side, obstacles, opts) {
    var v2 = !!(opts && opts.v2);
    var g = svg("g", { "class": animated ? "fade callout" : "callout" });
    var label = svg("text", { "class": v2 ? "callout-text v2" : "callout-text", "dominant-baseline": "central" }, g);
    label.textContent = text;
    var tw = 0;
    try { tw = label.getComputedTextLength(); } catch (e) { tw = 0; }
    if (!tw) tw = text.length * 7.4;
    var dotR = status === "info" ? 0 : 3.2;
    var padL = dotR ? 20 : 11, padR = 11, pw = tw + padL + padR, ph = 25;
    // Safe frame: inside the picture, below the glass controls at the top.
    var margin = 10;
    var safe = { x0: box.x + margin, y0: box.y + 64, x1: box.x + box.width - margin, y1: box.y + box.height - margin };
    // Capture only: verdict bubbles stay below the checkpoint card at the top of the frame.
    if (CAPTURE && state.captureReserve) safe.y0 = Math.max(safe.y0, state.captureReserve);
    var gx = Math.max(22, box.width * 0.07), gy = Math.max(26, box.height * 0.045);
    var ax = anchor[0], ay = anchor[1];
    function place(name, x, y) { return { name: name, x: x, y: y, w: pw, h: ph }; }
    var sides = side ? [side, -side] : (ax < box.x + box.width / 2 ? [1, -1] : [-1, 1]);
    var candidates = [];
    sides.forEach(function (s) {
      var hx = s > 0 ? ax + gx : ax - gx - pw;
      candidates.push(place(s > 0 ? "top-right" : "top-left", hx, ay - gy - ph));
      candidates.push(place(s > 0 ? "right" : "left", hx + (s > 0 ? 6 : -6), ay - ph / 2));
      candidates.push(place(s > 0 ? "below-right" : "below-left", hx, ay + gy));
    });
    candidates.push(place("above", ax - pw / 2, ay - gy * 1.6 - ph));
    candidates.push(place("below", ax - pw / 2, ay + gy * 1.4));
    sides.forEach(function (s) {  // higher, clear of a head above the anchor
      candidates.push(place(s > 0 ? "high-right" : "high-left", s > 0 ? ax + gx : ax - gx - pw, ay - gy * 3 - ph));
    });
    var best = null, scored = [];
    candidates.forEach(function (c, order) {
      // Each position is judged where it would really be drawn: clamped into
      // the safe frame. Covering the drawn geometry or the body costs most,
      // then how far clamping moved it, preference order and leader length.
      var cx = Math.max(safe.x0, Math.min(safe.x1 - pw, c.x)), cy = Math.max(safe.y0, Math.min(safe.y1 - ph, c.y));
      var placed = { name: c.name, x: cx, y: cy, w: pw, h: ph };
      // Distance to the anchor weighs in, so the label stays close to what it describes.
      var score = covers(placed, obstacles || []) * 120 + Math.hypot(cx - c.x, cy - c.y) * 3 + order * 20 +
                  Math.hypot(cx + pw / 2 - ax, cy + ph / 2 - ay) * 1.1;
      scored.push([c.name, Math.round(score), Math.round(covers(placed, obstacles || []))]);
      if (!best || score < best.score) { best = placed; best.score = score; }
    });
    state.placementScores = scored;  // developer diagnostics only
    var x = Math.max(safe.x0, Math.min(safe.x1 - pw, best.x));
    var y = Math.max(safe.y0, Math.min(safe.y1 - ph, best.y));
    // Leader: from the anchor to the nearest point of the pill's edge.
    var ex = Math.max(x, Math.min(x + pw, ax)), ey = Math.max(y, Math.min(y + ph, ay));
    if (ex > x && ex < x + pw && ey > y && ey < y + ph) ey = y + ph;  // anchor under the pill
    if (Math.hypot(ex - ax, ey - ay) > 6) {
      // A short horizontal step off the anchor, then straight to the pill.
      var step = Math.sign(ex - ax || 1) * Math.min(12, Math.abs(ex - ax) * 0.5);
      svg("path", { d: d([anchor, [ax + step, ay], [ex, ey]]), fill: "none", stroke: WHITE, "stroke-width": 1,
                    "stroke-linecap": "round", "stroke-linejoin": "round", opacity: 0.7 }, g);
    }
    var pill = svg("rect", { x: x, y: y, width: pw, height: ph, rx: ph / 2, fill: PILL,
                             stroke: "rgba(255,255,255,0.10)", "stroke-width": 0.75 }, g);
    g.insertBefore(pill, label);
    if (dotR) svg("circle", { cx: x + 12, cy: y + ph / 2, r: dotR, fill: STATUS_STROKE[status] || STATUS_STROKE.info }, g);
    label.setAttribute("x", (x + padL).toFixed(1));
    label.setAttribute("y", (y + ph / 2).toFixed(1));
    svg("circle", { cx: anchor[0], cy: anchor[1], r: 2.1, fill: "#fff" }, g);
    state.overlayAnchor = { anchor: anchor, pill: [x, y, pw, ph], placement: best.name };
  }

  /* ------------------------------------------------------------ screens */

  function buildPhaseChips() {
    els.phases.innerHTML = "";
    (data.phases || []).forEach(function (p) {
      var b = document.createElement("button");
      b.setAttribute("data-phase", p.phase);
      b.setAttribute("data-time", p.time_s === null ? "" : String(p.time_s));
      b.setAttribute("data-phase-status", p.status || "info");  // worst verdict among its guided checks
      b.setAttribute("aria-label", p.label);
      b.title = p.label;
      var has = p.checks && p.checks.length;
      b.disabled = !has;
      if (!has) b.title = p.label + " - no reliable measurement";
      b.addEventListener("click", function () {
        var e = events.filter(function (ev) { return ev.phase === p.phase; })[0];
        if (e) goToCheck(flat.indexOf(e.checks[0]), false);
      });
      els.phases.appendChild(b);
    });
  }

  function buildResults() {
    var s = data.summary || {};
    $("stat-checks").textContent = String(s.reliable_checks || 0);
    $("stat-phases").textContent = String(s.phases || 0);
    var focus = data.focus;
    var box = $("focus"), body = $("focus-body");
    body.innerHTML = "";
    // Main focus only ever comes from a validated interpretation rule.
    var interp = data.interpretations || {};
    if (focus && focus.text) {
      box.classList.remove("empty");
      body.innerHTML = "<div class='headline'></div><p class='sentence'></p>";
      body.querySelector(".headline").textContent = focus.text;
      body.querySelector(".sentence").textContent = focus.why_it_matters || "";
    } else {
      box.classList.add("empty");
      body.innerHTML = "<p></p>";
      body.querySelector("p").textContent = interp.focus_fallback || "Coaching priorities are coming next.";
      var observed = (interp.results || []).filter(function (r) { return r.status === "observed" && r.user_text; });
      observed.slice(0, 1).forEach(function (r) {
        var p = document.createElement("p");
        p.className = "observation";
        p.textContent = r.user_text + " (An observation, not yet a coaching verdict.)";
        body.appendChild(p);
      });
    }
    var groups = $("groups");
    groups.innerHTML = "";
    (data.phases || []).forEach(function (p) {
      var g = document.createElement("div");
      g.className = "group";
      var head = document.createElement("div");
      head.className = "group-head";
      head.innerHTML = "<strong></strong><span></span>";
      head.querySelector("strong").textContent = p.label;
      head.querySelector("span").textContent = p.checks.length ? p.checks.length + (p.checks.length === 1 ? " check" : " checks") : "";
      g.appendChild(head);
      if (!p.checks.length) {
        var none = document.createElement("div");
        none.className = "empty-row";
        none.textContent = "No reliable measurement at this moment.";
        g.appendChild(none);
      }
      p.checks.forEach(function (c) {
        var row = document.createElement("div");
        row.className = "row";
        row.innerHTML = "<div class='row-main'><div class='row-title'></div><div class='row-detail'></div></div><div class='row-value'></div>" +
          "<button class='replay' aria-label='Replay'><svg viewBox='0 0 24 24'><path d='M8 5.5v13l11-6.5z'/></svg></button>";
        // Verdict first (with its colour), then what was measured.
        var v = verdictOf(c);
        var title = row.querySelector(".row-title");
        var dot = document.createElement("span");
        dot.className = "row-dot";
        dot.setAttribute("data-status", v.status);
        title.appendChild(dot);
        title.appendChild(document.createTextNode(v.label));
        row.querySelector(".row-detail").textContent = c.title + (c.estimated ? " · estimated" : "");
        row.querySelector(".row-value").textContent = v.value || c.value;
        if (v.qualifier) {
          var q = document.createElement("small");
          q.textContent = v.qualifier;
          row.querySelector(".row-value").appendChild(q);
        }
        row.querySelector(".replay").addEventListener("click", function () { replayCheck(c.id); });
        g.appendChild(row);
      });
      groups.appendChild(g);
    });
    var hidden = (data.hidden || []).length;
    $("footnote").textContent = (hidden ? hidden + " further checks were left out because the tracking wasn't reliable enough. " : "") +
      "Moments are found automatically from your body movement. Values are measured in this camera view.";
  }

  function replayCheck(id) {
    var index = -1;
    flat.forEach(function (c, i) { if (c.id === id) index = i; });
    showScreen("analysis");
    if (index >= 0) { goToCheck(index, false); return; }
    var check = allChecks.filter(function (c) { return c.id === id; })[0];
    if (!check) return;
    clearTransition();
    clearTimeout(state.endTimer);
    els.video.pause();
    unwatchFrames();
    state.adhoc = null;
    state.mode = "seeking";
    state.awaitingSeek = { kind: "adhoc", check: check };
    seek(frameTime(check.time_s), "check");
    render();
    ensureLoop();
  }

  function showScreen(name) {
    state.screen = name;
    els.analysis.hidden = name !== "analysis";
    els.results.hidden = name !== "results";
    if (name === "results") {
      if (state.mode === "playing" || state.mode === "holding") pause();
      grabThumb();
    } else {
      requestAnimationFrame(layout);
    }
  }

  function grabThumb() {
    try {
      var v = els.video, c = document.createElement("canvas");
      c.width = 168; c.height = Math.round(168 * v.videoHeight / v.videoWidth);
      c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
      $("hero-thumb").src = c.toDataURL("image/jpeg", 0.8);
    } catch (e) { /* ignore */ }
  }

  /* -------------------------------------------------------------- setup */

  function bind() {
    els.video = $("video"); els.overlay = $("overlay"); els.stage = $("stage"); els.stageWrap = $("stage-wrap");
    els.progress = $("progress"); els.ticks = $("ticks"); els.seek = $("seek"); els.loading = $("loading");
    els.phasePill = $("phase-pill"); els.phases = $("phases"); els.play = $("play");
    els.cardPhase = $("card-phase"); els.cardTitle = $("card-title"); els.cardValue = $("card-value");
    els.cardDetail = $("card-detail"); els.badge = $("card-badge"); els.dots = $("dots");
    els.explain = $("explain"); els.explainText = $("explain-text"); els.card = $("card");
    els.explainTitle = $("explain-title"); els.explainNote = $("explain-note"); els.devMapping = $("dev-mapping");
    els.analysis = $("screen-analysis"); els.results = $("screen-results");

    els.play.addEventListener("click", toggle);
    $("prev").addEventListener("click", function () { stepCheck(-1); });
    $("next").addEventListener("click", function () { stepCheck(1); });
    els.seek.addEventListener("input", onScrub);
    $("speed").addEventListener("click", cycleSpeed);
    if (EMBED) {
      var back = $("to-results");
      var closing = false;  // one close per tap burst: a double tap must not pop two screens
      back.addEventListener("click", function () {
        if (closing) return;
        closing = true;
        setTimeout(function () { closing = false; }, 1200);
        notifyApp("close");  // first: a failed render must never block the exit
        try { pause(); } catch (e) { /* closing anyway */ }
      });
      back.setAttribute("aria-label", "Back to summary");
      back.innerHTML = '<svg viewBox="0 0 24 24"><path d="M14.5 6.5L9 12l5.5 5.5" /></svg>';
    } else {
      $("to-results").addEventListener("click", function () { showScreen("results"); });
    }
    $("back-to-video").addEventListener("click", function () { showScreen("analysis"); });
    $("watch-again").addEventListener("click", function () { showScreen("analysis"); replay(); });
    els.explain.addEventListener("toggle", function () { if (els.explain.open && state.mode === "holding") pause(); });
    document.addEventListener("keydown", function (e) {
      if (e.target && e.target.tagName === "INPUT") return;
      if (e.key === " ") { e.preventDefault(); toggle(); }
      else if (e.key === "ArrowRight") stepCheck(1);
      else if (e.key === "ArrowLeft") stepCheck(-1);
    });
    var v = els.video;
    v.addEventListener("seeked", onSeeked);
    v.addEventListener("loadstart", function () { state.stats.loadstarts += 1; });
    v.addEventListener("loadedmetadata", function () {
      els.loading.hidden = true;
      state.mode = "ready";
      layout();
      seek(segStart(), "prime");
      render();
    });
    v.addEventListener("loadeddata", function onFirstFrame() {
      v.removeEventListener("loadeddata", onFirstFrame);
      // The app shows its own loading state until the first frame is on screen.
      if (EMBED) notifyApp("ready");
    });
    v.addEventListener("ended", finishWalkthrough);
    v.addEventListener("error", function () { fail("This video could not be played."); });
    if (window.ResizeObserver) new ResizeObserver(layout).observe(els.stageWrap);
    window.addEventListener("resize", layout);
  }

  bind();
  load();

  window.__swingViewer = {
    get state() { return state.mode; },
    get screen() { return state.screen; },
    get currentTime() { return Number(els.video.currentTime.toFixed(4)); },
    get checkId() { var c = active(); return c ? c.id : null; },
    get check() { var c = active(); return c ? { id: c.id, title: c.title, value: c.value, detail: c.detail, estimated: c.estimated, anchor: state.anchorNorm || c.anchor, phase: c.phaseLabel, checkpoint: c.checkpoint, verdict: c.verdict ? c.verdict.label : null } : null; },
    get eventTimes() { return events.map(function (e) { return e.time_s; }); },
    get checkIds() { return flat.map(function (c) { return c.id; }); },
    get segment() { return [segStart(), segEnd()]; },
    get stats() { var o = {}; for (var k in state.stats) o[k] = state.stats[k]; o.frameCallbacks = "requestVideoFrameCallback" in HTMLVideoElement.prototype; return o; },
    get overlay() { return { box: state.overlayBox, callout: state.overlayAnchor, primitives: els.overlay.querySelectorAll("path,circle").length, scores: DEV ? state.placementScores : undefined }; },
    get v3() { return OVERLAY_V3 ? { elapsed: Math.round(v3.elapsed), end: Math.round(v3.end), running: v3.running, dwell: Math.round(state.dwellRemaining) } : null; },
    // QA only (v3): show the reveal at an exact time and stop the clock there (deterministic screenshots).
    v3At: function (ms) { if (!OVERLAY_V3) return null; v3Stop(); v3.elapsed = clamp(Number(ms), 0, v3.end); v3Apply(); return v3.elapsed; },
    get error() { return state.error; },
    goToCheck: function (id) { replayCheck(id); }
  };
})();
