"""Auto Shorts: the FFmpeg filtergraph for one rendered clip (docs/AUTO_SHORTS_DESIGN.md §4).

Pure: a GraphPlan in, filtergraph text out. Every value in the graph is a number or an enum chosen
here. File names are fixed relative names (`subs.ass`) resolved against FFmpeg's working
directory, the render's own folder. No user text ever enters a graph, so nothing needs escaping.

Inputs, in order: 0 the source (already trimmed to the clip by -ss/-t), then the logo, intro and
outro when used. Outputs: [vout] (yuv420p, the canvas size) and, for video, [aout] (48 kHz stereo).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.reframe import Path, path_expr

SUBTITLES_NAME = "subs.ass"
_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_BLUR_DOWNSCALE = 4  # the background is blurred at a quarter of the canvas size, then scaled up


@dataclass(frozen=True)
class CropPlan:
    """A window of the source, width×height, whose top-left corner follows x(t), y(t)."""
    width: int
    height: int
    x: Path
    y: Path


@dataclass(frozen=True)
class CropLayout:
    """One window scaled to the whole canvas (center, face, speaker, manual)."""
    crop: CropPlan


@dataclass(frozen=True)
class FitLayout:
    """The whole frame scaled to fg_width×fg_height at (fg_x, fg_y), over a blurred copy of itself
    (`blur`) or a plain colour (`color`, the `original` mode)."""
    fg_width: int
    fg_height: int
    fg_x: int
    fg_y: int
    background: str  # blur | color
    color: str = "#000000"


@dataclass(frozen=True)
class SplitLayout:
    """Two windows stacked: `top` scaled to canvas_width×top_height, `bottom` to the rest."""
    top: CropPlan
    bottom: CropPlan
    top_height: int


Layout = CropLayout | FitLayout | SplitLayout


@dataclass(frozen=True)
class LogoPlan:
    width: int  # pixels on the canvas (height follows the logo's aspect)
    x: int
    y: int
    opacity: float  # 0.1–1


@dataclass(frozen=True)
class Bumper:
    """An intro or outro: its own video (and audio, if any), fitted onto the canvas."""
    duration: float
    has_audio: bool


@dataclass(frozen=True)
class GraphPlan:
    canvas_width: int
    canvas_height: int
    fps: float
    duration: float  # the clip's length (without intro/outro)
    layout: Layout
    source_has_audio: bool
    source_sar: tuple[int, int] = (1, 1)
    subtitles: bool = False
    fonts_dir: str | None = None
    logo: LogoPlan | None = None
    intro: Bumper | None = None
    outro: Bumper | None = None
    pad_color: str = "#000000"
    frame_at: float | None = None  # a still at this clip time instead of a video
    extra: dict = field(default_factory=dict)

    @property
    def total_duration(self) -> float:
        return self.duration + sum(b.duration for b in (self.intro, self.outro) if b is not None)


def _color(value: str) -> str:
    if not _COLOR.fullmatch(value):
        raise ValueError(f"not a #RRGGBB colour: {value!r}")
    return "0x" + value[1:].upper()


def _n(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _crop(plan: CropPlan) -> str:
    x = _n(plan.x.points[0][1]) if plan.x.is_still else f"'{path_expr(plan.x)}'"
    y = _n(plan.y.points[0][1]) if plan.y.is_still else f"'{path_expr(plan.y)}'"
    return f"crop=w={plan.width}:h={plan.height}:x={x}:y={y}"


def _scale(width: int, height: int) -> str:
    return f"scale={width}:{height}:flags=lanczos,setsar=1"


def _layout(layout: Layout, w: int, h: int, src: str, out: str) -> list[str]:
    if isinstance(layout, CropLayout):
        return [f"[{src}]{_crop(layout.crop)},{_scale(w, h)}[{out}]"]
    if isinstance(layout, SplitLayout):
        top_h, bottom_h = layout.top_height, h - layout.top_height
        return [f"[{src}]split=2[sa][sb]",
                f"[sa]{_crop(layout.top)},{_scale(w, top_h)}[st]",
                f"[sb]{_crop(layout.bottom)},{_scale(w, bottom_h)}[sbt]",
                f"[st][sbt]vstack=inputs=2[{out}]"]
    fg = f"scale={layout.fg_width}:{layout.fg_height}:flags=lanczos,setsar=1"
    place = f"x={layout.fg_x}:y={layout.fg_y}"
    if layout.background == "blur":
        bw, bh = max(2, w // _BLUR_DOWNSCALE // 2 * 2), max(2, h // _BLUR_DOWNSCALE // 2 * 2)
        radius = max(1, min(bw, bh) // 12)
        return [f"[{src}]split=2[ba][fa]",
                f"[ba]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                f"boxblur=luma_radius={radius}:luma_power=3:chroma_radius={radius}:chroma_power=3,"
                f"scale={w}:{h}:flags=bilinear,eq=brightness=-0.08:saturation=1.1,setsar=1[bb]",
                f"[fa]{fg}[ff]",
                f"[bb][ff]overlay={place}[{out}]"]
    return [f"[{src}]{fg},pad={w}:{h}:{layout.fg_x}:{layout.fg_y}:color={_color(layout.color)}[{out}]"]


def _bumper(index: int, name: str, bumper: Bumper, plan: GraphPlan) -> list[str]:
    w, h = plan.canvas_width, plan.canvas_height
    d = _n(bumper.duration)
    video = (f"[{index}:V:0]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={_color(plan.pad_color)},setsar=1,fps={_n(plan.fps)},"
             f"format=yuv420p,trim=duration={d},setpts=PTS-STARTPTS[{name}v]")
    if bumper.has_audio:
        audio = (f"[{index}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                 f"atrim=duration={d},asetpts=PTS-STARTPTS[{name}a]")
    else:
        audio = f"anullsrc=r=48000:cl=stereo,atrim=duration={d},asetpts=PTS-STARTPTS[{name}a]"
    return [video, audio]


def build(plan: GraphPlan) -> str:
    """The filtergraph text (statements separated by `;` and newlines)."""
    w, h = plan.canvas_width, plan.canvas_height
    if w % 2 or h % 2 or w < 2 or h < 2:
        raise ValueError("the canvas needs even sides")
    lines: list[str] = []
    prefix = []
    sar_n, sar_d = plan.source_sar
    if sar_n > 0 and sar_d > 0 and sar_n != sar_d:
        prefix.append(f"scale=trunc(iw*{sar_n}/{sar_d}/2)*2:ih")
    prefix.append("setsar=1")
    if plan.frame_at is None:
        # start_time=0: the first frame decoded after the seek may sit a fraction of a frame past
        # the clip's start; it is held from 0, so video and audio both start at 0 and t (which crop
        # paths and captions use) is the clip's own time.
        prefix.append(f"fps={_n(plan.fps)}:start_time=0")
    else:  # one frame, stamped with its clip time so crop paths and captions match the video
        prefix.append(f"setpts=PTS-STARTPTS+{_n(plan.frame_at)}/TB")
    lines.append(f"[0:V:0]{','.join(prefix)}[src]")
    lines += _layout(plan.layout, w, h, "src", "base")
    last = "base"
    if plan.subtitles:
        fonts = f":fontsdir={plan.fonts_dir}" if plan.fonts_dir else ""
        lines.append(f"[{last}]ass=filename={SUBTITLES_NAME}:shaping=complex{fonts}[capt]")
        last = "capt"
    next_input = 1
    if plan.logo is not None:
        logo = plan.logo
        alpha = _n(max(0.0, min(1.0, logo.opacity)))
        lines.append(f"[{next_input}:v]format=rgba,scale={logo.width}:-2:flags=lanczos,"
                     f"colorchannelmixer=aa={alpha}[logo]")
        lines.append(f"[{last}][logo]overlay=x={logo.x}:y={logo.y}:format=auto[branded]")
        last = "branded"
        next_input += 1
    if plan.frame_at is not None:
        lines.append(f"[{last}]format=yuv420p[vout]")
        return ";\n".join(lines)
    lines.append(f"[{last}]format=yuv420p,setsar=1[vmain]")
    d = _n(plan.duration)
    if plan.source_has_audio:
        lines.append(f"[0:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,"
                     f"atrim=duration={d},asetpts=PTS-STARTPTS[amain]")
    else:
        lines.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d},asetpts=PTS-STARTPTS[amain]")
    parts = []
    for name, bumper in (("intro", plan.intro), ("outro", plan.outro)):
        if bumper is not None:
            lines += _bumper(next_input, name, bumper, plan)
            next_input += 1
    segments = []
    if plan.intro is not None:
        segments.append("[introv][introa]")
    segments.append("[vmain][amain]")
    if plan.outro is not None:
        segments.append("[outrov][outroa]")
    if len(segments) == 1:
        lines.append("[vmain]null[vout]")
        lines.append("[amain]anull[aout]")
    else:
        parts = "".join(segments)
        lines.append(f"{parts}concat=n={len(segments)}:v=1:a=1[vout][aout]")
    return ";\n".join(lines)
