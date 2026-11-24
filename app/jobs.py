"""In-memory job manager: lifecycle, bounded queue, disk budget, TTL and cleanup.

A job ends ready (then expired), failed, or cancelled by the user. A cancel kills
the worker but keeps its partial files for RESUME_WINDOW_SECONDS: a resume runs the
job again in the same folder, where yt-dlp continues the partial files; after the
window the sweep removes them. At most MAX_ACTIVE_JOBS + MAX_QUEUED_JOBS cancelled jobs
can be resumed at once (a later cancel ends the oldest window), and at most
MAX_FINISHED_RECORDS finished records are kept, so create-and-cancel can't grow the
record table. A ready file is sent to one client at a time: while it is being sent it
stays on disk (and in the disk budget) and the TTL, eviction and the ready-file cap
skip it; an interrupted send leaves it ready for another try. Job records live only
in this process. Each job's
files live in TEMP_DIR/<job_id>/ under fixed names; the job ID is the only name derived from
anything, and it is validated before any filesystem use.

Clip Studio adds two kinds of job beside downloads (kind "download"). A source (kind
"source") is a video kept for previewing and cutting while a studio is open: downloaded
from a link, or uploaded. It is never delivered, MAX_READY_FILES and disk-room eviction
skip it, and it ends when closed, after JOB_TTL_MINUTES unused (never while one of its
clips is pending), or when MAX_CLIP_SOURCES is reached and it is the least recently used.
A clip (kind "clip") cuts a range out of a ready source into its own folder and, once
ready, is delivered like a download; a cancelled clip can't be resumed. A batch (kind
"batch") is the same for a list of ranges, delivered as one ZIP. All kinds share the
queue, the slots and the disk budget.

AI Highlights (app.ai_jobs) uses ready sources without taking a slot or a queue place: an
analysis *holds* its source (hold_source/release_source), which then counts as in use (no idle
expiry, no replacement), hears through add_source_listener when the source ends (closed, idle,
replaced; before its folder is removed), and reserves disk for its audio with make_room. Its
files live in the source's folder, so they end with it.
"""
import asyncio
import logging
import math
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.config import ConfigError, Settings
from app.errors import AppError
from app.filenames import (ZIP_MEDIA_TYPE, batch_entry_name, batch_filename, clip_filename,
                           media_type_for, sanitize_filename)
from app.mediafile import BATCH_NAME, MAX_BATCH_CLIPS
from app.runner import DEFAULT_WORKER_CMD, RunWorker, dir_size, run_worker

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
MARKER_NAME = ".saveme-tempdir"
APP_DIR = Path(__file__).resolve().parent.parent

RECORD_RETENTION_SECONDS = 3600
# Failed, expired and no-longer-resumable records kept for status polls, newest first.
MAX_FINISHED_RECORDS = 100
DISK_RETRY_SECONDS = 2.0
RESUME_WINDOW_SECONDS = 300
UPLOAD_NAME = "upload.bin"  # an upload's bytes, before the probe run makes it media.<ext>
MIN_CLIP_SECONDS = 0.1
# A clip's end may pass the source's probed duration by this much (players and probes
# round differently); it is then cut at the duration.
END_TOLERANCE_SECONDS = 0.05

_ACTIVE = frozenset({"downloading", "converting", "uploading", "checking", "cutting"})
_PENDING = frozenset({"queued", *_ACTIVE})
_TERMINAL = frozenset({"failed", "expired", "cancelled"})
_JOB_KINDS = frozenset({"download", "clip", "batch"})  # what /api/jobs serves; sources have their own
_CUT_KINDS = frozenset({"clip", "batch"})  # jobs cut out of a source


def _is_job_id(value: object) -> bool:
    return isinstance(value, str) and JOB_ID_RE.fullmatch(value) is not None


def _remove_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _job_dirs(temp_dir: Path) -> list[Path]:
    """Real (non-symlink) subdirectories of temp_dir named like job IDs."""
    try:
        with os.scandir(temp_dir) as entries:
            return [Path(e.path) for e in entries
                    if _is_job_id(e.name) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def prepare_temp_dir(path: Path, *, app_dir: Path) -> None:
    """Refuse a temp dir that isn't dedicated to us; create it and its marker."""
    target = Path(path).resolve()
    app = Path(app_dir).resolve()
    if target.parent == target:
        raise ConfigError("TEMP_DIR", "must not be the filesystem root")
    if target.is_relative_to(app):
        raise ConfigError("TEMP_DIR", "must not be the app directory or inside it")
    marker = target / MARKER_NAME
    try:
        if target.exists():
            if not target.is_dir():
                raise ConfigError("TEMP_DIR", "must be a directory")
            if not marker.is_file() and any(target.iterdir()):
                raise ConfigError(
                    "TEMP_DIR", "is a non-empty folder saveme didn't create; "
                    "point it at an empty or dedicated folder")
        target.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError as exc:
        raise ConfigError("TEMP_DIR", f"can't be prepared ({exc.strerror})") from None


@dataclass(eq=False)
class Job:
    id: str
    url: str
    output: str
    quality: str
    workdir: Path
    created_at: float
    queued_at: float
    status: str = "queued"
    detail: str | None = None
    progress_percent: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    notice: str | None = None
    error: AppError | None = None
    reason: str | None = None
    title: str | None = None
    filename: str | None = None
    media_type: str | None = None
    file_path: Path | None = None
    started_at: float | None = None
    ready_at: float | None = None
    finished_at: float | None = None
    resume_until: float | None = None  # set while a cancelled job can be resumed
    delivering: bool = False  # its file is being sent to a client
    kind: str = "download"  # download | source | clip | batch
    origin: str | None = None  # a source's origin: link | upload
    source_id: str | None = None  # a clip's or batch's source
    start: float | None = None  # a clip's range, in seconds
    end: float | None = None
    ranges: tuple[tuple[float, float], ...] = ()  # a batch's ranges, in seconds, in order
    duration: float | None = None  # a ready source's length in seconds, when known
    last_used: float | None = None  # a ready source's last use, for its idle TTL
    task: asyncio.Task | None = field(default=None, repr=False)  # the latest run
    runs: set[asyncio.Task] = field(default_factory=set, repr=False)  # unfinished runs


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(100.0, max(0.0, float(value)))


def _clip_range(start: float, end: float, duration: float | None) -> tuple[float, float]:
    """The range to cut, with an end just past the source's duration pulled back to it;
    raises invalid_clip naming the rule a range breaks."""
    def invalid(message: str) -> AppError:
        return AppError("invalid_clip", message)

    if start < 0:
        raise invalid("The start can't be before 0:00.")
    if end <= start:
        raise invalid("The end must be after the start.")
    if duration is not None:
        if start >= duration:
            raise invalid("The start must be before the end of the video.")
        if end > duration + END_TOLERANCE_SECONDS:
            raise invalid("The end can't be after the end of the video.")
        end = min(end, duration)
    if round(end * 1000) - round(start * 1000) < round(MIN_CLIP_SECONDS * 1000):  # whole ms, as the page
        raise invalid("A clip must be at least 0.1 seconds long.")
    return start, end


def _seconds(value: object) -> float | None:
    """A positive, finite number of seconds from a worker result, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if value > 0 else None


class JobManager:
    def __init__(
        self,
        settings: Settings,
        *,
        run_worker: RunWorker = run_worker,
        worker_cmd: Sequence[str] = DEFAULT_WORKER_CMD,
        clock: Callable[[], float] = time.monotonic,
        disk_usage: Callable[[Path], int] = dir_size,
        sweep_interval: float = 60.0,
        app_dir: Path = APP_DIR,
    ):
        self._settings = settings
        self._run_worker = run_worker
        self._worker_cmd = tuple(worker_cmd)
        self._clock = clock
        self._disk_usage = disk_usage
        self._sweep_interval = sweep_interval
        self._app_dir = app_dir
        self._temp_dir = settings.temp_dir
        self._reserve = 2 * settings.max_file_bytes
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()
        self._slots = asyncio.Semaphore(settings.max_active_jobs)
        self._sweeper: asyncio.Task | None = None
        self._holds: dict[str, int] = {}  # source ID -> analyses using it
        self._source_listeners: list[Callable[[str], None]] = []

    # --- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        prepare_temp_dir(self._temp_dir, app_dir=self._app_dir)
        for path in _job_dirs(self._temp_dir):
            _remove_dir(path)
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def shutdown(self) -> None:
        tasks = [t for t in (self._sweeper, *self._tasks) if t is not None and not t.done()]
        for task in tasks:  # once only: run_worker's cleanup kills the process group,
            if not task.cancelling():  # so a task a user cancel already stopped is left to finish
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for path in _job_dirs(self._temp_dir):
            _remove_dir(path)

    # --- Public API -----------------------------------------------------------

    def submit(self, url: str, output: str, quality: str) -> Job:
        self._admit()
        job = self._new_job(url=url, output=output, quality=quality)
        self._start(job)
        logger.info("job %s queued (%s, %s)", job.id, output, quality)
        return job

    def get(self, job_id: str) -> Job:
        """A download or clip job; sources are only reachable through get_source."""
        job = self._jobs.get(job_id) if _is_job_id(job_id) else None
        if job is None or job.kind not in _JOB_KINDS:
            raise AppError("job_not_found")
        return job

    def public_view(self, job: Job) -> dict:
        return {
            "job_id": job.id,
            "kind": job.kind,
            "status": job.status,
            "detail": job.detail,
            "progress_percent": job.progress_percent,
            "downloaded_bytes": job.downloaded_bytes,
            "total_bytes": job.total_bytes,
            "notice": job.notice,
            "error": job.error.to_dict() if job.error is not None else None,
            "filename": job.filename,
            "reason": job.reason,
            "resume_seconds": self._resume_seconds(job),
        }

    # --- Clip Studio sources ---------------------------------------------------

    def open_link_source(self, url: str, quality: str) -> Job:
        """Download `url` at `quality` as a source to clip from."""
        self.ensure_queue_room()
        self._make_source_room()
        if not self._make_room(count_active=False, reserve=self._reserve):
            raise AppError("disk_full")
        job = self._new_job(url=url, output="video", quality=quality, kind="source", origin="link")
        self._start(job)
        logger.info("source %s queued (link, %s)", job.id, quality)
        return job

    def get_source(self, source_id: str) -> Job:
        job = self._jobs.get(source_id) if _is_job_id(source_id) else None
        if job is None or job.kind != "source":
            raise AppError("source_not_found")
        return job

    def source_view(self, job: Job) -> dict:
        return {
            "source_id": job.id,
            "status": job.status,
            "origin": job.origin,
            "title": job.title,
            "duration_seconds": job.duration,
            "detail": job.detail,
            "progress_percent": job.progress_percent,
            "downloaded_bytes": job.downloaded_bytes,
            "total_bytes": job.total_bytes,
            "notice": job.notice,
            "error": job.error.to_dict() if job.error is not None else None,
            "reason": job.reason,
        }

    def touch(self, job: Job) -> None:
        """Note a use of a ready source (a poll, a preview request), restarting its idle TTL."""
        if job.kind == "source" and job.status == "ready":
            job.last_used = self._clock()

    def source_media(self, source_id: str) -> tuple[Path, str]:
        """The ready source's file and media type, for preview; counts as a use."""
        job = self.get_source(source_id)
        if job.status in _TERMINAL:
            raise AppError("source_not_found")
        if job.status != "ready" or job.file_path is None or job.media_type is None:
            raise AppError("source_not_ready")
        self.touch(job)
        return job.file_path, job.media_type

    def close_source(self, source_id: str) -> Job:
        """End a source in any state and delete its files; a source that already ended is
        left as it is. A running download or check is cancelled (its run removes whatever
        it leaves); an upload in progress stops at its next chunk."""
        job = self.get_source(source_id)
        if job.status not in _TERMINAL:
            for clip in [j for j in self._jobs.values()
                         if j.kind in _CUT_KINDS and j.source_id == job.id and j.status in _PENDING]:
                self.cancel(clip.id)
            if job.task is not None:
                job.task.cancel()
            self._expire(job, "closed")
        return job

    def begin_upload(self, title: str | None) -> Job:
        """A source for a file the caller is about to write to workdir/UPLOAD_NAME. It holds
        a queue place while the bytes arrive, and it must fit the disk budget counting
        active jobs, because its bytes land before any slot is taken."""
        self.ensure_queue_room()
        self._make_source_room()
        if not self._make_room(count_active=True, reserve=self._reserve):
            raise AppError("disk_full")
        job = self._new_job(url="", output="video", quality="best", kind="source", origin="upload",
                            status="uploading", title=title or "Uploaded video")
        job.started_at = job.created_at
        try:
            job.workdir.mkdir()
        except OSError:  # never keep a record that would hold a queue and cap place forever
            del self._jobs[job.id]
            logger.exception("source %s: can't create its folder", job.id)
            raise AppError("internal") from None
        logger.info("source %s uploading", job.id)
        return job

    def finish_upload(self, job: Job) -> None:
        """The whole file has arrived: queue the probe run that checks it."""
        if job.status != "uploading":  # closed while the bytes were arriving
            raise AppError("source_not_found")
        job.status = "queued"
        job.queued_at = self._clock()
        self._start(job)

    def abort_upload(self, job: Job) -> None:
        """The upload failed or the client left: forget it and remove its folder."""
        if job.status == "uploading":
            self._jobs.pop(job.id, None)
        _remove_dir(job.workdir)

    def _ready_source(self, source_id: str) -> Job:
        source = self.get_source(source_id)
        if source.status in _TERMINAL:
            raise AppError("source_not_found")
        if source.status != "ready" or source.file_path is None:
            raise AppError("source_not_ready")
        return source

    def submit_clip(self, source_id: str, start: float, end: float) -> Job:
        """Queue a cut of [start, end) seconds out of a ready source (times in ms)."""
        source = self._ready_source(source_id)
        start, end = _clip_range(round(start, 3), round(end, 3), source.duration)
        self._admit("clip")
        self.touch(source)
        job = self._new_job(url="", output="video", quality="best", kind="clip", source_id=source.id,
                            start=start, end=end, title=source.title)
        self._start(job)
        logger.info("clip %s queued from source %s", job.id, source.id)
        return job

    def submit_batch(self, source_id: str, ranges: Sequence[tuple[float, float]]) -> Job:
        """Queue cuts of every (start, end) range out of a ready source, delivered as one
        ZIP; each range follows the clip rules, and an error names the clip it is about."""
        source = self._ready_source(source_id)
        if not 1 <= len(ranges) <= MAX_BATCH_CLIPS:
            raise AppError("invalid_request", f"Send 1 to {MAX_BATCH_CLIPS} clips.")
        checked = []
        for number, (start, end) in enumerate(ranges, 1):
            try:
                checked.append(_clip_range(round(start, 3), round(end, 3), source.duration))
            except AppError as exc:
                raise AppError(exc.code, f"Clip {number}: {exc.message}") from None
        self._admit("batch")
        self.touch(source)
        job = self._new_job(url="", output="video", quality="best", kind="batch", source_id=source.id,
                            ranges=tuple(checked), title=source.title)
        self._start(job)
        logger.info("batch %s of %d clips queued from source %s", job.id, len(checked), source.id)
        return job

    def ready_source(self, source_id: str) -> Job:
        """The source if it is ready; else source_not_found or source_not_ready."""
        return self._ready_source(source_id)

    def hold_source(self, source_id: str) -> Job:
        """Mark a ready source as in use by an analysis until release_source."""
        source = self._ready_source(source_id)
        self._holds[source.id] = self._holds.get(source.id, 0) + 1
        self.touch(source)
        return source

    def release_source(self, source: Job) -> None:
        """End one hold; the source's idle time starts again from now."""
        left = self._holds.get(source.id, 0) - 1
        if left > 0:
            self._holds[source.id] = left
        else:
            self._holds.pop(source.id, None)
        self.touch(source)

    def add_source_listener(self, callback: Callable[[str], None]) -> None:
        """Call callback(source_id) when a source ends, before its folder is removed."""
        self._source_listeners.append(callback)

    def make_room(self, nbytes: int) -> bool:
        """True if `nbytes` more fit the disk budget now (active jobs counted), evicting unsaved
        ready files if that is enough."""
        return self._make_room(count_active=True, reserve=nbytes)

    def _open_sources(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.kind == "source" and j.status not in _TERMINAL]

    def _in_use(self, source: Job) -> bool:
        """True while one of the source's clips or batches is queued or running, or an
        analysis holds it."""
        return self._holds.get(source.id, 0) > 0 or any(
            j.kind in _CUT_KINDS and j.source_id == source.id and j.status in _PENDING
            for j in self._jobs.values())

    def _make_source_room(self) -> None:
        """Keep open sources under MAX_CLIP_SOURCES: close the least recently used ready
        source with no pending clip, or raise busy when there's none to close."""
        open_sources = self._open_sources()
        if len(open_sources) < self._settings.max_clip_sources:
            return
        idle = sorted((j for j in open_sources if j.status == "ready" and not self._in_use(j)),
                      key=lambda j: j.last_used)
        if not idle:
            raise AppError("busy")
        logger.info("closing source %s (MAX_CLIP_SOURCES)", idle[0].id)
        self._expire(idle[0], "replaced")

    def cancel(self, job_id: str) -> Job:
        """Stop a queued or running job, or discard a ready file; a job that already
        ended is left as it is. The job is cancelled at once. A stopped job can be
        resumed for RESUME_WINDOW_SECONDS. Its run is cancelled (once) and finishes in
        the background: run_worker kills the worker's process group, the partial
        files stay, and the slot is released.
        """
        job = self.get(job_id)
        if job.status == "ready":
            if not job.delivering:  # else finish_delivery removes it once the send ends
                _remove_dir(job.workdir)  # its run has finished: nothing else will remove it
        elif job.status in _PENDING:
            if job.kind == "download":  # a clip can't continue where it stopped
                job.resume_until = self._clock() + RESUME_WINDOW_SECONDS
            if job.task is not None:
                job.task.cancel()
        else:
            return job
        job.status = "cancelled"
        job.detail = None
        job.progress_percent = None
        job.file_path = None
        job.finished_at = self._clock()
        logger.info("job %s cancelled", job.id)
        self._cap_resumable()
        return job

    def resume(self, job_id: str) -> Job:
        """Queue a cancelled job again within its resume window; a job that is queued,
        running or ready is left as it is (a repeated resume). The new run uses the
        same workdir, where yt-dlp continues partial files and skips finished tracks,
        and starts only once the cancelled run has finished killing its worker.
        """
        job = self.get(job_id)
        if job.status in _PENDING or job.status == "ready":
            return job
        if job.status != "cancelled" or job.resume_until is None:
            raise AppError("not_resumable")
        if self._clock() >= job.resume_until:  # the sweep hasn't got to it yet
            self._end_resume_window(job)
            raise AppError("not_resumable")
        self._admit(job.kind, job)  # its partial files count inside its reservation
        job.status = "queued"
        job.resume_until = None
        job.finished_at = None
        job.detail = None
        job.downloaded_bytes = job.total_bytes = job.progress_percent = None
        job.queued_at = self._clock()
        self._start(job)
        logger.info("job %s resumed", job.id)
        return job

    def claim_delivery(self, job_id: str) -> Job:
        """Claim a ready file for one client to download; finish_delivery ends the claim."""
        job = self.get(job_id)
        if job.status == "expired":
            raise AppError("expired")
        if job.status == "cancelled":
            raise AppError("cancelled")
        if job.status != "ready":
            raise AppError("not_ready")
        if job.delivering:
            raise AppError("delivery_in_progress")
        job.delivering = True
        return job

    def finish_delivery(self, job: Job, *, complete: bool) -> None:
        """End a claim. A complete delivery expires the job; an interrupted one leaves it
        ready (the TTL then applies). A job cancelled meanwhile loses its file now."""
        job.delivering = False
        if job.status != "ready":
            _remove_dir(job.workdir)
        elif complete:
            self._expire(job, "delivered")

    async def sweep(self) -> None:
        now = self._clock()
        ttl = self._settings.job_ttl_seconds
        for job in list(self._jobs.values()):
            if job.delivering:
                continue
            if job.kind == "source" and job.status == "ready":
                if now - job.last_used >= ttl and not self._in_use(job):
                    self._expire(job, "idle")
            elif job.status == "ready" and now - job.ready_at >= ttl:
                self._expire(job, "ttl")
            elif job.resume_until is not None and now >= job.resume_until:
                self._end_resume_window(job)
            elif (job.status in _TERMINAL
                  and now - job.finished_at >= RECORD_RETENTION_SECONDS):
                del self._jobs[job.id]
        live = {j.id for j in self._jobs.values()
                if j.status not in _TERMINAL or j.resume_until is not None or j.delivering}
        for path in _job_dirs(self._temp_dir):
            if path.name not in live:
                logger.info("removing orphan job directory %s", path.name)
                _remove_dir(path)

    def _resume_seconds(self, job: Job) -> int | None:
        """Whole seconds left to resume a cancelled job, or None if it can't be."""
        if job.resume_until is None:
            return None
        left = job.resume_until - self._clock()
        return max(1, math.ceil(left)) if left > 0 else None

    def _cap_resumable(self) -> None:
        """End the oldest resume windows beyond one per queue place."""
        limit = self._settings.max_active_jobs + self._settings.max_queued_jobs
        resumable = sorted((j for j in self._jobs.values() if j.resume_until is not None),
                           key=lambda j: j.finished_at)
        for job in resumable[:max(0, len(resumable) - limit)]:
            self._end_resume_window(job)

    def _prune_finished_records(self) -> None:
        """Forget the oldest finished records beyond MAX_FINISHED_RECORDS (their files are
        already gone); a poll for one then gets job_not_found."""
        finished = sorted((j for j in self._jobs.values() if j.status in _TERMINAL
                           and j.resume_until is None and not j.delivering),
                          key=lambda j: j.finished_at)
        for job in finished[:max(0, len(finished) - MAX_FINISHED_RECORDS)]:
            del self._jobs[job.id]

    def _end_resume_window(self, job: Job) -> None:
        job.resume_until = None
        _remove_dir(job.workdir)
        logger.info("job %s: resume window over, partial files removed", job.id)

    # --- Disk budget ----------------------------------------------------------

    def _reserve_for(self, kind: str) -> int:
        """Disk a job of this kind may need. A clip is a stream copy that -fs stops at
        MAX_FILE_SIZE_MB; a download (two tracks, then their merge), an upload (the file,
        then its MP4 remux) or a batch (the ZIP, and the part being added to it) may need
        two copies."""
        return self._settings.max_file_bytes if kind == "clip" else self._reserve

    def _make_room(self, *, count_active: bool, reserve: int, job: Job | None = None) -> bool:
        """True if one more job fits the disk budget. Unsaved ready files are
        evicted (oldest first, only as many as needed) only when that is enough.

        Projected use = TEMP_DIR usage outside active workdirs + this job's
        reservation. `job`, when given, is the job about to start: what its folder
        already holds (an upload, a resumed download's partial files) counts inside
        its reservation, as max(held, reservation), not on top of it. At start
        (count_active) each active job adds max(its actual usage, its reservation),
        so a partial download isn't counted twice. At submit, active jobs are left
        out entirely: they're transient, and a queued job is checked again when it
        starts.
        """
        usage = self._disk_usage
        active = [(usage(j.workdir), self._reserve_for(j.kind))
                  for j in self._jobs.values() if j.status in _ACTIVE and j is not job]
        own = usage(job.workdir) if job is not None else 0
        outside = usage(self._temp_dir) - sum(used for used, _ in active) - own
        projected = max(0, outside) + max(own, reserve)
        if count_active:
            projected += sum(max(used, held) for used, held in active)
        budget = self._settings.max_total_disk_bytes
        if projected <= budget:
            return True
        ready = self._waiting_files()
        sizes = [usage(j.workdir) for j in ready]
        if projected - sum(sizes) > budget:
            return False
        for job, size in zip(ready, sizes):
            if projected <= budget:
                break
            logger.info("evicting ready job %s to free disk space", job.id)
            self._expire(job, "evicted")
            projected -= size
        return True

    def ensure_queue_room(self, reserved: int = 0) -> None:
        """Raise busy unless one more job fits the queue beside `reserved` places
        (requests whose links are still being checked)."""
        pending = sum(j.status in _PENDING for j in self._jobs.values())
        if pending + reserved >= self._settings.max_active_jobs + self._settings.max_queued_jobs:
            raise AppError("busy")

    def _admit(self, kind: str = "download", job: Job | None = None) -> None:
        """Raise busy when the queue is full, or disk_full when one more job can't fit."""
        self.ensure_queue_room()
        if not self._make_room(count_active=False, reserve=self._reserve_for(kind), job=job):
            raise AppError("disk_full")

    async def _acquire_slot(self, job: Job) -> None:
        """Hold a concurrency slot with room on disk, or raise disk_full at the deadline."""
        deadline = job.queued_at + self._settings.max_job_seconds
        while True:
            await self._slots.acquire()
            if self._make_room(count_active=True, reserve=self._reserve_for(job.kind), job=job):
                return
            self._slots.release()
            if self._clock() >= deadline:
                raise AppError("disk_full")
            job.detail = "Waiting for storage space"
            await asyncio.sleep(DISK_RETRY_SECONDS)

    # --- Job task -------------------------------------------------------------

    def _new_job(self, **fields) -> Job:
        self._prune_finished_records()
        job_id = secrets.token_urlsafe(16)
        while job_id in self._jobs:
            job_id = secrets.token_urlsafe(16)
        now = self._clock()
        job = Job(id=job_id, workdir=self._temp_dir / job_id, created_at=now, queued_at=now, **fields)
        self._jobs[job_id] = job
        return job

    def _start(self, job: Job) -> None:
        earlier = set(job.runs)
        task = job.task = asyncio.create_task(self._run_job(job, earlier))
        for runs in (job.runs, self._tasks):
            runs.add(task)
            task.add_done_callback(runs.discard)

    async def _run_job(self, job: Job, earlier: set[asyncio.Task]) -> None:
        if earlier:  # a cancelled run of this job may still be killing its worker
            await asyncio.wait(earlier)  # (wait, unlike gather, never cancels them)
        try:
            await self._acquire_slot(job)
        except AppError as exc:
            self._fail(job, exc)
            return
        try:
            if job.kind in _CUT_KINDS:
                await self._clip(job)
            elif job.origin == "upload":
                await self._work(job, "checking", "Checking the video",
                                 {"mode": "probe", "source": str(job.workdir / UPLOAD_NAME),
                                  "workdir": str(job.workdir)})
            else:
                await self._work(job, "downloading", "Starting download",
                                 {"mode": "download", "url": job.url, "output": job.output,
                                  "quality": job.quality, "workdir": str(job.workdir),
                                  "max_bytes": self._settings.max_file_bytes})
        finally:
            self._slots.release()

    async def _clip(self, job: Job) -> None:
        source = self._jobs.get(job.source_id or "")
        try:
            if source is None or source.status != "ready" or source.file_path is None:
                self._fail(job, AppError("source_not_found"))  # closing it cancels its clips first
                return
            request = {"source": str(source.file_path), "workdir": str(job.workdir),
                       "keep_tags": source.origin == "link", "max_bytes": self._settings.max_file_bytes}
            if job.kind == "batch":
                ext = source.file_path.suffix[1:]
                request["clips"] = [{"start": start, "end": end,
                                     "name": batch_entry_name(number, job.title, start, end, ext)}
                                    for number, (start, end) in enumerate(job.ranges, 1)]
                await self._work(job, "cutting", f"Cutting clip 1 of {len(job.ranges)}",
                                 {"mode": "batch", **request})
            else:
                await self._work(job, "cutting", "Cutting the clip",
                                 {"mode": "clip", **request, "start": job.start, "end": job.end})
        finally:
            if source is not None:
                self.touch(source)  # the idle TTL restarts once its clip ends

    async def _work(self, job: Job, status: str, detail: str, request: dict) -> None:
        """Run the worker for `job` in its folder and make it ready, or fail it."""
        settings = self._settings
        keep = False  # keep the workdir: a ready file, or partial files after a cancel
        try:
            job.status = status
            job.detail = detail
            job.started_at = self._clock()
            job.workdir.mkdir(exist_ok=True)  # a resumed job or an upload reuses its folder
            result = await self._run_worker(
                request,
                cmd=self._worker_cmd,
                timeout=settings.max_job_seconds,
                on_event=lambda event: self._on_event(job, event),
                workdir=job.workdir,
                max_workdir_bytes=self._reserve,
            )
            self._make_ready(job, result)
            keep = True
        except asyncio.CancelledError:
            # A user cancel keeps a download's partial files for a resume (the sweep removes
            # them when the window ends); shutdown removes every job folder itself. A closed
            # source can't be resumed, so its partial files go now.
            keep = job.kind == "download"
            raise
        except AppError as exc:
            self._fail(job, exc)
        except Exception:
            logger.exception("job %s failed unexpectedly", job.id)
            self._fail(job, AppError("internal"))
        finally:
            if not keep:
                _remove_dir(job.workdir)

    def _on_event(self, job: Job, event: dict) -> None:
        if job.status not in _ACTIVE:
            return
        detail = event.get("detail")
        if event.get("event") == "progress":
            job.status = "cutting" if job.kind in _CUT_KINDS else "downloading"
            if isinstance(detail, str):
                job.detail = detail
            job.downloaded_bytes = _count(event.get("downloaded_bytes"))
            job.total_bytes = _count(event.get("total_bytes"))
            job.progress_percent = _percent(event.get("percent"))
        elif (event.get("event") == "phase" and event.get("phase") == "converting"
              and job.kind not in _CUT_KINDS):
            job.status = "converting"
            job.detail = detail if isinstance(detail, str) else None
            job.progress_percent = None

    def _make_ready(self, job: Job, result: dict) -> None:
        ext, name = result.get("ext"), result.get("file")
        zip_expected = job.kind == "batch"  # only a batch returns a ZIP, and always does
        if not isinstance(ext, str) or name != f"media.{ext}" or zip_expected != (name == BATCH_NAME):
            logger.error("job %s: worker returned an unexpected file entry", job.id)
            raise AppError("internal")
        try:
            media_type = ZIP_MEDIA_TYPE if job.kind == "batch" else media_type_for(ext)
        except KeyError:
            logger.error("job %s: worker returned unsupported extension %r", job.id, ext)
            raise AppError("internal") from None
        path = job.workdir / name
        if path.is_symlink() or not path.is_file():
            logger.error("job %s: worker result file is missing", job.id)
            raise AppError("internal")
        title, notice, duration = result.get("title"), result.get("notice"), result.get("duration")
        if job.kind == "download" or job.origin == "link":
            job.title = title if isinstance(title, str) else None
        job.notice = notice if isinstance(notice, str) else None
        if job.kind == "clip":
            job.filename = clip_filename(job.title, job.start, job.end, ext)
        elif job.kind == "batch":
            job.filename = batch_filename(job.title, len(job.ranges))
        else:
            job.filename = sanitize_filename(job.title, ext)
        job.media_type = media_type
        job.file_path = path
        job.status = "ready"
        job.detail = None
        job.ready_at = self._clock()
        if job.kind == "source":
            job.progress_percent = job.downloaded_bytes = job.total_bytes = None
            job.duration = _seconds(duration)
            job.last_used = job.ready_at
        logger.info("%s %s ready", "source" if job.kind == "source" else "job", job.id)
        self._enforce_ready_cap()

    def _waiting_files(self) -> list[Job]:
        """Ready files waiting to be saved, oldest first. A file being sent is on its way
        out, and a source isn't a file to save, so neither is listed."""
        return sorted((j for j in self._jobs.values()
                       if j.status == "ready" and not j.delivering and j.kind != "source"),
                      key=lambda j: j.ready_at)

    def _enforce_ready_cap(self) -> None:
        """Evict the oldest waiting files beyond MAX_READY_FILES; files being sent are
        on their way out and don't count."""
        ready = self._waiting_files()
        for job in ready[:max(0, len(ready) - self._settings.max_ready_files)]:
            logger.info("evicting ready job %s (ready-file cap)", job.id)
            self._expire(job, "evicted")

    def _fail(self, job: Job, error: AppError) -> None:
        job.status = "failed"
        job.error = error
        job.detail = None
        job.progress_percent = None
        job.finished_at = self._clock()
        logger.info("job %s failed: %s", job.id, error.code)

    def _expire(self, job: Job, reason: str) -> None:
        if job.kind == "source" and job.status not in _TERMINAL:
            for listener in list(self._source_listeners):
                try:
                    listener(job.id)
                except Exception:
                    logger.exception("source %s: a listener failed", job.id)
            self._holds.pop(job.id, None)
        job.status = "expired"
        job.reason = reason
        job.detail = None
        job.file_path = None
        job.finished_at = self._clock()
        _remove_dir(job.workdir)
        logger.info("job %s expired (%s)", job.id, reason)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval)
            try:
                await self.sweep()
            except Exception:
                logger.exception("job sweep failed")
