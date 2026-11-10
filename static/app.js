// SaveMe page logic: plain browser JavaScript, no dependencies.
// The CSP allows only same-origin scripts and styles, so all behaviour lives here and all
// styling is Tailwind classes in index.html. Text is set with textContent, never parsed as HTML.
//
// States: idle → inspecting → inspected → job (queued | downloading | converting) → ready
//         → saved | failed; a vanished job ("lost") resets to idle with a message.
// A running job can be cancelled. The server keeps its partial download for a few minutes
// (resume_seconds), and Resume picks it up where it stopped; the job stays in session
// storage meanwhile, so a reload still offers Resume.
// A job that turns ready is saved to the device straight away, once per job; the
// "Save file" link is the fallback when the browser holds the download back.
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
  const AUDIO = "audio"; // the MP3 format choice; video choices are "video:<quality id>"
  const VIDEO_PREFIX = "video:";

  const MSG = {
    lost: "This download was lost (the server may have restarted). Please start again.",
    saved: "Saved — file removed from server.",
    removed: "File removed from server.",
    noAudio: "This media has no audio track.",
    noVideo: "This media has no video track.",
    emptyUrl: "Paste a link first.",
    offline: "Can't reach SaveMe. Check that it's running, then try again.",
    retrying: "Waiting for SaveMe to respond. Retrying…",
    gaveUp: "Can't reach SaveMe. Check that it's running, then reload this page to pick the download back up.",
    badReply: "SaveMe sent an unexpected reply. Please try again.",
    jobFailed: "The download failed. Please try again.",
    expiredTtl: "This file expired before it was saved. Please start again.",
    expiredEvicted: "This file expired early to free up server space. Please start again.",
    partialRemoved: "The partial download was removed from the server.",
    cancelFailed: "Couldn't cancel.",
    resumed: "Download resumed.",
  };
  const keptFor = (seconds) =>
    `Kept on the server for ${fmtDuration(seconds)}. Resume to continue where it stopped.`;
  const HEADLINE = {
    starting: "Starting…",
    resuming: "Checking on your download…",
    queued: "Waiting to start",
    downloading: "Downloading",
    converting: "Converting",
    ready: "Ready to save",
    saving: "Saving to your device…",
    saved: "Saved",
    failed: "Download failed",
    expired: "File expired",
    offline: "Connection lost",
    cancelling: "Cancelling…",
    cancelled: "Cancelled",
    restarting: "Resuming…",
  };

  const el = {};
  for (const id of [
    "inspect-form", "url", "inspect", "status", "spinner", "status-text", "banner", "banner-text",
    "result", "media", "thumb", "duration", "duration-text", "duration-line", "duration-line-text",
    "result-title", "source", "notices", "formats", "format-note", "download", "action-msg", "chip-tpl",
    "job", "job-kind", "job-name", "job-spin", "job-done", "job-fail", "job-status", "job-percent", "bar",
    "bar-fill", "job-detail", "job-meta", "save-row", "save", "job-notice", "job-error", "job-actions",
    "resume", "cancel", "again", "announcer",
  ]) {
    el[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
  }

  let state = "idle"; // idle | inspecting | inspected | job | ready | saved | failed | cancelled
  let degraded = false; // server reported a config error on /health
  let inspected = null; // { url, info } from the last successful inspect
  let job = null; // { id, url, title, output, filename, autoSaved }
  let pollGen = 0; // bumped to cancel any in-flight poll
  let pollTimer = 0;
  let failures = 0;
  let deliverBy = 0; // while saving: keep polling until this time for "delivered"
  let readyNotice = ""; // the ready job's notice, kept across repaints
  let resumeBy = 0; // while cancelled: Resume works until this time
  let resumeTimer = 0; // ticks the "Kept on the server for …" countdown
  let jobError = ""; // a failed Cancel or Resume, shown on the panel until the next try

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

  const fileUrl = () => `/api/jobs/${encodeURIComponent(job.id)}/file`;

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
          autoSaved: saved.autoSaved === true,
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
    const canDownload = Boolean(inspected) && !degraded && ["inspected", "saved", "failed", "cancelled"].includes(next);
    el.download.disabled = !(canDownload && chosenFormat());
  }

  // The checked format choice as { output, quality }, or null when none is usable.
  function chosenFormat() {
    const input = el.formats.querySelector("input:checked");
    if (!input || input.disabled) return null;
    if (input.value === AUDIO) return { output: "audio", quality: "best" };
    return { output: "video", quality: input.value.slice(VIDEO_PREFIX.length) };
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
    clearTimeout(resumeTimer);
    clearJob();
    job = null;
    deliverBy = 0;
    readyNotice = "";
    resumeBy = 0;
    jobError = "";
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
    const duration = info.duration === null ? "" : fmtDuration(info.duration);
    setText(el.durationText, duration);
    setText(el.durationLineText, duration);

    el.thumb.removeAttribute("src");
    if (info.thumbnail) el.thumb.src = info.thumbnail;
    placeMedia(Boolean(info.thumbnail));

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

    renderFormats(info);
    setLine(el.actionMsg, "");
    show(el.result, true);
  }

  // The thumbnail carries the duration badge; without one, the duration joins the source line.
  function placeMedia(hasThumb) {
    const hasDuration = Boolean(el.durationText.textContent);
    show(el.media, hasThumb);
    show(el.duration, hasThumb && hasDuration);
    show(el.durationLine, !hasThumb && hasDuration);
  }

  // One radio "chip" per video quality, then MP3. The server's preferred quality starts checked.
  function renderFormats(info) {
    const chips = info.video.available
      ? info.video.qualities.map((q) => formatChip(VIDEO_PREFIX + q.id, q.label, false))
      : [];
    const mp3 = formatChip(AUDIO, "MP3 audio", true);
    const mp3Input = mp3.querySelector("input");
    mp3Input.disabled = !info.audio.available;
    if (mp3Input.disabled) mp3Input.setAttribute("aria-describedby", "format-note");
    el.formats.replaceChildren(...chips, mp3);

    const preferred = info.video.qualities.find((q) => q.id === info.video.preferred) || info.video.qualities[0];
    let pick = "";
    if (info.video.available && preferred) pick = VIDEO_PREFIX + preferred.id;
    else if (info.audio.available) pick = AUDIO;
    for (const input of el.formats.querySelectorAll("input")) input.checked = input.value === pick;

    const notes = [info.video.available ? "" : MSG.noVideo, info.audio.available ? "" : MSG.noAudio];
    setLine(el.formatNote, notes.filter(Boolean).join(" "));
  }

  function formatChip(value, label, audio) {
    const chip = el.chipTpl.content.firstElementChild.cloneNode(true);
    chip.querySelector("input").value = value;
    chip.querySelector("[data-label]").textContent = label;
    if (audio) chip.querySelector("[data-icon]").classList.remove("hidden");
    return chip;
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
    say("Fetching…", "busy");
    try {
      const info = normalize(await api("/api/inspect", { url }));
      inspected = { url, info };
      renderResult(info);
      setState("inspected");
      say("");
      announce(`Found “${info.title}”. Choose a format below.`);
    } catch (err) {
      if (err.code === "config_error") enterDegraded(err.message);
      setState("idle");
      say(err.message, "error");
    }
  }

  // ---- jobs -----------------------------------------------------------------------------

  async function startJob() {
    const choice = chosenFormat();
    if (!inspected || el.download.disabled || !choice) return;
    const { output, quality } = choice;
    if (output === "video" && !inspected.info.video.qualities.some((q) => q.id === quality)) return;
    if (output === "audio" && !inspected.info.audio.available) return;

    const previous = state;
    const { url, info } = inspected;
    dropJob();
    setLine(el.actionMsg, "");
    job = { id: "", url, title: info.title, output, filename: "", autoSaved: false };
    // Lock the button before the request goes out, so a double-click can't start two jobs.
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
      el.download.focus();
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
    } else if (status === "cancelled") {
      showCancelled(v);
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
      error: jobError,
      cancel: true,
    });
    announce(HEADLINE[status]);
  }

  // ---- cancel and resume ----------------------------------------------------------------

  async function onCancel() {
    if (!job || !job.id || state !== "job" || el.cancel.disabled) return;
    const id = job.id;
    stopPolling();
    jobError = "";
    paintJob({ headline: HEADLINE.cancelling, icon: "spin", bar: null, cancel: true, cancelling: true });
    announce(HEADLINE.cancelling);
    let view;
    try {
      view = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
    } catch (err) {
      if (!job || job.id !== id) return;
      if (lostWith(err)) return;
      jobError = `${MSG.cancelFailed} ${err.message}`;
      announce(jobError);
      pollSoon(0); // back to the progress, with the message and the Cancel button
      return;
    }
    if (!job || job.id !== id) return;
    applyView(view);
    (el.resume.hidden ? el.job : el.resume).focus();
  }

  // The server stopped the job. While it keeps the partial download, offer Resume.
  function showCancelled(v) {
    stopPolling();
    deliverBy = 0;
    const left = isNum(v.resume_seconds) && v.resume_seconds > 0 ? v.resume_seconds : 0;
    resumeBy = left ? Date.now() + left * 1000 : 0;
    if (resumeBy) storeJob();
    else clearJob();
    setState("cancelled");
    paintCancelled();
    announce(`${HEADLINE.cancelled}. ${resumeBy ? keptFor(left) : MSG.partialRemoved}`);
  }

  function paintCancelled() {
    clearTimeout(resumeTimer);
    const ms = resumeBy - Date.now();
    if (resumeBy && ms <= 0) {
      resumeBy = 0;
      clearJob(); // nothing left to resume after a reload
    }
    paintJob({
      headline: HEADLINE.cancelled,
      meta: resumeBy ? keptFor(Math.ceil(ms / 1000)) : MSG.partialRemoved,
      error: jobError,
      resume: Boolean(resumeBy),
      again: true,
    });
    if (resumeBy) resumeTimer = setTimeout(paintCancelled, ms % 1000 || 1000);
  }

  async function onResume() {
    if (!job || !job.id || state !== "cancelled" || resumeBy <= Date.now()) return;
    const id = job.id;
    clearTimeout(resumeTimer);
    jobError = "";
    setState("job");
    paintJob({ headline: HEADLINE.restarting, icon: "spin", bar: null });
    announce(HEADLINE.restarting);
    let view;
    try {
      view = await api(`/api/jobs/${encodeURIComponent(id)}/resume`, {});
    } catch (err) {
      if (!job || job.id !== id) return;
      if (lostWith(err)) return;
      if (err.code === "not_resumable") resumeBy = 0;
      jobError = err.message;
      setState("cancelled");
      paintCancelled();
      announce(err.message);
      (el.resume.hidden ? el.job : el.resume).focus();
      return;
    }
    if (!job || job.id !== id) return;
    resumeBy = 0;
    storeJob();
    announce(MSG.resumed);
    applyView(view);
    el.job.focus();
  }

  // The server no longer has the job, or can't run jobs at all: reset with a message.
  function lostWith(err) {
    if (err.code === "config_error") enterDegraded(err.message);
    else if (err.code !== "job_not_found") return false;
    lose();
    return true;
  }

  // The file is waiting on the server. The first time a job gets here it is saved without
  // asking (a reload after that shows the Save file link instead of saving it twice).
  function showReady(v) {
    job.filename = str(v.filename) || job.filename;
    readyNotice = str(v.notice);
    const autoSave = !job.autoSaved;
    job.autoSaved = true;
    storeJob();
    setState("ready");
    paintReady();
    if (autoSave) {
      el.save.click(); // the browser downloads the file; onSave starts watching for delivery
    } else if (deliverBy > Date.now()) {
      pollSoon(POLL_MS);
    } else {
      announce("Ready to save. Select Save file.");
    }
  }

  function paintReady() {
    const saving = deliverBy > Date.now();
    paintJob({
      headline: saving ? HEADLINE.saving : HEADLINE.ready,
      icon: saving ? "spin" : "",
      bar: 100,
      detail: job.filename,
      notice: readyNotice,
      save: true,
      again: !saving,
    });
  }

  // Terminal outcomes that keep the panel up: saved, failed or expired.
  function finish(kind, message) {
    stopPolling();
    clearJob();
    deliverBy = 0;
    if (kind === "saved") {
      setState("saved");
      paintJob({ headline: HEADLINE.saved, icon: "done", detail: job ? job.filename : "",
                meta: MSG.removed, again: true });
      announce(MSG.saved);
      return;
    }
    setState("failed");
    const headline = kind === "expired" ? HEADLINE.expired : HEADLINE.failed;
    paintJob({ headline, icon: "fail", error: message, again: true });
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
    paintJob({ headline: HEADLINE.offline, icon: "fail", error: MSG.gaveUp, again: true });
    announce(MSG.gaveUp);
  }

  // Draws the whole job panel from one description, so no stale piece is left behind.
  function paintJob(p) {
    setText(el.jobKind, job ? (job.output === "audio" ? "MP3" : "Video") : "");
    setText(el.jobName, job ? job.title : "");
    setText(el.jobStatus, p.headline);
    setText(el.jobPercent, p.percent || "");
    show(el.jobSpin, p.icon === "spin");
    show(el.jobDone, p.icon === "done");
    show(el.jobFail, p.icon === "fail");

    const hasBar = p.bar !== undefined;
    show(el.bar, hasBar);
    if (hasBar) setBar(p.bar, p.meta || p.detail || p.headline);

    setLine(el.jobDetail, p.detail || "");
    setLine(el.jobMeta, p.meta || "");
    setLine(el.jobNotice, p.notice || "");
    setLine(el.jobError, p.error || "");

    show(el.saveRow, Boolean(p.save));
    if (p.save && job) el.save.href = fileUrl();
    show(el.resume, Boolean(p.resume));
    show(el.cancel, Boolean(p.cancel));
    el.cancel.disabled = Boolean(p.cancelling);
    show(el.again, Boolean(p.again));
    show(el.jobActions, Boolean(p.resume || p.cancel || p.again));
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

  // Runs for the automatic save and for a click on Save file. The browser downloads the
  // file; the server deletes it once fully sent, so watch for that.
  function onSave() {
    if (!job || state !== "ready") return;
    deliverBy = Date.now() + DELIVERY_WAIT_MS;
    paintReady();
    announce(HEADLINE.saving);
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
    el.formats.addEventListener("change", () => setState(state));
    el.download.addEventListener("click", startJob);
    el.save.addEventListener("click", onSave);
    el.cancel.addEventListener("click", onCancel);
    el.resume.addEventListener("click", onResume);
    el.again.addEventListener("click", onAgain);
    el.thumb.addEventListener("error", () => placeMedia(false));
    setState("idle");

    const saved = loadJob();
    if (saved) {
      job = saved;
      if (saved.url) el.url.value = saved.url;
      setState("job");
      paintJob({ headline: HEADLINE.resuming, bar: null, cancel: true });
    }
    const healthy = await checkHealth();
    if (!job) return;
    if (healthy) pollSoon(0);
    else lose();
  }

  init();
})();
