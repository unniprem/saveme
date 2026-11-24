"""Renderer subprocess (`python -m app.renderer`): Auto Shorts rendering, entirely on this machine
(docs/AUTO_SHORTS_DESIGN.md §2, §4).

Protocol (as app.worker): the parent writes one JSON request to stdin and closes it; the worker
writes only JSON lines to stdout and ends with exactly one `result` event (exit 0) or `error`
event (exit 1). Modes:

- `render`: one clip of a ready source as an MP4 (`kind: "video"`) or one JPEG still
  (`kind: "frame"`), with the layout, captions (an ASS file the parent wrote into the workdir) and
  branding the request gives. `progress` events carry FFmpeg's measured percent.
- `analyze`: face tracks for a clip's range (app.faces), written as JSON.
- `asset`: probe an uploaded intro/outro video or logo.

The process caps its data segment (RENDER_MAX_MEMORY_MB, inherited by FFmpeg), runs at nice 10,
refuses every network connection, and only reads and writes inside its workdir (plus reading the
source and assets the parent names). On failure the workdir is left empty.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.encoders import HARDWARE, QUALITIES, SOFTWARE, audio_args, scaled_size, video_args
from app.errors import AppError, size_text
from app.layouts import LayoutRequest, plan_layout, request_from
from app.mediafile import FFMPEG, FFPROBE, stderr_tail
from app.netguard import install_connect_guard
from app.reframe import ASPECTS, source_canvas
from app.render_graph import SUBTITLES_NAME, Bumper, GraphPlan, LogoPlan, build
from app.worker import _clear, _read_request, _writer, limit_memory, protocol_out

GRAPH_NAME = "graph.txt"
FRAME_NAME = "frame.jpg"
_PROBE_TIMEOUT = 60
_EMIT_INTERVAL = 0.25
_MAX_FPS = 60.0
MAX_THREADS = 16
_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


# --- probing ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoInfo:
    """What the renderer needs to know about a video: its *displayed* size (rotation applied),
    pixel aspect, frame rate, duration and whether it has audio."""
    width: int
    height: int
    sar: tuple[int, int]
    fps: float | None
    duration: float | None
    has_audio: bool
    codec: str
    pix_fmt: str


def _ratio(text: object) -> float | None:
    if not isinstance(text, str) or "/" not in text:
        return None
    num, _, den = text.partition("/")
    try:
        n, d = float(num), float(den)
    except ValueError:
        return None
    return n / d if d > 0 and n > 0 and math.isfinite(n / d) else None


def _sar(text: object) -> tuple[int, int]:
    if isinstance(text, str) and ":" in text:
        num, _, den = text.partition(":")
        if num.isdigit() and den.isdigit() and int(num) > 0 and int(den) > 0:
            return int(num), int(den)
    return 1, 1


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(round(float(side["rotation"]))) % 360
            except (TypeError, ValueError):
                return 0
    tags = stream.get("tags") or {}
    try:
        return int(tags.get("rotate", 0)) % 360
    except (TypeError, ValueError):
        return 0


def probe_video(path: Path) -> VideoInfo | None:
    """The first real video stream of `path` (cover art skipped), through the ffprobe wrapper."""
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "stream=index,codec_type,codec_name,width,height,sample_aspect_ratio,avg_frame_rate,"
           "r_frame_rate,pix_fmt:stream_side_data=rotation:stream_tags=rotate:"
           "stream_disposition=attached_pic:format=duration", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
        streams, fmt = data["streams"], data["format"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        print(f"renderer: ffprobe failed: {exc!r}", file=sys.stderr)
        return None
    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        return None
    width, height = video.get("width"), video.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        return None
    if _rotation(video) in (90, 270):
        width, height = height, width
    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    if fps is not None and not 1.0 <= fps <= 240.0:
        fps = None
    try:
        duration = float(fmt.get("duration"))
        duration = duration if math.isfinite(duration) and duration > 0 else None
    except (TypeError, ValueError):
        duration = None
    return VideoInfo(width=width, height=height, sar=_sar(video.get("sample_aspect_ratio")), fps=fps,
                     duration=duration,
                     has_audio=any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams),
                     codec=str(video.get("codec_name")), pix_fmt=str(video.get("pix_fmt")))


# --- requests --------------------------------------------------------------------------------

def _number(value: object, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low <= value <= high:
        raise ValueError
    return float(value)


def _abs_file(value: object) -> Path:
    if (not isinstance(value, str) or not os.path.isabs(value) or os.path.islink(value)
            or not os.path.isfile(value)):
        raise ValueError
    return Path(value)


def _abs_dir(value: object) -> Path:
    if not isinstance(value, str) or not os.path.isabs(value) or not os.path.isdir(value):
        raise ValueError
    return Path(value)


def _plain_name(value: object, suffix: str) -> str:
    if (not isinstance(value, str) or not value.endswith(suffix) or value.startswith(".")
            or not set(value) <= _NAME_OK or len(value) > 64):
        raise ValueError
    return value


@dataclass(frozen=True)
class RenderRequest:
    kind: str  # video | frame
    source: Path
    workdir: Path
    output: str  # file name inside workdir
    start: float
    end: float
    at: float | None
    aspect: str  # 9:16 | 16:9 | 1:1 | 4:5 | source
    width: int | None
    height: int | None
    fps: float | None  # None: the source's (at most 60)
    quality: str
    encoder: str
    threads: int
    layout: LayoutRequest
    analysis: Path | None
    speech: tuple[tuple[float, float], ...]
    subtitles: bool
    fonts_dir: str | None
    logo: dict | None
    intro: Path | None
    outro: Path | None
    max_bytes: int


def parse_render(req: dict) -> RenderRequest:
    try:
        kind = req.get("kind")
        if kind not in ("video", "frame"):
            raise ValueError
        workdir = _abs_dir(req.get("workdir"))
        source = _abs_file(req.get("source"))
        if os.path.realpath(source.parent) == os.path.realpath(workdir):
            raise ValueError
        output = _plain_name(req.get("output"), ".mp4" if kind == "video" else ".jpg")
        start = _number(req.get("start"), 0, 1e7)
        end = _number(req.get("end"), start + 0.05, 1e7)
        at = _number(req.get("at"), 0, end - start) if kind == "frame" else None
        aspect = req.get("aspect")
        if aspect not in (*ASPECTS, "source"):
            raise ValueError
        width, height = req.get("width"), req.get("height")
        if width is not None or height is not None:
            if not all(type(v) is int and 2 <= v <= 3840 and v % 2 == 0 for v in (width, height)):
                raise ValueError
        fps = req.get("fps")
        fps = None if fps is None else _number(fps, 1, _MAX_FPS)
        quality, encoder, threads = req.get("quality"), req.get("encoder"), req.get("threads")
        if quality not in QUALITIES or encoder not in (SOFTWARE, HARDWARE):
            raise ValueError
        if type(threads) is not int or not 1 <= threads <= MAX_THREADS:
            raise ValueError
        layout = request_from(req.get("layout") or {})
        analysis = req.get("analysis")
        analysis = None if analysis is None else _abs_file(analysis)
        speech = tuple((_number(s, 0, 1e7), _number(e, 0, 1e7)) for s, e in req.get("speech") or [])
        subtitles = req.get("subtitles")
        if type(subtitles) is not bool:
            raise ValueError
        if subtitles and not (workdir / SUBTITLES_NAME).is_file():
            raise ValueError
        fonts_dir = req.get("fonts_dir")
        if fonts_dir is not None and (not isinstance(fonts_dir, str) or not set(fonts_dir) <= _NAME_OK | {"/"}
                                      or not fonts_dir.startswith("/")):
            raise ValueError
        logo = req.get("logo")
        if logo is not None:
            logo = {"path": _abs_file(logo.get("path")), "scale": _number(logo.get("scale"), 0.03, 0.5),
                    "opacity": _number(logo.get("opacity"), 0.1, 1.0),
                    "position": logo.get("position")}
            if logo["position"] not in ("top-left", "top-right", "bottom-left", "bottom-right"):
                raise ValueError
        intro = None if req.get("intro") is None else _abs_file(req.get("intro"))
        outro = None if req.get("outro") is None else _abs_file(req.get("outro"))
        max_bytes = req.get("max_bytes")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        print("renderer: malformed render request", file=sys.stderr)
        raise AppError("internal") from None
    return RenderRequest(kind=kind, source=source, workdir=workdir, output=output, start=start, end=end,
                         at=at, aspect=aspect, width=width, height=height, fps=fps, quality=quality,
                         encoder=encoder, threads=threads, layout=layout, analysis=analysis, speech=speech,
                         subtitles=subtitles, fonts_dir=fonts_dir, logo=logo, intro=intro, outro=outro,
                         max_bytes=max_bytes)


# --- planning --------------------------------------------------------------------------------

def canvas_size(req: RenderRequest, info: VideoInfo) -> tuple[int, int]:
    """The output size: the request's, or the aspect's default (1080 on the short side), or the
    source's own aspect; then the quality preset's scale (draft is half size)."""
    if req.width is not None and req.height is not None:
        width, height = req.width, req.height
    elif req.aspect == "source":
        width, height = source_canvas(info.width * info.sar[0] // info.sar[1], info.height)
    else:
        aw, ah = ASPECTS[req.aspect]
        width, height = (1080, 1080 * ah // aw) if aw <= ah else (1080 * aw // ah, 1080)
        width, height = width // 2 * 2, height // 2 * 2
    return scaled_size(width, height, req.quality)


def output_fps(req: RenderRequest, info: VideoInfo) -> float:
    if req.fps is not None:
        return req.fps
    fps = info.fps or 30.0
    return min(_MAX_FPS, round(fps, 3))


def _logo_plan(logo: dict, width: int, height: int) -> LogoPlan:
    size = max(16, int(width * logo["scale"]) // 2 * 2)
    margin = max(8, int(min(width, height) * 0.04))
    logo_h = _png_height(logo["path"], size)
    x = margin if logo["position"].endswith("left") else width - size - margin
    y = margin if logo["position"].startswith("top") else height - logo_h - margin
    return LogoPlan(width=size, x=x, y=max(0, y), opacity=logo["opacity"])


def _png_height(path: Path, width: int) -> int:
    """The logo's height once scaled to `width`, from its PNG header (the API checked it)."""
    with open(path, "rb") as f:
        head = f.read(24)
    w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return max(2, round(width * h / w)) if w > 0 and h > 0 else width


# --- running FFmpeg --------------------------------------------------------------------------

class _Progress:
    """`progress` events, at most four a second (the last always goes out)."""

    def __init__(self, emit: Callable[[dict], None], stage: str):
        self._emit, self._stage = emit, stage
        self._last = float("-inf")

    def __call__(self, percent: float | None, *, force: bool = False, **extra) -> None:
        now = time.monotonic()
        if percent is None and not force:  # FFmpeg hasn't written anything yet: nothing measured
            return
        if not force and now - self._last < _EMIT_INTERVAL:
            return
        self._last = now
        self._emit({"event": "progress", "stage": self._stage,
                    "percent": None if percent is None else round(min(100.0, max(0.0, percent)), 1), **extra})


def _progress_int(value: str) -> int | None:
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def run_ffmpeg(args: list[str], *, cwd: Path, duration: float | None,
               on_progress: Callable[[float | None, int | None], None] | None) -> tuple[int, str]:
    """Run the FFmpeg wrapper in `cwd` with `-progress pipe:1`; returns (exit code, stderr tail)."""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                                    text=True, cwd=str(cwd))
        except OSError as exc:
            print(f"renderer: ffmpeg failed to start: {exc!r}", file=sys.stderr)
            return 127, ""
        with proc:
            out_us = written = None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    out_us = _progress_int(value)
                elif key == "total_size":
                    written = _progress_int(value)
                elif key == "progress" and on_progress is not None:
                    if value == "end":
                        on_progress(100.0, written)
                    elif duration and out_us is not None:
                        on_progress(100.0 * out_us / 1e6 / duration, written)
                    else:
                        on_progress(None, written)
        errors.seek(0)
        return proc.returncode, stderr_tail(errors.read())


def _check_output(path: Path, *, width: int, height: int, duration: float, fps: float) -> dict:
    """Probe a rendered MP4 and refuse it unless it is H.264 yuv420p at the canvas size with AAC
    audio and the expected length (within a frame plus 0.1 s)."""
    cmd = [FFPROBE, "-v", "error", "-show_entries",
           "stream=codec_type,codec_name,width,height,pix_fmt,profile:format=duration,size", "-of", "json",
           str(path)]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=_PROBE_TIMEOUT)
        data = json.loads(proc.stdout)
        streams, fmt = data["streams"], data["format"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        audio = next(s for s in streams if s.get("codec_type") == "audio")
        got = float(fmt["duration"])
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, StopIteration) as exc:
        print(f"renderer: can't probe the output: {exc!r}", file=sys.stderr)
        raise AppError("render_failed") from None
    problems = []
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        problems.append(f"video {video.get('codec_name')}/{video.get('pix_fmt')}")
    if (video.get("width"), video.get("height")) != (width, height):
        problems.append(f"size {video.get('width')}x{video.get('height')}")
    if audio.get("codec_name") != "aac":
        problems.append(f"audio {audio.get('codec_name')}")
    if abs(got - duration) > 0.1 + 2.0 / fps:
        problems.append(f"duration {got:.3f} for {duration:.3f}")
    if problems:
        print(f"renderer: output check failed: {', '.join(problems)}", file=sys.stderr)
        raise AppError("render_failed")
    return {"width": width, "height": height, "duration": round(got, 3), "bytes": path.stat().st_size,
            "video_codec": "h264", "audio_codec": "aac", "profile": video.get("profile")}


def _inputs(req: RenderRequest, start: float, length: float, threads: int) -> list[str]:
    args = ["-threads", str(threads), "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(req.source)]
    if req.logo is not None:
        args += ["-f", "png_pipe", "-i", str(req.logo["path"])]
    return args


def render(req: RenderRequest, emit: Callable[[dict], None], *, tracker=None) -> dict:
    info = probe_video(req.source)
    if info is None:
        raise AppError("unreadable_media")
    width, height = canvas_size(req, info)
    fps = output_fps(req, info)
    display_w = info.width * info.sar[0] // info.sar[1]
    layout, used_mode = plan_layout(req.layout, display_w, info.height, width, height, tracker=tracker)
    length = req.end - req.start
    if info.duration is not None:
        length = min(length, max(0.05, info.duration - req.start))
    intro = outro = None
    bumpers: list[tuple[Path, Bumper] | None] = []
    if req.kind == "video":
        for path in (req.intro, req.outro):
            if path is None:
                bumpers.append(None)
                continue
            found = probe_video(path)
            if found is None or found.duration is None:
                raise AppError("invalid_asset", "The intro or outro video can't be read.")
            bumpers.append((path, Bumper(duration=min(found.duration, 15.0), has_audio=found.has_audio)))
        intro, outro = (b[1] if b else None for b in bumpers)
    logo = None if req.logo is None else _logo_plan(req.logo, width, height)
    plan = GraphPlan(canvas_width=width, canvas_height=height, fps=fps, duration=length, layout=layout,
                     source_has_audio=info.has_audio, source_sar=info.sar, subtitles=req.subtitles,
                     fonts_dir=req.fonts_dir, logo=logo, intro=intro, outro=outro,
                     pad_color=req.layout.pad_color, frame_at=req.at if req.kind == "frame" else None)
    (req.workdir / GRAPH_NAME).write_text(build(plan), encoding="utf-8")
    graph = ["-/filter_complex", GRAPH_NAME, "-filter_complex_threads", str(req.threads)]
    output = req.workdir / req.output
    if req.kind == "frame":
        args = [*_inputs(req, req.start + (req.at or 0.0), min(length, 1.0), req.threads), *graph,
                "-map", "[vout]", "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3", "-strict", "-1",
                "-f", "image2", "-update", "1", req.output]
        code, tail = run_ffmpeg(args, cwd=req.workdir, duration=None, on_progress=None)
        if code != 0 or not output.is_file() or output.stat().st_size == 0:
            print(f"renderer: frame failed (rc={code}): {tail}", file=sys.stderr)
            raise AppError("render_failed")
        return {"file": req.output, "width": width, "height": height, "mode": used_mode}

    progress = _Progress(emit, "rendering")
    total = plan.total_duration
    part = req.workdir / (req.output[:-4] + ".part.mp4")
    inputs = _inputs(req, req.start, length, req.threads)
    for item in bumpers:
        if item:
            inputs += ["-threads", str(req.threads), "-t", f"{item[1].duration:.3f}", "-i", str(item[0])]

    def attempt(encoder: str) -> tuple[int, str]:
        args = [*inputs, *graph, "-map", "[vout]", "-map", "[aout]",
                *video_args(encoder, req.quality, width=width, height=height, fps=fps, threads=req.threads),
                *audio_args(req.quality), "-map_metadata", "-1", "-map_chapters", "-1",
                "-movflags", "+faststart", "-fs", str(req.max_bytes), "-f", "mp4",
                "-progress", "pipe:1", "-nostats", part.name]
        return run_ffmpeg(args, cwd=req.workdir, duration=total,
                          on_progress=lambda percent, written: progress(percent, encoder=encoder, bytes=written))

    encoder = req.encoder
    progress(0.0, force=True, encoder=encoder)
    code, tail = attempt(encoder)
    too_large = part.is_file() and part.stat().st_size >= req.max_bytes
    notice = None
    if code != 0 and encoder == HARDWARE and not too_large:
        print(f"renderer: hardware encoding failed (rc={code}), using libx264: {tail}", file=sys.stderr)
        part.unlink(missing_ok=True)
        encoder = SOFTWARE
        notice = "Hardware encoding failed, so this clip was encoded in software."
        progress(0.0, force=True, encoder=encoder)
        code, tail = attempt(encoder)
        too_large = part.is_file() and part.stat().st_size >= req.max_bytes
    if too_large:
        raise AppError("too_large", f"This short is larger than the {size_text(req.max_bytes)} limit. "
                       "Choose a shorter clip or a lower quality.")
    if code != 0 or not part.is_file():
        print(f"renderer: render failed (rc={code}): {tail}", file=sys.stderr)
        raise AppError("render_failed")
    checked = _check_output(part, width=width, height=height, duration=total, fps=fps)
    part.rename(output)
    progress(100.0, force=True, encoder=encoder)
    return {"file": req.output, **checked, "fps": fps, "encoder": encoder, "mode": used_mode,
            "notice": notice}


# --- the process -----------------------------------------------------------------------------

def run_render(req: dict, emit: Callable[[dict], None]) -> dict:
    request = parse_render(req)
    try:
        tracker = None
        if request.layout.mode in ("face", "speaker", "split") and request.analysis is not None:
            from app.faces import tracker_for
            tracker = tracker_for(request.analysis, request.speech)
        result = render(request, emit, tracker=tracker)
    except BaseException:
        _clear(request.workdir)
        raise
    _clear(request.workdir, keep=request.output)
    return result


def _lower_priority() -> None:
    try:
        os.nice(10)
    except OSError:
        pass


def _to_app_error(exc: Exception) -> AppError:
    if isinstance(exc, AppError):
        err = exc
    elif isinstance(exc, MemoryError):
        err = AppError("render_failed", "Rendering ran out of memory. Try a lower quality or a smaller size.")
    else:
        traceback.print_exception(exc, file=sys.stderr)
        return AppError("internal")
    print(f"renderer: {err.code}: {exc}", file=sys.stderr)
    return err


def main() -> int:
    """Run one request; returns the exit code (0 after a result, 1 after an error)."""
    with protocol_out() as out:
        emit = _writer(out)
        try:
            req = _read_request(sys.stdin)
            memory = req.get("memory_bytes")
            limit_memory(memory if type(memory) is int and memory > 0 else 1536 * 1024 * 1024)
            _lower_priority()
            install_connect_guard(lambda _ip: False)
            threads = req.get("threads")
            os.environ["OMP_NUM_THREADS"] = str(threads if type(threads) is int and 0 < threads <= MAX_THREADS else 1)
            mode = req["mode"]
            if mode == "render":
                data = run_render(req, emit)
            elif mode == "analyze":
                from app.faces import run_analyze
                data = run_analyze(req, emit)
            elif mode == "asset":
                data = run_asset(req)
            else:
                print(f"renderer: unknown mode {mode!r}", file=sys.stderr)
                raise AppError("internal")
            event, code = {"event": "result", "data": data}, 0
        except Exception as exc:
            err = _to_app_error(exc)
            event, code = {"event": "error", "code": err.code, "message": err.message}, 1
        emit(event)
    return code


def run_asset(req: dict) -> dict:
    """Probe an uploaded intro/outro video: {"kind": "video", "width", "height", "duration", "has_audio"}."""
    try:
        path = _abs_file(req.get("path"))
    except ValueError:
        print("renderer: malformed asset request", file=sys.stderr)
        raise AppError("internal") from None
    info = probe_video(path)
    if info is None or info.duration is None:
        raise AppError("invalid_asset", "SaveMe can't read this file as a video.")
    return {"kind": "video", "width": info.width, "height": info.height, "duration": round(info.duration, 3),
            "has_audio": info.has_audio}


if __name__ == "__main__":
    sys.exit(main())
