"""RenderManager: Auto Shorts renders, previews and brand assets (docs/AUTO_SHORTS_DESIGN.md §2, §9).

A render turns 1–20 clips of a ready Clip Studio source into finished short videos, one clip at a
time, each in its own run of the renderer worker (`python -m app.renderer`). Its stages, per clip:

    waiting → analyzing (face modes, when no cached analysis) → rendering → ready
                                                      (or failed | cancelled at any point)

The render as a whole is queued → running → ready (at least one clip ready) | failed | cancelled.

- Renders have their own slots (RENDER_MAX_CONCURRENT_JOBS) and queue bound (RENDER_MAX_QUEUED_JOBS);
  they never take download slots. Previews (a still or a low-res draft) have one slot of their own, so
  checking a frame never waits behind a long batch.
- A render or preview *holds* its source while it runs; closing the source cancels it, and its files
  (in <source folder>/render/) go with the source. Each clip's run reserves its disk through the
  JobManager's budget.
- Captions come from the source's cached transcript (or the user's edited cues). Face analysis is
  cached per source and range, so changing the template or captions never re-analyses.
- Cancelling a render stops its current worker (the process group is killed) and every clip not yet
  done; cancelling one clip skips or stops just that clip. Finished clips stay downloadable. Part
  files and work folders are always removed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import shutil
import sys
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.ai_jobs import AI_DIR, TRANSCRIPT_FILE
from app.captions import (AssPlan, CaptionCue, clip_words, cues_from_edits, fit_chars, is_malayalam, segment,
                          to_ass, to_srt)
from app.config import ConfigError, RenderSettings, Settings
from app.encoders import Capabilities, bitrate_cap_bytes_per_second, choose_encoder
from app.errors import AppError
from app.faces import ANALYSIS_VERSION
from app.jobs import END_TOLERANCE_SECONDS, Job, JobManager
from app.render_spec import PreviewBody, RenderBody, RenderClip, RenderSpec, ResolvedSpec, resolve
from app.runner import RunWorker, run_worker
from app.transcript import Transcript, load_transcript

logger = logging.getLogger(__name__)

RENDERER_CMD: tuple[str, ...] = (sys.executable, "-P", "-m", "app.renderer")
RENDER_DIR = "render"
MAX_RECORDS = 50
RECORD_SECONDS = 3600
MAX_RENDERS_PER_SOURCE = 4  # finished renders whose files a source keeps
MAX_PREVIEWS_PER_SOURCE = 4
PREVIEW_CLIP_SECONDS = 30.0  # a draft preview covers at most the clip's first 30 s
PREVIEW_END_MARGIN = 0.1  # a still asked for at the clip's very end shows the frame this much earlier
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_SIDE = 2048
MAX_BUMPER_BYTES = 100 * 1024 * 1024
MAX_BUMPER_SECONDS = 15.0
MAX_ASSETS_PER_SOURCE = 12
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_PENDING = frozenset({"queued", "running"})
_CLIP_PENDING = frozenset({"waiting", "analyzing", "rendering"})
_SLACK = 64 * 1024 * 1024
_FACE_MODES = frozenset({"face", "speaker", "split"})
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _new_id(taken) -> str:
    value = secrets.token_urlsafe(16)
    while value in taken:
        value = secrets.token_urlsafe(16)
    return value


def check_png(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG logo, or invalid_asset: the signature, an IHDR first, sides 1–2048."""
    bad = AppError("invalid_asset", "The logo must be a PNG image of at most 2048 × 2048 pixels.")
    if len(data) < 33 or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR" \
            or int.from_bytes(data[8:12], "big") != 13:
        raise bad
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    depth, color = data[24], data[25]
    if not (1 <= width <= MAX_LOGO_SIDE and 1 <= height <= MAX_LOGO_SIDE) or color not in (0, 2, 3, 4, 6) \
            or depth not in (1, 2, 4, 8, 16):
        raise bad
    return width, height


@dataclass
class Asset:
    id: str
    source_id: str
    kind: str  # logo | intro | outro
    path: Path
    width: int
    height: int
    duration: float | None = None


@dataclass(eq=False)
class ClipState:
    number: int
    clip: RenderClip
    status: str = "waiting"
    percent: float | None = None
    detail: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    encoder: str | None = None
    mode: str | None = None
    notice: str | None = None
    timing: str | None = None  # caption timing: words | estimated | mixed | edited | None
    error: AppError | None = None
    result: dict = field(default_factory=dict)
    files: dict[str, Path] = field(default_factory=dict)
    task: asyncio.Task | None = field(default=None, repr=False)


@dataclass(eq=False)
class Render:
    id: str
    source_id: str
    spec: RenderSpec
    resolved: ResolvedSpec
    clips: list[ClipState]
    folder: Path
    title: str | None
    created_at: float
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    error: AppError | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    zips: dict[bool, Path] = field(default_factory=dict)
    zip_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass(eq=False)
class Preview:
    id: str
    source_id: str
    kind: str  # frame | clip
    created_at: float
    status: str = "queued"
    percent: float | None = None
    detail: str | None = None
    mode: str | None = None
    error: AppError | None = None
    file: Path | None = None
    finished_at: float | None = None
    assets: set[str] = field(default_factory=set)
    task: asyncio.Task | None = field(default=None, repr=False)


def _clip_seconds(clip: RenderClip, duration: float | None) -> tuple[float, float]:
    """The clip's range, by Clip Studio's rules: inside the video (an end a hair past it is pulled back)."""
    start, end = clip.start, clip.end
    if duration is not None:
        if start >= duration:
            raise AppError("invalid_clip", "The start must be before the end of the video.")
        if end > duration + END_TOLERANCE_SECONDS:
            raise AppError("invalid_clip", "The end can't be after the end of the video.")
        end = min(end, duration)
    if end - start < 0.5:
        raise AppError("invalid_clip", "A short must be at least half a second long.")
    return start, end


class RenderManager:
    def __init__(self, settings: RenderSettings | ConfigError, jobs: JobManager, file_settings: Settings, *,
                 run_worker: RunWorker = run_worker, renderer_cmd: Sequence[str] = RENDERER_CMD,
                 capabilities: Callable[[], Capabilities] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._jobs = jobs
        self._files = file_settings
        self._run_worker = run_worker
        self._cmd = tuple(renderer_cmd)
        self._clock = clock
        self._detect = capabilities
        self._caps: Capabilities | None = None
        self._renders: dict[str, Render] = {}
        self._previews: dict[str, Preview] = {}
        self._assets: dict[str, Asset] = {}
        self._analysis_locks: dict[str, asyncio.Lock] = {}
        if isinstance(settings, ConfigError):
            self._settings: RenderSettings | None = None
            self._config_error: AppError | None = AppError(
                "render_config_error", f"{settings.var} {settings.message.rstrip('.')}.")
            self._slots = asyncio.Semaphore(1)
        else:
            self._settings, self._config_error = settings, None
            self._slots = asyncio.Semaphore(settings.render_max_concurrent_jobs)
        self._preview_slot = asyncio.Semaphore(1)
        self._asset_slots = asyncio.Semaphore(2)
        self._asset_uploads: dict[str, int] = {}  # source id -> uploads still arriving or being checked
        jobs.add_source_listener(self._on_source_ended)

    # --- availability ---------------------------------------------------------------------

    async def start(self) -> None:
        """Find out (once, off the event loop) what the FFmpeg in use can do."""
        if self._settings is not None and self._caps is None:
            from app.encoders import detect

            self._caps = await asyncio.to_thread(self._detect or detect)
            missing = self._caps.missing
            if missing:
                logger.warning("Auto Shorts is off: FFmpeg lacks %s", ", ".join(missing))

    def _ready(self) -> tuple[RenderSettings, Capabilities, str]:
        if self._settings is None:
            raise self._config_error
        caps = self._caps
        if caps is None:
            raise AppError("render_unavailable", "Auto Shorts is still starting. Try again in a moment.")
        if caps.missing:
            raise AppError("render_unavailable", "Auto Shorts needs an FFmpeg with " + ", ".join(caps.missing)
                           + ". Use the Docker image, or install a full FFmpeg build.")
        encoder = choose_encoder(self._settings.render_encoder, caps)
        if encoder is None:
            raise AppError("render_unavailable", f"The encoder {self._settings.render_encoder} isn't in this FFmpeg.")
        return self._settings, caps, encoder

    def options(self) -> dict:
        from app.render_spec import options

        data = options()
        settings, caps = self._settings, self._caps
        error = None
        encoder = None
        try:
            _s, _c, encoder = self._ready()
        except AppError as exc:
            error = exc.to_dict()
        data.update({
            "available": error is None, "error": error,
            "encoder": None if encoder is None else {"name": encoder, "hardware": encoder != "libx264"},
            "captions_available": bool(caps and caps.captions),
            "limits": {"max_clips": 20, "max_clip_seconds": settings.render_max_clip_seconds if settings else None,
                       "max_logo_bytes": MAX_LOGO_BYTES, "max_bumper_bytes": MAX_BUMPER_BYTES,
                       "max_bumper_seconds": MAX_BUMPER_SECONDS, "preview_clip_seconds": PREVIEW_CLIP_SECONDS},
        })
        return data

    async def shutdown(self) -> None:
        tasks = [x.task for x in (*self._renders.values(), *self._previews.values())
                 if x.task is not None and not x.task.done()]
        for render in self._renders.values():
            if render.status in _PENDING:
                self._end_render(render, "cancelled")
        for preview in self._previews.values():
            if preview.status not in ("ready", "failed", "cancelled"):
                self._end_preview(preview, "cancelled")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # --- folders, transcripts, captions ---------------------------------------------------

    @staticmethod
    def _base(source: Job) -> Path:
        return source.workdir / RENDER_DIR

    def transcript_for(self, source: Job) -> Transcript | None:
        return load_transcript(source.workdir / AI_DIR / TRANSCRIPT_FILE)

    def captions(self, source_id: str, start: float, end: float, spec: RenderSpec) -> dict:
        """The automatic cues for a range, as the render would make them (clip time)."""
        self._ready()
        source = self._jobs.ready_source(source_id)
        self._jobs.touch(source)
        if not 0 <= start < end:
            raise AppError("invalid_clip")
        transcript = self.transcript_for(source)
        if transcript is None:
            return {"captions": None}
        resolved = resolve(spec)
        width, height = self._canvas_for(resolved)
        cues, timing = self._auto_cues(transcript, start, end, resolved, width, height)
        return {"captions": {
            "timing": timing, "language": transcript.language, "origin": transcript.origin,
            "has_words": transcript.has_words,
            "cues": [{"start": c.start, "end": c.end, "text": c.text,
                      "words": [{"start": w.start, "end": w.end, "text": w.text} for w in c.words]} for c in cues]}}

    @staticmethod
    def _auto_cues(transcript: Transcript, start: float, end: float, resolved: ResolvedSpec, width: int,
                   height: int) -> tuple[list[CaptionCue], str]:
        words, timing = clip_words(transcript, start, end)
        style = resolved.captions
        malayalam = is_malayalam(" ".join(w.text for w in words[:40]))
        chars = fit_chars(style, width, height, resolved.safe, malayalam=malayalam)
        cues = segment(words, max_words=style["max_words"], max_chars=chars, max_lines=style["max_lines"],
                       clip_seconds=end - start)
        return cues, timing

    def _clip_cues(self, clip: RenderClip, transcript: Transcript | None, start: float, end: float,
                   resolved: ResolvedSpec, width: int, height: int) -> tuple[list[CaptionCue], str | None]:
        style = resolved.captions
        if clip.cues is not None:
            chars = fit_chars(style, width, height, resolved.safe,
                              malayalam=is_malayalam(" ".join(c.text for c in clip.cues[:20])))
            return cues_from_edits(clip.cues, max_lines=style["max_lines"], max_chars=chars), "edited"
        if transcript is None:
            return [], None
        return self._auto_cues(transcript, start, end, resolved, width, height)

    @staticmethod
    def _speech(transcript: Transcript | None, start: float, end: float) -> list[list[float]]:
        if transcript is None:
            return []
        return [[round(max(0.0, s.start - start), 3), round(min(end, s.end) - start, 3)]
                for s in transcript.segments if s.end > start and s.start < end]

    @staticmethod
    def _caption_request(cues: list[CaptionCue], resolved: ResolvedSpec, *, title: str | None,
                         burn: bool) -> dict | None:
        """What the renderer burns in, as data (it writes the ASS at the real canvas size): the cues when
        captions are burned, a title bar and a watermark. None when nothing is burned in."""
        branding = resolved.branding
        title_text = title if resolved.title_bar else None
        enabled = bool(resolved.captions["enabled"]) and burn and bool(cues)
        if not (enabled or title_text or branding.watermark):
            return None
        return {
            "cues": [{"start": c.start, "end": c.end, "speaker": c.speaker, "lines": [list(x) for x in c.lines],
                      "words": [[w.start, w.end, w.text] for w in c.words]} for c in (cues if enabled else [])],
            "plan": {"style": dict(resolved.captions), "safe": dict(resolved.safe), "enabled": enabled,
                     "title": title_text, "brand_color": branding.brand_color, "watermark": branding.watermark,
                     "watermark_position": branding.watermark_position},
        }

    # --- assets -----------------------------------------------------------------------------

    def asset_limits(self, kind: str) -> int:
        if kind == "logo":
            return MAX_LOGO_BYTES
        if kind in ("intro", "outro"):
            return MAX_BUMPER_BYTES
        raise AppError("invalid_request", "kind must be logo, intro or outro")

    def _assets_in_use(self, source_id: str) -> set[str]:
        """Asset ids a queued or running render or preview of this source uses."""
        used: set[str] = set()
        for render in self._renders.values():
            if render.source_id == source_id and render.status in _PENDING:
                b = render.resolved.branding
                used |= {x for x in (b.logo, b.intro, b.outro) if x}
        for preview in self._previews.values():
            if preview.source_id == source_id and preview.status not in ("ready", "failed", "cancelled"):
                used |= preview.assets
        return used

    def begin_asset(self, source_id: str, kind: str, size: int) -> tuple[Job, Path, str]:
        """A path to write an uploaded asset to (inside the source's render folder) and its new id, once
        it fits: at most MAX_ASSETS_PER_SOURCE per source counting uploads still arriving (the oldest
        asset no render or preview is using makes room), and the disk budget (held until end_asset)."""
        self._ready()
        self.asset_limits(kind)
        source = self._jobs.ready_source(source_id)
        held = [a for a in self._assets.values() if a.source_id == source.id]
        pending = self._asset_uploads.get(source.id, 0)
        if len(held) + pending >= MAX_ASSETS_PER_SOURCE:
            in_use = self._assets_in_use(source.id)
            spare = [a for a in held if a.id not in in_use]
            if not spare or pending >= MAX_ASSETS_PER_SOURCE:
                raise AppError("busy", "This video already has as many logos and videos as SaveMe keeps. "
                               "Wait for the render using them to finish.")
            self._assets.pop(spare[0].id, None)
            spare[0].path.unlink(missing_ok=True)
        asset_id = _new_id(self._assets)
        folder = self._base(source) / "assets"
        # The upload, and for a logo its decoded pixels (at most 2048 × 2048 × 4 bytes).
        reserve = size + (MAX_LOGO_SIDE * MAX_LOGO_SIDE * 4 if kind == "logo" else 0) + 1024 * 1024
        if not self._jobs.reserve_disk(f"asset:{asset_id}", folder, reserve):
            raise AppError("disk_full")
        folder.mkdir(parents=True, exist_ok=True)
        self._asset_uploads[source.id] = pending + 1
        return source, folder / f"{asset_id}.{'png' if kind == 'logo' else 'video'}", asset_id

    def end_asset(self, source: Job, asset_id: str) -> None:
        """The upload of `asset_id` ended (kept or not): release its disk reservation and its place."""
        self._jobs.release_disk(f"asset:{asset_id}")
        left = self._asset_uploads.get(source.id, 0) - 1
        if left > 0:
            self._asset_uploads[source.id] = left
        else:
            self._asset_uploads.pop(source.id, None)

    async def finish_asset(self, source: Job, path: Path, asset_id: str, kind: str) -> Asset:
        """Check an uploaded asset in the renderer worker and keep it: a logo's PNG header is checked here,
        then the worker decodes it to raw RGBA (FFmpeg never parses an image); a video is probed."""
        work = path.parent / f"probe-{asset_id}"
        try:
            if kind == "logo":
                with open(path, "rb") as f:
                    check_png(f.read(64))
            work.mkdir(exist_ok=True)
            async with self._asset_slots:  # checks are quick, but never run many at once
                result = await self._run_worker({"mode": "asset", "kind": "logo" if kind == "logo" else "video",
                                                 "path": str(path), "threads": 1,
                                                 "memory_bytes": self._settings.max_memory_bytes},
                                                cmd=self._cmd, timeout=120, workdir=work)
            if kind == "logo":
                rgba = path.with_suffix(".rgba")
                if result.get("file") != rgba.name or not rgba.is_file():
                    raise AppError("internal")
                path.unlink(missing_ok=True)  # only the decoded pixels are kept
                asset = Asset(asset_id, source.id, kind, rgba, int(result["width"]), int(result["height"]))
            else:
                duration = result.get("duration")
                if not isinstance(duration, (int, float)) or duration > MAX_BUMPER_SECONDS + 0.05:
                    raise AppError("invalid_asset", f"An intro or outro can be at most {int(MAX_BUMPER_SECONDS)} "
                                   "seconds long.")
                asset = Asset(asset_id, source.id, kind, path, int(result["width"]), int(result["height"]),
                              float(duration))
        except BaseException:
            path.unlink(missing_ok=True)
            path.with_suffix(".rgba").unlink(missing_ok=True)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if source.status != "ready":
            asset.path.unlink(missing_ok=True)
            raise AppError("source_not_found")
        self._assets[asset.id] = asset
        logger.info("source %s: %s asset %s added", source.id, kind, asset.id)
        return asset

    def asset_view(self, asset: Asset) -> dict:
        return {"asset_id": asset.id, "kind": asset.kind, "width": asset.width, "height": asset.height,
                "duration": asset.duration}

    def _asset(self, asset_id: str | None, source: Job, kind: str) -> Asset | None:
        if asset_id is None:
            return None
        asset = self._assets.get(asset_id)
        if asset is None or asset.source_id != source.id or asset.kind != kind or not asset.path.is_file():
            raise AppError("asset_not_found")
        return asset

    def _branding_request(self, resolved: ResolvedSpec, source: Job, *, video: bool) -> dict:
        b = resolved.branding
        logo = self._asset(b.logo, source, "logo")
        intro = self._asset(b.intro, source, "intro") if video else None
        outro = self._asset(b.outro, source, "outro") if video else None
        return {
            "logo": None if logo is None else {"path": str(logo.path), "scale": b.logo_scale,
                                               "opacity": b.logo_opacity, "position": resolved.logo_position,
                                               "width": logo.width, "height": logo.height},
            "intro": None if intro is None else str(intro.path),
            "outro": None if outro is None else str(outro.path),
        }

    # --- face analysis (cached) -------------------------------------------------------------

    async def _analysis(self, source: Job, start: float, end: float, settings: RenderSettings,
                        on_progress: Callable[[float | None], None]) -> Path | None:
        """The cached face analysis for [start, end), made first if needed. None when it can't be made
        (the render then uses the layout's fallback)."""
        folder = self._base(source) / "analysis"
        name = f"{start:.3f}-{end:.3f}-{settings.render_analysis_fps}-v{ANALYSIS_VERSION}.json"
        path = folder / name
        lock = self._analysis_locks.setdefault(f"{source.id}/{name}", asyncio.Lock())
        async with lock:
            if path.is_file():
                return path
            work = folder / f"work-{secrets.token_hex(6)}"
            work.mkdir(parents=True, exist_ok=True)

            def on_event(event: dict) -> None:
                if event.get("event") == "progress" and isinstance(event.get("percent"), (int, float)):
                    on_progress(float(event["percent"]))

            try:
                await self._run_worker(
                    {"mode": "analyze", "source": str(source.file_path), "workdir": str(work), "start": start,
                     "end": end, "fps": settings.render_analysis_fps, "threads": settings.render_cpu_threads,
                     "output": "analysis.json", "memory_bytes": settings.max_memory_bytes},
                    cmd=self._cmd, timeout=settings.render_max_job_seconds, on_event=on_event, workdir=work,
                    max_workdir_bytes=256 * 1024 * 1024)
                (work / "analysis.json").replace(path)
                return path
            except AppError as exc:
                logger.warning("source %s: face analysis failed (%s); using the fallback layout", source.id, exc.code)
                return None
            finally:
                shutil.rmtree(work, ignore_errors=True)

    # --- renders ----------------------------------------------------------------------------

    def _pending_render(self, source_id: str) -> Render | None:
        return next((r for r in self._renders.values() if r.source_id == source_id and r.status in _PENDING), None)

    def start_render(self, source_id: str, body: RenderBody) -> Render:
        settings, caps, _encoder = self._ready()
        source = self._jobs.ready_source(source_id)
        if self._pending_render(source.id) is not None:
            raise AppError("render_running")
        pending = sum(1 for r in self._renders.values() if r.status in _PENDING)
        if pending >= settings.render_max_concurrent_jobs + settings.render_max_queued_jobs:
            raise AppError("busy")
        resolved = resolve(body.spec)
        if resolved.captions["burn"] and resolved.captions["enabled"] and not caps.captions:
            raise AppError("render_unavailable", "This FFmpeg can't burn in captions (no libass). Turn burning "
                           "off, or use the Docker image.")
        for n, clip in enumerate(body.clips, 1):
            try:
                start, end = _clip_seconds(clip, source.duration)
            except AppError as exc:
                raise AppError(exc.code, f"Clip {n}: {exc.message}") from None
            if end - start > settings.render_max_clip_seconds:
                raise AppError("invalid_clip", f"Clip {n} is longer than the {settings.render_max_clip_seconds}-second "
                               "limit for Auto Shorts.")
        self._branding_request(resolved, source, video=True)  # asset ids must exist now
        self._prune(source)
        self._jobs.hold_source(source.id)
        render_id = _new_id(self._renders)
        folder = self._base(source) / render_id
        render = Render(id=render_id, source_id=source.id, spec=body.spec, resolved=resolved,
                        clips=[ClipState(n, clip) for n, clip in enumerate(body.clips, 1)], folder=folder,
                        title=source.title, created_at=self._clock())
        self._renders[render.id] = render
        render.task = self._spawn(self._run(render, source), source)
        logger.info("render %s of %d clips queued for source %s", render.id, len(render.clips), source.id)
        return render

    def get_render(self, render_id: str) -> Render:
        render = self._renders.get(render_id) if _ID_RE.fullmatch(str(render_id)) else None
        if render is None:
            raise AppError("render_not_found")
        try:  # a poll keeps the source alive
            self._jobs.touch(self._jobs.get_source(render.source_id))
        except AppError:
            pass
        return render

    def cancel_render(self, render_id: str) -> Render:
        render = self.get_render(render_id)
        if render.status in _PENDING:
            for clip in render.clips:
                if clip.status in _CLIP_PENDING:
                    self._end_clip(clip, "cancelled")
            self._end_render(render, "cancelled")
            if render.task is not None:
                render.task.cancel()
            logger.info("render %s cancelled", render.id)
        return render

    def cancel_clip(self, render_id: str, number: int) -> Render:
        render = self.get_render(render_id)
        if not 1 <= number <= len(render.clips):
            raise AppError("render_not_found", "That clip isn't part of this render.")
        clip = render.clips[number - 1]
        if clip.status in _CLIP_PENDING:
            self._end_clip(clip, "cancelled")
            if clip.task is not None:
                clip.task.cancel()
        return render

    def _on_source_ended(self, source_id: str) -> None:
        for render in list(self._renders.values()):
            if render.source_id == source_id and render.status in _PENDING:
                self.cancel_render(render.id)
        for preview in list(self._previews.values()):
            if preview.source_id == source_id and preview.status not in ("ready", "failed", "cancelled"):
                self._cancel_preview(preview)
        for asset_id in [a.id for a in self._assets.values() if a.source_id == source_id]:
            self._assets.pop(asset_id, None)

    def _spawn(self, coro, source: Job) -> asyncio.Task:
        """Start `coro` for a source this manager has just held, and release that hold when the task ends,
        whether the coroutine ran (its own `finally` released it) or was cancelled before its first step."""
        state = {"released": False, "started": False}

        def release() -> None:
            if not state["released"]:
                state["released"] = True
                self._jobs.release_source(source)

        async def run():
            state["started"] = True
            try:
                await coro
            finally:
                release()

        def done(_task: asyncio.Task) -> None:
            if not state["started"]:
                coro.close()  # cancelled before its first step: it never ran
            release()

        task = asyncio.create_task(run())
        task.add_done_callback(done)
        return task

    def _end_clip(self, clip: ClipState, status: str, error: AppError | None = None) -> None:
        clip.status, clip.error, clip.detail = status, error, None
        if status != "ready":
            clip.percent = None
        clip.finished_at = self._clock()

    def _end_render(self, render: Render, status: str, error: AppError | None = None) -> None:
        render.status, render.error = status, error
        render.finished_at = self._clock()

    def _prune(self, source: Job | None = None) -> None:
        """Forget old records; keep each source's newest finished renders' files."""
        now = self._clock()
        finished = sorted((r for r in self._renders.values() if r.status not in _PENDING),
                          key=lambda r: r.finished_at or 0.0)
        for n, render in enumerate(finished):
            if len(finished) - n > MAX_RECORDS - 1 or now - (render.finished_at or now) >= RECORD_SECONDS:
                self._drop_render(render)
        if source is not None:
            mine = sorted((r for r in self._renders.values() if r.source_id == source.id and r.status not in _PENDING),
                          key=lambda r: r.finished_at or 0.0)
            for render in mine[:max(0, len(mine) - (MAX_RENDERS_PER_SOURCE - 1))]:
                self._drop_render(render)
        for preview in sorted(self._previews.values(), key=lambda p: p.created_at)[:-MAX_RECORDS or None]:
            self._drop_preview(preview)

    def _drop_render(self, render: Render) -> None:
        self._renders.pop(render.id, None)
        shutil.rmtree(render.folder, ignore_errors=True)

    def render_view(self, render: Render) -> dict:
        now = self._clock()
        clips = []
        total = sum(c.clip.end - c.clip.start for c in render.clips) or 1.0
        done = 0.0
        for c in render.clips:
            length = c.clip.end - c.clip.start
            eta = None
            if c.status == "rendering" and c.percent and c.started_at is not None and c.percent > 2:
                elapsed = now - c.started_at
                eta = max(0, round(elapsed * (100 - c.percent) / c.percent))
            if c.status in ("ready", "failed", "cancelled"):
                done += length
            elif c.percent is not None and c.status == "rendering":
                done += length * c.percent / 100
            result = c.result
            clips.append({
                "number": c.number, "start": c.clip.start, "end": c.clip.end, "title": c.clip.title,
                "status": c.status, "detail": c.detail, "percent": c.percent,
                "percent_measured": c.percent is not None, "eta_seconds": eta, "eta_estimated": eta is not None,
                "encoder": c.encoder, "mode": c.mode, "notice": c.notice, "captions_timing": c.timing,
                "error": None if c.error is None else c.error.to_dict(),
                "width": result.get("width"), "height": result.get("height"), "duration": result.get("duration"),
                "bytes": result.get("bytes"), "fps": result.get("fps"),
                "files": sorted(c.files) if c.status == "ready" else [],
            })
        return {"render_id": render.id, "source_id": render.source_id, "status": render.status,
                "error": None if render.error is None else render.error.to_dict(),
                "percent": round(100 * done / total, 1), "percent_estimated": True,
                "ready": sum(1 for c in render.clips if c.status == "ready"), "count": len(render.clips),
                "clips": clips, "spec": render.spec.model_dump()}

    async def _run(self, render: Render, source: Job) -> None:
        settings = self._settings
        assert settings is not None
        try:
            async with self._slots:
                if render.status != "queued":
                    return
                render.status, render.started_at = "running", self._clock()
                render.folder.mkdir(parents=True, exist_ok=True)
                transcript = self.transcript_for(source)
                for clip in render.clips:
                    if render.status != "running":
                        break
                    if clip.status != "waiting":
                        continue
                    clip.task = asyncio.create_task(self._render_clip(render, clip, source, settings, transcript))
                    try:
                        await clip.task
                    except asyncio.CancelledError:
                        if render.status != "running":
                            raise
                        # only this clip was cancelled: go on with the next
                        if asyncio.current_task().cancelling():
                            raise
            if render.status == "running":
                ready = any(c.status == "ready" for c in render.clips)
                errors = [c.error for c in render.clips if c.error is not None]
                if ready:
                    self._end_render(render, "ready")
                elif all(c.status == "cancelled" for c in render.clips):
                    self._end_render(render, "cancelled")
                else:
                    self._end_render(render, "failed", errors[0] if errors else AppError("render_failed"))
                logger.info("render %s %s (%d of %d clips)", render.id, render.status,
                            sum(c.status == "ready" for c in render.clips), len(render.clips))
        except asyncio.CancelledError:
            if render.status in _PENDING:
                for clip in render.clips:
                    if clip.status in _CLIP_PENDING:
                        self._end_clip(clip, "cancelled")
                self._end_render(render, "cancelled")
        except Exception:
            logger.exception("render %s failed unexpectedly", render.id)
            self._end_render(render, "failed", AppError("internal"))
        finally:
            for clip in render.clips:
                shutil.rmtree(render.folder / f"work-{clip.number:02d}", ignore_errors=True)

    async def _render_clip(self, render: Render, clip: ClipState, source: Job, settings: RenderSettings,
                           transcript: Transcript | None) -> None:
        _s, _caps, encoder = self._ready()
        resolved = render.resolved
        key = f"render:{render.id}:{clip.number}"
        work = render.folder / f"work-{clip.number:02d}"
        clip.started_at = self._clock()
        try:
            if source.status != "ready" or source.file_path is None:
                raise AppError("source_not_found")
            start, end = _clip_seconds(clip.clip, source.duration)
            length = end - start
            layout = dict(resolved.layout)
            if clip.clip.manual is not None:
                layout["manual"] = clip.clip.manual.model_dump()
            analysis = None
            if layout["mode"] in _FACE_MODES and clip.clip.manual is None:
                clip.status, clip.detail, clip.percent = "analyzing", "Finding faces", None

                def analysis_progress(percent):
                    if clip.status == "analyzing":
                        clip.percent = percent

                analysis = await self._analysis(source, start, end, settings, analysis_progress)
            clip.status, clip.detail, clip.percent = "rendering", "Rendering", 0.0
            cap_w, cap_h = self._canvas_for(resolved)
            cues, timing = self._clip_cues(clip.clip, transcript, start, end, resolved, cap_w, cap_h)
            captions = resolved.captions
            clip.timing = timing if captions["enabled"] else None
            if captions["enabled"] and not cues and clip.clip.cues is None:
                clip.notice = ("No transcript yet, so this short has no captions. Transcribe the video, then render "
                               "again." if transcript is None else "No speech in this clip, so it has no captions.")
            bumpers = sum(a.duration or 0 for a in (self._asset(resolved.branding.intro, source, "intro"),
                                                    self._asset(resolved.branding.outro, source, "outro")) if a)
            rate = bitrate_cap_bytes_per_second(resolved.quality, cap_w, cap_h, resolved.fps or 30.0)
            reserve = int(rate * (length + bumpers) * 1.5) + _SLACK
            if not self._jobs.reserve_disk(key, work, reserve):
                raise AppError("disk_full")
            work.mkdir(parents=True, exist_ok=True)
            burned = self._caption_request(cues, resolved, title=clip.clip.title or render.title,
                                           burn=captions["burn"])

            def on_event(event: dict) -> None:
                if event.get("event") != "progress" or clip.status != "rendering":
                    return
                percent = event.get("percent")
                if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                    clip.percent = min(100.0, max(0.0, float(percent)))
                if isinstance(event.get("encoder"), str):
                    clip.encoder = event["encoder"]

            request = {"mode": "render", "kind": "video", "source": str(source.file_path), "workdir": str(work),
                       "output": "short.mp4", "start": start, "end": end, "aspect": resolved.aspect,
                       "width": resolved.width, "height": resolved.height, "fps": resolved.fps,
                       "quality": resolved.quality, "encoder": encoder, "threads": settings.render_cpu_threads,
                       "layout": layout, "analysis": None if analysis is None else str(analysis),
                       "speech": self._speech(transcript, start, end), "captions": burned,
                       "fonts_dir": None if settings.render_fonts_dir is None else str(settings.render_fonts_dir),
                       **self._branding_request(resolved, source, video=True),
                       "max_bytes": self._files.max_file_bytes, "memory_bytes": settings.max_memory_bytes}
            result = await self._run_worker(request, cmd=self._cmd, timeout=settings.render_max_job_seconds,
                                            on_event=on_event, workdir=work, max_workdir_bytes=2 * reserve)
            if result.get("file") != "short.mp4" or not (work / "short.mp4").is_file():
                logger.error("render %s clip %d: the worker returned no file", render.id, clip.number)
                raise AppError("internal")
            mp4 = render.folder / f"{clip.number:02d}.mp4"
            (work / "short.mp4").replace(mp4)
            clip.files = {"mp4": mp4}
            if captions["enabled"] and cues:
                for kind in captions["export"]:
                    out = render.folder / f"{clip.number:02d}.{kind}"
                    if kind == "srt":
                        out.write_text(to_srt(cues, uppercase=bool(captions.get("uppercase"))), encoding="utf-8")
                    else:
                        plan = AssPlan(width=result.get("width", cap_w), height=result.get("height", cap_h),
                                       style=captions, safe=resolved.safe, duration=length)
                        out.write_text(to_ass(cues, plan), encoding="utf-8")
                    clip.files[kind] = out
            clip.result = {k: result.get(k) for k in ("width", "height", "duration", "bytes", "fps")}
            clip.encoder, clip.mode = result.get("encoder"), result.get("mode")
            if result.get("notice"):
                clip.notice = " ".join(n for n in (clip.notice, result["notice"]) if n)
            self._end_clip(clip, "ready")
            clip.percent = 100.0
        except asyncio.CancelledError:
            if clip.status in _CLIP_PENDING:
                self._end_clip(clip, "cancelled")
            raise
        except AppError as exc:
            if clip.status in _CLIP_PENDING:
                self._end_clip(clip, "failed", exc)
            logger.info("render %s clip %d failed: %s", render.id, clip.number, exc.code)
        except Exception:
            logger.exception("render %s clip %d failed unexpectedly", render.id, clip.number)
            if clip.status in _CLIP_PENDING:
                self._end_clip(clip, "failed", AppError("internal"))
        finally:
            self._jobs.release_disk(key)
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _canvas_for(resolved: ResolvedSpec) -> tuple[int, int]:
        """The canvas lines are fitted to: the spec's size, or (for the source's own aspect, known only to
        the renderer) a narrow 1080×1920 stand-in, which only ever makes lines shorter."""
        return resolved.width or 1080, resolved.height or 1920

    # --- delivery -----------------------------------------------------------------------------

    def clip_file(self, render_id: str, number: int, kind: str) -> tuple[Path, str, str]:
        """(path, media type, file name) of a finished clip's MP4, SRT or ASS."""
        from app.filenames import short_filename

        render = self.get_render(render_id)
        if not 1 <= number <= len(render.clips):
            raise AppError("render_not_found", "That clip isn't part of this render.")
        clip = render.clips[number - 1]
        if clip.status != "ready":
            raise AppError("not_ready", "This short isn't ready.")
        path = clip.files.get(kind)
        if path is None or not path.is_file():
            raise AppError("expired", "This file isn't on the server any more. Render again.")
        media = {"mp4": "video/mp4", "srt": "application/x-subrip", "ass": "text/x-ssa"}[kind]
        name = short_filename(clip.clip.title or render.title, number, clip.clip.start, clip.clip.end, kind)
        return path, media, name

    async def zip_file(self, render_id: str, subtitles: bool) -> tuple[Path, str]:
        """A ZIP of every finished clip (and its subtitle files), made on first request and kept."""
        from app.filenames import short_filename, shorts_zip_name

        render = self.get_render(render_id)
        if render.status in _PENDING:
            raise AppError("not_ready", "The ZIP can be downloaded once the render has finished.")
        ready = [c for c in render.clips if c.status == "ready" and c.files.get("mp4") and c.files["mp4"].is_file()]
        if not ready:
            raise AppError("not_ready", "No short in this render is ready.")
        name = shorts_zip_name(render.title, len(ready))
        async with render.zip_lock:  # two first requests (a double click) build it once
            path = render.zips.get(subtitles)
            if path is not None and path.is_file():
                return path, name
            return await self._build_zip(render, ready, subtitles), name

    async def _build_zip(self, render: Render, ready: list[ClipState], subtitles: bool) -> Path:
        from app.filenames import short_filename

        entries = []
        for clip in ready:
            for kind, file in sorted(clip.files.items()):
                if kind != "mp4" and not subtitles:
                    continue
                entries.append((file, short_filename(clip.clip.title or render.title, clip.number, clip.clip.start,
                                                     clip.clip.end, kind, numbered=True)))
        size = sum(f.stat().st_size + 1024 for f, _n in entries) + 1024
        key = f"zip:{render.id}:{int(subtitles)}"
        target = render.folder / f"shorts{'-subs' if subtitles else ''}.zip"
        if not self._jobs.reserve_disk(key, render.folder, size):
            raise AppError("disk_full")
        try:
            await asyncio.to_thread(_write_zip, target, entries)
        finally:
            self._jobs.release_disk(key)
        render.zips[subtitles] = target
        return target

    # --- previews -----------------------------------------------------------------------------

    def start_preview(self, source_id: str, body: PreviewBody) -> Preview:
        settings, caps, _encoder = self._ready()
        source = self._jobs.ready_source(source_id)
        resolved = resolve(body.spec)
        start, end = _clip_seconds(body.clip, source.duration)
        if end - start > settings.render_max_clip_seconds:  # face analysis covers the whole clip
            raise AppError("invalid_clip", f"This clip is longer than the {settings.render_max_clip_seconds}-second "
                           "limit for Auto Shorts.")
        if body.kind == "frame" and not 0 <= body.at <= end - start:
            raise AppError("invalid_clip", "Choose a moment inside the clip.")
        if body.kind == "frame" and not caps.jpeg:
            raise AppError("render_unavailable", "This FFmpeg can't write JPEG stills.")
        self._branding_request(resolved, source, video=False)
        for old in [p for p in self._previews.values() if p.source_id == source.id
                    and p.status not in ("ready", "failed", "cancelled")]:
            self._cancel_preview(old)  # the newest preview wins
        mine = sorted((p for p in self._previews.values() if p.source_id == source.id), key=lambda p: p.created_at)
        for old in mine[:max(0, len(mine) - (MAX_PREVIEWS_PER_SOURCE - 1))]:
            self._drop_preview(old)
        self._prune()
        self._jobs.hold_source(source.id)
        preview = Preview(id=_new_id(self._previews), source_id=source.id, kind=body.kind, created_at=self._clock(),
                          assets={x for x in (resolved.branding.logo,) if x})
        self._previews[preview.id] = preview
        preview.task = self._spawn(self._run_preview(preview, source, body, resolved, settings), source)
        return preview

    def get_preview(self, preview_id: str) -> Preview:
        preview = self._previews.get(preview_id) if _ID_RE.fullmatch(str(preview_id)) else None
        if preview is None:
            raise AppError("preview_not_found")
        return preview

    def preview_view(self, preview: Preview) -> dict:
        return {"preview_id": preview.id, "kind": preview.kind, "status": preview.status, "detail": preview.detail,
                "progress_percent": preview.percent, "mode": preview.mode,
                "error": None if preview.error is None else preview.error.to_dict()}

    def preview_file(self, preview_id: str) -> tuple[Path, str]:
        preview = self.get_preview(preview_id)
        if preview.status != "ready" or preview.file is None or not preview.file.is_file():
            raise AppError("not_ready", "This preview isn't ready.")
        return preview.file, "image/jpeg" if preview.kind == "frame" else "video/mp4"

    def _cancel_preview(self, preview: Preview) -> None:
        self._end_preview(preview, "cancelled")
        if preview.task is not None:
            preview.task.cancel()

    def _end_preview(self, preview: Preview, status: str, error: AppError | None = None) -> None:
        preview.status, preview.error, preview.detail = status, error, None
        preview.finished_at = self._clock()

    def _drop_preview(self, preview: Preview) -> None:
        if preview.status not in ("ready", "failed", "cancelled"):
            self._cancel_preview(preview)
        self._previews.pop(preview.id, None)
        if preview.file is not None:
            preview.file.unlink(missing_ok=True)

    async def _run_preview(self, preview: Preview, source: Job, body: PreviewBody, resolved: ResolvedSpec,
                           settings: RenderSettings) -> None:
        work = self._base(source) / "previews" / f"work-{preview.id}"
        key = f"preview:{preview.id}"
        try:
            async with self._preview_slot:
                if preview.status != "queued":
                    return
                _s, _caps, encoder = self._ready()
                start, end = _clip_seconds(body.clip, source.duration)
                if body.kind == "clip":
                    end = min(end, start + PREVIEW_CLIP_SECONDS)
                layout = dict(resolved.layout)
                if body.clip.manual is not None:
                    layout["manual"] = body.clip.manual.model_dump()
                analysis = None
                if layout["mode"] in _FACE_MODES and body.clip.manual is None:
                    preview.status, preview.detail = "analyzing", "Finding faces"

                    def progress(percent):
                        if preview.status == "analyzing":
                            preview.percent = percent

                    full_start, full_end = _clip_seconds(body.clip, source.duration)
                    analysis = await self._analysis(source, full_start, full_end, settings, progress)
                preview.status, preview.detail, preview.percent = "rendering", "Rendering the preview", None
                cap_w, cap_h = self._canvas_for(resolved)
                transcript = self.transcript_for(source)
                cues, _timing = self._clip_cues(body.clip, transcript, start, end, resolved, cap_w, cap_h)
                if not self._jobs.reserve_disk(key, work, 256 * 1024 * 1024):
                    raise AppError("disk_full")
                work.mkdir(parents=True, exist_ok=True)
                burned = self._caption_request(cues if resolved.captions["enabled"] else [], resolved,
                                               title=body.clip.title or source.title, burn=resolved.captions["burn"])
                video = body.kind == "clip"
                output = "preview.mp4" if video else "preview.jpg"

                def on_event(event: dict) -> None:
                    if event.get("event") == "progress" and isinstance(event.get("percent"), (int, float)):
                        preview.percent = float(event["percent"])

                # The last frame of a clip sits a little before its end (at the very end there is no frame).
                at = min(body.at, max(0.0, end - start - PREVIEW_END_MARGIN))
                request = {"mode": "render", "kind": "video" if video else "frame", "source": str(source.file_path),
                           "workdir": str(work), "output": output, "start": start, "end": end, "at": at,
                           "aspect": resolved.aspect, "width": resolved.width, "height": resolved.height,
                           "fps": resolved.fps, "quality": "draft", "encoder": encoder,
                           "threads": settings.render_cpu_threads, "layout": layout,
                           "analysis": None if analysis is None else str(analysis),
                           "speech": self._speech(transcript, start, end), "captions": burned,
                           "fonts_dir": None if settings.render_fonts_dir is None else str(settings.render_fonts_dir),
                           **self._branding_request(resolved, source, video=False),
                           "max_bytes": self._files.max_file_bytes, "memory_bytes": settings.max_memory_bytes}
                result = await self._run_worker(request, cmd=self._cmd, timeout=settings.render_max_job_seconds,
                                                on_event=on_event, workdir=work, max_workdir_bytes=512 * 1024 * 1024)
                if preview.status != "rendering":
                    return
                final = self._base(source) / "previews" / f"{preview.id}{Path(output).suffix}"
                (work / output).replace(final)
                preview.file, preview.mode = final, result.get("mode")
                self._end_preview(preview, "ready")
        except asyncio.CancelledError:
            if preview.status not in ("ready", "failed", "cancelled"):
                self._end_preview(preview, "cancelled")
        except AppError as exc:
            if preview.status not in ("ready", "failed", "cancelled"):
                self._end_preview(preview, "failed", exc)
        except Exception:
            logger.exception("preview %s failed unexpectedly", preview.id)
            self._end_preview(preview, "failed", AppError("internal"))
        finally:
            self._jobs.release_disk(key)
            shutil.rmtree(work, ignore_errors=True)


def _write_zip(target: Path, entries: list[tuple[Path, str]]) -> None:
    """Write the ZIP to a temporary name and rename it, so a reader never sees half of it. MP4s are
    stored (already compressed); subtitle files are deflated."""
    tmp = target.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", allowZip64=True) as bundle:
            for path, name in entries:
                method = zipfile.ZIP_STORED if path.suffix == ".mp4" else zipfile.ZIP_DEFLATED
                bundle.write(path, arcname=name, compress_type=method)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def eta(seconds: float | None) -> int | None:
    return None if seconds is None or not math.isfinite(seconds) else max(0, round(seconds))
