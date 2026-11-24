// SaveMe Make shorts, finding moments: steps 1 and 2 of Clip Studio's Make shorts tab. Transcribe the
// open video on the server (locally, with faster-whisper), ask OpenRouter for moments in the transcript,
// then show them as cards to pick from. shorts.js makes the picked ones into shorts. Plain browser
// JavaScript; shared helpers come from common.js (window.SaveMe), and the studio's editor and export from
// studio.js (window.SaveMeStudioBridge). Text is set with textContent only: titles, summaries and
// transcript lines come from an AI model and a video, so they are never treated as HTML.
//
// An analysis runs on the server (POST /api/sources/{id}/analyses) and is followed by polling
// GET /api/analyses/{id}: queued → preparing → extracting → transcribing → analyzing → ranking →
// ready (or failed / cancelled). sessionStorage keeps the analysis, the form, the tab, the ticks and
// the edits, so a reload comes back to the same moments.
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
  const PENDING = ["queued", "preparing", "extracting", "transcribing", "analyzing", "ranking"];
  const HEADLINE = {
    starting: "Starting…",
    queued: "Waiting for the AI to be free",
    preparing: "Preparing the video",
    extracting: "Extracting audio",
    transcribing: "Transcribing on this computer",
    analyzing: "Analyzing the transcript",
    ranking: "Ranking moments",
    ready: "Moments ready",
    transcribed: "Transcript ready",
    failed: "Analysis failed",
    cancelled: "Analysis cancelled",
    cancelling: "Cancelling…",
  };
  const MSG = {
    notConfigured: "Finding moments needs an OpenRouter key: add OPENROUTER_API_KEY to the server's .env and restart "
      + "SaveMe (see docs/OPENROUTER_SETUP.md). You can still make shorts from clips you cut by hand in Manual.",
    lost: "This analysis is no longer on the server. Start it again.",
    noTranscript: "No transcript yet. Find moments or Transcribe only makes one on this computer, or import subtitles below.",
    badTime: "Type a time like 1:05 or 65.5.",
    startAfterEnd: "The start must be before the end.",
    exportBusy: "Another export is running. Wait for it to finish, or cancel it.",
  };

  const el = {};
  for (const id of [
    "tab-manual", "tab-make", "manual-panel", "make-panel", "ai-status", "ai-model", "ai-test", "ai-test-msg", "ai-form",
    "ai-instruction", "ai-min", "ai-max", "ai-count", "ai-min-score", "ai-language", "ai-retranscribe",
    "ai-start", "ai-transcribe", "ai-estimate", "ai-form-msg", "ai-progress", "ai-progress-spin",
    "ai-progress-status", "ai-progress-percent", "ai-progress-bar", "ai-progress-fill", "ai-progress-detail",
    "ai-cancel", "ai-results", "ai-results-count", "ai-usage", "ai-notice", "ai-error", "ai-select-all",
    "make-no-moments", "ai-cards-msg", "ai-cards", "ai-transcript",
    "ai-transcript-summary", "ai-transcript-empty", "ai-transcript-body", "ai-search", "ai-search-count",
    "ai-lines", "ai-subs-file", "ai-subs-msg", "studio-video", "announcer",
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
  let overLimit = false; // the estimate from the transcript is over the cost limit: Find moments won't run
  let edits = {}; // highlight id -> [start, end] as adjusted by the user
  let selected = new Set(); // the ticked moments: these are made into shorts
  let pickedFor = ""; // the analysis whose moments the ticks and edits belong to
  let making = false; // shorts are being made (shorts.js): Find moments waits
  let transcript = null; // { segments, language, origin } of the source

  const announce = (text) => setText(el.announcer, text);
  const fmtTime = (s) => bridge.fmtTime(s);
  const fmtUsd = (v) => (v >= 0.01 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`);
  const count = (n, one, many) => `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
  const sourceBase = () => `/api/sources/${encodeURIComponent(source.id)}`;

  // ---- tabs -------------------------------------------------------------------------------

  // The studio's two tabs: Manual and Make shorts (ai.js and shorts.js fill the second).
  const TABS = ["manual", "make"];
  const OLD_TABS = { ai: "make", shorts: "make" }; // the tab names stored before Make shorts

  function tabParts(name) {
    return name === "manual" ? [el.tabManual, el.manualPanel] : [el.tabMake, el.makePanel];
  }

  function currentTab() {
    return TABS.find((name) => tabParts(name)[0].getAttribute("aria-selected") === "true") || "manual";
  }

  function selectTab(which, focus = false) {
    const chosen = TABS.includes(which) ? which : "manual";
    for (const name of TABS) {
      const [tab, panel] = tabParts(name);
      const on = name === chosen;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      show(panel, on);
    }
    if (focus) tabParts(chosen)[0].focus();
    store();
  }

  function onTabKey(event) {
    const at = TABS.indexOf(currentTab());
    const keys = { ArrowLeft: TABS[(at + TABS.length - 1) % TABS.length], ArrowRight: TABS[(at + 1) % TABS.length],
      Home: TABS[0], End: TABS[TABS.length - 1] };
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
        sourceId: source.id, analysisId, readyId, pickedFor, tab: currentTab(),
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
    pickedFor = "";
    transcript = null;
    show(el.aiProgress, false);
    show(el.aiResults, false);
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
      if (typeof saved.pickedFor === "string" && JOB_ID_RE.test(saved.pickedFor)) pickedFor = saved.pickedFor;
      const tab = OLD_TABS[saved.tab] || saved.tab;
      selectTab(TABS.includes(tab) ? tab : "manual");
    } else {
      unstore();
      selectTab("manual");
    }
    const g = gen;
    publish();
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

  // Step 1 says something only when Find moments can't run; the model line lives under More settings.
  function paintStatus() {
    const whisper = `Transcription: Whisper ${str(status.whisper_model)}, on this computer.`;
    if (!status.available) {
      const message = (status.error && str(status.error.message)) || "Finding moments is turned off.";
      setLine(el.aiStatus, `Finding moments is off: ${message} Fix the setting and restart SaveMe (see `
        + "docs/OPENROUTER_SETUP.md). You can still make shorts from clips you cut by hand in Manual.", "error");
      setText(el.aiModel, "");
    } else if (!status.configured) {
      setLine(el.aiStatus, MSG.notConfigured, "error");
      setText(el.aiModel, whisper);
    } else {
      setLine(el.aiStatus, "");
      setText(el.aiModel, `AI model: ${str(status.model)} (through OpenRouter). ${whisper}`);
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
    if (!Number.isFinite(min) || min < 3 || min > 600) [problem, bad] = ["The shortest moment must be 3 to 600 seconds.", el.aiMin];
    else if (!Number.isFinite(max) || max < 5 || max > 900) [problem, bad] = ["The longest moment must be 5 to 900 seconds.", el.aiMax];
    else if (max < min + 1) [problem, bad] = ["The longest moment must be at least 1 second longer than the shortest.", el.aiMax];
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
    el.aiStart.disabled = !source || running || making || overLimit || !configured;
    el.aiTranscribe.disabled = !source || running || making || !available;
    el.aiSubsFile.disabled = !source || running || making || !available;
    el.aiRetranscribe.disabled = running;
  }

  function scheduleEstimate() {
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(estimate, ESTIMATE_DELAY_MS);
  }

  function setOverLimit(on) {
    if (overLimit === on) return;
    overLimit = on;
    paintButtons();
  }

  async function estimate() {
    if (!source || !status || !status.available || !status.configured) {
      setLine(el.aiEstimate, "");
      setOverLimit(false);
      return;
    }
    const built = request(false);
    if (!built.body) {
      setLine(el.aiEstimate, "");
      setOverLimit(false);
      return;
    }
    const g = gen;
    const seq = ++estimateSeq;
    let result;
    try {
      result = await api(`${sourceBase()}/analyses/estimate`, built.body);
    } catch (err) {
      if (g === gen && seq === estimateSeq) {
        setLine(el.aiEstimate, `Couldn't estimate the cost. ${err.message}`, "error");
        setOverLimit(false);
      }
      return;
    }
    if (g !== gen || seq !== estimateSeq) return;
    const text = `Estimated cost: about ${fmtUsd(result.estimated_usd)} (at most ${fmtUsd(result.max_usd)}).`;
    const over = result.within_limit === false && result.basis === "transcript";
    setLine(el.aiEstimate, over ? `${text} That's over the ${fmtUsd(result.limit_usd)} limit, so it won't run.` : text,
      over ? "error" : "");
    setOverLimit(over);
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
    paintButtons();
    paintProgress({ status: "starting" });
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
    announce(transcribeOnly ? "Transcription started." : "Looking for moments.");
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
      announce(onlyTranscript ? "Transcript ready." : `${count(found, "moment", "moments")} found.`);
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
    el.aiCancel.disabled = state === "starting";
    show(el.aiCancel, pending);
  }

  // ---- results -------------------------------------------------------------------------------

  function rangeOf(h) {
    const edit = edits[h.id];
    if (Array.isArray(edit) && isNum(edit[0]) && isNum(edit[1]) && edit[1] > edit[0]) return { start: edit[0], end: edit[1] };
    return { start: h.start, end: h.end };
  }

  // A view's moments that can be shown, in rank order.
  function momentsOf(v) {
    return (v && Array.isArray(v.candidates) ? v.candidates : []).filter(
      (h) => h && typeof h.id === "string" && isNum(h.start) && isNum(h.end) && h.end > h.start);
  }

  function moments() {
    return momentsOf(shown);
  }

  // Tell shorts.js which moments are on screen and which are ticked, with their (edited) times and titles.
  function publish() {
    const picks = shown ? moments().filter((h) => selected.has(h.id)).map((h) => ({ ...rangeOf(h), title: str(h.title) })) : [];
    bridge.setMoments({ shown: Boolean(shown), picks });
  }

  function paintResults(v, onlyTranscript) {
    if (onlyTranscript) {
      show(el.aiResults, false);
      publish();
      return;
    }
    const highlights = momentsOf(v);
    if (str(v.analysis_id) !== pickedFor) { // new moments: all ticked, and the old edits don't apply to them
      pickedFor = str(v.analysis_id);
      edits = {};
      selected = new Set(highlights.map((h) => h.id));
      store();
    }
    show(el.aiResults, true);
    setText(el.aiResultsCount, highlights.length ? count(highlights.length, "moment", "moments") : "");
    show(el.makeNoMoments, highlights.length === 0);
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
    show(el.aiSelectAll, highlights.length > 0);
    setLine(el.aiCardsMsg, "");
    selected = new Set([...selected].filter((id) => highlights.some((h) => h.id === id)));
    el.aiCards.replaceChildren(...highlights.map((h, i) => card(h, i + 1)));
    paintSelectAll();
    publish();
  }

  // One button that selects every moment, or none once they're all selected.
  function paintSelectAll() {
    const all = moments();
    setText(el.aiSelectAll, all.length && all.every((h) => selected.has(h.id)) ? "Select none" : "Select all");
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

  const SCORE_NOTE = "Scores are the AI's editorial judgement of each moment, not a prediction of views.";
  const SUMMARY = "inline-flex h-10 cursor-pointer list-none items-center gap-1.5 rounded-lg px-2 text-sm font-semibold "
    + "text-ink-700 hover:text-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-700 "
    + "dark:text-ink-300 dark:hover:text-brand-300 dark:focus-visible:outline-brand-400 [&::-webkit-details-marker]:hidden";
  const BADGE = "rounded-md bg-brand-50 px-1.5 py-0.5 font-semibold text-brand-700 dark:bg-white/10 dark:text-ink-50";
  // The same key shorts.js gives a range, so it can show "Captions edited" on this card (it keeps the edits).
  const keyOf = (r) => `${Math.round(r.start * 1000)}-${Math.round(r.end * 1000)}`;

  function chevron() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("class", "size-4 shrink-0 transition-transform group-open:rotate-180 motion-reduce:transition-none");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "m6 9 6 6 6-6");
    svg.append(path);
    return svg;
  }

  // One moment. The face: tick box, title, rank and times, a two-line summary and Play. Behind More: the
  // times to change, why it was picked, the score, the tags, Edit captions and Save plain cut.
  function card(h, n) {
    const title = str(h.title) || "Untitled moment";
    const item = node("li", "rounded-xl border border-ink-200 p-3 sm:p-4 dark:border-ink-800");
    item.dataset.highlight = h.id;

    const top = node("div", "flex items-start gap-3");
    const check = node("input", "mt-1 size-4 shrink-0 accent-brand-700 dark:accent-brand-400");
    check.type = "checkbox";
    check.id = `ai-${h.id}-select`;
    check.checked = selected.has(h.id);
    check.setAttribute("aria-label", `Make moment ${n} into a short: ${title}`);
    check.addEventListener("change", () => {
      if (check.checked) selected.add(h.id);
      else selected.delete(h.id);
      paintSelectAll();
      store();
      publish();
    });
    const face = node("div", "min-w-0 flex-1");
    const when = node("p", "mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs tabular-nums text-ink-600 dark:text-ink-400");
    const whenText = node("span");
    const badge = node("span", BADGE, "Captions edited");
    badge.hidden = true;
    when.append(whenText, badge);
    face.append(node("h4", "font-semibold break-words", title), when);
    if (str(h.summary)) face.append(node("p", "mt-1 line-clamp-2 text-sm break-words text-ink-900 dark:text-ink-50", str(h.summary)));
    top.append(check, face);

    const actions = node("div", "mt-2 flex flex-wrap items-center gap-2 pl-7");
    actions.append(button("▶ Play", `Play moment ${n}`, SOFT, () => preview(h)));

    const more = node("details", "group mt-1 pl-7");
    const summary = node("summary", SUMMARY);
    summary.setAttribute("aria-label", `More about moment ${n}`);
    summary.append(node("span", "", "More"), chevron());
    const inside = node("div", "mt-2 space-y-3");
    more.append(summary, inside);

    const times = node("div", "flex flex-wrap items-end gap-3");
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
    const msg = node("p", "text-sm font-medium text-red-700 dark:text-red-400");
    msg.hidden = true;
    msg.id = `ai-${h.id}-msg`;
    fields.start.setAttribute("aria-describedby", msg.id);
    fields.end.setAttribute("aria-describedby", msg.id);

    function paintTimes() {
      const r = rangeOf(h);
      if (document.activeElement !== fields.start) fields.start.value = fmtTime(r.start);
      if (document.activeElement !== fields.end) fields.end.value = fmtTime(r.end);
      setText(length, `Length ${fmtTime(r.end - r.start)}`);
      setText(whenText, `#${n} · ${fmtTime(r.start)} – ${fmtTime(r.end)} · ${fmtTime(r.end - r.start)}`);
      badge.dataset.captionsKey = keyOf(r); // shorts.js shows the badge when this range's captions were edited
    }

    function fromField(which) {
      const input = fields[which];
      const value = bridge.parseTime(input.value);
      const r = rangeOf(h);
      const next = { ...r, [which]: value };
      let problem = value === null ? MSG.badTime : "";
      const end = source && source.duration > 0 ? source.duration : Infinity;
      if (!problem && Math.round(next.end * 1000) - Math.round(next.start * 1000) < Math.round(bridge.minClip * 1000)) problem = MSG.startAfterEnd;
      if (!problem && next.end > end + 0.05) problem = `The video is only ${fmtTime(end)} long.`;
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
      edits[h.id] = [Math.round(next.start * 1000) / 1000, Math.round(Math.min(next.end, end) * 1000) / 1000];
      store();
      input.value = fmtTime(edits[h.id][which === "start" ? 0 : 1]);
      paintTimes();
      publish();
      const shorts = window.SaveMeShorts; // an open caption editor follows the new times
      if (shorts) shorts.moved(r, rangeOf(h), `moment ${n}`);
      announce(`Moment ${n} now ${fmtTime(edits[h.id][0])} to ${fmtTime(edits[h.id][1])}.`);
    }
    for (const which of ["start", "end"]) {
      fields[which].addEventListener("change", () => fromField(which));
      fields[which].addEventListener("keydown", (e) => {
        if (e.key === "Enter") fromField(which);
      });
    }
    paintTimes();
    inside.append(times, msg);

    const about = node("div", "space-y-1 text-sm");
    if (str(h.reason)) about.append(node("p", "break-words text-ink-700 dark:text-ink-300", `Why it was picked: ${str(h.reason)}`));
    about.append(node("p", "text-ink-600 dark:text-ink-400", `Score ${isNum(h.score) ? Math.round(h.score) : "–"}. ${SCORE_NOTE}`));
    const tags = Array.isArray(h.tags) ? h.tags.filter((t) => typeof t === "string" && t.trim()) : [];
    if (tags.length) {
      const list = node("ul", "mt-2 flex flex-wrap gap-1.5");
      list.setAttribute("aria-label", "Tags");
      for (const tag of tags) list.append(node("li", "rounded-md bg-ink-100 px-1.5 py-0.5 text-xs text-ink-700 dark:bg-white/10 dark:text-ink-300", tag));
      about.append(list);
    }
    inside.append(about);

    const tools = node("div", "flex flex-wrap gap-2");
    tools.append(
      button("Edit captions", `Edit the captions of moment ${n}`, OUTLINE, () => {
        const shorts = window.SaveMeShorts; // shorts.js loads after this file
        if (shorts) shorts.editCaptions(rangeOf(h), `moment ${n}`);
      }),
      button("Save plain cut", `Save moment ${n} as a plain cut`, OUTLINE, () => exportOne(h, n)),
    );
    inside.append(tools);

    item.append(top, actions, more);
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

  function exportOne(h, n) {
    const r = rangeOf(h);
    if (bridge.isExporting() || !bridge.exportRange(r.start, r.end)) {
      setLine(el.aiCardsMsg, MSG.exportBusy, "error");
      announce(MSG.exportBusy);
      return;
    }
    setLine(el.aiCardsMsg, `Saving moment ${n} as a plain cut. Its progress shows below the steps.`);
  }

  function toggleAll() {
    const all = moments();
    const on = !(all.length && all.every((h) => selected.has(h.id)));
    selected = new Set(on ? all.map((h) => h.id) : []);
    for (const box of el.aiCards.querySelectorAll("input[type=checkbox]")) box.checked = on;
    paintSelectAll();
    store();
    publish();
    announce(on ? "All moments selected." : "No moments selected.");
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
    setLine(el.aiSubsMsg, `Imported ${count(lines, "subtitle line", "subtitle lines")}. Find moments will use them instead of transcribing.`);
    announce("Subtitles imported.");
    loadTranscript(g, true);
    scheduleEstimate();
  }

  // ---- wiring ----------------------------------------------------------------------------------

  for (const name of TABS) {
    const [tab] = tabParts(name);
    tab.addEventListener("click", () => selectTab(name));
    tab.addEventListener("keydown", onTabKey);
  }
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
  el.aiSelectAll.addEventListener("click", toggleAll);
  el.aiSearch.addEventListener("input", filterTranscript);
  el.aiSubsFile.addEventListener("change", importSubtitles);
  paintButtons();
  // shorts.js asks which analysis is running here, so Make can wait for it (it may make the transcript)
  // instead of trying to start a second one, which the server refuses.
  window.SaveMeMoments = Object.freeze({ running: () => (running && analysisId ? analysisId : "") });
  bridge.onMaking((on) => {
    making = on;
    paintButtons();
  });
  bridge.onSource(onSource);
})();
