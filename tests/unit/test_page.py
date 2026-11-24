"""Static checks on the page files (the HTTP-level checks live in tests/integration/test_api.py).

The CSP is `script-src 'self'; style-src 'self'`, so the page must not carry inline scripts,
inline styles, style attributes or inline event handlers.
"""
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "static"
FOOTER = ("Only download media you have the right to save. "
          "Works with many sites, not all — compatibility depends on the source.")
LOST = "This download was lost (the server may have restarted). Please start again."
SAVED = "Saved — file removed from server."
NO_AUDIO = "This media has no audio track."


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_bodies: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.script_bodies.append("")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self.script_bodies[-1] += data


def _html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _parsed() -> _Collector:
    c = _Collector()
    c.feed(_html())
    c.close()
    return c


def test_index_has_labelled_url_field_and_live_region():
    h = _html()
    assert '<label for="url"' in h
    assert 'id="url"' in h
    assert 'aria-live="polite"' in h
    inputs = [a for t, a in _parsed().tags if t == "input" and a.get("id") == "url"]
    assert len(inputs) == 1


def test_how_it_works_mentions_m4a():
    assert "MP3 or M4A audio" in _html()


def test_footer_copy_is_exact():
    assert FOOTER in _html()


SCRIPTS = ["common.js", "studio.js", "ai.js", "app.js"]  # deferred, so they run in this order


def test_references_built_css_and_deferred_scripts():
    tags = _parsed().tags
    assert any(t == "link" and a.get("rel") == "stylesheet" and a.get("href") == "/static/dist/styles.css"
               for t, a in tags)
    scripts = [a for t, a in tags if t == "script"]
    assert scripts == [{"src": f"/static/{name}", "defer": None} for name in SCRIPTS]
    for name in SCRIPTS:
        assert (STATIC / name).is_file(), name


def test_shared_helpers_come_from_common_js():
    common = (STATIC / "common.js").read_text(encoding="utf-8")
    assert "window.SaveMe = Object.freeze(" in common
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "window.SaveMe" in app
    for helper in ("async function api(", "class ApiError", "function fmtDuration("):
        assert helper in common and helper not in app, helper


def test_no_inline_script_style_or_handlers():
    c = _parsed()
    assert all(body.strip() == "" for body in c.script_bodies), "inline <script> content"
    assert not any(t == "style" for t, _ in c.tags), "<style> tag"
    for tag, attrs in c.tags:
        assert "style" not in attrs, f"style= attribute on <{tag}>"
        handlers = [k for k in attrs if k.startswith("on")]
        assert not handlers, f"inline handler {handlers} on <{tag}>"
        for k in ("href", "src", "action"):
            assert not (attrs.get(k) or "").strip().lower().startswith("javascript:")
    assert "style=\"" not in _html()


def test_page_uses_brand_icon_and_logo():
    tags = _parsed().tags
    icons = [a for t, a in tags if t == "link" and a.get("rel") == "icon"]
    assert icons == [{"rel": "icon", "href": "/static/brand/icon.svg", "type": "image/svg+xml"}]
    logos = [a for t, a in tags if t == "img" and a.get("alt") == "SaveMe"]
    assert [a.get("src") for a in logos] == ["/static/brand/lockup-white.svg"]
    for name in ("icon.svg", "lockup-white.svg"):
        assert (STATIC / "brand" / name).is_file(), name


def test_brand_svgs_are_plain_drawings():
    # Shown as <img> under the page CSP: no scripts, embedded styles, HTML or remote references.
    svgs = sorted((STATIC / "brand").glob("*.svg"))
    assert svgs
    for svg in svgs:
        text = svg.read_text(encoding="utf-8").lower()
        for bad in ("<script", "<style", "<foreignobject", "href=\"http", "url(http", " on"):
            assert bad not in text, (svg.name, bad)


def test_format_choice_is_one_radio_group_with_one_download_button():
    tags = _parsed().tags
    radios = [a for t, a in tags if t == "input" and a.get("type") == "radio"]
    assert radios and all(a.get("name") == "format" for a in radios)
    buttons = [a.get("id") for t, a in tags if t == "button"]
    assert "download" in buttons
    assert not {"dl-video", "dl-audio"} & set(buttons)
    assert any(t == "fieldset" for t, _ in tags)


def test_app_js_carries_pinned_copy():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in (LOST, SAVED, NO_AUDIO):
        assert text in js
    assert "sessionStorage" in js
    assert "M4A audio (original)" in js


def test_static_js_never_injects_html():
    scripts = sorted(STATIC.glob("*.js"))
    assert [p.name for p in scripts] == sorted(SCRIPTS)
    for path in scripts:
        js = path.read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                    "new Function", "setAttribute(\"style\"", "setAttribute('style'", "cssText"):
            assert bad not in js, (path.name, bad)


def test_job_panel_has_cancel_and_resume_buttons():
    buttons = {a.get("id"): a for t, a in _parsed().tags if t == "button"}
    for name in ("cancel", "resume", "again"):
        assert name in buttons and buttons[name].get("type") == "button", name
        assert "hidden" in buttons[name], name  # app.js shows the ones that apply
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/cancel`" in js and "/resume`" in js
    assert "resume_seconds" in js
    assert "The partial download was removed from the server." in js


STUDIO_IDS = [
    "clip", "upload-file", "upload-label", "studio", "studio-title", "studio-close",
    "studio-loading", "studio-load-status", "studio-load-percent", "studio-load-bar", "studio-load-fill",
    "studio-load-meta", "studio-load-error", "studio-load-cancel",
    "studio-editor", "studio-video", "studio-preview-note", "timeline", "timeline-range",
    "timeline-playhead", "handle-start", "handle-end", "clip-start", "clip-end", "set-start", "set-end",
    "clip-length", "play-clip", "export-clip", "clip-msg",
    "export", "export-status", "export-percent", "export-bar", "export-fill", "export-meta",
    "export-save", "export-error", "export-cancel",
    "add-clip", "clip-list-wrap", "clip-list", "clip-list-count", "export-zip", "export-zip-label",
]
STUDIO_COPY = [
    "Clips are cut without re-encoding, so they keep the original quality. "
    "A few players may show up to a few seconds before your start.",
    "Clip saved. Mark another part, or close the studio.",
    "Your browser can't preview this video. You can still type a start and end.",
    "ZIP saved. Mark more parts, or close the studio.",
    "Every clip in the list is cut and saved together in one ZIP file.",
    "This part is already in the clip list.",
]


def test_studio_markup():
    by_id = {a["id"]: (t, a) for t, a in _parsed().tags if a.get("id")}
    for name in STUDIO_IDS:
        assert name in by_id, name
    for tag, attrs in by_id.values():
        if tag == "button":
            assert attrs.get("type") == "button" or attrs.get("id") == "inspect", attrs
    for name in ("studio", "studio-editor", "export", "clip-list-wrap"):
        assert "hidden" in by_id[name][1], name
    for name in ("export", "studio-preview-note"):  # take focus from a control that disables itself
        assert by_id[name][1].get("tabindex") == "-1", name
    tag, rows = by_id["clip-list"]
    assert tag == "ol" and rows.get("aria-labelledby") == "clip-list-title"
    tag, upload = by_id["upload-file"]
    assert tag == "input" and upload.get("type") == "file" and "video/*" in upload.get("accept", "")
    for name in ("handle-start", "handle-end"):
        tag, attrs = by_id[name]
        assert attrs.get("role") == "slider" and attrs.get("tabindex") == "0" and attrs.get("aria-label"), name
    tag, video = by_id["studio-video"]
    assert tag == "video" and "controls" in video and video.get("preload") == "metadata"
    assert "Clip a video from your computer" in _html()


def test_studio_copy_is_pinned():
    page, studio = _html(), (STATIC / "studio.js").read_text(encoding="utf-8")
    for text in STUDIO_COPY:
        assert text in page or text in studio, text
    assert "/api/sources" in studio and "sessionStorage" in studio and "XMLHttpRequest" in studio
    assert "`${base}/batch`" in studio and "max_batch_clips" in studio
    assert "window.SaveMeStudio = Object.freeze(" in studio
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Close Clip Studio to fetch another link." in app and "SaveMeStudio" in app


def test_tailwind_scans_every_page_file():
    css = (STATIC / "src" / "input.css").read_text(encoding="utf-8")
    for name in ("index.html", "app.js", "studio.js", "ai.js"):
        assert f'@source "../{name}";' in css, name


AI_IDS = [
    "studio-tabs", "tab-manual", "tab-ai", "manual-panel", "ai-panel",
    "ai-status", "ai-test", "ai-test-msg", "ai-privacy",
    "ai-form", "ai-instruction", "ai-min", "ai-max", "ai-count", "ai-min-score", "ai-language",
    "ai-retranscribe", "ai-start", "ai-transcribe", "ai-estimate", "ai-form-msg",
    "ai-progress", "ai-stages", "ai-progress-status", "ai-progress-percent", "ai-progress-bar",
    "ai-progress-fill", "ai-progress-detail", "ai-cancel",
    "ai-results", "ai-results-title", "ai-usage", "ai-notice", "ai-error", "ai-select-all",
    "ai-select-none", "ai-add-selected", "ai-cards",
    "ai-transcript", "ai-transcript-summary", "ai-search", "ai-search-count", "ai-lines",
    "ai-subs-file", "ai-subs-msg",
]
AI_COPY = [
    "Transcription runs on this computer. To find highlights, SaveMe sends the transcript text "
    "(never the audio or video) to OpenRouter, which passes it to the AI model.",
    "Scores are the AI's editorial judgement of each moment, not a prediction of views.",
    "Add selected to clip list",
]


def test_ai_highlights_markup():
    by_id = {a["id"]: (t, a) for t, a in _parsed().tags if a.get("id")}
    for name in AI_IDS:
        assert name in by_id, name
    tag, tabs = by_id["studio-tabs"]
    assert tabs.get("role") == "tablist" and tabs.get("aria-label")
    for tab, panel in (("tab-manual", "manual-panel"), ("tab-ai", "ai-panel")):
        tag, attrs = by_id[tab]
        assert tag == "button" and attrs.get("role") == "tab" and attrs.get("aria-controls") == panel
        assert by_id[panel][1].get("role") == "tabpanel" and by_id[panel][1].get("aria-labelledby") == tab
    assert by_id["tab-manual"][1].get("aria-selected") == "true"
    assert by_id["tab-ai"][1].get("aria-selected") == "false" and "hidden" in by_id["ai-panel"][1]
    labels = {a.get("for") for t, a in _parsed().tags if t == "label"}
    for name in ("ai-instruction", "ai-min", "ai-max", "ai-count", "ai-min-score", "ai-language",
                 "ai-retranscribe", "ai-search", "ai-subs-file"):
        assert name in labels, name
    assert by_id["ai-instruction"][0] == "textarea" and by_id["ai-instruction"][1].get("maxlength") == "500"
    assert by_id["ai-subs-file"][1].get("type") == "file" and ".srt" in by_id["ai-subs-file"][1]["accept"]
    assert by_id["ai-cards"][0] == "ol" and by_id["ai-cards"][1].get("aria-labelledby") == "ai-results-title"
    assert by_id["ai-progress-bar"][1].get("role") == "progressbar"
    for name in ("ai-progress", "ai-results"):  # follow progress when a button disables itself
        assert by_id[name][1].get("tabindex") == "-1", name
    for name in ("ai-form-msg", "ai-test-msg", "ai-subs-msg"):
        assert by_id[name][1].get("aria-live") == "polite", name
    assert by_id["ai-transcript"][0] == "details"


def test_ai_copy_is_pinned():
    page, ai = _html(), (STATIC / "ai.js").read_text(encoding="utf-8")
    for text in AI_COPY:
        assert text in page or text in ai, text
    for route in ("/api/ai/status", "/api/ai/test", "/analyses/estimate", "/analyses`", "/transcript",
                  "/cancel`"):
        assert route in ai, route
    assert "window.SaveMeStudioBridge" in ai and "sessionStorage" in ai
    studio = (STATIC / "studio.js").read_text(encoding="utf-8")
    assert "window.SaveMeStudioBridge = Object.freeze(" in studio
