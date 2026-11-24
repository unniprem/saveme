// SaveMe AI Highlights: the second tab of Clip Studio. Transcribe the open video on the server
// (locally, with faster-whisper), ask OpenRouter for highlights in the transcript, then review,
// adjust, preview and keep them. Plain browser JavaScript; shared helpers come from common.js
// (window.SaveMe), and the studio's editor, clip list and export from studio.js
// (window.SaveMeStudioBridge). Text is set with textContent only: titles, summaries and
// transcript lines come from an AI model and a video, so they are never treated as HTML.
//
// An analysis runs on the server (POST /api/sources/{id}/analyses) and is followed by polling
// GET /api/analyses/{id}: queued → preparing → extracting → transcribing → analyzing → ranking →
// ready (or failed / cancelled). sessionStorage keeps the analysis, the form, the tab and the
// edits, so a reload comes back to the same highlights.
"use strict";

(() => {
  const { JOB_ID_RE, isNum, str, setText, show, setLine, errorFrom, api } = window.SaveMe;
  const bridge = window.SaveMeStudioBridge;
  const STORE_KEY = "saveme.ai";
  const POLL_MS = 1000;
  const RETRY_MS = 2000;
  const MAX_POLL_FAILURES = 30;
  const ESTIMATE_DELAY_MS = 400;
  const MAX_SUBTITLE_BYTES = 4 * 1024 * 1024;
  const STAGES = ["preparing", "extracting", "transcribing", "analyzing", "ranking", "ready"];
  const PENDING = ["queued", "preparing", "extracting", "transcribing", "analyzing", "ranking"];
  const HEADLINE = {
    starting: "Starting…",
    queued: "Waiting for the AI to be free",
    preparing: "Preparing the video",
    extracting: "Extracting audio",
    transcribing: "Transcribing on this computer",
    analyzing: "Analyzing the transcript",
    ranking: "Ranking highlights",
    ready: "Highlights ready",
    transcribed: "Transcript ready",
    failed: "Analysis failed",
    cancelled: "Analysis cancelled",
    cancelling: "Cancelling…",
  };
  const MSG = {
    notConfigured: "AI analysis isn't set up: add OPENROUTER_API_KEY to the server's .env and restart SaveMe. "
      + "You can still transcribe and search this video.",
    lost: "This analysis is no longer on the server. Start it again.",
    noTranscript: "No transcript yet. Find highlights or Transcribe only makes one on this computer, or import subtitles below.",
    badTime: "Type a time like 1:05 or 65.5.",
    startAfterEnd: "The start must be before the end.",
    exportBusy: "Another export is running. Wait for it to finish, or cancel it.",
    noneSelected: "Select at least one highlight first.",
  };

  const el = {};
  for (const id of [
    "tab-manual", "tab-ai", "manual-panel", "ai-panel", "ai-status", "ai-test", "ai-test-msg", "ai-form",
    "ai-instruction", "ai-min", "ai-max", "ai-count", "ai-min-score", "ai-language", "ai-retranscribe",
    "ai-start", "ai-transcribe", "ai-estimate", "ai-form-msg", "ai-progress", "ai-progress-spin",
    "ai-progress-status", "ai-progress-percent", "ai-progress-bar", "ai-progress-fill", "ai-progress-detail",
    "ai-stages", "ai-cancel", "ai-results", "ai-results-count", "ai-usage", "ai-notice", "ai-error", "ai-bulk",
    "ai-select-all", "ai-select-none", "ai-add-selected", "ai-cards-msg", "ai-cards", "ai-transcript",
    "ai-transcript-summary", "ai-transcript-empty", "ai-transcript-body", "ai-search", "ai-search-count",
    "ai-lines", "ai-subs-file", "ai-subs-msg", "studio-video", "announcer", "clip-start",
  ]) {
    el[id.replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
  }

  let source = null; // { id, title, duration } while the studio has a ready video
  let gen = 0; // bumped when the source changes, so late replies are ignored
  let status = null; // GET /api/ai/status
  let analysisId = "";
  let view = null; // the latest analysis view
  let shown = null; // the view whose highlights are on screen: the latest ready one
  let readyId = ""; // its analysis, kept when a later run is cancelled or fails
  let running = false;
  let pollTimer = 0;
  let failures = 0;
  let estimateTimer = 0;
  let estimateSeq = 0;
  let edits = {}; // highlight id -> [start, end] as adjusted by the user
  let selected = new Set();
  let transcript = null; // { segments, language, origin } of the source
  let lastStage = ""; // the last stage seen running, to show where a run stopped

  const announce = (text) => setText(el.announcer, text);
  const fmtTime = (s) => bridge.fmtTime(s);
  const fmtUsd = (v) => (v >= 0.01 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`);
  const count = (n, one, many) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
  const sourceBase = () => `/api/sources/${encodeURIComponent(source.id)}`;

  // ---- tabs -------------------------------------------------------------------------------

  function selectTab(which, focus = false) {
    const ai = which === "ai";
    el.tabManual.setAttribute("aria-selected", String(!ai));
    el.tabAi.setAttribute("aria-selected", String(ai));
    el.tabManual.tabIndex = ai ? -1 : 0;
    el.tabAi.tabIndex = ai ? 0 : -1;
    show(el.manualPanel, !ai);
    show(el.aiPanel, ai);
    if (focus) (ai ? el.tabAi : el.tabManual).focus();
    store();
  }

  function onTabKey(event) {
    const keys = { ArrowLeft: "manual", ArrowRight: "ai", Home: "manual", End: "ai" };
    if (!(event.key in keys)) return;
    event.preventDefault();
    selectTab(keys[event.key], true);
  }

  // ---- storage ----------------------------------------------------------------------------

  function formState() {
    return {
      instruction: el.aiInstruction.value, min: el.aiMin.value, max: el.aiMax.value, count: el.aiCount.value,
      minScore: el.aiMinScore.value, language: el.aiLanguage.value,
    };
  }

  function store() {
    if (!source) return;
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        sourceId: source.id, analysisId, readyId, tab: el.tabAi.getAttribute("aria-selected") === "true" ? "ai" : "manual",
        form: formState(), edits, selected: [...selected],
      }));
    } catch {
      // Storage unavailable: everything works, it just doesn't survive a reload.
    }
  }

  function stored(sourceId) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
      if (saved && saved.sourceId === sourceId) return saved;
    } catch {
      // Unreadable: ignored.
    }
    return null;
  }

  function unstore() {
    try {
      sessionStorage.removeItem(STORE_KEY);
    } catch {
      // Nothing to clear.
    }
  }

  // ---- source -----------------------------------------------------------------------------

  function onSource(info) {
    if (info && source && info.id === source.id) { // the same video, now with a known length
      source.duration = info.duration;
      return;
    }
    const had = source;
    gen += 1;
    clearTimeout(pollTimer);
    clearTimeout(estimateTimer);
    source = info ? { ...info } : null;
    analysisId = "";
    view = null;
    shown = null;
    readyId = "";
    running = false;
    edits = {};
    selected = new Set();
    transcript = null;
    lastStage = "";
    show(el.aiProgress, false);
    show(el.aiResults, false);
    delete el.aiCards.dataset.analysis;
    setLine(el.aiFormMsg, "");
    setLine(el.aiTestMsg, "");
    setLine(el.aiSubsMsg, "");
    renderTranscript();
    if (!source) {
      if (had) unstore(); // the studio closed (not the first call, before any studio opened)
      selectTab("manual");
      return;
    }
    const saved = stored(source.id);
    if (saved) {
      restoreForm(saved.form || {});
      edits = saved.edits && typeof saved.edits === "object" ? saved.edits : {};
      selected = new Set(Array.isArray(saved.selected) ? saved.selected.filter((s) => typeof s === "string") : []);
      if (typeof saved.analysisId === "string" && JOB_ID_RE.test(saved.analysisId)) analysisId = saved.analysisId;
      if (typeof saved.readyId === "string" && JOB_ID_RE.test(saved.readyId)) readyId = saved.readyId;
      selectTab(saved.tab === "ai" ? "ai" : "manual");
    } else {
      unstore();
      selectTab("manual");
    }
    const g = gen;
    loadStatus(g);
    loadTranscript(g);
    restoreRuns(g);
  }

  // After a reload: the last ready highlights first, then the latest run (which may still be going).
  async function restoreRuns(g) {
    if (readyId && readyId !== analysisId) {
      try {
        const ready = await api(`/api/analyses/${encodeURIComponent(readyId)}`);
        if (g !== gen) return;
        if (str(ready.status) === "ready") {
          shown = ready;
          paintResults(ready, false);
        } else {
          readyId = "";
        }
      } catch {
        if (g !== gen) return;
        readyId = ""; // gone from the server
      }
    }
    if (analysisId) {
      running = true;
      paintButtons();
      poll(g, analysisId);
    }
  }

  function restoreForm(form) {
    if (typeof form.instruction === "string") el.aiInstruction.value = form.instruction.slice(0, 500);
    for (const [key, input] of [["min", el.aiMin], ["max", el.aiMax], ["minScore", el.aiMinScore]]) {
      if (typeof form[key] === "string" && /^\d{1,4}(\.\d+)?$/.test(form[key])) input.value = form[key];
    }
    el.aiCount.dataset.want = typeof form.count === "string" ? form.count : "";
    el.aiLanguage.dataset.want = typeof form.language === "string" ? form.language : "";
  }

  // ---- status and connection ---------------------------------------------------------------

  async function loadStatus(g) {
    try {
      status = status || await api("/api/ai/status");
    } catch (err) {
      if (g === gen) setLine(el.aiStatus, `Couldn't check the AI settings. ${err.message}`, "error");
      return;
    }
    if (g !== gen) return;
    paintStatus();
    fillChoices();
    paintButtons();
    scheduleEstimate();
  }

  function paintStatus() {
    if (!status.available) {
      const message = (status.error && str(status.error.message)) || "AI Highlights is turned off.";
      setLine(el.aiStatus, `AI Highlights is off: ${message} Fix the setting and restart SaveMe.`, "error");
    } else if (!status.configured) {
      setLine(el.aiStatus, MSG.notConfigured);
    } else {
      setLine(el.aiStatus, `AI model: ${str(status.model)} (through OpenRouter). Transcription: Whisper `
        + `${str(status.whisper_model)}, on this computer.`);
    }
    el.aiTest.disabled = !status.available || !status.configured;
  }

  function fillChoices() {
    const max = isNum(status.max_clips) && status.max_clips > 0 ? status.max_clips : 10;
    const want = Number(el.aiCount.dataset.want || el.aiCount.value || 5);
    const options = [];
    for (let n = 1; n <= max; n += 1) {
      const option = document.createElement("option");
      option.value = String(n);
      option.textContent = String(n);
      options.push(option);
    }
    el.aiCount.replaceChildren(...options);
    el.aiCount.value = String(Math.min(max, Math.max(1, Number.isInteger(want) ? want : 5)));
    if (Array.isArray(status.languages) && el.aiLanguage.options.length === 1) {
      for (const lang of status.languages) {
        if (!lang || typeof lang.code !== "string" || typeof lang.name !== "string") continue;
        const option = document.createElement("option");
        option.value = lang.code;
        option.textContent = lang.name;
        el.aiLanguage.append(option);
      }
    }
    const lang = el.aiLanguage.dataset.want;
    if (lang && [...el.aiLanguage.options].some((o) => o.value === lang)) el.aiLanguage.value = lang;
  }

  async function testConnection() {
    if (el.aiTest.disabled) return;
    const g = gen;
    el.aiTest.disabled = true;
    setLine(el.aiTestMsg, "Checking the key and the model with OpenRouter…");
    try {
      const result = await api("/api/ai/test", {});
      if (g !== gen) return;
      const model = result.model || {};
      const key = result.key || {};
      let text = `Connected. ${str(model.name) || str(model.id)}: $${model.prompt_usd_per_million} per million input `
        + `tokens, $${model.completion_usd_per_million} per million output tokens.`;
      if (isNum(key.usage_usd)) {
        text += ` This key has used ${fmtUsd(key.usage_usd)}${isNum(key.limit_usd) ? ` of its ${fmtUsd(key.limit_usd)} limit` : ""}.`;
      }
      setLine(el.aiTestMsg, text);
    } catch (err) {
      if (g === gen) setLine(el.aiTestMsg, err.message, "error");
    } finally {
      if (g === gen) paintStatus();
    }
  }

  // ---- form -------------------------------------------------------------------------------

  // The request body, or a message saying what to fix.
  function request(transcribeOnly) {
    const min = Number(el.aiMin.value);
    const max = Number(el.aiMax.value);
    const score = Number(el.aiMinScore.value || 0);
    for (const input of [el.aiMin, el.aiMax, el.aiMinScore]) input.removeAttribute("aria-invalid");
    let problem = "";
    let bad = null;
    if (!Number.isFinite(min) || min < 3 || min > 600) [problem, bad] = ["The shortest clip must be 3 to 600 seconds.", el.aiMin];
    else if (!Number.isFinite(max) || max < 5 || max > 900) [problem, bad] = ["The longest clip must be 5 to 900 seconds.", el.aiMax];
    else if (max < min + 1) [problem, bad] = ["The longest clip must be at least 1 second longer than the shortest.", el.aiMax];
    else if (!Number.isInteger(score) || score < 0 || score > 100) [problem, bad] = ["The minimum score must be a whole number from 0 to 100.", el.aiMinScore];
    if (problem) {
      bad.setAttribute("aria-invalid", "true");
      return { problem, field: bad };
    }
    return {
      body: {
        instruction: el.aiInstruction.value.trim().slice(0, 500), min_seconds: min, max_seconds: max,
        count: Number(el.aiCount.value) || 5, min_score: score, language: el.aiLanguage.value || "auto",
        retranscribe: el.aiRetranscribe.checked, transcribe_only: transcribeOnly,
      },
    };
  }

  function paintButtons() {
    const available = Boolean(status && status.available);
    const configured = available && status.configured;
    el.aiStart.disabled = !source || running || !configured;
    el.aiTranscribe.disabled = !source || running || !available;
    el.aiSubsFile.disabled = !source || running || !available;
    el.aiRetranscribe.disabled = running;
  }

  function scheduleEstimate() {
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(estimate, ESTIMATE_DELAY_MS);
  }

  async function estimate() {
    if (!source || !status || !status.available || !status.configured) {
      setLine(el.aiEstimate, "");
      return;
    }
    const built = request(false);
    if (!built.body) {
      setLine(el.aiEstimate, "");
      return;
    }
    const g = gen;
    const seq = ++estimateSeq;
    let result;
    try {
      result = await api(`${sourceBase()}/analyses/estimate`, built.body);
    } catch (err) {
      if (g === gen && seq === estimateSeq) setLine(el.aiEstimate, `Couldn't estimate the cost. ${err.message}`, "error");
      return;
    }
    if (g !== gen || seq !== estimateSeq) return;
    const basis = result.basis === "transcript" ? "from the transcript"
      : "from the video's length (checked again once it's transcribed)";
    const text = `Estimated cost: about ${fmtUsd(result.estimated_usd)}, at most ${fmtUsd(result.max_usd)}, `
      + `${basis}. Limit: ${fmtUsd(result.limit_usd)} per analysis.`;
    const over = result.within_limit === false && result.basis === "transcript";
    setLine(el.aiEstimate, over ? `${text} That's over the limit, so it won't run.` : text, over ? "error" : "");
  }

  // ---- analysis -----------------------------------------------------------------------------

  async function start(transcribeOnly) {
    const button = transcribeOnly ? el.aiTranscribe : el.aiStart;
    if (!source || running || button.disabled) return;
    const built = request(transcribeOnly);
    if (!built.body) {
      setLine(el.aiFormMsg, built.problem, "error");
      announce(built.problem);
      built.field.focus();
      return;
    }
    const g = gen;
    const hadFocus = document.activeElement === button;
    running = true;
    setLine(el.aiFormMsg, "");
    lastStage = "";
    paintButtons();
    paintProgress({ status: "starting", skipped: [] });
    if (hadFocus) el.aiProgress.focus(); // the button is now disabled: follow the progress instead
    let created;
    try {
      created = await api(`${sourceBase()}/analyses`, built.body);
    } catch (err) {
      if (g !== gen) return;
      running = false;
      show(el.aiProgress, false);
      paintButtons();
      setLine(el.aiFormMsg, err.message, "error");
      announce(err.message);
      if (hadFocus) button.focus();
      return;
    }
    if (g !== gen) return;
    if (typeof created.analysis_id !== "string" || !JOB_ID_RE.test(created.analysis_id)) {
      running = false;
      show(el.aiProgress, false);
      paintButtons();
      setLine(el.aiFormMsg, "SaveMe sent an unexpected reply. Please try again.", "error");
      return;
    }
    analysisId = created.analysis_id;
    failures = 0;
    store();
    announce(transcribeOnly ? "Transcription started." : "Looking for highlights.");
    poll(g, analysisId);
  }

  async function poll(g, id) {
    if (g !== gen || id !== analysisId) return;
    let next;
    try {
      next = await api(`/api/analyses/${encodeURIComponent(id)}`);
    } catch (err) {
      if (g !== gen || id !== analysisId) return;
      if (err.code === "analysis_not_found") {
        ended({ status: "failed", error: { message: MSG.lost } });
        return;
      }
      if (err.status >= 400 && err.status < 500 && err.status !== 429) {
        ended({ status: "failed", error: { message: err.message } });
        return;
      }
      failures += 1;
      if (failures >= MAX_POLL_FAILURES) {
        ended({ status: "failed", error: { message: "Can't reach SaveMe. Check that it's running, then reload this page." } });
        return;
      }
      pollTimer = setTimeout(() => poll(g, id), RETRY_MS);
      return;
    }
    if (g !== gen || id !== analysisId) return;
    failures = 0;
    if (PENDING.includes(str(next.status))) {
      running = true;
      paintButtons();
      paintProgress(next);
      pollTimer = setTimeout(() => poll(g, id), POLL_MS);
    } else {
      ended(next);
    }
  }

  function ended(next) {
    clearTimeout(pollTimer);
    running = false;
    view = next;
    paintButtons();
    paintProgress(next);
    const state = str(next.status);
    const message = (next.error && str(next.error.message)) || "";
    if (state === "ready") {
      const onlyTranscript = Boolean(next.options && next.options.transcribe_only);
      if (!onlyTranscript) {
        shown = next;
        readyId = str(next.analysis_id) || analysisId;
      }
      paintResults(shown || next, onlyTranscript && !shown);
      if (source) loadTranscript(gen, onlyTranscript);
      const found = Array.isArray(next.candidates) ? next.candidates.length : 0;
      announce(onlyTranscript ? "Transcript ready." : `${count(found, "highlight", "highlights")} found.`);
    } else {
      if (shown) paintResults(shown, false); // the earlier highlights are still good
      else show(el.aiResults, false);
      announce([state === "cancelled" ? HEADLINE.cancelled : HEADLINE.failed, message].filter(Boolean).join(". "));
    }
    scheduleEstimate();
    store();
  }

  async function cancel() {
    if (!analysisId || el.aiCancel.disabled) return;
    const g = gen;
    const id = analysisId;
    clearTimeout(pollTimer);
    el.aiCancel.disabled = true;
    setText(el.aiProgressStatus, HEADLINE.cancelling);
    try {
      const next = await api(`/api/analyses/${encodeURIComponent(id)}/cancel`, {});
      if (g === gen && id === analysisId) ended(next);
    } catch (err) {
      if (g !== gen || id !== analysisId) return;
      setText(el.aiProgressDetail, `Couldn't cancel. ${err.message}`);
      show(el.aiProgressDetail, true);
      pollTimer = setTimeout(() => poll(g, id), 0);
    }
  }

  function setBar(value, text) {
    if (isNum(value)) {
      const pct = Math.min(100, Math.max(0, value));
      delete el.aiProgressFill.dataset.indeterminate;
      el.aiProgressFill.style.width = `${pct}%`;
      el.aiProgressBar.setAttribute("aria-valuenow", String(Math.floor(pct)));
    } else {
      el.aiProgressFill.dataset.indeterminate = "";
      el.aiProgressFill.style.width = "100%";
      el.aiProgressBar.removeAttribute("aria-valuenow");
    }
    el.aiProgressBar.setAttribute("aria-valuetext", text);
  }

  function paintProgress(v) {
    const state = str(v.status);
    const pending = state === "starting" || PENDING.includes(state);
    const transcribeOnly = Boolean(v.options && v.options.transcribe_only);
    const headline = state === "ready" && transcribeOnly ? HEADLINE.transcribed : HEADLINE[state] || HEADLINE.queued;
    const skipped = Array.isArray(v.skipped) ? v.skipped : [];
    if (STAGES.includes(state) && state !== "ready") lastStage = state;
    const stopped = state === "failed" || state === "cancelled";
    // A stopped run shows the stages it finished and the one it stopped in.
    const at = stopped ? STAGES.indexOf(lastStage) : STAGES.indexOf(state);
    const message = (v.error && str(v.error.message)) || "";
    show(el.aiProgress, true);
    setText(el.aiProgressStatus, headline);
    show(el.aiProgressSpin, pending);
    const pct = isNum(v.progress_percent) ? v.progress_percent : null;
    setText(el.aiProgressPercent, pending && pct !== null ? `${Math.floor(pct)}%` : "");
    show(el.aiProgressBar, pending);
    if (pending) setBar(pct, str(v.detail) || headline);
    const detail = state === "failed" || state === "cancelled" ? message || (state === "cancelled" ? "Nothing more was sent or kept." : "")
      : str(v.detail);
    setText(el.aiProgressDetail, detail);
    show(el.aiProgressDetail, Boolean(detail));
    el.aiProgressDetail.dataset.tone = state === "failed" ? "error" : "";
    for (const item of el.aiStages.querySelectorAll("[data-stage]")) {
      const key = item.dataset.stage;
      const index = STAGES.indexOf(key);
      let mark = "pending";
      let note = "";
      if (transcribeOnly && (key === "analyzing" || key === "ranking")) [mark, note] = ["skipped", " (not needed)"];
      else if (skipped.includes(key)) [mark, note] = ["skipped", " (saved transcript reused)"];
      else if (state === "ready") [mark, note] = ["done", " (done)"];
      else if (at >= 0 && index < at) [mark, note] = ["done", " (done)"];
      else if (index === at && stopped) [mark, note] = state === "failed" ? ["failed", " (failed)"] : ["pending", " (stopped)"];
      else if (index === at) [mark, note] = ["active", " (in progress)"];
      item.dataset.state = mark;
      setText(item.querySelector("[data-note]"), note);
    }
    el.aiCancel.disabled = state === "starting";
    show(el.aiCancel, pending);
  }

  // ---- results -------------------------------------------------------------------------------

  function rangeOf(h) {
    const edit = edits[h.id];
    if (Array.isArray(edit) && isNum(edit[0]) && isNum(edit[1]) && edit[1] > edit[0]) return { start: edit[0], end: edit[1] };
    return { start: h.start, end: h.end };
  }

  function paintResults(v, onlyTranscript) {
    if (onlyTranscript) {
      show(el.aiResults, false);
      return;
    }
    if (el.aiCards.dataset.analysis && el.aiCards.dataset.analysis !== str(v.analysis_id)) {
      edits = {}; // edits and choices belong to the highlights they were made on
      selected = new Set();
    }
    el.aiCards.dataset.analysis = str(v.analysis_id);
    const highlights = (Array.isArray(v.candidates) ? v.candidates : []).filter(
      (h) => h && typeof h.id === "string" && isNum(h.start) && isNum(h.end) && h.end > h.start);
    show(el.aiResults, true);
    setText(el.aiResultsCount, highlights.length ? count(highlights.length, "highlight", "highlights") : "");
    const usage = v.usage;
    if (usage && isNum(usage.cost_usd)) {
      const how = usage.cost_reported ? "reported by OpenRouter" : "estimated from the token counts";
      setLine(el.aiUsage, `Cost ${fmtUsd(usage.cost_usd)} (${how}) · ${count(usage.prompt_tokens || 0, "token", "tokens")} in, `
        + `${(usage.completion_tokens || 0).toLocaleString("en-US")} out · ${count(usage.calls || 0, "request", "requests")}`);
    } else {
      setLine(el.aiUsage, "");
    }
    setLine(el.aiNotice, str(v.notice));
    setLine(el.aiError, "");
    show(el.aiBulk, highlights.length > 0);
    setLine(el.aiCardsMsg, "");
    selected = new Set([...selected].filter((id) => highlights.some((h) => h.id === id)));
    el.aiCards.replaceChildren(...highlights.map((h, i) => card(h, i + 1)));
  }

  const BUTTON = "h-10 shrink-0 rounded-lg px-3 text-sm font-semibold focus-visible:outline-2 focus-visible:outline-offset-2 "
    + "focus-visible:outline-brand-700 disabled:cursor-not-allowed disabled:opacity-60 dark:focus-visible:outline-brand-400";
  const SOFT = "bg-brand-50 text-brand-700 hover:bg-brand-100 dark:bg-white/10 dark:text-ink-50 dark:hover:bg-white/15";
  const OUTLINE = "border border-ink-300 text-ink-700 hover:border-brand-700/50 hover:text-brand-700 dark:border-ink-700 "
    + "dark:text-ink-200 dark:hover:border-brand-400/60 dark:hover:text-brand-300";
  const TIME_INPUT = "h-10 w-24 min-w-0 rounded-lg border border-ink-300 bg-white px-2 text-sm tabular-nums text-ink-900 "
    + "focus-visible:border-brand-700 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-brand-700 "
    + "aria-invalid:border-red-500 dark:border-ink-700 dark:bg-ink-950 dark:text-ink-50 dark:focus-visible:border-brand-400 "
    + "dark:focus-visible:outline-brand-400";

  function node(tag, className, text) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function button(text, label, extra, onClick) {
    const b = node("button", `${BUTTON} ${extra}`, text);
    b.type = "button";
    b.setAttribute("aria-label", label);
    b.addEventListener("click", onClick);
    return b;
  }

  // One highlight: select box, rank, score, title, editable times, summary, reason, tags, actions.
  function card(h, n) {
    const title = str(h.title) || "Untitled highlight";
    const item = node("li", "rounded-xl border border-ink-200 p-3 sm:p-4 dark:border-ink-800");
    item.dataset.highlight = h.id;

    const top = node("div", "flex items-start gap-3");
    const check = node("input", "mt-1 size-4 shrink-0 accent-brand-700 dark:accent-brand-400");
    check.type = "checkbox";
    check.id = `ai-${h.id}-select`;
    check.checked = selected.has(h.id);
    check.setAttribute("aria-label", `Select highlight ${n}: ${title}`);
    check.addEventListener("change", () => {
      if (check.checked) selected.add(h.id);
      else selected.delete(h.id);
      store();
    });
    const heading = node("div", "min-w-0 flex-1");
    const meta = node("p", "flex flex-wrap items-center gap-2 text-xs text-ink-600 dark:text-ink-400");
    meta.append(node("span", "font-semibold tabular-nums", `#${n}`));
    const score = node("span", "rounded-md bg-brand-100 px-1.5 py-0.5 font-bold tabular-nums text-brand-700 dark:bg-brand-500/20 dark:text-brand-300",
      `Score ${isNum(h.score) ? Math.round(h.score) : "–"}`);
    meta.append(score);
    const when = node("span", "tabular-nums");
    meta.append(when);
    heading.append(meta, node("h4", "mt-1 font-semibold break-words", title));
    top.append(check, heading);

    const times = node("div", "mt-3 flex flex-wrap items-end gap-3");
    const fields = {};
    for (const which of ["start", "end"]) {
      const wrap = node("div");
      const label = node("label", "block text-xs font-semibold text-ink-700 dark:text-ink-300", which === "start" ? "Start" : "End");
      const input = node("input", `mt-1 ${TIME_INPUT}`);
      input.type = "text";
      input.inputMode = "decimal";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.id = `ai-${h.id}-${which}`;
      label.htmlFor = input.id;
      wrap.append(label, input);
      times.append(wrap);
      fields[which] = input;
    }
    const length = node("span", "pb-2.5 text-sm tabular-nums text-ink-600 dark:text-ink-400");
    times.append(length);
    const msg = node("p", "mt-2 text-sm font-medium text-red-700 dark:text-red-400");
    msg.hidden = true;
    msg.id = `ai-${h.id}-msg`;
    fields.start.setAttribute("aria-describedby", msg.id);
    fields.end.setAttribute("aria-describedby", msg.id);

    function paintTimes() {
      const r = rangeOf(h);
      if (document.activeElement !== fields.start) fields.start.value = fmtTime(r.start);
      if (document.activeElement !== fields.end) fields.end.value = fmtTime(r.end);
      setText(length, `Length ${fmtTime(r.end - r.start)}`);
      setText(when, `${fmtTime(r.start)} – ${fmtTime(r.end)}`);
    }

    function fromField(which) {
      const input = fields[which];
      const value = bridge.parseTime(input.value);
      const r = rangeOf(h);
      const next = { ...r, [which]: value };
      let problem = value === null ? MSG.badTime : "";
      const top = source && source.duration > 0 ? source.duration : Infinity;
      if (!problem && Math.round(next.end * 1000) - Math.round(next.start * 1000) < Math.round(bridge.minClip * 1000)) problem = MSG.startAfterEnd;
      if (!problem && next.end > top + 0.05) problem = `The video is only ${fmtTime(top)} long.`;
      fields.start.removeAttribute("aria-invalid");
      fields.end.removeAttribute("aria-invalid");
      if (problem) {
        input.setAttribute("aria-invalid", "true");
        setText(msg, problem);
        msg.hidden = false;
        announce(problem);
        return;
      }
      msg.hidden = true;
      edits[h.id] = [Math.round(next.start * 1000) / 1000, Math.round(Math.min(next.end, top) * 1000) / 1000];
      store();
      input.value = fmtTime(edits[h.id][which === "start" ? 0 : 1]);
      paintTimes();
      announce(`Highlight ${n} now ${fmtTime(edits[h.id][0])} to ${fmtTime(edits[h.id][1])}.`);
    }
    for (const which of ["start", "end"]) {
      fields[which].addEventListener("change", () => fromField(which));
      fields[which].addEventListener("keydown", (e) => {
        if (e.key === "Enter") fromField(which);
      });
    }
    paintTimes();

    const body = node("div", "mt-3 space-y-1 text-sm");
    if (str(h.summary)) body.append(node("p", "break-words text-ink-900 dark:text-ink-50", str(h.summary)));
    if (str(h.reason)) body.append(node("p", "break-words text-ink-600 dark:text-ink-400", `Why it works: ${str(h.reason)}`));
    const tags = Array.isArray(h.tags) ? h.tags.filter((t) => typeof t === "string" && t.trim()) : [];
    if (tags.length) {
      const list = node("ul", "mt-2 flex flex-wrap gap-1.5");
      list.setAttribute("aria-label", "Tags");
      for (const tag of tags) list.append(node("li", "rounded-md bg-ink-100 px-1.5 py-0.5 text-xs text-ink-700 dark:bg-white/10 dark:text-ink-300", tag));
      body.append(list);
    }

    const actions = node("div", "mt-3 flex flex-wrap gap-2");
    actions.append(
      button("Preview", `Preview highlight ${n}`, SOFT, () => preview(h)),
      button("Load into editor", `Load highlight ${n} into the editor`, OUTLINE, () => loadIntoEditor(h, n)),
      button("Export", `Export highlight ${n} as a clip`, OUTLINE, () => exportOne(h, n)),
    );
    item.append(top, times, msg, body, actions);
    return item;
  }

  function showVideo() {
    const box = el.studioVideo.getBoundingClientRect();
    if (box.top < 0 || box.bottom > window.innerHeight) el.studioVideo.scrollIntoView({ block: "nearest" });
  }

  function preview(h) {
    const r = rangeOf(h);
    if (bridge.loadRange(r.start, r.end, true)) {
      showVideo();
      announce(`Playing ${fmtTime(r.start)} to ${fmtTime(r.end)}.`);
    }
  }

  function loadIntoEditor(h, n) {
    const r = rangeOf(h);
    if (!bridge.loadRange(r.start, r.end)) return;
    selectTab("manual");
    el.clipStart.focus();
    announce(`Highlight ${n} loaded into the editor: ${fmtTime(r.start)} to ${fmtTime(r.end)}.`);
  }

  function exportOne(h, n) {
    const r = rangeOf(h);
    if (bridge.isExporting() || !bridge.exportRange(r.start, r.end)) {
      setLine(el.aiCardsMsg, MSG.exportBusy, "error");
      announce(MSG.exportBusy);
      return;
    }
    setLine(el.aiCardsMsg, `Exporting highlight ${n}. Its progress shows under the clip list.`);
  }

  function setAll(on) {
    const highlights = shown && Array.isArray(shown.candidates) ? shown.candidates : [];
    selected = new Set(on ? highlights.map((h) => h.id) : []);
    for (const box of el.aiCards.querySelectorAll("input[type=checkbox]")) box.checked = on;
    store();
    announce(on ? "All highlights selected." : "No highlights selected.");
  }

  function addSelected() {
    const highlights = shown && Array.isArray(shown.candidates) ? shown.candidates : [];
    const chosen = highlights.filter((h) => selected.has(h.id));
    if (!chosen.length) {
      setLine(el.aiCardsMsg, MSG.noneSelected, "error");
      announce(MSG.noneSelected);
      return;
    }
    const result = bridge.addRanges(chosen.map(rangeOf));
    const parts = [result.added ? `Added ${count(result.added, "clip", "clips")} to the clip list.` : "No clips were added."];
    if (result.duplicates) parts.push(`${count(result.duplicates, "was", "were")} already in it.`.replace(/^1 was/, "1 was").replace(/^(\d+) were/, "$1 were"));
    if (result.full) parts.push(`${count(result.full, "clip", "clips")} didn't fit: the list holds up to ${result.max}.`);
    const text = parts.join(" ");
    setLine(el.aiCardsMsg, text, result.added ? "" : "error");
    announce(text);
  }

  // ---- transcript ------------------------------------------------------------------------------

  async function loadTranscript(g, open = false) {
    let data;
    try {
      data = await api(`${sourceBase()}/transcript`);
    } catch (err) {
      if (g !== gen) return;
      transcript = null;
      renderTranscript();
      setText(el.aiTranscriptEmpty, `Couldn't load the transcript. ${err.message}`);
      return;
    }
    if (g !== gen) return;
    const found = data.transcript && typeof data.transcript === "object" ? data.transcript : null;
    transcript = found && {
      origin: str(found.origin), language: str(found.language),
      segments: (Array.isArray(found.segments) ? found.segments : []).filter(
        (s) => s && isNum(s.start) && isNum(s.end) && typeof s.text === "string"),
    };
    renderTranscript();
    if (open) el.aiTranscript.open = true;
  }

  function languageName(code) {
    const lang = status && Array.isArray(status.languages) ? status.languages.find((l) => l && l.code === code) : null;
    return lang ? lang.name : "";
  }

  function renderTranscript() {
    const has = Boolean(transcript);
    show(el.aiTranscriptBody, has);
    show(el.aiTranscriptEmpty, !has || !transcript.segments.length);
    if (!has) {
      setText(el.aiTranscriptSummary, "Transcript");
      setText(el.aiTranscriptEmpty, MSG.noTranscript);
      el.aiLines.replaceChildren();
      return;
    }
    const from = transcript.origin === "subtitles" ? "from subtitles" : "made on this computer";
    const lang = languageName(transcript.language);
    setText(el.aiTranscriptSummary, `Transcript · ${lang ? `${lang} · ` : ""}${count(transcript.segments.length, "line", "lines")}, ${from}`);
    if (!transcript.segments.length) setText(el.aiTranscriptEmpty, "No speech was found in this video.");
    filterTranscript();
  }

  function filterTranscript() {
    if (!transcript) return;
    const query = el.aiSearch.value.trim().toLowerCase();
    const rows = [];
    let matches = 0;
    for (const segment of transcript.segments) {
      if (query && !segment.text.toLowerCase().includes(query)) continue;
      matches += 1;
      const row = node("li", "flex items-start gap-3 py-1.5");
      const time = button(fmtTime(segment.start), `Play from ${fmtTime(segment.start)}`,
        "h-8 px-2 tabular-nums text-brand-700 hover:bg-brand-50 dark:text-brand-300 dark:hover:bg-white/10", () => {
          bridge.seek(segment.start);
          showVideo();
        });
      row.append(time, node("span", "min-w-0 flex-1 pt-1 text-sm break-words", segment.text));
      rows.push(row);
    }
    el.aiLines.replaceChildren(...rows);
    setText(el.aiSearchCount, query ? (matches ? `${count(matches, "line matches", "lines match")}.` : "No lines match.") : "");
  }

  async function importSubtitles() {
    const file = el.aiSubsFile.files && el.aiSubsFile.files[0];
    el.aiSubsFile.value = "";
    if (!file || !source) return;
    if (file.size > MAX_SUBTITLE_BYTES) {
      setLine(el.aiSubsMsg, "Subtitle files can be up to 4 MB.", "error");
      return;
    }
    const g = gen;
    setLine(el.aiSubsMsg, "Importing…");
    let res;
    let data = null;
    try {
      const language = encodeURIComponent(el.aiLanguage.value || "auto");
      res = await fetch(`${sourceBase()}/transcript?language=${language}`, {
        method: "POST", body: file, cache: "no-store",
        headers: { "Content-Type": "text/plain", Accept: "application/json", "X-SaveMe-Filename": encodeURIComponent(file.name) },
      });
      try {
        data = await res.json();
      } catch {
        // Not JSON; handled below.
      }
    } catch {
      if (g === gen) setLine(el.aiSubsMsg, "Can't reach SaveMe. Check that it's running, then try again.", "error");
      return;
    }
    if (g !== gen) return;
    if (!res.ok) {
      setLine(el.aiSubsMsg, errorFrom(res.status, data).message, "error");
      return;
    }
    const lines = data && isNum(data.segments) ? data.segments : 0;
    setLine(el.aiSubsMsg, `Imported ${count(lines, "subtitle line", "subtitle lines")}. Find highlights will use them instead of transcribing.`);
    announce("Subtitles imported.");
    loadTranscript(g, true);
    scheduleEstimate();
  }

  // ---- wiring ----------------------------------------------------------------------------------

  el.tabManual.addEventListener("click", () => selectTab("manual"));
  el.tabAi.addEventListener("click", () => selectTab("ai"));
  el.tabManual.addEventListener("keydown", onTabKey);
  el.tabAi.addEventListener("keydown", onTabKey);
  el.aiTest.addEventListener("click", testConnection);
  el.aiForm.addEventListener("submit", (event) => event.preventDefault()); // Enter in a field never starts a paid run
  el.aiStart.addEventListener("click", () => start(false));
  el.aiTranscribe.addEventListener("click", () => start(true));
  el.aiCancel.addEventListener("click", cancel);
  for (const input of [el.aiInstruction, el.aiMin, el.aiMax, el.aiCount, el.aiMinScore, el.aiLanguage, el.aiRetranscribe]) {
    input.addEventListener("change", () => {
      store();
      scheduleEstimate();
    });
  }
  el.aiSelectAll.addEventListener("click", () => setAll(true));
  el.aiSelectNone.addEventListener("click", () => setAll(false));
  el.aiAddSelected.addEventListener("click", addSelected);
  el.aiSearch.addEventListener("input", filterTranscript);
  el.aiSubsFile.addEventListener("change", importSubtitles);
  paintButtons();
  bridge.onSource(onSource);
})();
