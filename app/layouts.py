"""Auto Shorts layouts: a layout request plus the source's size becomes a concrete render_graph
Layout (docs/AUTO_SHORTS_DESIGN.md §5.1).

Modes: center (or a manual position), face and speaker (tracked by app.faces; without a usable
analysis they fall back), split (two stacked panels), blur (whole frame over a blurred copy) and
original (whole frame over a plain colour). Pure.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.reframe import Path, centred_offset, clamp, crop_window, even, fit_size, nearly_same_aspect
from app.render_graph import CropLayout, CropPlan, FitLayout, Layout, SplitLayout

MODES = ("center", "face", "speaker", "split", "blur", "original")
FALLBACKS = ("center", "blur", "original")


@dataclass(frozen=True)
class Manual:
    x: float = 0.5  # the window's centre, 0–1 of the frame
    y: float = 0.5
    zoom: float = 1.0  # 1–3


@dataclass(frozen=True)
class LayoutRequest:
    mode: str = "center"
    fallback: str = "center"
    margin: float = 0.08  # safe margin around tracked faces, 0–0.25 of the window
    deadzone: float = 0.3  # the central share of the window a face may move in without the camera following
    manual: Manual | None = None
    fg_position: float = 0.5  # blur/original: the frame's vertical centre on the canvas, 0–1
    pad_color: str = "#000000"
    split_ratio: float = 0.5  # split: the top panel's share of the height


def request_from(data: dict) -> LayoutRequest:
    """A LayoutRequest from the worker's JSON, refusing anything out of range (the parent validated
    it already; this is the worker's own guard)."""
    mode, fallback = data.get("mode"), data.get("fallback", "center")
    if mode not in MODES or fallback not in FALLBACKS:
        raise ValueError("bad layout mode")
    manual = data.get("manual")
    m = None
    if manual is not None:
        m = Manual(x=_unit(manual.get("x", 0.5)), y=_unit(manual.get("y", 0.5)),
                   zoom=_ranged(manual.get("zoom", 1.0), 1.0, 3.0))
    return LayoutRequest(mode=mode, fallback=fallback, margin=_ranged(data.get("margin", 0.08), 0.0, 0.25),
                         deadzone=_ranged(data.get("deadzone", 0.3), 0.0, 0.8), manual=m,
                         fg_position=_unit(data.get("fg_position", 0.5)),
                         pad_color=str(data.get("pad_color", "#000000")),
                         split_ratio=_ranged(data.get("split_ratio", 0.5), 0.3, 0.7))


def _ranged(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise ValueError("layout value out of range")
    return float(value)


def _unit(value: object) -> float:
    return _ranged(value, 0.0, 1.0)


def center_crop(src_w: int, src_h: int, out_w: int, out_h: int, manual: Manual | None = None) -> CropPlan:
    """A still window of the output's aspect: centred, or at the manual position and zoom."""
    zoom = manual.zoom if manual else 1.0
    w, h = crop_window(src_w, src_h, out_w, out_h, zoom)
    x = centred_offset(src_w, w, manual.x if manual else None)
    y = centred_offset(src_h, h, manual.y if manual else None)
    return CropPlan(width=w, height=h, x=Path.still(round(x)), y=Path.still(round(y)))


def fit_layout(src_w: int, src_h: int, out_w: int, out_h: int, *, background: str, position: float,
               color: str) -> FitLayout:
    fw, fh = fit_size(src_w, src_h, out_w, out_h)
    x = (out_w - fw) // 2 // 2 * 2
    y = int(clamp(position * out_h - fh / 2, 0, out_h - fh)) // 2 * 2
    return FitLayout(fg_width=fw, fg_height=fh, fg_x=x, fg_y=y, background=background, color=color)


def halves_split(src_w: int, src_h: int, out_w: int, out_h: int, ratio: float) -> SplitLayout:
    """Two panels from the left and right halves of the frame (two people side by side), each a
    still window centred in its half."""
    top_h = even(out_h * ratio)
    panels = []
    for half, panel_h in ((0, top_h), (1, out_h - top_h)):
        w, h = crop_window(src_w // 2, src_h, out_w, panel_h)
        x = half * (src_w // 2) + centred_offset(src_w // 2, w)
        y = centred_offset(src_h, h)
        panels.append(CropPlan(width=w, height=h, x=Path.still(round(x)), y=Path.still(round(y))))
    return SplitLayout(top=panels[0], bottom=panels[1], top_height=top_h)


Tracker = Callable[[LayoutRequest, int, int, int, int], Layout | None]


def plan_layout(request: LayoutRequest, src_w: int, src_h: int, out_w: int, out_h: int,
                tracker: Tracker | None = None) -> tuple[Layout, str]:
    """The layout to render and the mode actually used (a tracked mode with no usable analysis
    reports its fallback). `tracker` builds face, speaker and split layouts from the analysis."""
    mode = request.mode
    if request.manual is not None and mode in ("center", "face", "speaker"):
        return CropLayout(center_crop(src_w, src_h, out_w, out_h, request.manual)), "manual"
    if mode in ("face", "speaker", "split") and tracker is not None:
        tracked = tracker(request, src_w, src_h, out_w, out_h)
        if tracked is not None:
            return tracked, mode
    if mode == "split":
        return halves_split(src_w, src_h, out_w, out_h, request.split_ratio), "split"
    if mode in ("face", "speaker"):
        mode = request.fallback
    if mode in ("blur", "original"):
        background = "blur" if mode == "blur" else "color"
        return fit_layout(src_w, src_h, out_w, out_h, background=background, position=request.fg_position,
                          color=request.pad_color), mode
    if nearly_same_aspect(src_w, src_h, out_w, out_h):
        return fit_layout(src_w, src_h, out_w, out_h, background="blur", position=0.5,
                          color=request.pad_color), "center"
    return CropLayout(center_crop(src_w, src_h, out_w, out_h)), "center"
