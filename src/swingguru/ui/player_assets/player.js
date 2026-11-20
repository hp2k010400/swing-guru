/* Guided swing playback: a self-contained viewer with no dependencies.
 *
 * The real video plays between moments. Just before a moment's frame is
 * presented the video pauses and steps *forward* onto that exact frame (never
 * back), then holds while one measurement overlay and card are shown. Several
 * checks share a moment, so the video does not move between them.
 *
 * All biomechanics live in Python: this file only draws the lines, rings and
 * dots it is handed. It talks to Streamlit only to receive its data and to
 * report an error - never during playback, so the page never reruns while the
 * swing is playing.
 */
(function () {
  "use strict";

  var RENDER_EVENT = "streamlit:render";
  var COMPONENT_READY = "streamlit:componentReady";
  var SET_FRAME_HEIGHT = "streamlit:setFrameHeight";
  var SET_COMPONENT_VALUE = "streamlit:setComponentValue";

  var OVERLAY_PRIMARY = "#5cc583";
  var OVERLAY_REFERENCE = "rgba(255,255,255,0.92)";
  var OVERLAY_HALO = "rgba(8,10,9,0.38)";
  var CALLOUT_FILL = "rgba(28,31,30,0.72)";
  var FADE_OUT_MS = 160;
  var SWITCH_MS = 120;

  var reducedMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var state = {
    runKey: null,
    data: null,
    events: [],
    flat: [],
    holdSeconds: 1.8,
    mode: "ready",
    checkIndex: -1,
    dwelled: [],
    dwellRemaining: 0,
    pendingEvent: 0,
    speed: 1,
    error: "",
    generation: 0,
    awaitingSeek: null,
    lastFrameMs: 0,
    rafHandle: 0,
    frameCallback: 0,
    transitionTimer: 0,
    holdEventIndex: -1,
    renders: 0,
    note: "",
    lastHeight: 0,
    layoutKey: "",
    stats: {
      seeks: 0,
      holdSeeks: 0,
      sameMomentSeeks: 0,
      backwardHoldSeeks: 0,
      maxHoldSeekFrames: 0,
      loadstarts: 0,
      sourceSets: 0,
      streamlitMessages: 0,
      heightPosts: 0,
      overlayBuilds: 0,
      layouts: 0,
      renderLog: [],
      holds: 0
    },
    lastSeek: null,
    lastSeeked: null,
    overlayBox: null,
    overlayAnchor: null
  };

  var els = {};
  var objectUrl = null;

  function $(id) {
    return document.getElementById(id);
  }

  function now() {
    return window.performance ? performance.now() : Date.now();
  }

  /* ------------------------------------------------------------- messages */

  function postMessage(type, payload) {
    var message = { isStreamlitMessage: true, type: type };
    if (payload) {
      for (var key in payload) {
        if (Object.prototype.hasOwnProperty.call(payload, key)) {
          message[key] = payload[key];
        }
      }
    }
    window.parent.postMessage(message, "*");
  }

  function setFrameHeight(force) {
    if (!els.player) {
      return;
    }
    var height = Math.ceil(els.player.getBoundingClientRect().height + 4);
    if (height > 0 && (force || Math.abs(height - state.lastHeight) > 1)) {
      state.lastHeight = height;
      state.stats.heightPosts += 1;
      postMessage(SET_FRAME_HEIGHT, { height: height });
    }
  }

  /* Only failures go back to Python: a value posted to Streamlit reruns the
   * whole page, which is exactly what must not happen during playback. */
  function reportError() {
    state.stats.streamlitMessages += 1;
    postMessage(SET_COMPONENT_VALUE, {
      value: { run_key: state.runKey, error: state.error || null },
      dataType: "json"
    });
  }

  /* -------------------------------------------------------------- helpers */

  function fps() {
    return state.data && state.data.fps > 0 ? Number(state.data.fps) : 30;
  }

  function frameDuration() {
    return 1 / fps();
  }

  /* Seek to the middle of a frame so rounding can never land on its
   * neighbour. */
  function frameTime(timeS) {
    return Number(timeS) + 0.5 * frameDuration();
  }

  function segmentStart() {
    var value = state.data ? Number(state.data.segment_start_s) : 0;
    return isFinite(value) && value > 0 ? value : 0;
  }

  function segmentEnd() {
    var video = els.video;
    var duration = video && isFinite(video.duration) ? video.duration : Infinity;
    var value = state.data ? Number(state.data.segment_end_s) : NaN;
    if (!isFinite(value) || value <= segmentStart()) {
      return duration;
    }
    return Math.min(value, duration);
  }

  function activeCheck() {
    if (state.checkIndex < 0 || state.checkIndex >= state.flat.length) {
      return null;
    }
    return state.flat[state.checkIndex];
  }

  function eventIndexForCheck(check) {
    if (!check) {
      return -1;
    }
    for (var index = 0; index < state.events.length; index += 1) {
      if (state.events[index].checks.indexOf(check) >= 0) {
        return index;
      }
    }
    return -1;
  }

  function firstEventAtOrAfter(time) {
    for (var index = 0; index < state.events.length; index += 1) {
      if (frameTime(state.events[index].time_s) >= time - 0.25 * frameDuration()) {
        return index;
      }
    }
    return state.events.length;
  }

  function firstCheckOfEvent(eventIndex) {
    var event = state.events[eventIndex];
    if (!event || !event.checks.length) {
      return -1;
    }
    return state.flat.indexOf(event.checks[0]);
  }

  function clearTransition() {
    if (state.transitionTimer) {
      window.clearTimeout(state.transitionTimer);
      state.transitionTimer = 0;
    }
  }

  /* ----------------------------------------------------------------- data */

  function flatten(events) {
    var flat = [];
    for (var index = 0; index < events.length; index += 1) {
      for (var position = 0; position < events[index].checks.length; position += 1) {
        flat.push(events[index].checks[position]);
      }
    }
    return flat;
  }

  function applyData(args) {
    if (!args || typeof args !== "object") {
      return;
    }
    // Streamlit hands the component the whole kwargs dict, so the payload the
    // Python side passes as ``data=`` arrives nested; accept both shapes.
    var data =
      args.data && typeof args.data === "object" && args.data.events !== undefined
        ? args.data
        : args;
    if (!data || typeof data !== "object") {
      return;
    }
    var incomingKey = String(data.run_key || "");
    var srcChanged =
      state.data === null || String(state.data.video_src || "") !== String(data.video_src || "");
    if (srcChanged || incomingKey !== state.runKey) {
      resetForRun(data, srcChanged);
    } else {
      // Same run and same video: refresh text only. The media element and the
      // playback position are never touched here.
      state.data = data;
      state.note = String(data.labels && data.labels.note ? data.labels.note : "");
      render();
    }
  }

  function resetForRun(data, srcChanged) {
    state.generation += 1;
    var generation = state.generation;
    clearTransition();
    state.runKey = String(data.run_key || "");
    state.data = data;
    state.events = Array.isArray(data.events) ? data.events : [];
    state.flat = flatten(state.events);
    state.holdSeconds = Number(data.hold_seconds) > 0 ? Number(data.hold_seconds) : 1.8;
    state.mode = "ready";
    state.checkIndex = -1;
    state.dwelled = [];
    state.holdEventIndex = -1;
    state.dwellRemaining = 0;
    state.pendingEvent = 0;
    state.error = "";
    state.awaitingSeek = null;
    state.note = String(data.labels && data.labels.note ? data.labels.note : "");

    var video = els.video;
    if (video) {
      video.pause();
      if (srcChanged) {
        if (data.video_src) {
          loadVideoSource(data.video_src, generation);
        } else {
          video.removeAttribute("src");
        }
        if (data.poster) {
          video.setAttribute("poster", data.poster);
        } else {
          video.removeAttribute("poster");
        }
      } else if (video.readyState >= 1) {
        primeAtSegmentStart();
      }
      applySpeed(video);
    }
    renderTicks();
    layout();
    render();
  }

  /* Streamlit's static file handler does not answer range requests reliably,
   * and a media element cannot seek without them, so the bounded local
   * preview is fetched once and played from an in-memory blob URL. */
  function loadVideoSource(src, generation) {
    var video = els.video;
    if (!video) {
      return;
    }
    revokeObjectUrl();
    function assign(url) {
      state.stats.sourceSets += 1;
      applySpeed(video);
      video.setAttribute("src", url);
      video.load();
    }
    if (/^(data:|blob:|https?:)/i.test(src)) {
      assign(src);
      return;
    }
    try {
      window
        .fetch(src, { cache: "force-cache" })
        .then(function (response) {
          if (!response.ok) {
            throw new Error("HTTP " + response.status);
          }
          return response.blob();
        })
        .then(function (blob) {
          if (generation !== state.generation) {
            return;
          }
          objectUrl = window.URL.createObjectURL(blob);
          assign(objectUrl);
        })
        .catch(function () {
          if (generation === state.generation) {
            assign(src);
          }
        });
    } catch (error) {
      assign(src);
    }
  }

  function revokeObjectUrl() {
    if (objectUrl) {
      try {
        window.URL.revokeObjectURL(objectUrl);
      } catch (error) {
        /* nothing to do */
      }
      objectUrl = null;
    }
  }

  /* --------------------------------------------------------- frame clock */

  /* requestVideoFrameCallback reports the media time of every frame that is
   * actually presented, which is what makes a frame-accurate stop possible.
   * Browsers without it fall back to requestAnimationFrame polling with a
   * look-ahead. */
  function watchFrames() {
    var video = els.video;
    if (!video || state.frameCallback) {
      return;
    }
    if (typeof video.requestVideoFrameCallback === "function") {
      state.frameCallback = video.requestVideoFrameCallback(onVideoFrame);
    }
  }

  function unwatchFrames() {
    var video = els.video;
    if (video && state.frameCallback && typeof video.cancelVideoFrameCallback === "function") {
      video.cancelVideoFrameCallback(state.frameCallback);
    }
    state.frameCallback = 0;
  }

  function onVideoFrame(_now, metadata) {
    state.frameCallback = 0;
    if (state.mode !== "playing") {
      return;
    }
    checkUpcoming(Number(metadata && metadata.mediaTime), false);
    if (state.mode === "playing") {
      watchFrames();
    }
  }

  /* Stop when the *next* presented frame would reach the moment. At that point
   * the moment is at most one frame ahead, so the seek onto it goes forward
   * by a frame and nothing ever jumps back. */
  function checkUpcoming(mediaTime, polling) {
    if (!isFinite(mediaTime)) {
      return;
    }
    var step = frameDuration();
    var lookahead = polling ? step + (0.017 * (els.video.playbackRate || 1)) : step;
    if (mediaTime >= segmentEnd() - 0.5 * step) {
      finishWalkthrough();
      return;
    }
    var index = state.pendingEvent;
    if (index < 0 || index >= state.events.length) {
      return;
    }
    var target = Number(state.events[index].time_s);
    if (mediaTime + lookahead >= target - 0.25 * step) {
      beginHold(index);
    }
  }

  function ensureLoop() {
    if (state.rafHandle) {
      return;
    }
    state.lastFrameMs = now();
    state.rafHandle = window.requestAnimationFrame(tick);
  }

  function stopLoop() {
    if (state.rafHandle) {
      window.cancelAnimationFrame(state.rafHandle);
      state.rafHandle = 0;
    }
    unwatchFrames();
    clearTransition();
  }

  function tick(stamp) {
    state.rafHandle = 0;
    var delta = Math.max(0, stamp - (state.lastFrameMs || stamp));
    state.lastFrameMs = stamp;
    if (state.mode === "holding" && !state.transitionTimer) {
      state.dwellRemaining -= delta;
      if (state.dwellRemaining <= 0) {
        advanceCheck();
      }
    } else if (state.mode === "playing" && !state.frameCallback && els.video) {
      checkUpcoming(Number(els.video.currentTime), true);
    }
    renderProgress();
    if (state.mode === "playing" || state.mode === "holding" || state.mode === "seeking") {
      state.rafHandle = window.requestAnimationFrame(tick);
    }
  }

  /* ------------------------------------------------------------- playback */

  function setCurrentTime(value, kind) {
    var video = els.video;
    if (!video) {
      return;
    }
    var before = Number(video.currentTime);
    state.stats.seeks += 1;
    try {
      video.currentTime = value;
      state.lastSeek = { requested: Number(value), before: before, kind: kind || "" };
    } catch (error) {
      fail("Could not seek in the preview video (" + (error && error.name ? error.name : "unknown") + ")");
    }
  }

  function primeAtSegmentStart() {
    var start = segmentStart();
    if (start > 0 && els.video && Math.abs(els.video.currentTime - start) > frameDuration()) {
      state.awaitingSeek = { kind: "prime" };
      setCurrentTime(start, "prime");
    }
  }

  function beginHold(eventIndex) {
    var event = state.events[eventIndex];
    var video = els.video;
    if (!event || !video) {
      return;
    }
    video.pause();
    unwatchFrames();
    state.stats.holds += 1;
    var target = frameTime(event.time_s);
    var offset = target - Number(video.currentTime);
    if (Math.abs(offset) > 0.25 * frameDuration()) {
      state.stats.holdSeeks += 1;
      if (offset < 0) {
        state.stats.backwardHoldSeeks += 1;
      }
      state.stats.maxHoldSeekFrames = Math.max(
        state.stats.maxHoldSeekFrames,
        Math.abs(offset) / frameDuration()
      );
      state.mode = "seeking";
      state.awaitingSeek = { kind: "hold", eventIndex: eventIndex };
      setCurrentTime(target, "hold");
      render();
      ensureLoop();
      return;
    }
    enterHold(eventIndex);
  }

  function enterHold(eventIndex) {
    state.mode = "holding";
    state.holdEventIndex = eventIndex;
    state.pendingEvent = eventIndex + 1;
    state.checkIndex = firstCheckOfEvent(eventIndex);
    state.dwellRemaining = state.holdSeconds * 1000;
    render({ animate: true });
    ensureLoop();
  }

  function advanceCheck() {
    var check = activeCheck();
    if (!check) {
      finishEvent();
      return;
    }
    if (state.dwelled.indexOf(check.id) < 0) {
      state.dwelled.push(check.id);
    }
    var event = state.events[eventIndexForCheck(check)];
    var position = event ? event.checks.indexOf(check) : -1;
    if (event && position >= 0 && position + 1 < event.checks.length) {
      // Same moment: the video stays exactly where it is; only the overlay
      // and card change.
      var seeksBefore = state.stats.seeks;
      fadeOverlay(SWITCH_MS, function () {
        state.checkIndex = state.flat.indexOf(event.checks[position + 1]);
        state.dwellRemaining = state.holdSeconds * 1000;
        state.stats.sameMomentSeeks += state.stats.seeks - seeksBefore;
        render({ animate: true });
      });
      return;
    }
    finishEvent();
  }

  function finishEvent() {
    fadeOverlay(FADE_OUT_MS, function () {
      state.checkIndex = -1;
      state.dwellRemaining = 0;
      state.mode = "playing";
      state.pendingEvent = Math.max(
        state.pendingEvent,
        firstEventAtOrAfter(Number(els.video.currentTime) + frameDuration())
      );
      render();
      resumeVideo();
      ensureLoop();
    });
  }

  /* Fade the overlay out, then run ``next``; the dwell clock is paused while
   * the transition runs. */
  function fadeOverlay(duration, next) {
    clearTransition();
    if (reducedMotion || !els.overlay.firstChild) {
      next();
      return;
    }
    els.overlay.classList.add("sg-leaving");
    state.transitionTimer = window.setTimeout(function () {
      state.transitionTimer = 0;
      els.overlay.classList.remove("sg-leaving");
      next();
    }, duration);
  }

  function finishWalkthrough() {
    var video = els.video;
    video.pause();
    unwatchFrames();
    state.mode = "ended";
    state.checkIndex = -1;
    render();
  }

  function resumeVideo() {
    var video = els.video;
    if (!video) {
      return;
    }
    watchFrames();
    var promise = video.play();
    if (promise && typeof promise.catch === "function") {
      promise.catch(function (error) {
        if (error && error.name === "AbortError") {
          window.setTimeout(function () {
            if (state.mode === "playing") {
              resumeVideo();
            }
          }, 80);
          return;
        }
        fail("Playback could not start (" + (error && error.name ? error.name : "unknown") + ")");
      });
    }
  }

  function play() {
    var video = els.video;
    if (!video) {
      return;
    }
    clearError();
    if (state.mode === "paused" && state.checkIndex >= 0) {
      var check = activeCheck();
      if (check && Math.abs(video.currentTime - frameTime(check.time_s)) > 0.5 * frameDuration()) {
        state.mode = "seeking";
        state.awaitingSeek = { kind: "resume-check", checkIndex: state.checkIndex };
        setCurrentTime(frameTime(check.time_s), "resume");
        render();
        ensureLoop();
        return;
      }
      state.mode = "holding";
      if (state.dwellRemaining <= 0) {
        state.dwellRemaining = state.holdSeconds * 1000;
      }
      render();
      ensureLoop();
      return;
    }
    var time = Number(video.currentTime);
    if (
      state.mode === "ended" ||
      time < segmentStart() - frameDuration() ||
      time >= segmentEnd() - frameDuration()
    ) {
      replay();
      return;
    }
    state.mode = "playing";
    state.checkIndex = -1;
    state.pendingEvent = firstEventAtOrAfter(time);
    render();
    resumeVideo();
    ensureLoop();
  }

  function pause() {
    var video = els.video;
    if (!video) {
      return;
    }
    if (state.mode === "holding") {
      state.mode = "paused";
    } else if (state.mode === "playing") {
      video.pause();
      unwatchFrames();
      state.mode = "paused";
    } else {
      return;
    }
    render();
  }

  function togglePlay() {
    if (state.mode === "playing" || state.mode === "holding") {
      pause();
    } else {
      play();
    }
  }

  function goToCheck(delta) {
    if (!state.flat.length || !els.video) {
      return;
    }
    clearTransition();
    els.video.pause();
    unwatchFrames();
    var target = state.checkIndex;
    if (target < 0) {
      target = delta >= 0 ? firstCheckOfEvent(Math.min(state.pendingEvent, state.events.length - 1)) : state.flat.length - 1;
      if (target < 0) {
        target = 0;
      }
    } else {
      target = Math.min(state.flat.length - 1, Math.max(0, target + delta));
    }
    var check = state.flat[target];
    var destination = frameTime(check.time_s);
    if (Math.abs(els.video.currentTime - destination) <= 0.25 * frameDuration()) {
      state.checkIndex = target;
      state.mode = "paused";
      state.dwellRemaining = state.holdSeconds * 1000;
      state.pendingEvent = eventIndexForCheck(check);
      render({ animate: true });
      return;
    }
    state.mode = "seeking";
    state.checkIndex = -1;
    state.awaitingSeek = { kind: "check", checkIndex: target };
    setCurrentTime(destination, "check");
    render();
    ensureLoop();
  }

  function replay() {
    clearTransition();
    state.dwelled = [];
    state.checkIndex = -1;
    state.dwellRemaining = 0;
    state.pendingEvent = 0;
    state.error = "";
    clearError();
    els.video.pause();
    unwatchFrames();
    var start = segmentStart();
    if (Math.abs(Number(els.video.currentTime) - start) <= 0.5 * frameDuration()) {
      state.mode = "playing";
      render();
      resumeVideo();
      ensureLoop();
      return;
    }
    state.mode = "seeking";
    state.awaitingSeek = { kind: "replay" };
    setCurrentTime(start, "replay");
    render();
    ensureLoop();
  }

  function onSeekInput() {
    var video = els.video;
    if (!video || !state.data) {
      return;
    }
    var start = segmentStart();
    var end = segmentEnd();
    if (!isFinite(end) || end <= start) {
      return;
    }
    clearTransition();
    video.pause();
    unwatchFrames();
    var fraction = Number(els.seek.value) / 1000;
    state.mode = "seeking";
    state.checkIndex = -1;
    state.dwellRemaining = 0;
    state.awaitingSeek = { kind: "scrub" };
    setCurrentTime(start + fraction * (end - start), "scrub");
    render();
    ensureLoop();
  }

  function onSeeked() {
    state.lastSeeked = els.video ? Number(els.video.currentTime) : null;
    var pending = state.awaitingSeek;
    state.awaitingSeek = null;
    if (!pending) {
      return;
    }
    if (pending.kind === "prime") {
      renderProgress();
      return;
    }
    if (pending.kind === "hold") {
      enterHold(pending.eventIndex);
      return;
    }
    if (pending.kind === "check") {
      state.checkIndex = pending.checkIndex;
      state.dwellRemaining = state.holdSeconds * 1000;
      state.mode = "paused";
      state.pendingEvent = eventIndexForCheck(activeCheck());
      render({ animate: true });
      return;
    }
    if (pending.kind === "resume-check") {
      state.checkIndex = pending.checkIndex;
      if (state.dwellRemaining <= 0) {
        state.dwellRemaining = state.holdSeconds * 1000;
      }
      state.mode = "holding";
      render({ animate: true });
      ensureLoop();
      return;
    }
    if (pending.kind === "replay") {
      state.mode = "playing";
      state.pendingEvent = 0;
      render();
      resumeVideo();
      ensureLoop();
      return;
    }
    state.pendingEvent = firstEventAtOrAfter(Number(els.video.currentTime));
    state.mode = "paused";
    render();
  }

  function fail(message) {
    state.error = message;
    state.mode = "error";
    render();
    reportError();
  }

  function clearError() {
    state.error = "";
    if (els.error) {
      els.error.hidden = true;
      els.error.textContent = "";
    }
  }

  function setSpeed(speed) {
    state.speed = speed;
    applySpeed(els.video);
    var buttons = document.querySelectorAll(".sg-speed");
    for (var index = 0; index < buttons.length; index += 1) {
      var value = Number(buttons[index].getAttribute("data-speed"));
      buttons[index].setAttribute("aria-pressed", value === speed ? "true" : "false");
    }
  }

  /* ``load()`` resets ``playbackRate`` to ``defaultPlaybackRate``, so both are
   * kept in step or a chosen speed would be lost when a preview loads. */
  function applySpeed(video) {
    if (!video) {
      return;
    }
    try {
      video.defaultPlaybackRate = state.speed;
      video.playbackRate = state.speed;
    } catch (error) {
      /* nothing to do */
    }
  }

  function onEnded() {
    finishWalkthrough();
  }

  function onVideoError() {
    var video = els.video;
    var code = video && video.error ? video.error.code : 0;
    fail(
      "The preview video could not be played (media error " +
        code +
        "). Use the stills below or rebuild the preview."
    );
  }

  /* --------------------------------------------------------------- layout */

  function viewportHeight() {
    try {
      if (window.parent && window.parent !== window && window.parent.innerHeight) {
        return window.parent.innerHeight;
      }
    } catch (error) {
      /* cross-origin parent: fall through */
    }
    return window.screen && window.screen.availHeight ? window.screen.availHeight * 0.85 : 800;
  }

  function videoAspect() {
    var video = els.video;
    if (video && video.videoWidth > 0 && video.videoHeight > 0) {
      return video.videoWidth / video.videoHeight;
    }
    var data = state.data;
    if (data && data.width > 0 && data.height > 0) {
      return data.width / data.height;
    }
    return 9 / 16;
  }

  /* Size the stage to the video's own aspect and to the viewport height, so a
   * portrait swing is a phone-shaped hero on a desktop and never needs
   * scrolling to reach the card. */
  function layout() {
    if (!els.player || !els.column) {
      return;
    }
    var available = els.player.clientWidth || 360;
    var aspect = videoAspect();
    var maxHeight = Math.max(320, Math.min(860, viewportHeight() * 0.7));
    var width = Math.min(available, maxHeight * aspect);
    var sideWidth = 340;
    var gap = 28;
    var wide = available >= 640 && available >= width + gap + sideWidth;
    els.player.classList.toggle("sg-wide", wide);
    var columnWidth = wide
      ? Math.min(available, width + gap + Math.min(400, available - width - gap))
      : Math.min(available, Math.max(width, Math.min(available, 360)));
    if (!wide) {
      // Stacked: leave room under the video for the controls and the card.
      var stackedHeight = Math.max(300, Math.min(860, viewportHeight() * 0.62));
      width = Math.min(available, stackedHeight * aspect);
      columnWidth = Math.min(available, Math.max(width, Math.min(available, 360)));
    }
    els.column.style.width = Math.round(columnWidth) + "px";
    els.stage.style.width = Math.round(width) + "px";
    els.stage.style.aspectRatio = String(aspect);
    state.stats.layouts += 1;
    // Rebuild the overlay only when the picture actually changed size: the
    // iframe resizing itself must not restart the overlay over and over.
    var box = contentBox();
    var key = box ? [box.x, box.y, box.width, box.height].map(Math.round).join("|") : "";
    if (key !== state.layoutKey) {
      state.layoutKey = key;
      renderOverlay(activeCheck(), false);
    }
    setFrameHeight();
  }

  /* Where the picture actually is inside the element (object-fit: contain may
   * letterbox it), so overlay coordinates always land on the body. */
  function contentBox() {
    var video = els.video;
    var boxWidth = video.clientWidth || 0;
    var boxHeight = video.clientHeight || 0;
    var sourceWidth = video.videoWidth || (state.data && state.data.width) || boxWidth;
    var sourceHeight = video.videoHeight || (state.data && state.data.height) || boxHeight;
    if (!boxWidth || !boxHeight || !sourceWidth || !sourceHeight) {
      return null;
    }
    var scale = Math.min(boxWidth / sourceWidth, boxHeight / sourceHeight);
    var width = sourceWidth * scale;
    var height = sourceHeight * scale;
    return {
      x: (boxWidth - width) / 2,
      y: (boxHeight - height) / 2,
      width: width,
      height: height,
      boxWidth: boxWidth,
      boxHeight: boxHeight
    };
  }

  /* --------------------------------------------------------------- render */

  function render(options) {
    if (!els.player) {
      return;
    }
    var animate = Boolean(options && options.animate);
    els.player.setAttribute("data-state", state.mode);
    els.player.setAttribute("data-check-index", String(state.checkIndex));
    var check = activeCheck();
    els.player.setAttribute("data-check-id", check ? check.id : "");

    renderBadge(check);
    renderCard(check);
    renderOverlay(check, animate);
    renderTicks();

    els.play.setAttribute(
      "aria-label",
      state.mode === "playing" || state.mode === "holding"
        ? "Pause the guided walkthrough"
        : "Play the guided walkthrough"
    );
    els.prev.disabled = !state.flat.length;
    els.next.disabled = !state.flat.length;
    els.note.textContent = state.note;
    els.note.hidden = !state.note;
    if (state.error) {
      els.error.hidden = false;
      els.error.textContent = state.error;
    } else {
      els.error.hidden = true;
    }
    renderProgress();
    setFrameHeight();
  }

  function phaseLabel(phase) {
    var names = {
      address: "Address",
      takeaway: "Takeaway",
      mid_back: "Mid-Back",
      top: "Top",
      downswing: "Mid-Down",
      impact: "Estimated impact",
      finish: "Finish"
    };
    return names[phase] || "Swing moment";
  }

  function renderBadge(check) {
    var show = check && (state.mode === "holding" || state.mode === "paused");
    els.badge.hidden = !show;
    els.badge.textContent = show ? phaseLabel(check.phase) : "";
    els.badge.setAttribute("data-kind", show ? "estimated" : state.mode);
  }

  function renderCard(check) {
    if (!check) {
      var moments = state.events.length;
      var checks = state.flat.length;
      els.phase.textContent =
        state.mode === "ended" ? "Walkthrough finished" : state.mode === "playing" ? "Playing" : "Swing walkthrough";
      els.label.textContent = moments
        ? moments + " moments · " + checks + " checks"
        : "No moments to hold";
      els.value.textContent = "";
      els.value.classList.remove("sg-review");
      els.against.textContent =
        state.mode === "ended"
          ? "Replay to watch again, or step through the checks."
          : "The video pauses at each estimated moment.";
      els.provenance.textContent = "";
      els.tracking.hidden = true;
      els.reason.hidden = true;
      els.dots.innerHTML = "";
      return;
    }
    els.phase.textContent = phaseLabel(check.phase);
    els.label.textContent = check.label;
    els.against.textContent = check.measured_against || "";
    var provenance = check.moment_source === "manual" ? "Selected manually" : "Estimated moment";
    els.provenance.textContent = provenance;
    if (check.valid) {
      els.value.classList.remove("sg-review");
      els.value.innerHTML =
        escapeHtml(check.value_text) +
        (check.unit ? "<small>" + escapeHtml(shortUnit(check.unit)) + "</small>" : "");
      els.reason.hidden = true;
      els.reason.textContent = "";
    } else {
      els.value.classList.add("sg-review");
      els.value.textContent = "Can't measure";
      els.reason.hidden = !check.reason;
      els.reason.textContent = check.reason || "";
    }
    var level = check.valid ? check.tracking || "unknown" : "unmeasured";
    var labels = {
      clear: "Tracking clear",
      uncertain: "Tracking uncertain",
      unmeasured: "Needs review",
      unknown: ""
    };
    els.tracking.hidden = !labels[level];
    els.tracking.textContent = labels[level] || "";
    els.tracking.setAttribute("data-level", level);
    renderDots(check);
  }

  function shortUnit(unit) {
    if (unit === "% of frame height") {
      return "% of height";
    }
    return unit;
  }

  function renderDots(check) {
    var event = state.events[eventIndexForCheck(check)];
    els.dots.innerHTML = "";
    if (!event) {
      return;
    }
    var total = event.checks.length;
    var active = event.checks.indexOf(check);
    for (var index = 0; index < total; index += 1) {
      var dot = document.createElement("span");
      dot.className = "sg-dot";
      if (index < active) {
        dot.setAttribute("data-state", "done");
      } else if (index === active) {
        dot.setAttribute("data-state", "active");
      }
      els.dots.appendChild(dot);
    }
    var counter = document.createElement("span");
    counter.className = "sg-counter";
    counter.textContent = "Check " + (active + 1) + " of " + total;
    els.dots.appendChild(counter);
  }

  function renderTicks() {
    if (!els.ticks) {
      return;
    }
    var start = segmentStart();
    var end = segmentEnd();
    var span = end - start;
    var active = activeCheck();
    var activeEvent = eventIndexForCheck(active);
    var key = [start, end, state.events.length, activeEvent].join("|");
    if (els.ticks.getAttribute("data-key") === key) {
      return;
    }
    els.ticks.setAttribute("data-key", key);
    els.ticks.innerHTML = "";
    if (!isFinite(span) || span <= 0) {
      return;
    }
    for (var index = 0; index < state.events.length; index += 1) {
      var fraction = (frameTime(state.events[index].time_s) - start) / span;
      if (fraction < 0 || fraction > 1) {
        continue;
      }
      var tick = document.createElement("span");
      tick.className = "sg-tick";
      tick.style.left = (fraction * 100).toFixed(2) + "%";
      tick.setAttribute("data-active", index === activeEvent ? "true" : "false");
      els.ticks.appendChild(tick);
    }
  }

  function renderProgress() {
    var video = els.video;
    if (!video || !els.progress) {
      return;
    }
    var start = segmentStart();
    var end = segmentEnd();
    var fraction = 0;
    if (isFinite(end) && end > start) {
      fraction = Math.min(1, Math.max(0, (video.currentTime - start) / (end - start)));
    }
    els.progress.style.width = (fraction * 100).toFixed(2) + "%";
    if (document.activeElement !== els.seek) {
      els.seek.value = String(Math.round(fraction * 1000));
      els.seek.setAttribute("aria-valuetext", video.currentTime.toFixed(2) + " seconds");
    }
    els.player.setAttribute("data-current-time", Number(video.currentTime).toFixed(4));
  }

  /* -------------------------------------------------------------- overlay */

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svg(tag, attributes, parent) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var key in attributes) {
      if (Object.prototype.hasOwnProperty.call(attributes, key)) {
        node.setAttribute(key, String(attributes[key]));
      }
    }
    (parent || els.overlay).appendChild(node);
    return node;
  }

  function pathFor(points) {
    return points
      .map(function (point, index) {
        return (index ? "L" : "M") + point[0].toFixed(1) + " " + point[1].toFixed(1);
      })
      .join(" ");
  }

  function renderOverlay(check, animate) {
    var overlay = els.overlay;
    var video = els.video;
    if (!overlay || !video) {
      return;
    }
    while (overlay.firstChild) {
      overlay.removeChild(overlay.firstChild);
    }
    overlay.classList.remove("sg-in");
    state.overlayBox = null;
    state.overlayAnchor = null;
    state.stats.overlayBuilds += 1;
    var animated = Boolean(animate) && !reducedMotion;
    // Transition classes only when animating; a rebuild after a resize is
    // drawn straight in its final state.
    var fadeQuick = animated ? "sg-fade-quick" : "";
    var box = contentBox();
    if (!box) {
      return;
    }
    overlay.setAttribute("viewBox", "0 0 " + box.boxWidth + " " + box.boxHeight);
    state.overlayBox = box;
    if (!check || !check.valid || !check.geometry || !check.geometry.length) {
      return;
    }

    function toPx(point) {
      return [box.x + point[0] * box.width, box.y + point[1] * box.height];
    }

    var stroke = Math.max(3, Math.min(6, box.width * 0.009));
    var referenceStroke = Math.max(2, stroke * 0.62);
    var dotRadius = Math.max(3.5, stroke * 1.05);
    var drawn = [];
    var haloGroup = svg("g", { opacity: "0.9" });
    var shapeGroup = svg("g", {});

    for (var index = 0; index < check.geometry.length; index += 1) {
      var item = check.geometry[index];
      var points = (item.points || []).map(toPx);
      if (!points.length) {
        continue;
      }
      var reference = item.role === "reference";
      var colour = reference ? OVERLAY_REFERENCE : OVERLAY_PRIMARY;
      var width = reference ? referenceStroke : stroke;
      if ((item.kind === "line" || item.kind === "polyline") && points.length >= 2) {
        var d = pathFor(points);
        svg("path", {
          d: d,
          fill: "none",
          stroke: OVERLAY_HALO,
          "stroke-width": width + 3,
          "stroke-linecap": "round",
          "stroke-linejoin": "round"
        }, haloGroup);
        var path = svg("path", {
          d: d,
          fill: "none",
          stroke: colour,
          "stroke-width": width,
          "stroke-linecap": "round",
          "stroke-linejoin": "round"
        }, shapeGroup);
        if (reference) {
          path.setAttribute("stroke-dasharray", (width * 2.6).toFixed(1) + " " + (width * 2.4).toFixed(1));
          path.setAttribute("class", fadeQuick);
        } else {
          drawn.push(path);
        }
      } else if (item.kind === "ring" && item.radius) {
        var centre = points[0];
        var radius = Math.max(6, Number(item.radius) * box.height);
        svg("circle", {
          cx: centre[0],
          cy: centre[1],
          r: radius,
          fill: "none",
          stroke: OVERLAY_HALO,
          "stroke-width": width + 3
        }, haloGroup);
        var ring = svg("circle", {
          cx: centre[0],
          cy: centre[1],
          r: radius,
          fill: reference ? "none" : "rgba(92,197,131,0.14)",
          stroke: colour,
          "stroke-width": width
        }, shapeGroup);
        if (reference) {
          ring.setAttribute("stroke-dasharray", (width * 2.6).toFixed(1) + " " + (width * 2.4).toFixed(1));
          ring.setAttribute("class", fadeQuick);
        } else {
          drawn.push(ring);
        }
      } else if (item.kind === "dot") {
        var hollow = item.filled === false;
        svg("circle", {
          cx: points[0][0],
          cy: points[0][1],
          r: (hollow ? dotRadius * 0.8 : dotRadius) + 1.5,
          fill: OVERLAY_HALO
        }, haloGroup);
        svg("circle", {
          cx: points[0][0],
          cy: points[0][1],
          r: hollow ? dotRadius * 0.8 : dotRadius,
          fill: hollow ? "rgba(15,18,17,0.35)" : colour,
          stroke: hollow ? OVERLAY_REFERENCE : "#ffffff",
          "stroke-width": hollow ? 1.8 : 1.2,
          "class": fadeQuick
        }, shapeGroup);
      }
    }

    if (check.anchor) {
      drawCallout(toPx(check.anchor), check.callout || check.label, box, stroke, animated);
    }

    for (var position = 0; position < drawn.length; position += 1) {
      var shape = drawn[position];
      var length = 0;
      try {
        length = shape.getTotalLength();
      } catch (error) {
        length = 0;
      }
      if (animated && length > 0) {
        shape.style.strokeDasharray = length.toFixed(1);
        shape.style.strokeDashoffset = length.toFixed(1);
        shape.setAttribute("class", "sg-draw");
      }
    }
    if (animated) {
      // Commit the start state, then let CSS transitions draw the lines in.
      void overlay.getBoundingClientRect();
      window.requestAnimationFrame(function () {
        for (var index = 0; index < drawn.length; index += 1) {
          drawn[index].style.strokeDashoffset = "0";
        }
        overlay.classList.add("sg-in");
      });
    } else {
      overlay.classList.add("sg-in");
    }
  }

  /* An anchor dot on the measured geometry, a thin leader, and a pill beside
   * the body that never sits on top of the anchor. */
  function drawCallout(anchor, text, box, stroke, animated) {
    var group = svg("g", { "class": animated ? "sg-fade" : "" });
    var fontSize = Math.max(12, Math.min(16, box.width * 0.034));
    var label = svg("text", {
      "class": "sg-callout-text",
      "font-size": fontSize.toFixed(1),
      "dominant-baseline": "central"
    }, group);
    label.textContent = text;
    var textWidth = 0;
    try {
      textWidth = label.getComputedTextLength();
    } catch (error) {
      textWidth = text.length * fontSize * 0.55;
    }
    if (!textWidth) {
      textWidth = text.length * fontSize * 0.55;
    }
    var padX = fontSize * 0.85;
    var pillWidth = textWidth + padX * 2;
    var pillHeight = fontSize * 2.1;
    var margin = 10;
    var minX = box.x + margin;
    var maxX = box.x + box.width - margin - pillWidth;
    var toRight = anchor[0] < box.x + box.width * 0.5;
    var gap = Math.max(26, box.width * 0.1);
    var pillX = toRight ? anchor[0] + gap : anchor[0] - gap - pillWidth;
    pillX = Math.max(minX, Math.min(maxX, pillX));
    var pillY = anchor[1] - pillHeight - Math.max(14, box.height * 0.03);
    // Keep clear of the phase pill at the top and the scrubber at the bottom.
    pillY = Math.max(box.y + 48, Math.min(box.y + box.height - pillHeight - 52, pillY));
    var pillCentreY = pillY + pillHeight / 2;
    var edgeX = toRight ? pillX : pillX + pillWidth;
    if (edgeX < minX || (toRight && pillX < anchor[0]) || (!toRight && pillX + pillWidth > anchor[0])) {
      edgeX = pillX + pillWidth / 2;
    }
    var elbowX = anchor[0] + (toRight ? 1 : -1) * Math.min(18, Math.abs(edgeX - anchor[0]) * 0.4);

    svg("path", {
      d: pathFor([anchor, [elbowX, anchor[1]], [edgeX, pillCentreY]]),
      fill: "none",
      stroke: OVERLAY_HALO,
      "stroke-width": 4,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    }, group);
    svg("path", {
      d: pathFor([anchor, [elbowX, anchor[1]], [edgeX, pillCentreY]]),
      fill: "none",
      stroke: "rgba(255,255,255,0.9)",
      "stroke-width": 1.6,
      "stroke-linecap": "round",
      "stroke-linejoin": "round"
    }, group);
    var pill = svg("rect", {
      x: pillX,
      y: pillY,
      width: pillWidth,
      height: pillHeight,
      rx: pillHeight / 2,
      fill: CALLOUT_FILL
    }, group);
    group.insertBefore(pill, label);
    label.setAttribute("x", (pillX + padX).toFixed(1));
    label.setAttribute("y", pillCentreY.toFixed(1));
    svg("circle", {
      cx: anchor[0],
      cy: anchor[1],
      r: Math.max(4, stroke * 1.1),
      fill: "#ffffff",
      stroke: OVERLAY_HALO,
      "stroke-width": 1.5
    }, group);
    state.overlayAnchor = {
      anchor: [anchor[0], anchor[1]],
      pill: [pillX, pillY, pillWidth, pillHeight]
    };
  }

  function escapeHtml(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  /* ---------------------------------------------------------------- setup */

  function bind() {
    els.player = $("sg-player");
    els.column = $("sg-column");
    els.stage = $("sg-stage");
    els.video = $("sg-video");
    els.overlay = $("sg-overlay");
    els.badge = $("sg-badge");
    els.progress = $("sg-progress");
    els.ticks = $("sg-ticks");
    els.card = $("sg-card");
    els.phase = $("sg-phase");
    els.provenance = $("sg-provenance");
    els.tracking = $("sg-tracking");
    els.label = $("sg-label");
    els.value = $("sg-value");
    els.against = $("sg-against");
    els.reason = $("sg-reason");
    els.dots = $("sg-dots");
    els.play = $("sg-play");
    els.prev = $("sg-prev");
    els.next = $("sg-next");
    els.replay = $("sg-replay");
    els.seek = $("sg-seek");
    els.note = $("sg-note");
    els.error = $("sg-error");

    els.play.addEventListener("click", togglePlay);
    els.prev.addEventListener("click", function () {
      goToCheck(-1);
    });
    els.next.addEventListener("click", function () {
      goToCheck(1);
    });
    els.replay.addEventListener("click", replay);
    els.seek.addEventListener("input", onSeekInput);
    var speeds = document.querySelectorAll(".sg-speed");
    for (var index = 0; index < speeds.length; index += 1) {
      speeds[index].addEventListener("click", function () {
        setSpeed(Number(this.getAttribute("data-speed")));
      });
    }
    document.addEventListener("keydown", function (event) {
      if (event.target && event.target.tagName === "INPUT") {
        return;
      }
      if (event.key === " " || event.key === "k") {
        event.preventDefault();
        togglePlay();
      } else if (event.key === "ArrowRight") {
        goToCheck(1);
      } else if (event.key === "ArrowLeft") {
        goToCheck(-1);
      }
    });
    els.video.addEventListener("seeked", onSeeked);
    els.video.addEventListener("ended", onEnded);
    els.video.addEventListener("error", onVideoError);
    els.video.addEventListener("loadstart", function () {
      state.stats.loadstarts += 1;
    });
    els.video.addEventListener("loadedmetadata", function () {
      if (state.mode === "error") {
        state.mode = "ready";
      }
      layout();
      primeAtSegmentStart();
      render();
    });
    if (window.ResizeObserver) {
      var observer = new ResizeObserver(function () {
        layout();
      });
      observer.observe(els.player);
    }
    window.addEventListener("resize", layout);
    try {
      window.parent.addEventListener("resize", layout);
    } catch (error) {
      /* cross-origin parent */
    }
    window.addEventListener("pagehide", function () {
      stopLoop();
      revokeObjectUrl();
    });
  }

  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type !== RENDER_EVENT) {
      return;
    }
    state.renders += 1;
    if (state.stats.renderLog.length < 40) {
      state.stats.renderLog.push({ mode: state.mode, height: state.lastHeight, t: Math.round(now()) });
    }
    applyData(data.args || {});
    setFrameHeight();
  });

  bind();
  postMessage(COMPONENT_READY, { apiVersion: 1 });
  layout();
  render();

  /* Test hooks: the browser QA reads these instead of scraping layout. */
  window.__swingPlayer = {
    get state() {
      return state.mode;
    },
    get currentTime() {
      return els.video ? Number(els.video.currentTime.toFixed(4)) : 0;
    },
    get checkIndex() {
      return state.checkIndex;
    },
    get checkId() {
      var check = activeCheck();
      return check ? check.id : null;
    },
    get dwelled() {
      return state.dwelled.slice();
    },
    get eventTimes() {
      return state.events.map(function (event) {
        return event.time_s;
      });
    },
    get segment() {
      return [segmentStart(), segmentEnd()];
    },
    get speed() {
      return state.speed;
    },
    get error() {
      return state.error;
    },
    get renders() {
      return state.renders;
    },
    get stats() {
      var copy = {};
      for (var key in state.stats) {
        copy[key] = state.stats[key];
      }
      copy.frameCallbacks = typeof HTMLVideoElement !== "undefined" &&
        "requestVideoFrameCallback" in HTMLVideoElement.prototype;
      return copy;
    },
    get overlay() {
      var check = activeCheck();
      return {
        box: state.overlayBox,
        callout: state.overlayAnchor,
        expectedAnchor: check && check.anchor ? check.anchor : null,
        primitives: els.overlay ? els.overlay.querySelectorAll("path, circle").length : 0
      };
    },
    get videoSrc() {
      return els.video ? els.video.getAttribute("src") : null;
    },
    get readyState() {
      return els.video ? els.video.readyState : null;
    },
    get lastSeek() {
      return state.lastSeek;
    },
    get lastSeeked() {
      return state.lastSeeked;
    },
    get seekable() {
      if (!els.video) {
        return null;
      }
      var ranges = els.video.seekable;
      if (!ranges || ranges.length === 0) {
        return { length: 0, end: null };
      }
      return { length: ranges.length, end: ranges.end(0) };
    }
  };
})();
