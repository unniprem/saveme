// SaveMe Auto Shorts: the third tab of Clip Studio. Turn clips from the clip list (made in Manual or
// AI Highlights) into finished short videos on this computer: reframed for a platform, captioned from
// the transcript, optionally branded, and re-encoded as H.264 MP4. Plain browser JavaScript; shared
// helpers come from common.js (window.SaveMe), and the open video and clip list from studio.js
// (window.SaveMeStudioBridge). Text is set with textContent only.
//
// A render runs on the server (POST /api/sources/{id}/renders), one clip at a time, and is followed by
// polling GET /api/renders/{id}. Each clip's percentage is measured by FFmpeg; the total and the time
// left are estimates. sessionStorage keeps this video's choices and render; saved presets live in
// localStorage (this browser only) and can be exported and imported as JSON.
"use strict";

(() => {
  const { JOB_ID_RE, isNum, str, setText, show, setLine, errorFrom, api } = window.SaveMe;
  const bridge = window.SaveMeStudioBridge;
  const STORE_KEY = "saveme.shorts";
  const PRESET_KEY = "saveme.shorts.presets.v1";
  const POLL_MS = 1000;
  const MAX_POLL_FAILURES = 30;
  const MAX_PRESETS = 20;
  const MAX_LOGO_BYTES = 2 * 1024 * 1024;
  const MAX_PRESET_LOGO = 512 * 1024; // a logo kept inside a preset, as a data URL
  const MAX_BUMPER_BYTES = 100 * 1024 * 1024;
  const RENDER_PENDING = ["queued", "running"];
  const CLIP_PENDING = ["waiting", "analyzing", "rendering"];
  const CLIP_STATUS = {
    waiting: "Waiting", analyzing: "Finding faces", rendering: "Rendering", ready: "Ready", failed: "Failed",
    cancelled: "Cancelled",
  };
  const CORNERS = [["top-right", "Top right"], ["top-left", "Top left"], ["bottom-right", "Bottom right"],
    ["bottom-left", "Bottom left"]];
  const POSITIONS = [["", "Template default"], ["bottom", "Bottom"], ["middle", "Middle"], ["top", "Top"]];
  const HIGHLIGHTS = [["none", "None"], ["color", "Colour the word"], ["box", "Box behind the word"]];
  const MSG = {
    noClips: "Select at least one clip to render.",
    loading: "Checking Auto Shorts…",
    offline: "Can't reach SaveMe. Check that it's running, then try again.",
    lost: "This render is no longer on the server. Render again.",
    noTranscript: "No transcript yet, so shorts get no captions. Transcribe the video first (this runs on this computer).",
    words: (lang) => `Captions use the transcript's word timing${lang ? ` (${lang})` : ""}.`,
    estimated: "Captions use the transcript, with word timing estimated within each line. Transcribing again gives exact word timing.",
    transcribing: "Transcribing on this computer…",
    presetSaved: (name) => `Preset “${name}” saved in this browser.`,
    presetLoaded: (name) => `Preset “${name}” loaded.`,
    presetBad: "That file doesn't hold SaveMe presets.",
  };

  const el = {};
  for (const id of [
    "shorts-panel", "shorts-status", "shorts-use-marks", "shorts-select-all", "shorts-clips-empty", "shorts-clips",
    "shorts-platform", "shorts-template", "shorts-template-desc", "shorts-quality", "shorts-fps", "shorts-layout",
    "shorts-margin", "shorts-margin-out", "shorts-manual", "shorts-manual-fields", "shorts-manual-x",
    "shorts-manual-x-out", "shorts-manual-y", "shorts-manual-y-out", "shorts-manual-zoom", "shorts-manual-zoom-out",
    "shorts-transcript", "shorts-transcribe", "shorts-captions", "shorts-burn", "shorts-srt", "shorts-caption-fields",
    "shorts-style", "shorts-position", "shorts-size", "shorts-words", "shorts-color", "shorts-highlight",
    "shorts-highlight-mode", "shorts-uppercase", "shorts-branding-summary", "shorts-logo", "shorts-logo-name",
    "shorts-logo-remove", "shorts-logo-position", "shorts-logo-size", "shorts-logo-size-out", "shorts-logo-opacity",
    "shorts-logo-opacity-out", "shorts-watermark", "shorts-brand-color", "shorts-brand-on", "shorts-title-bar",
    "shorts-intro", "shorts-intro-name", "shorts-outro", "shorts-outro-name", "shorts-branding-msg",
    "shorts-preset-name", "shorts-preset-save", "shorts-preset-list", "shorts-preset-load", "shorts-preset-delete",
    "shorts-preset-export", "shorts-preset-import", "shorts-preset-msg", "shorts-preview-pick", "shorts-preview-at",
    "shorts-preview-at-out", "shorts-preview-frame", "shorts-preview-clip", "shorts-preview-msg", "shorts-preview",
    "shorts-preview-img", "shorts-preview-video", "shorts-editor", "shorts-editor-title", "shorts-editor-timing",
    "shorts-cues", "shorts-editor-msg", "shorts-cues-add", "shorts-cues-reset", "shorts-cues-done",
    "shorts-render-msg", "shorts-render", "shorts-render-label", "shorts-progress", "shorts-progress-spin",
    "shorts-progress-status", "shorts-progress-percent", "shorts-progress-bar", "shorts-progress-fill",
    "shorts-results", "shorts-zip", "shorts-cancel", "announcer",
  ]) {
    el[id.replace(/^shorts-/, "").replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = document.getElementById(id);
  }

  let options = null; // GET /api/shorts/options
  let source = null; // { id, title, duration } while the studio has a ready video
  let gen = 0; // bumped when the source changes, so late replies are ignored
  let clips = []; // the studio's clip list, in seconds
  let selected = new Set(); // clip keys
  let edits = {}; // clip key -> edited cues [{start, end, text, speaker}]
  let editing = ""; // the clip key open in the caption editor
  let assets = {}; // kind -> { id, name } uploaded for this source
  let logoData = ""; // the logo as a data URL (kept in presets when small enough)
  let logoName = "";
  let renderId = "";
  let renderView = null;
  let pollTimer = 0;
  let failures = 0;
  let previewId = "";
  let previewSeq = 0; // the newest preview request, so a late reply for an older one is ignored
  let previewTimer = 0;
  let transcribing = "";
  let captionInfo = null;

  const announce = (text) => setText(el.announcer, text);
  const fmtTime = (s) => bridge.fmtTime(s);
  const keyOf = (c) => `${Math.round(c.start * 1000)}-${Math.round(c.end * 1000)}`;
  const base = () => `/api/sources/${encodeURIComponent(source.id)}`;
  const byId = (list, id) => (list || []).find((x) => x.id === id);

  // ---- form helpers ---------------------------------------------------------------------------

  function fill(select, items, value) {
    const opts = items.map(([v, label]) => {
      const o = document.createElement("option");
      o.value = String(v);
      o.textContent = label;
      return o;
    });
    select.replaceChildren(...opts);
    if (value !== undefined) select.value = String(value);
  }

  function outputs() {
    setText(el.marginOut, `${el.margin.value}%`);
    setText(el.manualXOut, `${el.manualX.value}%`);
    setText(el.manualYOut, `${el.manualY.value}%`);
    setText(el.manualZoomOut, `${(Number(el.manualZoom.value) / 100).toFixed(2)}×`);
    setText(el.logoSizeOut, `${el.logoSize.value}%`);
    setText(el.logoOpacityOut, `${el.logoOpacity.value}%`);
    const clip = previewClip();
    setText(el.previewAtOut, clip ? fmtTime(previewAt(clip)) : "");
    show(el.manualFields, el.manual.checked);
    for (const input of el.captionFields.querySelectorAll("input, select")) input.disabled = !el.captions.checked;
    el.burn.disabled = el.srt.disabled = !el.captions.checked;
    const branded = assets.logo || logoData || el.watermark.value.trim() || el.brandOn.checked || el.titleBar.checked
      || assets.intro || assets.outro;
    setText(el.brandingSummary, branded ? "Branding (on)" : "Branding (off)");
  }

  function applyStyle(styleId) {
    const style = byId(options && options.styles, styleId);
    if (!style) return;
    el.style.value = styleId;
    el.size.value = String(style.size);
    el.words.value = String(style.max_words);
    el.color.value = style.color.toLowerCase();
    el.highlight.value = style.highlight.toLowerCase();
    el.highlightMode.value = style.highlight_mode;
    el.uppercase.checked = Boolean(style.uppercase);
  }

  function applyTemplate(templateId) {
    const t = byId(options && options.templates, templateId);
    if (!t) return;
    el.template.value = templateId;
    setText(el.templateDesc, t.description);
    el.layout.value = "";
    el.margin.value = String(Math.round(100 * (t.layout.margin ?? 0.08)));
    applyStyle(t.captions.style);
    el.position.value = "";
    el.titleBar.checked = Boolean(t.branding.title_bar);
    el.logoPosition.value = t.branding.logo_position;
  }

  function manualFrame() {
    if (!el.manual.checked) return undefined;
    return { x: Number(el.manualX.value) / 100, y: Number(el.manualY.value) / 100,
      zoom: Number(el.manualZoom.value) / 100 };
  }

  // The render spec from the form (validated again by the server).
  function spec() {
    const s = {
      template: el.template.value, platform: el.platform.value, quality: el.quality.value,
      fps: el.fps.value === "source" ? "source" : Number(el.fps.value),
      layout: { margin: Number(el.margin.value) / 100 },
      captions: {
        enabled: el.captions.checked, burn: el.burn.checked, export: el.srt.checked ? ["srt"] : [],
        style: el.style.value, size: Number(el.size.value), max_words: Number(el.words.value),
        color: el.color.value.toUpperCase(), highlight: el.highlight.value.toUpperCase(),
        highlight_mode: el.highlightMode.value, uppercase: el.uppercase.checked,
      },
      branding: {
        logo_position: el.logoPosition.value, logo_scale: Number(el.logoSize.value) / 100,
        logo_opacity: Number(el.logoOpacity.value) / 100, watermark: el.watermark.value.trim(),
        title_bar: el.titleBar.checked,
      },
    };
    if (el.layout.value) s.layout.mode = el.layout.value;
    if (el.position.value) s.captions.position = el.position.value;
    if (el.brandOn.checked) s.branding.brand_color = el.brandColor.value.toUpperCase();
    for (const kind of ["logo", "intro", "outro"]) if (assets[kind]) s.branding[kind] = assets[kind].id;
    return s;
  }

  // Ask the server whether a saved preset's spec is valid; its normalised spec, or an error to show.
  async function validated(s) {
    const r = await api("/api/shorts/validate", { spec: s });
    if (!r.valid) throw new Error((r.error && r.error.message) || "This preset isn't valid.");
    return r.spec;
  }

  // Put a (validated) spec back into the form; asset ids aren't restored (they belong to one video).
  function applySpec(s) {
    if (!s || typeof s !== "object") return;
    if (byId(options.templates, s.template)) applyTemplate(s.template);
    if (byId(options.platforms, s.platform)) el.platform.value = s.platform;
    if (byId(options.qualities, s.quality)) el.quality.value = s.quality;
    if (options.fps.map(String).includes(String(s.fps))) el.fps.value = String(s.fps);
    const layout = s.layout || {};
    el.layout.value = layout.mode && byId(options.layouts, layout.mode) ? layout.mode : "";
    if (isNum(layout.margin)) el.margin.value = String(Math.round(layout.margin * 100));
    const c = s.captions || {};
    if (byId(options.styles, c.style)) applyStyle(c.style);
    if (typeof c.enabled === "boolean") el.captions.checked = c.enabled;
    if (typeof c.burn === "boolean") el.burn.checked = c.burn;
    el.srt.checked = Array.isArray(c.export) && c.export.includes("srt");
    if (isNum(c.size)) el.size.value = String(c.size);
    if (isNum(c.max_words)) el.words.value = String(c.max_words);
    if (/^#[0-9a-f]{6}$/i.test(c.color || "")) el.color.value = c.color.toLowerCase();
    if (/^#[0-9a-f]{6}$/i.test(c.highlight || "")) el.highlight.value = c.highlight.toLowerCase();
    if (HIGHLIGHTS.some(([v]) => v === c.highlight_mode)) el.highlightMode.value = c.highlight_mode;
    if (typeof c.uppercase === "boolean") el.uppercase.checked = c.uppercase;
    el.position.value = POSITIONS.some(([v]) => v && v === c.position) ? c.position : "";
    const b = s.branding || {};
    if (CORNERS.some(([v]) => v === b.logo_position)) el.logoPosition.value = b.logo_position;
    if (isNum(b.logo_scale)) el.logoSize.value = String(Math.round(b.logo_scale * 100));
    if (isNum(b.logo_opacity)) el.logoOpacity.value = String(Math.round(b.logo_opacity * 100));
    el.watermark.value = typeof b.watermark === "string" ? b.watermark : "";
    el.brandOn.checked = /^#[0-9a-f]{6}$/i.test(b.brand_color || "");
    if (el.brandOn.checked) el.brandColor.value = b.brand_color.toLowerCase();
    if (typeof b.title_bar === "boolean") el.titleBar.checked = b.title_bar;
    outputs();
  }

  // ---- storage -------------------------------------------------------------------------------

  function store() {
    if (!source) return;
    try {
      sessionStorage.setItem(STORE_KEY, JSON.stringify({
        sourceId: source.id, selected: [...selected], edits, renderId, spec: spec(),
        manual: el.manual.checked ? manualFrame() : null,
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

  // ---- availability --------------------------------------------------------------------------

  async function loadOptions() {
    try {
      options = await api("/api/shorts/options");
    } catch (err) {
      setLine(el.status, err.message, "error");
      paint();
      return;
    }
    fill(el.platform, options.platforms.map((p) => [p.id, `${p.name} · ${p.aspect === "source" ? "as filmed" : p.aspect}`]));
    fill(el.template, options.templates.map((t) => [t.id, t.name]));
    fill(el.quality, options.qualities.map((q) => [q.id, q.name]), "standard");
    fill(el.fps, options.fps.map((f) => [f, f === "source" ? "Same as the video" : `${f} fps`]), "source");
    fill(el.layout, [["", "Template default"], ...options.layouts.map((l) => [l.id, l.name])]);
    fill(el.style, options.styles.map((s) => [s.id, s.name]));
    fill(el.position, POSITIONS);
    fill(el.highlightMode, HIGHLIGHTS);
    fill(el.logoPosition, CORNERS);
    applyTemplate(options.templates[0].id);
    if (options.available) {
      const encoder = options.encoder || {};
      setLine(el.status, `Ready. Encoding with ${encoder.hardware ? "the Mac's video hardware" : "x264 (software)"}`
        + `${options.captions_available ? "" : "; this FFmpeg can't burn in captions"}.`);
    } else {
      setLine(el.status, (options.error && options.error.message) || "Auto Shorts isn't available.", "error");
    }
    listPresets();
    if (source) restore();
    paint();
  }

  // ---- source and clips ------------------------------------------------------------------------

  function onSource(info) {
    if (info && source && info.id === source.id) {
      source.duration = info.duration;
      return;
    }
    gen += 1;
    clearTimeout(pollTimer);
    clearTimeout(previewTimer);
    source = info ? { ...info } : null;
    selected = new Set();
    edits = {};
    editing = "";
    assets = {};
    renderId = "";
    renderView = null;
    previewId = "";
    captionInfo = null;
    transcribing = "";
    show(el.editor, false);
    show(el.progress, false);
    show(el.preview, false);
    setLine(el.renderMsg, "");
    setLine(el.previewMsg, "");
    setText(el.introName, "");
    setText(el.outroName, "");
    if (!logoData) { // a large logo isn't kept in the page: it has to be added again for another video
      logoName = "";
      setText(el.logoName, "");
      show(el.logoRemove, false);
    }
    if (source && options) restore();
    paint();
  }

  function restore() {
    const saved = stored(source.id);
    if (!saved) {
      selected = new Set(clips.map(keyOf));
      refreshCaptions();
      return;
    }
    selected = new Set(Array.isArray(saved.selected) ? saved.selected.filter((k) => typeof k === "string") : []);
    edits = saved.edits && typeof saved.edits === "object" ? saved.edits : {};
    applySpec(saved.spec);
    if (saved.manual && typeof saved.manual === "object") {
      el.manual.checked = true;
      el.manualX.value = String(Math.round((saved.manual.x ?? 0.5) * 100));
      el.manualY.value = String(Math.round((saved.manual.y ?? 0.5) * 100));
      el.manualZoom.value = String(Math.round((saved.manual.zoom ?? 1) * 100));
    }
    if (typeof saved.renderId === "string" && JOB_ID_RE.test(saved.renderId)) {
      renderId = saved.renderId;
      poll(gen);
    }
    refreshCaptions();
    outputs();
  }

  function onClips(list) {
    const before = new Set(clips.map(keyOf));
    clips = list.slice();
    for (const c of clips) if (!before.has(keyOf(c))) selected.add(keyOf(c)); // new rows start selected
    const keys = new Set(clips.map(keyOf));
    selected = new Set([...selected].filter((k) => keys.has(k)));
    renderClips();
    paint();
    store();
  }

  function chosen() {
    return clips.filter((c) => selected.has(keyOf(c)));
  }

  function renderClips() {
    const rows = clips.map((c, i) => {
      const key = keyOf(c);
      const li = document.createElement("li");
      li.className = "flex flex-wrap items-center gap-x-3 gap-y-1 py-2";
      const label = document.createElement("label");
      label.className = "flex min-w-0 flex-1 items-center gap-2 text-sm tabular-nums text-ink-900 dark:text-ink-50";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "size-4 accent-brand-700 dark:accent-brand-400";
      box.checked = selected.has(key);
      box.addEventListener("change", () => {
        if (box.checked) selected.add(key);
        else selected.delete(key);
        paint();
        store();
        refreshCaptions();
      });
      const text = document.createElement("span");
      text.textContent = `Clip ${i + 1} · ${fmtTime(c.start)} – ${fmtTime(c.end)} · ${fmtTime(c.end - c.start)}`;
      label.append(box, text);
      const actions = document.createElement("span");
      actions.className = "flex items-center gap-2";
      if (edits[key]) {
        const badge = document.createElement("span");
        badge.className = "rounded-md bg-brand-50 px-1.5 py-0.5 text-xs font-semibold text-brand-700 dark:bg-white/10 dark:text-ink-50";
        badge.textContent = "Captions edited";
        actions.append(badge);
      }
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "h-9 rounded-lg bg-brand-50 px-3 text-sm font-semibold text-brand-700 hover:bg-brand-100 "
        + "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-700 disabled:opacity-60 "
        + "dark:bg-white/10 dark:text-ink-50 dark:hover:bg-white/15 dark:focus-visible:outline-brand-400";
      edit.textContent = "Edit captions";
      edit.setAttribute("aria-label", `Edit captions of clip ${i + 1}`);
      edit.disabled = !options || !options.available;
      edit.addEventListener("click", () => openEditor(c, i + 1));
      actions.append(edit);
      li.append(label, actions);
      return li;
    });
    el.clips.replaceChildren(...rows);
    show(el.clipsEmpty, clips.length === 0);
    const pick = el.previewPick.value;
    fill(el.previewPick, clips.map((c, i) => [keyOf(c), `Clip ${i + 1} · ${fmtTime(c.start)} – ${fmtTime(c.end)}`]));
    if (clips.some((c) => keyOf(c) === pick)) el.previewPick.value = pick;
    outputs();
  }

  function useMarks() {
    const m = bridge.marks();
    const result = bridge.addRanges([m]);
    if (result.added) announce(`Added ${fmtTime(m.start)} to ${fmtTime(m.end)} to the clip list.`);
    else if (result.duplicates) {
      selected.add(keyOf({ start: Math.round(m.start * 1000) / 1000, end: Math.round(m.end * 1000) / 1000 }));
      renderClips();
      announce("That part is already in the clip list; it's selected.");
    } else setLine(el.renderMsg, "Mark a start and an end in the editor first (the list may also be full).", "error");
  }

  // ---- captions and transcript -------------------------------------------------------------------

  async function refreshCaptions() {
    const g = gen;
    const first = chosen()[0] || clips[0];
    if (!source || !options || !options.available || !first) {
      setLine(el.transcript, "");
      show(el.transcribe, false);
      return;
    }
    try {
      const data = await api(`${base()}/shorts/captions`, { start: first.start, end: first.end, spec: spec() });
      if (g !== gen) return;
      captionInfo = data.captions;
    } catch {
      return;
    }
    if (transcribing) return;
    if (!captionInfo) {
      setLine(el.transcript, MSG.noTranscript, "error");
      show(el.transcribe, true);
    } else {
      setLine(el.transcript, captionInfo.timing === "words" ? MSG.words(captionInfo.language) : MSG.estimated);
      show(el.transcribe, captionInfo.timing !== "words");
      setText(el.transcribe, captionInfo.timing === "words" ? "Transcribe this video" : "Transcribe again for exact timing");
    }
  }

  async function transcribe() {
    if (!source) return;
    const g = gen;
    el.transcribe.disabled = true;
    setLine(el.transcript, MSG.transcribing);
    try {
      const r = await api(`${base()}/analyses`, { transcribe_only: true, retranscribe: Boolean(captionInfo) });
      transcribing = r.analysis_id;
      while (g === gen) {
        await new Promise((resolve) => { setTimeout(resolve, POLL_MS); });
        const view = await api(`/api/analyses/${encodeURIComponent(transcribing)}`);
        if (view.status === "ready") break;
        if (view.status === "failed" || view.status === "cancelled") {
          throw new Error((view.error && view.error.message) || "Transcription stopped.");
        }
        const pct = isNum(view.progress_percent) ? ` ${Math.round(view.progress_percent)}%` : "";
        setLine(el.transcript, `${view.detail || MSG.transcribing}${pct}`);
      }
      announce("Transcript ready.");
    } catch (err) {
      setLine(el.transcript, err.message, "error");
    } finally {
      transcribing = "";
      el.transcribe.disabled = false;
      if (g === gen) refreshCaptions();
    }
  }

  // ---- caption editor ------------------------------------------------------------------------------

  function cueRow(cue, length) {
    const li = document.createElement("li");
    li.className = "grid grid-cols-[5.5rem_5.5rem_1fr_auto] items-center gap-2";
    const num = (value, label) => {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "0.1";
      input.min = "0";
      input.max = String(length);
      input.value = String(Math.round(value * 100) / 100);
      input.setAttribute("aria-label", label);
      input.className = "h-10 w-full rounded-lg border border-ink-300 bg-white px-2 text-sm tabular-nums text-ink-900 "
        + "focus-visible:outline-2 focus-visible:outline-brand-700 aria-invalid:border-red-500 dark:border-ink-700 "
        + "dark:bg-ink-950 dark:text-ink-50";
      return input;
    };
    const start = num(cue.start, "Start (seconds)");
    const end = num(cue.end, "End (seconds)");
    const text = document.createElement("input");
    text.type = "text";
    text.maxLength = 200;
    text.value = cue.text;
    text.setAttribute("aria-label", "Caption text");
    text.className = "h-10 w-full min-w-0 rounded-lg border border-ink-300 bg-white px-2 text-sm text-ink-900 "
      + "focus-visible:outline-2 focus-visible:outline-brand-700 aria-invalid:border-red-500 dark:border-ink-700 "
      + "dark:bg-ink-950 dark:text-ink-50";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.setAttribute("aria-label", "Remove this caption");
    remove.className = "h-10 rounded-lg border border-ink-300 px-2 text-sm text-ink-700 hover:border-red-300 "
      + "hover:text-red-700 focus-visible:outline-2 focus-visible:outline-brand-700 dark:border-ink-700 dark:text-ink-200";
    remove.addEventListener("click", () => li.remove());
    li.append(start, end, text, remove);
    li.dataset.speaker = cue.speaker || "";
    return li;
  }

  async function openEditor(clip, number) {
    if (!source) return;
    const g = gen;
    editing = keyOf(clip);
    setText(el.editorTitle, `Captions for clip ${number} (${fmtTime(clip.start)} – ${fmtTime(clip.end)})`);
    setLine(el.editorMsg, "");
    show(el.editor, true);
    let cues = edits[editing];
    if (cues) setText(el.editorTiming, "Edited");
    else {
      setText(el.editorTiming, "Loading…");
      try {
        const data = await api(`${base()}/shorts/captions`, { start: clip.start, end: clip.end, spec: spec() });
        if (g !== gen) return;
        if (!data.captions) {
          setText(el.editorTiming, "");
          setLine(el.editorMsg, MSG.noTranscript, "error");
          cues = [];
        } else {
          cues = data.captions.cues.map((c) => ({ start: c.start, end: c.end, text: c.text }));
          setText(el.editorTiming, data.captions.timing === "words" ? "From word timing" : "Timing estimated");
        }
      } catch (err) {
        setLine(el.editorMsg, err.message, "error");
        cues = [];
      }
    }
    el.cues.replaceChildren(...cues.map((c) => cueRow(c, clip.end - clip.start)));
    el.editor.dataset.length = String(clip.end - clip.start);
    el.editor.focus();
  }

  function readEditor() {
    const length = Number(el.editor.dataset.length) || 0;
    const cues = [];
    let problem = "";
    for (const row of el.cues.children) {
      const [start, end, text] = row.querySelectorAll("input");
      for (const input of [start, end, text]) input.removeAttribute("aria-invalid");
      const s = Number(start.value);
      const e = Number(end.value);
      const t = text.value.trim();
      if (!t) continue;
      if (!(isNum(s) && isNum(e)) || s < 0 || e <= s || e > length + 0.05) {
        start.setAttribute("aria-invalid", "true");
        end.setAttribute("aria-invalid", "true");
        problem = problem || "Each caption needs a start before its end, inside the clip.";
        continue;
      }
      if (cues.length && s < cues[cues.length - 1].end - 1e-6) {
        start.setAttribute("aria-invalid", "true");
        problem = problem || "Captions must be in order and must not overlap.";
        continue;
      }
      const cue = { start: Math.round(s * 1000) / 1000, end: Math.round(e * 1000) / 1000, text: t };
      if (row.dataset.speaker) cue.speaker = row.dataset.speaker;
      cues.push(cue);
    }
    return { cues, problem };
  }

  function editorDone() {
    const { cues, problem } = readEditor();
    if (problem) {
      setLine(el.editorMsg, problem, "error");
      return;
    }
    edits[editing] = cues;
    show(el.editor, false);
    renderClips();
    store();
    announce("Captions saved for this clip.");
  }

  function editorReset() {
    delete edits[editing];
    const clip = clips.find((c) => keyOf(c) === editing);
    renderClips();
    store();
    if (clip) openEditor(clip, clips.indexOf(clip) + 1);
  }

  function editorAdd() {
    const length = Number(el.editor.dataset.length) || 0;
    const rows = el.cues.children;
    const last = rows.length ? Number(rows[rows.length - 1].querySelectorAll("input")[1].value) || 0 : 0;
    const start = Math.min(last, Math.max(0, length - 0.5));
    const row = cueRow({ start, end: Math.min(length, start + 2), text: "" }, length);
    el.cues.append(row);
    row.querySelectorAll("input")[2].focus();
  }

  // ---- branding --------------------------------------------------------------------------------------

  async function upload(kind, blob, name) {
    if (!source) throw new Error("Open a video first.");
    let res;
    try {
      res = await fetch(`${base()}/shorts/assets?kind=${kind}`, {
        method: "POST", body: blob, headers: { "Content-Type": blob.type || "application/octet-stream" },
      });
    } catch {
      throw new Error(MSG.offline);
    }
    let data = null;
    try {
      data = await res.json();
    } catch {
      // handled below
    }
    if (!res.ok) throw errorFrom(res.status, data);
    assets[kind] = { id: data.asset_id, name };
    return data;
  }

  function readDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error("Couldn't read that file."));
      reader.readAsDataURL(file);
    });
  }

  async function onLogo() {
    const file = el.logo.files[0];
    el.logo.value = "";
    if (!file) return;
    if (file.size > MAX_LOGO_BYTES) {
      setLine(el.brandingMsg, "The logo must be a PNG of at most 2 MB.", "error");
      return;
    }
    setLine(el.brandingMsg, "Adding the logo…");
    try {
      const data = await upload("logo", file, file.name);
      logoName = file.name;
      logoData = file.size <= MAX_PRESET_LOGO ? await readDataUrl(file) : "";
      setText(el.logoName, `${file.name} · ${data.width} × ${data.height}`);
      show(el.logoRemove, true);
      setLine(el.brandingMsg, "");
    } catch (err) {
      setLine(el.brandingMsg, err.message, "error");
    }
    outputs();
    store();
  }

  function removeLogo() {
    delete assets.logo;
    logoData = "";
    logoName = "";
    setText(el.logoName, "");
    show(el.logoRemove, false);
    outputs();
    store();
  }

  async function onBumper(kind) {
    const input = kind === "intro" ? el.intro : el.outro;
    const label = kind === "intro" ? el.introName : el.outroName;
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    if (file.size > MAX_BUMPER_BYTES) {
      setLine(el.brandingMsg, `An ${kind} can be at most 100 MB.`, "error");
      return;
    }
    setLine(el.brandingMsg, `Adding the ${kind}…`);
    try {
      const data = await upload(kind, file, file.name);
      setText(label, `${file.name} · ${data.duration.toFixed(1)} s`);
      setLine(el.brandingMsg, "");
    } catch (err) {
      setText(label, "");
      setLine(el.brandingMsg, err.message, "error");
    }
    outputs();
  }

  // A data URL as a Blob, decoded here: fetch() can't read data: URLs under the page's CSP (connect-src 'self').
  function dataUrlBlob(url) {
    const comma = url.indexOf(",");
    const bin = atob(url.slice(comma + 1));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: "image/png" });
  }

  // A logo from a preset (or from another video) is uploaded again for this video when needed.
  async function ensureLogo() {
    if (assets.logo || !logoData || !source) return;
    try {
      await upload("logo", dataUrlBlob(logoData), logoName || "logo.png");
    } catch (err) {
      setLine(el.brandingMsg, `The logo couldn't be added: ${err.message}`, "error");
      throw err;
    }
  }

  // ---- presets (this browser only) ----------------------------------------------------------------------

  function presets() {
    try {
      const list = JSON.parse(localStorage.getItem(PRESET_KEY) || "[]");
      return Array.isArray(list) ? list.filter((p) => p && typeof p.name === "string" && p.spec) : [];
    } catch {
      return [];
    }
  }

  function savePresets(list) {
    try {
      localStorage.setItem(PRESET_KEY, JSON.stringify(list.slice(0, MAX_PRESETS)));
      return true;
    } catch {
      setLine(el.presetMsg, "This browser couldn't save the preset (storage is full or off).", "error");
      return false;
    }
  }

  function listPresets() {
    const list = presets();
    fill(el.presetList, list.length ? list.map((p, i) => [String(i), p.name]) : [["", "No saved presets"]]);
    el.presetLoad.disabled = el.presetDelete.disabled = el.presetExport.disabled = !list.length;
  }

  function presetSpec() {
    const s = spec();
    delete s.branding.logo;
    delete s.branding.intro;
    delete s.branding.outro;
    return s;
  }

  function savePreset() {
    const name = el.presetName.value.trim().slice(0, 40);
    if (!name) {
      setLine(el.presetMsg, "Give the preset a name.", "error");
      el.presetName.focus();
      return;
    }
    const list = presets().filter((p) => p.name !== name);
    list.unshift({ name, spec: presetSpec(), logo: logoData ? { name: logoName, data: logoData } : null,
      bumpers: Boolean(assets.intro || assets.outro), saved: new Date().toISOString() });
    if (list.length > MAX_PRESETS) list.length = MAX_PRESETS;
    if (savePresets(list)) setLine(el.presetMsg, MSG.presetSaved(name));
    listPresets();
  }

  async function loadPreset() {
    const p = presets()[Number(el.presetList.value)];
    if (!p) return;
    try {
      applySpec(await validated(p.spec));
      if (p.logo && typeof p.logo.data === "string" && p.logo.data.startsWith("data:image/png;base64,")) {
        logoData = p.logo.data;
        logoName = str(p.logo.name) || "logo.png";
        delete assets.logo;
        await ensureLogo();
        setText(el.logoName, logoName);
        show(el.logoRemove, true);
      }
      setLine(el.presetMsg, MSG.presetLoaded(p.name) + (p.bumpers ? " Add its intro or outro again: videos aren't saved in presets." : ""));
      store();
      refreshCaptions();
    } catch (err) {
      setLine(el.presetMsg, `This preset can't be used: ${err.message}`, "error");
    }
    outputs();
  }

  function deletePreset() {
    const list = presets();
    const at = Number(el.presetList.value);
    if (!list[at]) return;
    const [gone] = list.splice(at, 1);
    savePresets(list);
    listPresets();
    setLine(el.presetMsg, `Preset “${gone.name}” deleted.`);
  }

  function exportPresets() {
    const blob = new Blob([JSON.stringify({ saveme_presets: 1, presets: presets() }, null, 2)],
      { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "saveme-shorts-presets.json";
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  }

  async function importPresets() {
    const file = el.presetImport.files[0];
    el.presetImport.value = "";
    if (!file) return;
    let incoming;
    try {
      const data = JSON.parse(await file.text());
      incoming = data && data.saveme_presets === 1 && Array.isArray(data.presets) ? data.presets : null;
    } catch {
      incoming = null;
    }
    if (!incoming) {
      setLine(el.presetMsg, MSG.presetBad, "error");
      return;
    }
    const list = presets();
    let added = 0;
    let refused = 0;
    for (const p of incoming.slice(0, MAX_PRESETS)) {
      const name = p && typeof p.name === "string" ? p.name.trim().slice(0, 40) : "";
      if (!name) {
        refused += 1;
        continue;
      }
      try {
        const checkedSpec = await validated(p.spec);
        const logo = p.logo && typeof p.logo.data === "string" && p.logo.data.startsWith("data:image/png;base64,")
          && p.logo.data.length <= MAX_PRESET_LOGO * 1.4 ? { name: str(p.logo.name).slice(0, 100), data: p.logo.data } : null;
        const at = list.findIndex((x) => x.name === name);
        if (at >= 0) list.splice(at, 1);
        list.unshift({ name, spec: checkedSpec, logo, bumpers: Boolean(p.bumpers), saved: new Date().toISOString() });
        added += 1;
      } catch {
        refused += 1;
      }
    }
    savePresets(list);
    listPresets();
    setLine(el.presetMsg, `Imported ${added} preset${added === 1 ? "" : "s"}${refused ? `; ${refused} couldn't be used` : ""}.`,
      added ? undefined : "error");
  }

  // ---- preview ------------------------------------------------------------------------------------------

  function previewClip() {
    return clips.find((c) => keyOf(c) === el.previewPick.value) || chosen()[0] || clips[0] || null;
  }

  function previewAt(clip) {
    return ((clip.end - clip.start) * Number(el.previewAt.value)) / 1000;
  }

  async function startPreview(kind) {
    const clip = previewClip();
    if (!source || !clip) {
      setLine(el.previewMsg, "Add a clip first.", "error");
      return;
    }
    const g = gen;
    const seq = ++previewSeq;
    clearTimeout(previewTimer);
    previewId = "";
    setLine(el.previewMsg, kind === "frame" ? "Making the preview…" : "Rendering a draft (half size)…");
    try {
      await ensureLogo();
      const body = { clip: clipBody(clip), spec: spec(), kind, at: Math.round(previewAt(clip) * 1000) / 1000 };
      const r = await api(`${base()}/shorts/previews`, body);
      if (g !== gen || seq !== previewSeq) return; // a newer preview was asked for meanwhile
      previewId = r.preview_id;
      pollPreview(g, previewId, kind);
    } catch (err) {
      if (seq === previewSeq) setLine(el.previewMsg, err.message, "error");
    }
  }

  async function pollPreview(g, id, kind) {
    if (g !== gen || id !== previewId) return;
    let view;
    try {
      view = await api(`/api/shorts/previews/${encodeURIComponent(id)}`);
    } catch (err) {
      setLine(el.previewMsg, err.message, "error");
      return;
    }
    if (g !== gen || id !== previewId) return;
    if (view.status === "ready") {
      const url = `/api/shorts/previews/${encodeURIComponent(id)}/file`;
      show(el.preview, true);
      show(el.previewImg, kind === "frame");
      show(el.previewVideo, kind !== "frame");
      if (kind === "frame") el.previewImg.src = url;
      else el.previewVideo.src = url;
      const mode = view.mode && byId(options.layouts, view.mode);
      setLine(el.previewMsg, mode ? `Framing: ${mode.name}.` : view.mode === "manual" ? "Framing: set by hand." : "");
      return;
    }
    if (view.status === "failed" || view.status === "cancelled") {
      setLine(el.previewMsg, view.status === "failed" ? (view.error && view.error.message) || "The preview failed."
        : "Replaced by a newer preview.", view.status === "failed" ? "error" : undefined);
      return;
    }
    const pct = isNum(view.progress_percent) ? ` ${Math.round(view.progress_percent)}%` : "";
    setLine(el.previewMsg, `${view.detail || "Working"}…${pct}`);
    previewTimer = setTimeout(() => pollPreview(g, id, kind), 500);
  }

  // ---- render ---------------------------------------------------------------------------------------------

  function clipBody(c) {
    const body = { start: c.start, end: c.end };
    const cues = edits[keyOf(c)];
    if (cues) body.cues = cues;
    const manual = manualFrame();
    if (manual) body.manual = manual;
    return body;
  }

  async function startRender() {
    const list = chosen();
    if (!source || !list.length) {
      setLine(el.renderMsg, MSG.noClips, "error");
      return;
    }
    const g = gen;
    el.render.disabled = true;
    setLine(el.renderMsg, "");
    try {
      await ensureLogo();
      const r = await api(`${base()}/renders`, { clips: list.map(clipBody), spec: spec() });
      if (g !== gen) return;
      renderId = r.render_id;
      renderView = null;
      failures = 0;
      store();
      show(el.progress, true);
      el.progress.focus();
      announce(`Rendering ${list.length} short${list.length === 1 ? "" : "s"}.`);
      poll(g);
    } catch (err) {
      setLine(el.renderMsg, err.message, "error");
    } finally {
      paint();
    }
  }

  async function poll(g) {
    clearTimeout(pollTimer);
    if (g !== gen || !renderId) return;
    try {
      renderView = await api(`/api/renders/${encodeURIComponent(renderId)}`);
      failures = 0;
    } catch (err) {
      if (err.status === 404) {
        renderId = "";
        renderView = null;
        store();
        setLine(el.renderMsg, MSG.lost, "error");
        show(el.progress, false);
        paint();
        return;
      }
      failures += 1;
      if (failures >= MAX_POLL_FAILURES) {
        setLine(el.renderMsg, MSG.offline, "error");
        return;
      }
    }
    if (g !== gen) return;
    paintProgress();
    if (renderView && RENDER_PENDING.includes(renderView.status)) pollTimer = setTimeout(() => poll(g), POLL_MS);
    else if (renderView) {
      const done = renderView.ready;
      announce(renderView.status === "cancelled" ? "Render cancelled." : `${done} of ${renderView.count} shorts ready.`);
      paint();
    }
  }

  function link(text, href, label) {
    const a = document.createElement("a");
    a.href = href;
    a.setAttribute("download", "");
    a.textContent = text;
    a.setAttribute("aria-label", label);
    a.className = "rounded-md text-sm font-semibold text-brand-700 underline decoration-brand-700/30 underline-offset-4 "
      + "hover:decoration-brand-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-700 "
      + "dark:text-brand-300";
    return a;
  }

  function paintProgress() {
    const view = renderView;
    show(el.progress, Boolean(view));
    if (!view) return;
    const pending = RENDER_PENDING.includes(view.status);
    const headline = pending ? `Rendering ${view.ready} of ${view.count} ready`
      : view.status === "ready" ? `${view.ready} of ${view.count} short${view.count === 1 ? "" : "s"} ready`
        : view.status === "cancelled" ? "Render cancelled" : "Render failed";
    setText(el.progressStatus, headline);
    show(el.progressSpin, pending);
    const pct = isNum(view.percent) ? Math.round(view.percent) : 0;
    setText(el.progressPercent, pending ? `~${pct}%` : "");
    el.progressFill.style.width = `${pending ? pct : 100}%`;
    el.progressBar.setAttribute("aria-valuenow", String(pending ? pct : 100));
    el.progressBar.setAttribute("aria-valuetext", pending ? `about ${pct} percent (estimate)` : headline);
    const rows = view.clips.map((c) => {
      const li = document.createElement("li");
      li.className = "flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm";
      const what = document.createElement("span");
      what.className = "min-w-0 flex-1 tabular-nums text-ink-900 dark:text-ink-50";
      what.textContent = `Short ${c.number} · ${fmtTime(c.start)} – ${fmtTime(c.end)}`;
      const state = document.createElement("span");
      state.className = "tabular-nums text-ink-700 dark:text-ink-300";
      let text = CLIP_STATUS[c.status] || c.status;
      if (c.status === "rendering" && isNum(c.percent)) text += ` ${Math.round(c.percent)}%`;
      if (c.status === "analyzing" && isNum(c.percent)) text += ` ${Math.round(c.percent)}%`;
      if (isNum(c.eta_seconds) && c.status === "rendering") text += ` · about ${fmtTime(c.eta_seconds)} left (estimate)`;
      if (c.status === "ready" && isNum(c.width)) text += ` · ${c.width}×${c.height}, ${fmtTime(c.duration || 0)}`;
      state.textContent = text;
      li.append(what, state);
      if (c.status === "ready") {
        const base = `/api/renders/${encodeURIComponent(view.render_id)}/clips/${c.number}/file`;
        li.append(link("MP4", base, `Download short ${c.number} as MP4`));
        if ((c.files || []).includes("srt")) li.append(link("SRT", `${base}?kind=srt`, `Download short ${c.number}'s subtitles`));
      }
      if (CLIP_PENDING.includes(c.status) && pending) {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.textContent = "Skip";
        cancel.setAttribute("aria-label", `Cancel short ${c.number}`);
        cancel.className = "h-8 rounded-lg border border-ink-300 px-2 text-xs font-semibold text-ink-700 hover:border-red-300 "
          + "hover:text-red-700 focus-visible:outline-2 focus-visible:outline-brand-700 dark:border-ink-700 dark:text-ink-200";
        cancel.addEventListener("click", () => cancelClip(c.number));
        li.append(cancel);
      }
      const extra = (c.error && c.error.message) || c.notice;
      if (extra) {
        const note = document.createElement("p");
        note.className = `w-full text-xs ${c.error ? "font-medium text-red-700 dark:text-red-400" : "text-amber-800 dark:text-amber-200"}`;
        note.textContent = extra;
        li.append(note);
      }
      return li;
    });
    el.results.replaceChildren(...rows);
    const zip = !pending && view.ready > 0;
    show(el.zip, zip);
    if (zip) el.zip.href = `/api/renders/${encodeURIComponent(view.render_id)}/zip`;
    show(el.cancel, pending);
  }

  async function cancelRender() {
    if (!renderId) return;
    el.cancel.disabled = true;
    try {
      renderView = await api(`/api/renders/${encodeURIComponent(renderId)}/cancel`, {});
      paintProgress();
    } catch (err) {
      setLine(el.renderMsg, err.message, "error");
    } finally {
      el.cancel.disabled = false;
      paint();
    }
  }

  async function cancelClip(number) {
    try {
      renderView = await api(`/api/renders/${encodeURIComponent(renderId)}/clips/${number}/cancel`, {});
      paintProgress();
    } catch (err) {
      setLine(el.renderMsg, err.message, "error");
    }
  }

  // ---- buttons -------------------------------------------------------------------------------------------

  function paint() {
    const ready = Boolean(options && options.available && source);
    const busy = Boolean(renderView && RENDER_PENDING.includes(renderView.status));
    const count = chosen().length;
    el.render.disabled = !ready || busy || count === 0;
    setText(el.renderLabel, count ? `Render ${count} short${count === 1 ? "" : "s"}` : "Render shorts");
    for (const b of [el.previewFrame, el.previewClip]) b.disabled = !ready || clips.length === 0;
    el.useMarks.disabled = !source;
    el.selectAll.disabled = !clips.length;
    setText(el.selectAll, clips.length && count === clips.length ? "Select none" : "Select all");
    el.transcribe.disabled = !source || Boolean(transcribing);
  }

  function toggleAll() {
    if (chosen().length === clips.length) selected = new Set();
    else selected = new Set(clips.map(keyOf));
    renderClips();
    paint();
    store();
    refreshCaptions();
  }

  // ---- wiring ---------------------------------------------------------------------------------------------

  const changed = () => {
    outputs();
    store();
  };
  el.template.addEventListener("change", () => {
    applyTemplate(el.template.value);
    changed();
    refreshCaptions();
  });
  el.style.addEventListener("change", () => {
    applyStyle(el.style.value);
    changed();
    refreshCaptions();
  });
  for (const input of [el.platform, el.quality, el.fps, el.layout, el.margin, el.manual, el.manualX, el.manualY,
    el.manualZoom, el.captions, el.burn, el.srt, el.position, el.size, el.words, el.color, el.highlight,
    el.highlightMode, el.uppercase, el.logoPosition, el.logoSize, el.logoOpacity, el.watermark, el.brandColor,
    el.brandOn, el.titleBar, el.previewAt, el.previewPick]) {
    input.addEventListener("input", outputs);
    input.addEventListener("change", changed);
  }
  for (const input of [el.platform, el.words, el.size]) input.addEventListener("change", refreshCaptions);
  el.useMarks.addEventListener("click", useMarks);
  el.selectAll.addEventListener("click", toggleAll);
  el.transcribe.addEventListener("click", transcribe);
  el.cuesAdd.addEventListener("click", editorAdd);
  el.cuesReset.addEventListener("click", editorReset);
  el.cuesDone.addEventListener("click", editorDone);
  el.logo.addEventListener("change", onLogo);
  el.logoRemove.addEventListener("click", removeLogo);
  el.intro.addEventListener("change", () => onBumper("intro"));
  el.outro.addEventListener("change", () => onBumper("outro"));
  el.presetSave.addEventListener("click", savePreset);
  el.presetLoad.addEventListener("click", loadPreset);
  el.presetDelete.addEventListener("click", deletePreset);
  el.presetExport.addEventListener("click", exportPresets);
  el.presetImport.addEventListener("change", importPresets);
  el.previewFrame.addEventListener("click", () => startPreview("frame"));
  el.previewClip.addEventListener("click", () => startPreview("clip"));
  el.render.addEventListener("click", startRender);
  el.cancel.addEventListener("click", cancelRender);
  setLine(el.status, MSG.loading);
  paint();
  bridge.onSource(onSource);
  bridge.onClips(onClips);
  loadOptions();
})();
