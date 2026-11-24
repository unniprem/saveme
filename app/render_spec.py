"""Auto Shorts requests: the strict request schemas and their resolution, template ⊕ platform ⊕
overrides, into one ResolvedSpec (docs/AUTO_SHORTS_DESIGN.md §7, §8).

Everything a browser sends is untrusted, a saved preset included. Every field is bounded and
typed (extra fields refused), and every colour is #RRGGBB. Text (titles, cue text, the watermark)
is cleaned and length-capped, and only ever reaches an ASS file through app.captions' escaping.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.caption_styles import FONT_PRESETS, HIGHLIGHT_MODES, POSITIONS, RANGES, STYLES
from app.mediafile import MAX_BATCH_CLIPS
from app.shorts_templates import FPS_CHOICES, LAYOUT_DEFAULTS, PLATFORMS, QUALITY_PRESETS, TEMPLATES
from app.transcript import clean_text

Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")]
Unit = Annotated[float, Field(strict=True, ge=0, le=1, allow_inf_nan=False)]
Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
AssetId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22}$")]
TemplateId = Literal[tuple(TEMPLATES)]  # type: ignore[valid-type]
PlatformId = Literal[tuple(PLATFORMS)]  # type: ignore[valid-type]
StyleId = Literal[tuple(STYLES)]  # type: ignore[valid-type]
FontId = Literal[tuple(FONT_PRESETS)]  # type: ignore[valid-type]
Corner = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
MAX_CUES = 500
MAX_CUE_CHARS = 200
MAX_TITLE_CHARS = 120
MAX_WATERMARK_CHARS = 60
MAX_SPEAKER_CHARS = 32


def _num(low: float, high: float):
    return Annotated[float, Field(strict=True, ge=low, le=high, allow_inf_nan=False)]


def _int(low: int, high: int):
    return Annotated[int, Field(strict=True, ge=low, le=high)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManualFrame(_Strict):
    x: Unit = 0.5
    y: Unit = 0.5
    zoom: _num(1.0, 3.0) = 1.0


class LayoutSpec(_Strict):
    mode: Literal["center", "face", "speaker", "split", "blur", "original"] | None = None
    fallback: Literal["center", "blur", "original"] | None = None
    margin: _num(0.0, 0.25) | None = None
    deadzone: _num(0.0, 0.8) | None = None
    fg_position: Unit | None = None
    split_ratio: _num(0.3, 0.7) | None = None


class CaptionSpec(_Strict):
    enabled: StrictBool = True
    burn: StrictBool = True
    export: Annotated[list[Literal["srt", "ass"]], Field(max_length=2)] = []
    style: StyleId | None = None
    font: FontId | None = None
    bold: StrictBool | None = None
    size: _int(*RANGES["size"]) | None = None
    color: Color | None = None
    highlight: Color | None = None
    highlight_mode: Literal[HIGHLIGHT_MODES] | None = None  # type: ignore[valid-type]
    outline: _num(*RANGES["outline"]) | None = None
    outline_color: Color | None = None
    shadow: _num(*RANGES["shadow"]) | None = None
    box: StrictBool | None = None
    box_color: Color | None = None
    box_opacity: _num(*RANGES["box_opacity"]) | None = None
    uppercase: StrictBool | None = None
    max_words: _int(*RANGES["max_words"]) | None = None
    max_chars: _int(*RANGES["max_chars"]) | None = None
    max_lines: _int(*RANGES["max_lines"]) | None = None
    position: Literal[POSITIONS] | None = None  # type: ignore[valid-type]
    offset: _num(-0.4, 0.4) | None = None

    @field_validator("export")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("export lists a format twice")
        return value


class BrandingSpec(_Strict):
    logo: AssetId | None = None
    logo_position: Corner | None = None
    logo_scale: _num(0.05, 0.4) = 0.15
    logo_opacity: _num(0.1, 1.0) = 0.9
    watermark: Annotated[str, Field(max_length=MAX_WATERMARK_CHARS * 4)] = ""
    watermark_position: Corner = "bottom-right"
    brand_color: Color | None = None
    intro: AssetId | None = None
    outro: AssetId | None = None
    title_bar: StrictBool | None = None

    @field_validator("watermark")
    @classmethod
    def _clean(cls, value: str) -> str:
        return clean_text(value, MAX_WATERMARK_CHARS)


class RenderSpec(_Strict):
    template: TemplateId = "talking_head"
    platform: PlatformId = "youtube_shorts"
    width: _int(240, 3840) | None = None
    height: _int(240, 3840) | None = None
    fps: Literal[FPS_CHOICES] = "source"  # type: ignore[valid-type]
    quality: Literal[tuple(QUALITY_PRESETS)] = "standard"  # type: ignore[valid-type]
    layout: LayoutSpec = LayoutSpec()
    captions: CaptionSpec = CaptionSpec()
    branding: BrandingSpec = BrandingSpec()

    @model_validator(mode="after")
    def _size(self) -> "RenderSpec":
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height go together")
        if self.width is not None and (self.width % 2 or self.height % 2):
            raise ValueError("width and height must be even")
        if self.width is not None and self.width * self.height > 2160 * 3840:
            raise ValueError("width × height can be at most 2160 × 3840")
        return self


class Cue(_Strict):
    start: Seconds
    end: Seconds
    text: Annotated[str, Field(min_length=1, max_length=MAX_CUE_CHARS * 4)]
    speaker: Annotated[str, Field(max_length=MAX_SPEAKER_CHARS * 4)] | None = None

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        text = clean_text(value, MAX_CUE_CHARS)
        if not text:
            raise ValueError("a cue needs text")
        return text

    @field_validator("speaker")
    @classmethod
    def _speaker(cls, value: str | None) -> str | None:
        return clean_text(value, MAX_SPEAKER_CHARS) or None if value is not None else None

    @model_validator(mode="after")
    def _range(self) -> "Cue":
        if self.end <= self.start:
            raise ValueError("a cue must end after it starts")
        return self


class RenderClip(_Strict):
    start: Seconds
    end: Seconds
    title: Annotated[str, Field(max_length=MAX_TITLE_CHARS * 4)] | None = None
    cues: Annotated[list[Cue], Field(max_length=MAX_CUES)] | None = None
    manual: ManualFrame | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        return clean_text(value, MAX_TITLE_CHARS) or None if value is not None else None

    @model_validator(mode="after")
    def _times(self) -> "RenderClip":
        if self.end <= self.start:
            raise ValueError("a clip must end after it starts")
        if self.cues is not None:
            previous = 0.0
            for cue in self.cues:
                if cue.start < previous - 1e-6:
                    raise ValueError("cues must be in order and must not overlap")
                if cue.end > self.end - self.start + 0.05:
                    raise ValueError("a cue ends after the clip")
                previous = cue.end
        return self


class RenderBody(_Strict):
    clips: Annotated[list[RenderClip], Field(min_length=1, max_length=MAX_BATCH_CLIPS)]
    spec: RenderSpec = RenderSpec()


class PreviewBody(_Strict):
    clip: RenderClip
    spec: RenderSpec = RenderSpec()
    kind: Literal["frame", "clip"] = "frame"
    at: Seconds = 0.0


# --- resolution ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedSpec:
    template: str
    platform: str
    aspect: str
    width: int | None
    height: int | None
    fps: float | None
    quality: str
    layout: dict  # the renderer's layout request (app.layouts.request_from)
    captions: dict  # the full caption style, plus enabled/burn/export/position/offset
    safe: dict  # the platform's safe area, as shares of each edge
    branding: BrandingSpec
    title_bar: bool
    logo_position: str


def resolve(spec: RenderSpec) -> ResolvedSpec:
    """Merge the template's defaults, the platform's format and the request's overrides."""
    template = TEMPLATES[spec.template]
    platform = PLATFORMS[spec.platform]
    aspect = platform["aspect"]
    width, height = (spec.width, spec.height) if spec.width is not None else (platform["width"], platform["height"])
    layout = {**LAYOUT_DEFAULTS, **template["layout"]}
    layout.update({k: v for k, v in spec.layout.model_dump().items() if v is not None})
    brand = spec.branding.brand_color
    layout["pad_color"] = brand or "#000000"
    style_id = spec.captions.style or template["captions"]["style"]
    style = dict(STYLES[style_id])
    overrides = spec.captions.model_dump(exclude={"enabled", "burn", "export", "style", "position", "offset"})
    style.update({k: v for k, v in overrides.items() if v is not None})
    if spec.captions.highlight is None and brand and style["highlight_mode"] != "none":
        style["highlight"] = brand
    captions = {
        **style, "style": style_id, "enabled": spec.captions.enabled, "burn": spec.captions.burn,
        "export": list(spec.captions.export),
        "position": spec.captions.position or template["captions"]["position"],
        "offset": template["captions"]["offset"] if spec.captions.offset is None else spec.captions.offset,
    }
    # A split layout puts captions on the seam unless asked otherwise.
    if layout["mode"] == "split" and spec.captions.position is None:
        captions["position"] = "middle"
    title_bar = template["branding"]["title_bar"] if spec.branding.title_bar is None else spec.branding.title_bar
    return ResolvedSpec(
        template=spec.template, platform=spec.platform, aspect=aspect, width=width, height=height,
        fps=None if spec.fps == "source" else float(spec.fps), quality=spec.quality, layout=layout,
        captions=captions, safe=dict(platform["safe"]), branding=spec.branding, title_bar=title_bar,
        logo_position=spec.branding.logo_position or template["branding"]["logo_position"])


def options() -> dict:
    """The presets the page offers (served by GET /api/shorts/options)."""
    return {
        "platforms": [{"id": k, **{f: v[f] for f in ("name", "aspect", "width", "height", "max_seconds", "note")},
                       "safe": dict(v["safe"])} for k, v in PLATFORMS.items()],
        "templates": [{"id": k, "name": v["name"], "description": v["description"], "aspect": v["aspect"],
                       "layout": dict(v["layout"]), "captions": dict(v["captions"]),
                       "branding": dict(v["branding"])} for k, v in TEMPLATES.items()],
        "qualities": [{"id": k, **v} for k, v in QUALITY_PRESETS.items()],
        "fps": list(FPS_CHOICES),
        "styles": [{"id": k, **v} for k, v in STYLES.items()],
        "fonts": [{"id": k, "name": v["name"], "licence": v["licence"]} for k, v in FONT_PRESETS.items()],
        "layouts": [
            {"id": "center", "name": "Center crop"},
            {"id": "face", "name": "Face tracking"},
            {"id": "speaker", "name": "Speaker focus (best effort)"},
            {"id": "split", "name": "Split-screen (two people)"},
            {"id": "blur", "name": "Blurred background"},
            {"id": "original", "name": "Original framing"},
        ],
    }
