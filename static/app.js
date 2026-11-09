// saveme page logic: plain browser JavaScript, no dependencies.
// The CSP allows only same-origin scripts and styles, so all behaviour lives here and all
// styling is Tailwind classes in index.html. Text is set with textContent, never parsed as HTML.
//
// States: idle → inspecting → inspected → job (queued | downloading | converting) → ready
//         → saved | failed; a vanished job ("lost") resets to idle with a message.
"use strict";

(() => {
  const STORE_KEY = "saveme.job";
  const POLL_MS = 1000;
  const RETRY_MS = 2000;
  const MAX_POLL_FAILURES = 30;
  const DELIVERY_WAIT_MS = 60000;
  const JOB_ID_RE = /^[A-Za-z0-9_-]{22}$/;
  const ACTIVE = ["queued", "downloading", "converting"];
  const MB = 1024 * 1024;

  const MSG = {
    lost: "This download was lost (the server may have restarted). Please start again.",
    saved: "Saved — file removed from server.",
    noAudio: "This media has no audio track.",
    noVideo: "This media has no video track.",
    emptyUrl: "Paste a link first.",
    offline: "Can't reach saveme. Check that it's running, then try again.",
    retrying: "Waiting for saveme to respond. Retrying…",
    gaveUp: "Can't reach saveme. Check that it's running, then reload this page to pick the download back up.",
    badReply: "saveme sent an unexpected reply. Please try again.",
    jobFailed: "The download failed. Please try again.",
    expiredTtl: "This file expired before it was saved. Please start again.",
    expiredEvicted: "This file expired early to free up server space. Please start again.",
    saveAgain: "If the download didn't start, select Save File again.",
  };
  const HEADLINE = {
    starting: "Starting…",
    resuming: "Checking on your download…",
    queued: "Waiting to start",
    downloading: "Downloading",
    converting: "Converting",
    ready: "Ready to save",
    saving: "Saving to your device…",
    failed: "Download failed",
    expired: "File expired",
    offline: "Connection lost",
  };

  const el = {};
  for (const id of [
    "inspect-form", "url", "inspect", "status", "spinner", "status-text", "banner", "banner-text",
    "result", "thumb", "result-title", "duration", "duration-text", "source", "notices", "quality-row",
    "quality", "dl-video", "dl-audio", "video-reason", "audio-reason", "action-msg",
    "job", "job-kind", "job-name", "job-status", "job-percent", "bar", "bar-fill", "job-detail",
    "job-meta", "job-notice", "job-error", "job-actions", "save", "again", "announcer",
  ]) {
    el[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
  }

  let state = "idle"; // idle | inspecting | inspected | job | ready | saved | failed
  let degraded = false; // server reported a config error on /health
  let inspected = null; // { url, info } from the last successful inspect
  let job = null; // { id, url, title, output, filename }
  let pollGen = 0; // bumped to cancel any in-flight poll
  let pollTimer = 0;
  let failures = 0;
  let deliverBy = 0; // after Save File: keep polling until this time for "delivered"

  // ---- small helpers ------------------------------------------------------------------

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const str = (v) => (typeof v === "string" ? v.trim() : "");
  const fmtMB = (bytes) => `${(bytes / MB).toFixed(1)} MB`;

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const pad = (n) => String(n).padStart(2, "0");
    return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
  }

  function httpUrl(value) {
    if (typeof value !== "string") return null;
    try {
      const u = new URL(value);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch {
      return null;
    }
  }

  function setText(node, text) {
    const value = text == null ? "" : String(text);
    if (node.textContent !== value) node.textContent = value;
  }

  function show(node, visible) {
    node.hidden = !visible;
  }

  // A text line that is hidden when empty; tone "error" turns it red (data-[tone=error]: classes).
  function setLine(node, text, tone) {
    setText(node, text);
    show(node, Boolean(text));
    if (tone) node.dataset.tone = tone;
    else delete node.dataset.tone;
  }

  // The visible status line under the link field (aria-live).
  function say(text, tone) {
    setText(el.statusText, text);
    if (tone) el.status.dataset.tone = tone;
    else delete el.status.dataset.tone;
    show(el.spinner, tone === "busy");
  }

  // Screen-reader-only announcements for job changes (aria-live). Same text is not repeated.
  function announce(text) {
    setText(el.announcer, text);
  }

  // ---- session storage (resume after reload) ------------------------------------------

  function storeJob() {
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify(job));
    } catch {
      // Storage unavailable: the download still works, it just can't resume after a reload.
    }
  }

  function clearJob() {
    try {
      sessionStorage.removeItem(STORE_KEY);
    } catch {
      // Nothing to clear.
    }
  }

  function loadJob() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      if (saved && typeof saved.id === "string" && JOB_ID_RE.test(saved.id)) {
        return {
          id: saved.id,
          url: str(saved.url),
          title: str(saved.title),
          output: saved.output === "audio" ? "audio" : "video",
          filename: str(saved.filename),
        };
      }
    } catch {
      // Unreadable entry: drop it below.
    }
    clearJob();
    return null;
  }

  // ---- API ------------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  // GET when body is undefined, otherwise POST JSON. Errors carry the server's message.
  async function api(path, body) {
    const init = { cache: "no-store", headers: { Accept: "application/json" } };
    if (body !== undefined) {
      init.method = "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new ApiError(0, "network", MSG.offline);
    }
    let data = null;
    try {
      data = await res.json();
    } catch {
      // Not JSON; handled below.
    }
    if (!res.ok) {
      const err = data && typeof data === "object" && data.error && typeof data.error === "object" ? data.error : {};
      throw new ApiError(
        res.status,
        str(err.code) || `http_${res.status}`,
        str(err.message) || `Something went wrong (HTTP ${res.status}). Please try again.`,
      );
    }
    if (!data || typeof data !== "object") throw new ApiError(res.status, "bad_response", MSG.badReply);
    return data;
  }

  // ---- state ----------------------------------------------------------------------------

  function setState(next) {
    state = next;
    const busy = next === "inspecting" || next === "job";
    el.inspect.disabled = busy || degraded;
    el.url.readOnly = busy;
    const canDownload = Boolean(inspected) && !degraded && ["inspected", "saved", "failed"].includes(next);
    el.dlVideo.disabled = !(canDownload && inspected.info.video.available);
    el.dlAudio.disabled = !(canDownload && inspected.info.audio.available);
    el.quality.disabled = el.dlVideo.disabled;
  }

  function stopPolling() {
    pollGen += 1;
    clearTimeout(pollTimer);
    failures = 0;
  }

  function pollSoon(delay) {
    clearTimeout(pollTimer);
    const gen = pollGen;
    pollTimer = setTimeout(() => poll(gen), delay);
  }

  function dropJob() {
    stopPolling();
    clearJob();
    job = null;
    deliverBy = 0;
    show(el.job, false);
  }

  function clearResult() {
    inspected = null;
    show(el.result, false);
    setLine(el.actionMsg, "");
  }

  // ---- inspect --------------------------------------------------------------------------

  function normalize(d) {
    const video = d.video && typeof d.video === "object" ? d.video : {};
    const audio = d.audio && typeof d.audio === "object" ? d.audio : {};
    let qualities = Array.isArray(video.qualities)
      ? video.qualities.filter((q) => q && str(q.id)).map((q) => ({ id: str(q.id), label: str(q.label) || str(q.id) }))
      : [];
    if (video.available && qualities.length === 0) qualities = [{ id: "best", label: "Best" }];
    return {
      title: str(d.title) || "Untitled",
      duration: isNum(d.duration_seconds) && d.duration_seconds > 0 ? d.duration_seconds : null,
      thumbnail: httpUrl(d.thumbnail_url),
      source: str(d.source),
      page: httpUrl(d.webpage_url),
      video: { available: Boolean(video.available), qualities, preferred: str(video.default) },
      audio: { available: Boolean(audio.available) },
      notices: Array.isArray(d.notices) ? d.notices.map(str).filter(Boolean) : [],
    };
  }

  function renderResult(info) {
    setText(el.resultTitle, info.title);
    setText(el.durationText, info.duration === null ? "" : fmtDuration(info.duration));
    show(el.duration, info.duration !== null);

    show(el.thumb, false);
    el.thumb.removeAttribute("src");
    if (info.thumbnail) {
      el.thumb.src = info.thumbnail;
      show(el.thumb, true);
    }

    setText(el.source, info.source || (info.page ? new URL(info.page).hostname : ""));
    if (info.page) el.source.href = info.page;
    else el.source.removeAttribute("href");
    show(el.source, Boolean(el.source.textContent));

    el.notices.replaceChildren(
      ...info.notices.map((text) => {
        const li = document.createElement("li");
        li.textContent = text;
        return li;
      }),
    );
    show(el.notices, info.notices.length > 0);

    el.quality.replaceChildren(...info.video.qualities.map((q) => new Option(q.label, q.id)));
    const preferred = info.video.qualities.find((q) => q.id === info.video.preferred) || info.video.qualities[0];
    if (preferred) el.quality.value = preferred.id;
    show(el.qualityRow, info.video.available);

    setLine(el.videoReason, info.video.available ? "" : MSG.noVideo);
    setLine(el.audioReason, info.audio.available ? "" : MSG.noAudio);
    setLine(el.actionMsg, "");
    show(el.result, true);
  }

  async function inspect(event) {
    event.preventDefault();
    if (state === "inspecting" || state === "job" || degraded) return;
    const url = el.url.value.trim();
    if (!url) {
      say(MSG.emptyUrl, "error");
      el.url.focus();
      return;
    }
    dropJob();
    clearResult();
    setState("inspecting");
    say("Inspecting…", "busy");
    try {
      const info = normalize(await api("/api/inspect", { url }));
      inspected = { url, info };
      renderResult(info);
      setState("inspected");
      say("");
      announce(`Found “${info.title}”. Choose a download below.`);
    } catch (err) {
      if (err.code === "config_error") enterDegraded(err.message);
      setState("idle");
      say(err.message, "error");
    }
  }

  // ---- jobs -----------------------------------------------------------------------------

  async function startJob(output) {
    const button = output === "video" ? el.dlVideo : el.dlAudio;
    if (!inspected || button.disabled) return;
    const quality = output === "video" ? el.quality.value : "best";
    if (output === "video" && !inspected.info.video.qualities.some((q) => q.id === quality)) return;

    const previous = state;
    const { url, info } = inspected;
    dropJob();
    setLine(el.actionMsg, "");
    job = { id: "", url, title: info.title, output, filename: "" };
    // Lock both buttons before the request goes out, so a double-click can't start two jobs.
    setState("job");
    paintJob({ headline: HEADLINE.starting, bar: null });
    el.job.focus();
    try {
      const created = await api("/api/jobs", { url, output, quality });
      if (typeof created.job_id !== "string" || !JOB_ID_RE.test(created.job_id)) {
        throw new ApiError(0, "bad_response", MSG.badReply);
      }
      job.id = created.job_id;
      storeJob();
      announce(output === "video" ? "Video download started." : "Audio download started.");
      applyView({ status: str(created.status) || "queued" });
    } catch (err) {
      if (err.code === "config_error") enterDegraded(err.message);
      job = null;
      show(el.job, false);
      setState(previous);
      setLine(el.actionMsg, err.message, "error");
      announce(err.message);
      button.focus();
    }
  }

  async function poll(gen) {
    if (gen !== pollGen || !job || !job.id) return;
    let view;
    try {
      view = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
    } catch (err) {
      if (gen !== pollGen) return;
      if (err.code === "job_not_found") {
        lose();
        return;
      }
      if (err.code === "config_error") {
        enterDegraded(err.message); // restarted with bad settings: the job is gone
        lose();
        return;
      }
      if (err.status >= 400 && err.status < 500 && err.status !== 429) {
        finish("failed", err.message);
        return;
      }
      failures += 1;
      if (failures >= MAX_POLL_FAILURES) {
        giveUp();
        return;
      }
      setLine(el.jobMeta, MSG.retrying);
      pollSoon(RETRY_MS);
      return;
    }
    if (gen !== pollGen) return;
    failures = 0;
    applyView(view);
  }

  function applyView(v) {
    const status = str(v.status);
    if (ACTIVE.includes(status)) {
      setState("job");
      paintProgress(status, v);
      pollSoon(POLL_MS);
    } else if (status === "ready") {
      showReady(v);
    } else if (status === "failed") {
      finish("failed", (v.error && str(v.error.message)) || MSG.jobFailed);
    } else if (status === "expired") {
      if (v.reason === "delivered") finish("saved");
      else finish("expired", v.reason === "evicted" ? MSG.expiredEvicted : MSG.expiredTtl);
    } else {
      pollSoon(POLL_MS); // unknown status: keep asking
    }
  }

  function paintProgress(status, v) {
    const downloading = status === "downloading";
    const pct = downloading && isNum(v.progress_percent) ? Math.min(100, Math.max(0, v.progress_percent)) : null;
    const done = isNum(v.downloaded_bytes) && v.downloaded_bytes > 0 ? v.downloaded_bytes : 0;
    const total = isNum(v.total_bytes) && v.total_bytes > 0 ? v.total_bytes : null;
    const detail = str(v.detail) === HEADLINE[status] ? "" : str(v.detail);
    let meta = "";
    if (downloading && done) meta = pct !== null && total ? `${fmtMB(done)} of ${fmtMB(total)}` : `${fmtMB(done)} downloaded`;
    paintJob({
      headline: HEADLINE[status],
      percent: pct === null ? "" : `${Math.floor(pct)}%`,
      bar: pct,
      detail,
      meta,
      notice: str(v.notice),
    });
    announce(HEADLINE[status]);
  }

  function showReady(v) {
    job.filename = str(v.filename) || job.filename;
    storeJob();
    const saving = deliverBy > Date.now();
    setState("ready");
    paintJob({
      headline: saving ? HEADLINE.saving : HEADLINE.ready,
      bar: 100,
      detail: job.filename,
      meta: deliverBy && !saving ? MSG.saveAgain : "",
      notice: str(v.notice),
      save: true,
      again: true,
    });
    announce("Ready to save. Select Save File.");
    if (saving) pollSoon(POLL_MS);
  }

  // Terminal outcomes that keep the panel up: saved, failed or expired.
  function finish(kind, message) {
    stopPolling();
    clearJob();
    deliverBy = 0;
    if (kind === "saved") {
      setState("saved");
      paintJob({ headline: MSG.saved, detail: job ? job.filename : "", again: true });
      announce(MSG.saved);
      return;
    }
    setState("failed");
    const headline = kind === "expired" ? HEADLINE.expired : HEADLINE.failed;
    paintJob({ headline, error: message, again: true });
    announce(`${headline}. ${message}`);
  }

  // The server no longer knows the job (restart or purge): reset and say so.
  function lose() {
    const url = job ? job.url : "";
    dropJob();
    clearResult();
    if (url && !el.url.value.trim()) el.url.value = url;
    setState("idle");
    say(MSG.lost, "error");
  }

  // The server stopped answering. Keep the stored job so a reload can pick it back up.
  function giveUp() {
    stopPolling();
    setState("failed");
    paintJob({ headline: HEADLINE.offline, error: MSG.gaveUp, again: true });
    announce(MSG.gaveUp);
  }

  // Draws the whole job panel from one description, so no stale piece is left behind.
  function paintJob(p) {
    setText(el.jobKind, job ? (job.output === "audio" ? "MP3" : "Video") : "");
    setText(el.jobName, job ? job.title : "");
    setText(el.jobStatus, p.headline);
    setText(el.jobPercent, p.percent || "");

    const hasBar = p.bar !== undefined;
    show(el.bar, hasBar);
    if (hasBar) setBar(p.bar, p.meta || p.detail || p.headline);

    setLine(el.jobDetail, p.detail || "");
    setLine(el.jobMeta, p.meta || "");
    setLine(el.jobNotice, p.notice || "");
    setLine(el.jobError, p.error || "");

    show(el.save, Boolean(p.save));
    if (p.save && job) el.save.href = `/api/jobs/${encodeURIComponent(job.id)}/file`;
    show(el.again, Boolean(p.again));
    show(el.jobActions, Boolean(p.save || p.again));
    show(el.job, true);
  }

  // Determinate only for a real percent; otherwise a faded, gently pulsing full-width bar.
  function setBar(value, text) {
    if (isNum(value)) {
      const pct = Math.min(100, Math.max(0, value));
      delete el.barFill.dataset.indeterminate;
      el.barFill.style.width = `${pct}%`;
      el.bar.setAttribute("aria-valuenow", String(Math.floor(pct)));
    } else {
      el.barFill.dataset.indeterminate = "";
      el.barFill.style.width = "100%";
      el.bar.removeAttribute("aria-valuenow");
    }
    el.bar.setAttribute("aria-valuetext", text);
  }

  // ---- wiring ---------------------------------------------------------------------------

  function enterDegraded(message) {
    degraded = true;
    setText(el.bannerText, message);
    show(el.banner, true);
    setState(state);
  }

  async function checkHealth() {
    try {
      await api("/health");
      return true;
    } catch (err) {
      if (err.status !== 503) return true; // unreachable or odd: the next action reports it
      enterDegraded(err.message);
      return false;
    }
  }

  function onSave() {
    if (!job || state !== "ready") return;
    // The browser downloads the file; the server deletes it once fully sent. Watch for that.
    deliverBy = Date.now() + DELIVERY_WAIT_MS;
    setText(el.jobStatus, HEADLINE.saving);
    setLine(el.jobMeta, "");
    pollSoon(POLL_MS);
  }

  function onAgain() {
    dropJob();
    clearResult();
    say("");
    el.url.value = "";
    setState("idle");
    el.url.focus();
  }

  // Editing the link after inspecting hides the old result, so a download always matches
  // the link shown.
  function onUrlInput() {
    if (inspected && state !== "job" && el.url.value.trim() !== inspected.url) {
      clearResult();
      setState(state === "inspected" ? "idle" : state);
    }
    if (el.status.dataset.tone === "error") say("");
  }

  async function init() {
    el.inspectForm.addEventListener("submit", inspect);
    el.url.addEventListener("input", onUrlInput);
    el.dlVideo.addEventListener("click", () => startJob("video"));
    el.dlAudio.addEventListener("click", () => startJob("audio"));
    el.save.addEventListener("click", onSave);
    el.again.addEventListener("click", onAgain);
    el.thumb.addEventListener("error", () => show(el.thumb, false));
    setState("idle");

    const saved = loadJob();
    if (saved) {
      job = saved;
      if (saved.url) el.url.value = saved.url;
      setState("job");
      paintJob({ headline: HEADLINE.resuming, bar: null });
    }
    const healthy = await checkHealth();
    if (!job) return;
    if (healthy) pollSoon(0);
    else lose();
  }

  init();
})();
