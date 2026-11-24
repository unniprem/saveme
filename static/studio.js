// SaveMe Clip Studio: preview a video, mark a start and an end, and export that part.
// Plain browser JavaScript, no dependencies; shared helpers come from common.js (window.SaveMe).
// The CSP allows only same-origin scripts and styles: styling is Tailwind classes in index.html,
// positions are set through element.style, and text is set with textContent, never as HTML.
//
// A studio holds one source: a video kept on the server while the studio is open, downloaded
// from a link (openLink) or uploaded from this computer (openFile). Its states:
//   closed → loading (upload | queued | downloading | converting | checking) → ready → closed
//   loading or ready → gone (failed, removed after a while unused, or the server restarted)
// While ready, the current marks can be added to a clip list, and one export runs at a time: the
// current marks as one clip (a clip job), or the whole list as one ZIP (a batch job). Either is
// saved to the device like a download. sessionStorage keeps the source, the marks and the list,
// so a reload comes back to the studio; an upload still in progress can't survive a reload.
// app.js owns the page around the studio and calls in through window.SaveMeStudio.
"use strict";

(() => {
  const { JOB_ID_RE, isNum, str, fmtMB, setText, show, setLine, errorFrom, api } = window.SaveMe;
  const STORE_KEY = "saveme.studio";
  const POLL_MS = 1000;
  const RETRY_MS = 2000;
  const MAX_POLL_FAILURES = 30;
  // While the tab is visible, an open studio keeps its source alive by polling it: a quarter of
  // the server's idle time (idle_minutes), between 15 s and 60 s.
  const KEEPALIVE_MS = { min: 15000, max: 60000 };
  const DELIVERY_WAIT_MS = 60000;
  const DEFAULT_LENGTH = 60; // seconds selected when a video opens
  const MIN_CLIP = 0.1;
  const STEPS = { small: 0.1, large: 1, page: 10 };
  const LOADING = ["uploading", "queued", "downloading", "converting", "checking"];
  const EXPORTING = ["queued", "cutting"];
  const MAX_LIST = 20; // the server's max_batch_clips, until /api/studio says otherwise

  const MSG = {
    saved: "Clip saved. Mark another part, or close the studio.", // announced; shown as headline + next
    next: "Mark another part, or close the studio.",
    ready: "Video ready. Mark the start and end of your clip.",
    badTime: "Type a time like 1:05 or 65.5.",
    startAfterEnd: "The start must be before the end.",
    endBeforeStart: "The end must be after the start.",
    tooShort: `A clip must be at least ${MIN_CLIP} seconds long.`,
    uploadStopped: "The upload stopped. Check that SaveMe is running, then try again.",
    emptyFile: "This file is empty.",
    gone: "This video is no longer on the server. Close the studio and load it again.",
    idle: "This video was removed from the server after a while unused. Close the studio and load it again.",
    replaced: "This video was closed to make room for another one. Close the studio and load it again.",
    offline: "Can't reach SaveMe. Check that it's running, then reload this page.",
    exportLost: "This clip couldn't be found on the server. Export it again.",
    exportExpired: "This clip expired before it was saved. Export it again.",
    exportCancelled: "Nothing was kept on the server.",
    zipSaved: "ZIP saved. Mark more parts, or close the studio.",
    zipNext: "Mark more parts, or close the studio.",
    zipExpired: "This ZIP expired before it was saved. Export it again.",
    zipLost: "This ZIP couldn't be found on the server. Export it again.",
    inList: "This part is already in the clip list.",
    listFull: (max) => `The clip list holds up to ${max} clips. Export or remove some first.`,
  };
  const HEADLINE = {
    starting: "Starting…",
    restoring: "Checking on your video…",
    uploading: "Uploading",
    queued: "Waiting to start",
    downloading: "Downloading the video",
    converting: "Converting",
    checking: "Checking the video",
    failed: "Couldn't load this video",
    gone: "Video removed",
    exportQueued: "Waiting to start",
    cutting: "Cutting the clip",
    saving: "Saving to your device…",
    exportReady: "Clip ready",
    exportSaved: "Clip saved",
    exportFailed: "Export failed",
    exportCancelling: "Cancelling…",
    exportCancelled: "Export cancelled",
    exportExpired: "Clip expired",
    zipCutting: "Cutting the clips",
    zipReady: "ZIP ready",
    zipSaved: "ZIP saved",
    zipExpired: "ZIP expired",
  };

  const el = {};
  for (const id of [
    "studio", "studio-title", "studio-close", "studio-loading", "studio-load-spin", "studio-load-fail",
    "studio-load-status", "studio-load-percent", "studio-load-bar", "studio-load-fill", "studio-load-meta",
    "studio-load-error", "studio-load-cancel", "studio-editor", "studio-video", "studio-preview-note",
    "timeline-wrap", "timeline", "timeline-range", "timeline-playhead", "timeline-total", "handle-start",
    "handle-end", "clip-start", "clip-end", "set-start", "set-end", "clip-length", "play-clip", "export-clip",
    "clip-msg", "export", "export-spin", "export-done", "export-fail", "export-status-text", "export-percent",
    "export-bar", "export-fill", "export-meta", "export-save-row", "export-save", "export-error",
    "export-actions", "export-cancel", "add-clip", "clip-list-wrap", "clip-list", "clip-list-count",
    "export-zip", "export-zip-label", "announcer",
  ]) {
    el[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
  }

  let state = "closed"; // closed | loading | ready | gone
  let gen = 0; // bumped when the studio closes, so late replies from the old source are ignored
  let source = null; // { id, title }
  let duration = 0; // the timeline's length in seconds: the server's probe, else the video's own
  const marks = { start: 0, end: 0 };
  let upload = null; // the XMLHttpRequest of an upload in progress
  let limits = null; // GET /api/studio, fetched once
  let loadTimer = 0;
  let loadFailures = 0;
  let keepTimer = 0;
  let previewing = false; // "Play clip" is playing: pause at the end mark
  let playable = true; // false once the browser can't play the source at all (no playhead to use)
  let drag = null; // the handle being dragged: "start" | "end"
  let list = []; // the clip list: { start, end } in whole milliseconds, sorted by start
  let exportJob = null; // { id, autoSaved, kind: "clip" | "zip" }
  let exportTimer = 0;
  let exportFailures = 0;
  let deliverBy = 0;
  let exportFilename = "";
  let closedCallback = () => {};

  // ---- small helpers --------------------------------------------------------------------

  // 0:05.0, 1:05.5, 1:02:05.0: tenths always, hours only when there are any.
  function fmtTime(seconds) {
    const tenths = Math.round(Math.max(0, seconds) * 10);
    const whole = Math.floor(tenths / 10);
    const h = Math.floor(whole / 3600);
    const m = Math.floor((whole % 3600) / 60);
    const pad = (n) => String(n).padStart(2, "0");
    const tail = `${pad(whole % 60)}.${tenths % 10}`;
    return h ? `${h}:${pad(m)}:${tail}` : `${m}:${tail}`;
  }

  // "65.5", "1:05", "1:05.5" or "1:02:05" as seconds; null for anything else. Minutes and
  // seconds after a colon must be under 60.
  function parseTime(text) {
    const t = String(text).trim().replace(",", ".");
    if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(t)) return null;
    let total = 0;
    const parts = t.split(":");
    for (let i = 0; i < parts.length; i += 1) {
      const value = Number(parts[i]);
      if (!Number.isFinite(value) || (i > 0 && value >= 60)) return null;
      total = total * 60 + value;
    }
    return total;
  }

  const ms = (seconds) => Math.round(seconds * 1000) / 1000;
  // At least MIN_CLIP apart, counted in whole milliseconds as the server does (as floats,
  // 1.2 - 1.1 is just under 0.1).
  const longEnough = (start, end) => Math.round(end * 1000) - Math.round(start * 1000) >= Math.round(MIN_CLIP * 1000);
  const announce = (text) => setText(el.announcer, text);

  // A source created for a studio that has since closed: end it on the server too.
  function closeLate(id) {
    api(`/api/sources/${encodeURIComponent(id)}/close`, {}).catch(() => {});
  }

  // Stop the preview and let go of its stream (the server may already have deleted the file).
  function dropVideo() {
    el.studioVideo.pause();
    el.studioVideo.removeAttribute("src");
    el.studioVideo.load();
  }
  const mediaUrl = () => `/api/sources/${encodeURIComponent(source.id)}/media`;

  function setBar(fill, bar, value, text) {
    if (isNum(value)) {
      const pct = Math.min(100, Math.max(0, value));
      delete fill.dataset.indeterminate;
      fill.style.width = `${pct}%`;
      bar.setAttribute("aria-valuenow", String(Math.floor(pct)));
    } else {
      fill.dataset.indeterminate = "";
      fill.style.width = "100%";
      bar.removeAttribute("aria-valuenow");
    }
    bar.setAttribute("aria-valuetext", text);
  }

  // ---- session storage ------------------------------------------------------------------

  function store() {
    if (!source) return;
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        id: source.id, title: source.title, start: marks.start, end: marks.end,
        clips: list.map((c) => [c.start, c.end]),
        exportId: exportJob ? exportJob.id : "", exportAutoSaved: Boolean(exportJob && exportJob.autoSaved),
        exportKind: exportJob ? exportJob.kind : "",
      }));
    } catch {
      // Storage unavailable: the studio works, it just can't come back after a reload.
    }
  }

  function unstore() {
    try {
      sessionStorage.removeItem(STORE_KEY);
    } catch {
      // Nothing to clear.
    }
  }

  function stored() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      if (saved && typeof saved.id === "string" && JOB_ID_RE.test(saved.id)) return saved;
    } catch {
      // Unreadable: dropped below.
    }
    unstore();
    return null;
  }

  // ---- opening and closing --------------------------------------------------------------

  function begin(title) {
    gen += 1;
    state = "loading";
    source = null;
    duration = 0;
    marks.start = 0;
    marks.end = 0;
    list = [];
    loadFailures = 0;
    setText(el.studioTitle, title || "Your video");
    resetExport();
    show(el.studioEditor, false);
    show(el.studio, true);
    el.studio.focus();
  }

  function openLink({ url, quality, title }) {
    begin(title);
    const g = gen;
    paintLoading({ headline: HEADLINE.starting });
    api("/api/sources", { url, quality }).then((created) => {
      if (g !== gen) {
        if (typeof created.source_id === "string" && JOB_ID_RE.test(created.source_id)) closeLate(created.source_id);
        return;
      }
      if (typeof created.source_id !== "string" || !JOB_ID_RE.test(created.source_id)) {
        loadFailed("SaveMe sent an unexpected reply. Please try again.");
        return;
      }
      source = { id: created.source_id, title: title || "" };
      store();
      announce("Loading the video for clipping.");
      pollSource(g);
    }, (err) => {
      if (g === gen) loadFailed(err.message);
    });
  }

  async function openFile(file) {
    const title = file.name.replace(/\.[^.]*$/, "") || file.name;
    begin(title);
    const g = gen;
    paintLoading({ headline: HEADLINE.uploading, bar: 0, meta: `0.0 MB of ${fmtMB(file.size)}` });
    if (file.size === 0) {
      loadFailed(MSG.emptyFile);
      return;
    }
    try {
      limits = limits || await api("/api/studio");
    } catch (err) {
      if (g === gen) loadFailed(err.message);
      return;
    }
    if (g !== gen) return;
    if (isNum(limits.max_upload_bytes) && file.size > limits.max_upload_bytes) {
      loadFailed(`This file is larger than the ${fmtLimit(limits.max_upload_bytes)} upload limit.`);
      return;
    }
    sendFile(file, title, g);
  }

  function fmtLimit(bytes) {
    const mb = Math.floor(bytes / (1024 * 1024));
    return mb >= 1024 ? `${Math.floor(mb / 1024)} GB` : `${mb} MB`;
  }

  function sendFile(file, title, g) {
    const xhr = new XMLHttpRequest();
    upload = xhr;
    xhr.open("POST", "/api/sources/upload");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-SaveMe-Filename", encodeURIComponent(file.name));
    xhr.upload.addEventListener("progress", (event) => {
      if (g !== gen || !event.lengthComputable) return;
      paintLoading({
        headline: HEADLINE.uploading,
        bar: (event.loaded / event.total) * 100,
        meta: `${fmtMB(event.loaded)} of ${fmtMB(event.total)}`,
      });
    });
    xhr.addEventListener("load", () => {
      upload = null;
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        // Not JSON; handled below.
      }
      if (g !== gen) {
        if (data && typeof data.source_id === "string" && JOB_ID_RE.test(data.source_id)) closeLate(data.source_id);
        return;
      }
      if (xhr.status < 200 || xhr.status >= 300) {
        loadFailed(errorFrom(xhr.status, data).message);
        return;
      }
      if (!data || typeof data.source_id !== "string" || !JOB_ID_RE.test(data.source_id)) {
        loadFailed("SaveMe sent an unexpected reply. Please try again.");
        return;
      }
      source = { id: data.source_id, title };
      store();
      paintLoading({ headline: HEADLINE.checking });
      pollSource(g);
    });
    xhr.addEventListener("error", () => {
      upload = null;
      if (g === gen) loadFailed(MSG.uploadStopped);
    });
    xhr.send(file);
    announce("Uploading the video.");
  }

  // Come back to a studio kept in session storage. True when there was one to restore.
  function restore() {
    const saved = stored();
    if (!saved) return false;
    begin(str(saved.title));
    source = { id: saved.id, title: str(saved.title) };
    if (isNum(saved.start) && isNum(saved.end) && saved.end > saved.start) {
      marks.start = saved.start;
      marks.end = saved.end;
    }
    if (Array.isArray(saved.clips)) {
      list = tidyList(saved.clips.filter((c) => Array.isArray(c) && isNum(c[0]) && isNum(c[1]))
        .map((c) => ({ start: c[0], end: c[1] })));
    }
    if (typeof saved.exportId === "string" && JOB_ID_RE.test(saved.exportId)) {
      exportJob = { id: saved.exportId, autoSaved: saved.exportAutoSaved === true,
        kind: saved.exportKind === "zip" ? "zip" : "clip" };
    }
    paintLoading({ headline: HEADLINE.restoring });
    pollSource(gen);
    return true;
  }

  function close() {
    if (state === "closed") return;
    const closing = source;
    gen += 1;
    state = "closed";
    if (upload) upload.abort();
    upload = null;
    clearTimeout(loadTimer);
    clearTimeout(keepTimer);
    resetExport();
    stopPreview();
    dropVideo();
    show(el.studio, false);
    unstore();
    source = null;
    if (closing) api(`/api/sources/${encodeURIComponent(closing.id)}/close`, {}).catch(() => {});
    closedCallback();
  }

  // ---- loading ----------------------------------------------------------------------------

  function paintLoading({ headline, bar = null, percent = "", meta = "", error = "", failed = false }) {
    show(el.studioLoading, true);
    setText(el.studioLoadStatus, headline);
    setText(el.studioLoadPercent, percent || (isNum(bar) ? `${Math.floor(bar)}%` : ""));
    show(el.studioLoadSpin, !failed);
    show(el.studioLoadFail, failed);
    show(el.studioLoadBar, !failed);
    if (!failed) setBar(el.studioLoadFill, el.studioLoadBar, bar, meta || headline);
    setLine(el.studioLoadMeta, meta);
    setLine(el.studioLoadError, error);
    show(el.studioLoadCancel, !failed);
  }

  function loadFailed(message, headline = HEADLINE.failed) {
    state = "gone";
    clearTimeout(loadTimer);
    clearTimeout(keepTimer);
    resetExport();
    stopPreview();
    dropVideo();
    show(el.studioEditor, false);
    unstore();
    paintLoading({ headline, error: message, failed: true });
    announce(`${headline}. ${message}`);
    el.studioClose.focus();
  }

  function gone(reason) {
    const message = reason === "idle" ? MSG.idle : reason === "replaced" ? MSG.replaced : MSG.gone;
    loadFailed(message, HEADLINE.gone);
  }

  async function pollSource(g) {
    if (g !== gen || !source) return;
    let view;
    try {
      view = await api(`/api/sources/${encodeURIComponent(source.id)}`);
    } catch (err) {
      if (g !== gen) return;
      if (err.code === "source_not_found") {
        gone("");
        return;
      }
      if (err.status >= 400 && err.status < 500 && err.status !== 429) {
        loadFailed(err.message);
        return;
      }
      loadFailures += 1;
      if (loadFailures >= MAX_POLL_FAILURES) {
        loadFailed(MSG.offline);
        return;
      }
      loadTimer = setTimeout(() => pollSource(g), RETRY_MS);
      return;
    }
    if (g !== gen) return;
    loadFailures = 0;
    applySource(view, g);
  }

  function applySource(view, g) {
    const status = str(view.status);
    if (str(view.title)) {
      source.title = str(view.title);
      setText(el.studioTitle, source.title);
    }
    if (LOADING.includes(status)) {
      if (state === "ready") {  // a ready source doesn't go back to loading; keep polling slowly
        scheduleKeepAlive(g);
        return;
      }
      const pct = status === "downloading" && isNum(view.progress_percent) ? view.progress_percent : null;
      const done = isNum(view.downloaded_bytes) && view.downloaded_bytes > 0 ? view.downloaded_bytes : 0;
      const total = isNum(view.total_bytes) && view.total_bytes > 0 ? view.total_bytes : 0;
      let meta = str(view.detail);
      if (status === "downloading" && done) meta = total ? `${fmtMB(done)} of ${fmtMB(total)}` : `${fmtMB(done)} downloaded`;
      paintLoading({ headline: HEADLINE[status] || HEADLINE.queued, bar: pct, meta });
      loadTimer = setTimeout(() => pollSource(g), POLL_MS);
    } else if (status === "ready") {
      if (state !== "ready") enterEditor(view);
      else scheduleKeepAlive(g);
    } else if (status === "failed") {
      loadFailed((view.error && str(view.error.message)) || "This video couldn't be loaded. Please try again.");
    } else if (status === "expired") {
      gone(str(view.reason));
    } else {
      loadTimer = setTimeout(() => pollSource(g), POLL_MS);
    }
  }

  // ---- editor -----------------------------------------------------------------------------

  function enterEditor(view) {
    state = "ready";
    duration = isNum(view.duration_seconds) && view.duration_seconds > 0 ? view.duration_seconds : 0;
    if (!(marks.end > marks.start) || (duration && marks.start >= duration)) {
      marks.start = 0;
      marks.end = duration ? Math.min(duration, DEFAULT_LENGTH) : DEFAULT_LENGTH;
    }
    if (duration) marks.end = Math.min(marks.end, duration);
    list = tidyList(list);
    store();
    show(el.studioLoading, false);
    show(el.studioPreviewNote, false);
    playable = true;
    setLine(el.clipMsg, "");
    el.studioVideo.src = mediaUrl();
    show(el.studioEditor, true);
    renderList();
    render();
    announce(MSG.ready);
    scheduleKeepAlive(gen);
    if (!limits) {
      const g = gen;
      api("/api/studio").then((l) => {
        limits = l;
        if (g !== gen) return;
        renderList();
        scheduleKeepAlive(g); // now at the server's pace
      }, () => {});
    }
    if (exportJob) pollExport(gen, exportJob.id);
  }

  function keepAliveMs() {
    const minutes = limits && isNum(limits.idle_minutes) && limits.idle_minutes > 0 ? limits.idle_minutes : 30;
    return Math.min(KEEPALIVE_MS.max, Math.max(KEEPALIVE_MS.min, (minutes * 60000) / 4));
  }

  function scheduleKeepAlive(g) {
    clearTimeout(keepTimer);
    keepTimer = setTimeout(() => {
      if (g !== gen || state !== "ready") return;
      if (document.visibilityState === "visible") pollSource(g);
      else scheduleKeepAlive(g);
    }, keepAliveMs());
  }

  function onVisibility() {
    if (document.visibilityState === "visible" && state === "ready") pollSource(gen);
  }

  // The timeline needs a length: the server's, or the video's once its metadata loads. Every
  // source has a video stream, so no picture size means the browser can't decode it (it may
  // still play the sound, without an error event).
  function onMetadata() {
    if (el.studioVideo.videoWidth === 0) show(el.studioPreviewNote, true);
    const own = el.studioVideo.duration;
    if (!duration && isNum(own) && own > 0) {
      duration = own;
      marks.end = Math.min(marks.end, duration);
      list = tidyList(list); // rows added before the length was known may run past the end
      store();
      renderList();
    }
    render();
  }

  // Nothing plays, so the playhead stays at 0: the controls that use it are turned off. (When
  // only the picture can't be decoded, the sound still plays and moves the playhead.)
  function onVideoError() {
    if (state !== "ready" || !el.studioVideo.getAttribute("src")) return;
    const lost = [el.setStart, el.setEnd, el.playClip].includes(document.activeElement);
    show(el.studioPreviewNote, true);
    playable = false;
    stopPreview();
    render();
    if (lost) el.studioPreviewNote.focus(); // its button is now disabled: focus the reason why
  }

  function render() {
    const known = duration > 0;
    show(el.timelineWrap, known);
    if (known) {
      const at = (t) => `${(Math.min(duration, Math.max(0, t)) / duration) * 100}%`;
      el.timelineRange.style.left = at(marks.start);
      el.timelineRange.style.width = `${((marks.end - marks.start) / duration) * 100}%`;
      el.handleStart.style.left = at(marks.start);
      el.handleEnd.style.left = at(marks.end);
      el.timelinePlayhead.style.left = at(el.studioVideo.currentTime || 0);
      setText(el.timelineTotal, fmtTime(duration));
      for (const [handle, value] of [[el.handleStart, marks.start], [el.handleEnd, marks.end]]) {
        handle.setAttribute("aria-valuemax", String(Math.round(duration * 10) / 10));
        handle.setAttribute("aria-valuenow", String(Math.round(value * 10) / 10));
        handle.setAttribute("aria-valuetext", fmtTime(value));
      }
    }
    if (document.activeElement !== el.clipStart) el.clipStart.value = fmtTime(marks.start);
    if (document.activeElement !== el.clipEnd) el.clipEnd.value = fmtTime(marks.end);
    setText(el.clipLength, fmtTime(marks.end - marks.start));
    el.exportClip.disabled = !longEnough(marks.start, marks.end) || exportRunning();
    el.setStart.disabled = el.setEnd.disabled = el.playClip.disabled = !playable;
    el.exportZip.disabled = list.length === 0 || exportRunning();
  }

  // Move one mark, keeping at least MIN_CLIP between them and both inside the video.
  function setMark(which, seconds) {
    const top = duration || Infinity;
    const t = Math.min(top, Math.max(0, seconds));
    if (which === "start") marks.start = Math.max(0, Math.min(t, marks.end - MIN_CLIP));
    else marks.end = Math.min(top, Math.max(t, marks.start + MIN_CLIP));
    el.clipStart.removeAttribute("aria-invalid");
    el.clipEnd.removeAttribute("aria-invalid");
    setLine(el.clipMsg, "");
    store();
    render();
  }

  function fromField(which) {
    const input = which === "start" ? el.clipStart : el.clipEnd;
    const value = parseTime(input.value);
    let problem = value === null ? MSG.badTime : "";
    if (!problem && which === "start" && !longEnough(value, marks.end)) problem = MSG.startAfterEnd;
    if (!problem && which === "end" && !longEnough(marks.start, value)) problem = MSG.endBeforeStart;
    if (!problem && duration && value > duration) problem = `The video is only ${fmtTime(duration)} long.`;
    if (problem) {
      input.setAttribute("aria-invalid", "true");
      setLine(el.clipMsg, problem, "error");
      announce(problem);
      return;
    }
    setMark(which, value);
    input.value = fmtTime(which === "start" ? marks.start : marks.end);
  }

  function toPlayhead(which) {
    setMark(which, el.studioVideo.currentTime || 0);
    announce(`${which === "start" ? "Start" : "End"} set to ${fmtTime(which === "start" ? marks.start : marks.end)}.`);
  }

  function timeAt(clientX) {
    const box = el.timeline.getBoundingClientRect();
    return (Math.min(box.width, Math.max(0, clientX - box.left)) / box.width) * duration;
  }

  function onTimelineDown(event) {
    if (!duration || event.button > 0) return;
    const handle = event.target === el.handleStart ? "start" : event.target === el.handleEnd ? "end" : null;
    if (handle) {
      drag = handle;
      event.target.setPointerCapture(event.pointerId);
      event.target.focus();
    } else {
      el.studioVideo.currentTime = timeAt(event.clientX);
      render();
    }
    event.preventDefault();
  }

  function onTimelineMove(event) {
    if (drag) setMark(drag, timeAt(event.clientX));
  }

  function onTimelineUp() {
    drag = null;
  }

  function onHandleKey(which, event) {
    const value = which === "start" ? marks.start : marks.end;
    const step = event.shiftKey ? STEPS.large : STEPS.small;
    const moves = {
      ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step,
      PageDown: -STEPS.page, PageUp: STEPS.page,
    };
    if (event.key in moves) setMark(which, value + moves[event.key]);
    else if (event.key === "Home") setMark(which, 0);
    else if (event.key === "End") setMark(which, duration || value);
    else return;
    event.preventDefault();
  }

  // I and O set the start and end at the playhead, unless typing in a field.
  function onKey(event) {
    if (state !== "ready" || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target.isContentEditable) return;
    if (!playable) return;
    const key = event.key.toLowerCase();
    if (key === "i") toPlayhead("start");
    else if (key === "o") toPlayhead("end");
    else return;
    event.preventDefault();
  }

  function playClip() {
    const video = el.studioVideo;
    video.currentTime = marks.start;
    previewing = true;
    video.play().catch(() => {
      previewing = false;
    });
    requestAnimationFrame(watchPreview);
  }

  function watchPreview() {
    if (!previewing) return;
    if (el.studioVideo.currentTime >= marks.end) {
      el.studioVideo.pause();
      previewing = false;
      return;
    }
    requestAnimationFrame(watchPreview);
  }

  function stopPreview() {
    previewing = false;
  }

  // ---- clip list ---------------------------------------------------------------------------

  const maxList = () => (limits && isNum(limits.max_batch_clips) ? limits.max_batch_clips : MAX_LIST);

  // The list as the server will take it: whole milliseconds, ends pulled back to the video's
  // length when it's known, each clip at least MIN_CLIP long, no repeats, sorted by start, and
  // at most maxList() rows. Used on restore and whenever the length becomes known.
  function tidyList(items) {
    const top = duration > 0 ? ms(duration) : Infinity;
    const seen = new Set();
    return items
      .map((c) => ({ start: ms(c.start), end: ms(Math.min(c.end, top)) }))
      .filter((c) => {
        const key = `${c.start}-${c.end}`;
        if (c.start < 0 || !longEnough(c.start, c.end) || seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .sort((a, b) => a.start - b.start || a.end - b.end)
      .slice(0, maxList());
  }
  const rangeText = (c) => `${fmtTime(c.start)} to ${fmtTime(c.end)}`;

  function addToList() {
    if (state !== "ready" || !longEnough(marks.start, marks.end)) return;
    const item = { start: ms(marks.start), end: ms(marks.end) };
    let problem = "";
    if (list.some((c) => c.start === item.start && c.end === item.end)) problem = MSG.inList;
    else if (list.length >= maxList()) problem = MSG.listFull(maxList());
    if (problem) {
      setLine(el.clipMsg, problem, "error");
      announce(problem);
      return;
    }
    list.push(item);
    list.sort((a, b) => a.start - b.start || a.end - b.end);
    setLine(el.clipMsg, "");
    store();
    renderList();
    render();
    const n = list.indexOf(item) + 1;
    announce(`Clip ${n}, ${rangeText(item)}, added. ${list.length} in the list.`);
  }

  function loadFromList(item) {
    marks.start = item.start;
    marks.end = item.end;
    el.clipStart.removeAttribute("aria-invalid");
    el.clipEnd.removeAttribute("aria-invalid");
    setLine(el.clipMsg, "");
    stopPreview();
    if (el.studioVideo.getAttribute("src")) el.studioVideo.currentTime = item.start;
    store();
    render();
    announce(`Clip ${list.indexOf(item) + 1} loaded: ${rangeText(item)}.`);
  }

  function removeFromList(item) {
    const at = list.indexOf(item);
    if (at < 0) return;
    list.splice(at, 1);
    store();
    renderList();
    render();
    announce(`Clip ${at + 1} removed. ${list.length ? `${list.length} in the list.` : "The list is empty."}`);
    // The removed row had focus: move it to the row that took its place, else the one before.
    const rows = el.clipList.querySelectorAll("[data-remove]");
    const next = rows[Math.min(at, rows.length - 1)];
    (next || el.addClip).focus();
  }

  const ROW_BUTTON = "h-10 shrink-0 rounded-lg px-3 text-sm font-semibold focus-visible:outline-2 "
    + "focus-visible:outline-offset-2 focus-visible:outline-brand-700 dark:focus-visible:outline-brand-400";

  function listButton(text, label, extra, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `${ROW_BUTTON} ${extra}`;
    button.textContent = text;
    button.setAttribute("aria-label", label);
    button.addEventListener("click", onClick);
    return button;
  }

  // Rebuild the list's rows (text only, through textContent) and its counts.
  function renderList() {
    const rows = list.map((item, i) => {
      const n = i + 1;
      const row = document.createElement("li");
      row.className = "flex flex-wrap items-center gap-x-3 gap-y-1 py-2";
      const number = document.createElement("span");
      number.className = "w-6 shrink-0 text-sm font-semibold tabular-nums text-ink-600 dark:text-ink-400";
      number.textContent = String(n);
      const range = document.createElement("span");
      range.className = "min-w-0 flex-1 text-sm tabular-nums text-ink-900 dark:text-ink-50";
      const times = document.createElement("span");
      times.className = "whitespace-nowrap";
      times.textContent = `${fmtTime(item.start)} – ${fmtTime(item.end)}`;
      const length = document.createElement("span");
      length.className = "whitespace-nowrap text-ink-600 dark:text-ink-400";
      length.textContent = `· ${fmtTime(item.end - item.start)}`;
      range.append(times, " ", length);
      const actions = document.createElement("span");
      actions.className = "flex gap-2";
      actions.append(
        listButton("Load", `Load clip ${n} into the editor`,
          "bg-brand-50 text-brand-700 hover:bg-brand-100 dark:bg-white/10 dark:text-ink-50 dark:hover:bg-white/15",
          () => loadFromList(item)),
        listButton("Remove", `Remove clip ${n}`,
          "border border-ink-300 text-ink-700 hover:border-red-300 hover:bg-red-50 hover:text-red-700 "
          + "dark:border-ink-700 dark:text-ink-200 dark:hover:border-red-400/40 dark:hover:bg-red-950/40 dark:hover:text-red-300",
          () => removeFromList(item)),
      );
      actions.lastChild.dataset.remove = "";
      row.append(number, range, actions);
      return row;
    });
    el.clipList.replaceChildren(...rows);
    show(el.clipListWrap, list.length > 0);
    setText(el.clipListCount, `${list.length} of ${maxList()}`);
    setText(el.exportZipLabel, `Export ${list.length} clip${list.length === 1 ? "" : "s"} as ZIP`);
  }

  // ---- export ------------------------------------------------------------------------------

  const exportRunning = () => Boolean(exportJob) && !el.exportCancel.hidden;

  function resetExport() {
    clearTimeout(exportTimer);
    exportJob = null;
    exportFailures = 0;
    deliverBy = 0;
    exportFilename = "";
    show(el.export, false);
  }

  function paintExport(p) {
    setText(el.exportStatusText, p.headline);
    setText(el.exportPercent, p.percent || "");
    show(el.exportSpin, p.icon === "spin");
    show(el.exportDone, p.icon === "done");
    show(el.exportFail, p.icon === "fail");
    const hasBar = p.bar !== undefined;
    show(el.exportBar, hasBar);
    if (hasBar) setBar(el.exportFill, el.exportBar, p.bar, p.meta || p.headline);
    setLine(el.exportMeta, p.meta || "");
    setLine(el.exportError, p.error || "");
    show(el.exportSaveRow, Boolean(p.save));
    if (p.save && exportJob) {
      el.exportSave.href = `/api/jobs/${encodeURIComponent(exportJob.id)}/file`;
      setText(el.exportSave, exportJob.kind === "zip" ? "Save ZIP" : "Save clip");
    }
    show(el.exportCancel, Boolean(p.cancel));
    el.exportCancel.disabled = Boolean(p.cancelling);
    show(el.exportActions, Boolean(p.cancel));
    show(el.export, true);
    render();
  }

  // Export the current marks as one clip (kind "clip"), or the whole list as one ZIP ("zip").
  async function startExport(kind) {
    const button = kind === "zip" ? el.exportZip : el.exportClip;
    if (state !== "ready" || exportRunning() || button.disabled) return;
    const g = gen;
    const hadFocus = document.activeElement === button;
    resetExport();
    exportJob = { id: "", autoSaved: false, kind };
    paintExport({ headline: HEADLINE.exportQueued, icon: "spin", bar: null, cancel: true, cancelling: true });
    if (hadFocus) el.export.focus(); // the button is disabled while the export runs: follow its progress
    const base = `/api/sources/${encodeURIComponent(source.id)}`;
    let created;
    try {
      created = kind === "zip"
        ? await api(`${base}/batch`, { clips: list.map((c) => ({ start: c.start, end: c.end })) })
        : await api(`${base}/clips`, { start: ms(marks.start), end: ms(marks.end) });
    } catch (err) {
      if (g !== gen) return;
      if (err.code === "source_not_found" || err.code === "source_not_ready") {
        resetExport();
        if (err.code === "source_not_found") gone("");
        else loadFailed(err.message);
      } else if (kind === "zip") { // shown under the list, next to the button that was used
        exportEnded({ headline: HEADLINE.exportFailed, icon: "fail", error: err.message });
        return;
      } else {
        resetExport();
        render();
        setLine(el.clipMsg, err.message, "error");
      }
      announce(err.message);
      return;
    }
    if (g !== gen) return;
    if (typeof created.job_id !== "string" || !JOB_ID_RE.test(created.job_id)) {
      resetExport();
      setLine(el.clipMsg, "SaveMe sent an unexpected reply. Please try again.", "error");
      render();
      return;
    }
    exportJob = { id: created.job_id, autoSaved: false, kind };
    store();
    announce("Export started.");
    applyExport({ status: str(created.status) || "queued" }, g);
  }

  async function pollExport(g, id) {
    if (g !== gen || !exportJob || exportJob.id !== id) return;
    let view;
    try {
      view = await api(`/api/jobs/${encodeURIComponent(id)}`);
    } catch (err) {
      if (g !== gen || !exportJob || exportJob.id !== id) return;
      if (err.code === "job_not_found") {
        exportEnded({ headline: HEADLINE.exportFailed, icon: "fail",
          error: exportJob.kind === "zip" ? MSG.zipLost : MSG.exportLost });
        return;
      }
      if (err.status >= 400 && err.status < 500 && err.status !== 429) {
        exportEnded({ headline: HEADLINE.exportFailed, icon: "fail", error: err.message });
        return;
      }
      exportFailures += 1;
      if (exportFailures >= MAX_POLL_FAILURES) {
        exportEnded({ headline: HEADLINE.exportFailed, icon: "fail", error: MSG.offline });
        return;
      }
      exportTimer = setTimeout(() => pollExport(g, id), RETRY_MS);
      return;
    }
    if (g !== gen || !exportJob || exportJob.id !== id) return;
    exportFailures = 0;
    applyExport(view, g);
  }

  function applyExport(v, g) {
    const status = str(v.status);
    const id = exportJob.id;
    const zip = exportJob.kind === "zip";
    if (EXPORTING.includes(status)) {
      const pct = status === "cutting" && isNum(v.progress_percent) ? v.progress_percent : null;
      const written = isNum(v.downloaded_bytes) && v.downloaded_bytes > 0 ? `${fmtMB(v.downloaded_bytes)} written` : "";
      // A ZIP's detail says which clip is being cut: "Cutting clip 2 of 5".
      const cutting = zip ? str(v.detail) || HEADLINE.zipCutting : HEADLINE.cutting;
      paintExport({
        headline: status === "cutting" ? cutting : HEADLINE.exportQueued,
        icon: "spin", bar: pct, percent: pct === null ? "" : `${Math.floor(pct)}%`, meta: written, cancel: true,
      });
      exportTimer = setTimeout(() => pollExport(g, id), POLL_MS);
    } else if (status === "ready") {
      exportFilename = str(v.filename) || exportFilename;
      const autoSave = !exportJob.autoSaved;
      exportJob.autoSaved = true;
      store();
      if (autoSave) deliverBy = Date.now() + DELIVERY_WAIT_MS;
      const saving = deliverBy > Date.now();
      paintExport({
        headline: saving ? HEADLINE.saving : zip ? HEADLINE.zipReady : HEADLINE.exportReady,
        icon: saving ? "spin" : "", bar: 100, meta: exportFilename, save: true,
      });
      if (autoSave) {
        el.exportSave.click(); // the browser downloads the file; the server then deletes it
        announce(HEADLINE.saving);
      }
      if (saving) exportTimer = setTimeout(() => pollExport(g, id), POLL_MS);
      else announce(zip ? "ZIP ready. Select Save ZIP." : "Clip ready. Select Save clip.");
    } else if (status === "failed") {
      exportEnded({ headline: HEADLINE.exportFailed, icon: "fail",
        error: (v.error && str(v.error.message)) || "The export failed. Please try again." });
    } else if (status === "expired") {
      if (v.reason === "delivered") {
        exportEnded(zip ? { headline: HEADLINE.zipSaved, icon: "done", meta: MSG.zipNext }
          : { headline: HEADLINE.exportSaved, icon: "done", meta: MSG.next }, zip ? MSG.zipSaved : MSG.saved);
      } else {
        exportEnded({ headline: zip ? HEADLINE.zipExpired : HEADLINE.exportExpired, icon: "fail",
          error: zip ? MSG.zipExpired : MSG.exportExpired });
      }
    } else if (status === "cancelled") {
      exportEnded({ headline: HEADLINE.exportCancelled, meta: MSG.exportCancelled });
    } else {
      exportTimer = setTimeout(() => pollExport(g, id), POLL_MS);
    }
  }

  // The export is over: show how it ended, with Export clip ready for the next one.
  function exportEnded(p, said = "") {
    clearTimeout(exportTimer);
    deliverBy = 0;
    paintExport({ ...p, cancel: false });
    exportJob = null;
    store();
    render();
    announce(said || [p.headline, p.error || p.meta].filter(Boolean).join(". "));
  }

  async function cancelExport() {
    if (!exportJob || !exportJob.id || el.exportCancel.disabled) return;
    const g = gen;
    const id = exportJob.id;
    clearTimeout(exportTimer);
    paintExport({ headline: HEADLINE.exportCancelling, icon: "spin", bar: null, cancel: true, cancelling: true });
    let view;
    try {
      view = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
    } catch (err) {
      if (g !== gen || !exportJob || exportJob.id !== id) return;
      setLine(el.exportError, `Couldn't cancel. ${err.message}`);
      exportTimer = setTimeout(() => pollExport(g, id), 0);
      return;
    }
    if (g === gen && exportJob && exportJob.id === id) applyExport(view, g);
  }

  // ---- wiring -------------------------------------------------------------------------------

  el.studioClose.addEventListener("click", close);
  el.studioLoadCancel.addEventListener("click", close);
  el.studioVideo.addEventListener("loadedmetadata", onMetadata);
  el.studioVideo.addEventListener("timeupdate", render);
  el.studioVideo.addEventListener("seeked", render);
  el.studioVideo.addEventListener("pause", stopPreview);
  el.studioVideo.addEventListener("error", onVideoError);
  el.timeline.addEventListener("pointerdown", onTimelineDown);
  el.timeline.addEventListener("pointermove", onTimelineMove);
  el.timeline.addEventListener("pointerup", onTimelineUp);
  el.timeline.addEventListener("pointercancel", onTimelineUp);
  el.handleStart.addEventListener("keydown", (e) => onHandleKey("start", e));
  el.handleEnd.addEventListener("keydown", (e) => onHandleKey("end", e));
  for (const which of ["start", "end"]) {
    const input = which === "start" ? el.clipStart : el.clipEnd;
    input.addEventListener("change", () => fromField(which));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") fromField(which);
    });
  }
  el.setStart.addEventListener("click", () => toPlayhead("start"));
  el.setEnd.addEventListener("click", () => toPlayhead("end"));
  el.playClip.addEventListener("click", playClip);
  el.exportClip.addEventListener("click", () => startExport("clip"));
  el.exportZip.addEventListener("click", () => startExport("zip"));
  el.addClip.addEventListener("click", addToList);
  el.exportCancel.addEventListener("click", cancelExport);
  document.addEventListener("keydown", onKey);
  document.addEventListener("visibilitychange", onVisibility);

  window.SaveMeStudio = Object.freeze({
    openLink,
    openFile,
    restore,
    isOpen: () => state !== "closed",
    onClosed(callback) {
      closedCallback = typeof callback === "function" ? callback : () => {};
    },
  });
})();
