"""Auto Shorts geometry: crop windows, fitted sizes and crop paths (docs/AUTO_SHORTS_DESIGN.md §5).

Pure functions on numbers. A *path* is a piecewise-linear function of the clip's time (seconds
from the clip's start), given as keyframes and turned into an FFmpeg expression of `t` for the
`crop` filter. Face tracking (app.faces) produces target centres; `smooth_path` turns them into a
path with no sudden moves except at scene cuts.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

ASPECTS: dict[str, tuple[int, int]] = {"9:16": (9, 16), "16:9": (16, 9), "1:1": (1, 1), "4:5": (4, 5)}
MAX_SOURCE_SIDE = 1920  # the long side of an output that keeps the source's own aspect


def even(value: float) -> int:
    """`value` rounded down to an even whole number, at least 2 (H.264 4:2:0 needs even sides)."""
    return max(2, int(value + 1e-9) // 2 * 2)


def clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def source_canvas(width: int, height: int, *, max_side: int = MAX_SOURCE_SIDE) -> tuple[int, int]:
    """An output size with the source's own aspect, its long side at most `max_side`."""
    scale = min(1.0, max_side / max(width, height))
    return even(width * scale), even(height * scale)


def crop_window(src_w: int, src_h: int, out_w: int, out_h: int, zoom: float = 1.0) -> tuple[int, int]:
    """The largest window with the output's aspect inside a src_w×src_h frame, made `zoom` times
    smaller (zoom ≥ 1 never asks for more than the frame). Even sides."""
    target = out_w / out_h
    zoom = max(1.0, zoom)
    if src_w / src_h > target:
        h = src_h / zoom
        w = h * target
    else:
        w = src_w / zoom
        h = w / target
    return min(even(w), even(src_w)), min(even(h), even(src_h))


def fit_size(src_w: int, src_h: int, box_w: int, box_h: int) -> tuple[int, int]:
    """The largest size with the source's aspect inside box_w×box_h. Even sides."""
    scale = min(box_w / src_w, box_h / src_h)
    return min(even(src_w * scale), even(box_w)), min(even(src_h * scale), even(box_h))


def nearly_same_aspect(src_w: int, src_h: int, out_w: int, out_h: int, tolerance: float = 0.10) -> bool:
    """True when the source is within `tolerance` of the output's aspect: scaling it (a hair of
    padding) beats cropping anything off."""
    ratio = (src_w / src_h) / (out_w / out_h)
    return abs(math.log(ratio)) <= math.log(1 + tolerance)


def centred_offset(frame: int, window: int, centre: float | None = None) -> float:
    """The window's left/top edge for a centre at `centre` (0–1 of the frame, None = middle),
    kept inside the frame."""
    middle = frame / 2 if centre is None else centre * frame
    return clamp(middle - window / 2, 0.0, float(frame - window))


@dataclass(frozen=True)
class Path:
    """A piecewise-linear value over time: (t, value) keyframes with increasing t. Before the
    first keyframe and after the last, the value holds. A repeated time is a jump (a scene cut):
    the value changes instantly there."""

    points: tuple[tuple[float, float], ...]

    @staticmethod
    def still(value: float) -> Path:
        return Path(((0.0, float(value)),))

    def at(self, t: float) -> float:
        pts = self.points
        if t <= pts[0][0]:
            return pts[0][1]
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if t < t1 or (t == t1 and t0 == t1):
                if t1 == t0:
                    return v0 if t < t1 else v1
                return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return pts[-1][1]

    @property
    def is_still(self) -> bool:
        first = self.points[0][1]
        return all(abs(v - first) < 1e-6 for _t, v in self.points)


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def path_expr(path: Path) -> str:
    """An FFmpeg expression of `t` equal to `path`: the first value plus one term per segment,
    `slope*clip(t-t0,0,dt)` for a ramp and `jump*gte(t,t0)` for a cut. A flat sum (no nesting), so
    its depth doesn't grow with the clip's length."""
    pts = path.points
    terms = [_num(pts[0][1])]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        dv = v1 - v0
        if abs(dv) < 1e-6:
            continue
        if t1 - t0 <= 1e-6:
            terms.append(f"{_num(dv)}*gte(t,{_num(t1)})")
        else:
            terms.append(f"{_num(dv / (t1 - t0))}*clip(t-{_num(t0)},0,{_num(t1 - t0)})")
    return "+".join(terms).replace("+-", "-")


def simplify(points: Sequence[tuple[float, float]], tolerance: float = 1.0) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker on (t, value) keyframes: drop points within `tolerance` (pixels) of the
    line through their neighbours. Cuts (repeated times) are kept."""
    pts = list(points)
    if len(pts) <= 2:
        return pts
    # Split at jumps so each continuous run is simplified on its own.
    runs, run = [], [pts[0]]
    for prev, cur in zip(pts, pts[1:]):
        if cur[0] - prev[0] <= 1e-6:
            runs.append(run)
            run = [cur]
        else:
            run.append(cur)
    runs.append(run)
    out: list[tuple[float, float]] = []
    for run in runs:
        out.extend(_rdp(run, tolerance))
    return out


def _rdp(pts: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(pts) <= 2:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        (t0, v0), (t1, v1) = pts[lo], pts[hi]
        worst, index = 0.0, -1
        for i in range(lo + 1, hi):
            t, v = pts[i]
            line = v0 + (v1 - v0) * (t - t0) / (t1 - t0) if t1 != t0 else v0
            err = abs(v - line)
            if err > worst:
                worst, index = err, i
        if worst > tolerance and index > 0:
            keep[index] = True
            stack += [(lo, index), (index, hi)]
    return [p for p, k in zip(pts, keep) if k]
