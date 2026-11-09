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


def test_footer_copy_is_exact():
    assert FOOTER in _html()


def test_references_built_css_and_deferred_app_js():
    tags = _parsed().tags
    assert any(t == "link" and a.get("rel") == "stylesheet" and a.get("href") == "/static/dist/styles.css"
               for t, a in tags)
    scripts = [a for t, a in tags if t == "script"]
    assert scripts == [{"src": "/static/app.js", "defer": None}]


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


def test_app_js_carries_pinned_copy():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for text in (LOST, SAVED, NO_AUDIO):
        assert text in js
    assert "sessionStorage" in js


def test_app_js_never_injects_html():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                "setAttribute(\"style\"", "setAttribute('style'", "cssText"):
        assert bad not in js, bad
