"""Auto Shorts presets and requests: templates are complete and declarative, resolution merges
template ⊕ platform ⊕ overrides, and invalid or tampered presets are refused."""
import json

import pytest
from pydantic import ValidationError

from app.caption_styles import FONT_PRESETS, STYLES
from app.layouts import MODES, request_from
from app.render_spec import PreviewBody, RenderBody, RenderClip, RenderSpec, options, resolve
from app.shorts_templates import PLATFORMS, TEMPLATES

REQUIRED_TEMPLATES = {"talking_head", "interview", "podcast_split", "educational", "news_commentary",
                      "full_frame_blur"}
REQUIRED_STYLES = {"clean_minimal", "bold_creator", "karaoke", "podcast_classic", "cinematic", "high_contrast"}


def test_every_required_template_and_style_exists():
    assert set(TEMPLATES) == REQUIRED_TEMPLATES
    assert set(STYLES) == REQUIRED_STYLES
    assert {p["aspect"] for p in PLATFORMS.values()} == {"9:16", "4:5", "1:1", "16:9", "source"}


@pytest.mark.parametrize("template_id", sorted(REQUIRED_TEMPLATES))
def test_each_template_defines_aspect_layout_captions_and_branding(template_id):
    template = TEMPLATES[template_id]
    assert {"name", "description", "aspect", "layout", "captions", "branding"} <= set(template)
    assert template["layout"]["mode"] in MODES
    assert template["captions"]["style"] in STYLES
    assert template["captions"]["position"] in ("top", "middle", "bottom")
    assert template["branding"]["logo_position"] in ("top-left", "top-right", "bottom-left", "bottom-right")
    resolved = resolve(RenderSpec(template=template_id))
    request_from(resolved.layout)  # the renderer accepts the resolved layout
    assert resolved.captions["style"] == template["captions"]["style"]


def test_templates_map_to_the_brief():
    modes = {k: v["layout"]["mode"] for k, v in TEMPLATES.items()}
    assert modes == {"talking_head": "face", "interview": "speaker", "podcast_split": "split",
                     "educational": "blur", "news_commentary": "blur", "full_frame_blur": "blur"}
    assert TEMPLATES["news_commentary"]["branding"]["title_bar"] is True


def test_styles_are_complete_and_use_known_fonts():
    keys = set(STYLES["clean_minimal"])
    for style in STYLES.values():
        assert set(style) == keys and style["font"] in FONT_PRESETS
    assert STYLES["karaoke"]["highlight_mode"] != "none" and STYLES["bold_creator"]["uppercase"]


def test_resolution_merges_template_platform_and_overrides():
    spec = RenderSpec.model_validate({
        "template": "talking_head", "platform": "instagram_feed", "fps": 30, "quality": "high",
        "layout": {"mode": "blur", "fg_position": 0.4}, "captions": {"style": "cinematic", "size": 44,
                                                                      "position": "top"},
        "branding": {"brand_color": "#E5484D"}})
    r = resolve(spec)
    assert (r.aspect, r.width, r.height, r.fps, r.quality) == ("4:5", 1080, 1350, 30.0, "high")
    assert r.layout["mode"] == "blur" and r.layout["fg_position"] == 0.4 and r.layout["fallback"] == "center"
    assert r.layout["pad_color"] == "#E5484D"
    assert r.captions["font"] == "serif" and r.captions["size"] == 44 and r.captions["position"] == "top"
    assert r.safe == dict(PLATFORMS["instagram_feed"]["safe"])


def test_brand_colour_becomes_the_default_highlight_only_where_one_is_used():
    r = resolve(RenderSpec.model_validate({"template": "talking_head", "branding": {"brand_color": "#00AA55"}}))
    assert r.captions["highlight"] == "#00AA55"
    r = resolve(RenderSpec.model_validate({"template": "talking_head", "branding": {"brand_color": "#00AA55"},
                                           "captions": {"highlight": "#FF0000"}}))
    assert r.captions["highlight"] == "#FF0000"
    r = resolve(RenderSpec.model_validate({"template": "educational", "branding": {"brand_color": "#00AA55"}}))
    assert r.captions["highlight"] == STYLES["clean_minimal"]["highlight"]


def test_defaults_are_a_9_16_youtube_short_with_branding_off():
    r = resolve(RenderSpec())
    assert (r.platform, r.aspect, r.width, r.height, r.fps, r.quality) == \
        ("youtube_shorts", "9:16", 1080, 1920, None, "standard")
    b = r.branding
    assert b.logo is None and b.watermark == "" and b.intro is None and b.outro is None and not r.title_bar
    assert r.captions["enabled"] and r.captions["burn"] and r.captions["export"] == []


def test_split_puts_captions_on_the_seam_unless_told_otherwise():
    assert resolve(RenderSpec.model_validate({"layout": {"mode": "split"}})).captions["position"] == "middle"
    spec = RenderSpec.model_validate({"layout": {"mode": "split"}, "captions": {"position": "bottom"}})
    assert resolve(spec).captions["position"] == "bottom"


def test_custom_size():
    r = resolve(RenderSpec.model_validate({"platform": "landscape", "width": 1280, "height": 720}))
    assert (r.width, r.height) == (1280, 720)


@pytest.mark.parametrize("bad", [
    {"template": "vlog"}, {"platform": "myspace"}, {"fps": 29.97}, {"fps": "30"}, {"quality": "ultra"},
    {"width": 1080}, {"width": 1081, "height": 1920}, {"width": 100, "height": 100}, {"width": 3840, "height": 3840},
    {"layout": {"mode": "zoom"}}, {"layout": {"margin": 0.5}}, {"layout": {"split_ratio": 0.9}},
    {"layout": {"fg_position": "0.5"}}, {"captions": {"style": "comic"}}, {"captions": {"size": 1000}},
    {"captions": {"color": "red"}}, {"captions": {"color": "#FFF"}}, {"captions": {"export": ["srt", "srt"]}},
    {"captions": {"export": ["vtt"]}}, {"captions": {"enabled": "yes"}}, {"captions": {"font": "Comic Sans"}},
    {"branding": {"logo": "../../etc/passwd"}}, {"branding": {"logo_scale": 0.9}},
    {"branding": {"logo_position": "center"}}, {"branding": {"brand_color": "javascript:alert(1)"}},
    {"branding": {"intro": "x"}}, {"extra": 1}, {"captions": {"script": "{\\p1}"}},
])
def test_invalid_presets_are_refused(bad):
    with pytest.raises(ValidationError):
        RenderSpec.model_validate(bad)


def test_text_is_cleaned_and_capped():
    spec = RenderSpec.model_validate({"branding": {"watermark": "  a‮b\x00c " + "x" * 200}})
    assert spec.branding.watermark.startswith("abc x") and len(spec.branding.watermark) == 60
    with pytest.raises(ValidationError):  # far too long is refused before cleaning
        RenderSpec.model_validate({"branding": {"watermark": "x" * 241}})
    clip = RenderClip.model_validate({"start": 0, "end": 10, "title": "t​\u0007itle",
                                      "cues": [{"start": 0, "end": 1, "text": " hi\nthere ", "speaker": " Ann "}]})
    assert clip.title == "title" and clip.cues[0].text == "hi there" and clip.cues[0].speaker == "Ann"


@pytest.mark.parametrize("clip", [
    {"start": 5, "end": 5}, {"start": -1, "end": 3}, {"start": 0, "end": "3"},
    {"start": 0, "end": 5, "cues": [{"start": 1, "end": 0.5, "text": "x"}]},
    {"start": 0, "end": 5, "cues": [{"start": 0, "end": 1, "text": "   "}]},
    {"start": 0, "end": 5, "cues": [{"start": 2, "end": 3, "text": "b"}, {"start": 0, "end": 1, "text": "a"}]},
    {"start": 0, "end": 5, "cues": [{"start": 0, "end": 2, "text": "a"}, {"start": 1, "end": 3, "text": "b"}]},
    {"start": 0, "end": 5, "cues": [{"start": 0, "end": 9, "text": "past the end"}]},
    {"start": 0, "end": 5, "manual": {"x": 2}}, {"start": 0, "end": 5, "manual": {"zoom": 0.5}},
])
def test_invalid_clips_are_refused(clip):
    with pytest.raises(ValidationError):
        RenderClip.model_validate(clip)


def test_bodies_bound_their_lists():
    clip = {"start": 0, "end": 5}
    assert len(RenderBody.model_validate({"clips": [clip] * 20}).clips) == 20
    for bad in ({"clips": []}, {"clips": [clip] * 21}, {"clips": [clip], "spec": {"template": "x"}}):
        with pytest.raises(ValidationError):
            RenderBody.model_validate(bad)
    with pytest.raises(ValidationError):
        RenderClip.model_validate({"start": 0, "end": 600, "cues": [{"start": i, "end": i + 0.5, "text": "x"}
                                                                    for i in range(501)]})
    assert PreviewBody.model_validate({"clip": clip, "kind": "clip"}).kind == "clip"
    with pytest.raises(ValidationError):
        PreviewBody.model_validate({"clip": clip, "kind": "gif"})


def test_options_serialise_and_list_everything():
    data = json.loads(json.dumps(options()))
    assert {t["id"] for t in data["templates"]} == REQUIRED_TEMPLATES
    assert {s["id"] for s in data["styles"]} == REQUIRED_STYLES
    assert {p["id"] for p in data["platforms"]} == set(PLATFORMS)
    assert {q["id"] for q in data["qualities"]} == {"draft", "standard", "high"}
    assert {lay["id"] for lay in data["layouts"]} == set(MODES)
    assert "best effort" in next(lay["name"] for lay in data["layouts"] if lay["id"] == "speaker")
