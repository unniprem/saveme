"""In-memory job manager: lifecycle, bounded queue, disk budget, TTL and cleanup.

Job records live only in this process. Each job's files live in
TEMP_DIR/<job_id>/ under fixed names; the job ID is the only name derived from
anything, and it is validated before any filesystem use.
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import ConfigError, Settings
from app.errors import AppError
from app.filenames import media_type_for, sanitize_filename
from app.runner import DEFAULT_WORKER_CMD, RunWorker, dir_size, run_worker

logger = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
MARKER_NAME = ".saveme-tempdir"
APP_DIR = Path(__file__).resolve().parent.parent

RECORD_RETENTION_SECONDS = 3600
DISK_RETRY_SECONDS = 2.0

_ACTIVE = frozenset({"downloading", "converting"})
_PENDING = frozenset({"queued", "downloading", "converting"})
_TERMINAL = frozenset({"failed", "expired"})


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


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(100.0, max(0.0, float(value)))


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

    # --- Lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        prepare_temp_dir(self._temp_dir, app_dir=self._app_dir)
        for path in _job_dirs(self._temp_dir):
            _remove_dir(path)
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def shutdown(self) -> None:
        tasks = [t for t in (self._sweeper, *self._tasks) if t is not None and not t.done()]
        for task in tasks:  # once only: run_worker's cleanup kills the process group
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for path in _job_dirs(self._temp_dir):
            _remove_dir(path)

    # --- Public API -----------------------------------------------------------

    def submit(self, url: str, output: str, quality: str) -> Job:
        pending = sum(j.status in _PENDING for j in self._jobs.values())
        if pending >= self._settings.max_active_jobs + self._settings.max_queued_jobs:
            raise AppError("busy")
        if not self._make_room(lambda: self._fits(other_active=0)):
            raise AppError("disk_full")
        job_id = secrets.token_urlsafe(16)
        while job_id in self._jobs:
            job_id = secrets.token_urlsafe(16)
        job = Job(id=job_id, url=url, output=output, quality=quality,
                  workdir=self._temp_dir / job_id, created_at=self._clock())
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run_job(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.info("job %s queued (%s, %s)", job_id, output, quality)
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id) if _is_job_id(job_id) else None
        if job is None:
            raise AppError("job_not_found")
        return job

    def public_view(self, job: Job) -> dict:
        return {
            "job_id": job.id,
            "status": job.status,
            "detail": job.detail,
            "progress_percent": job.progress_percent,
            "downloaded_bytes": job.downloaded_bytes,
            "total_bytes": job.total_bytes,
            "notice": job.notice,
            "error": job.error.to_dict() if job.error is not None else None,
            "filename": job.filename,
            "reason": job.reason,
        }

    def file_for_delivery(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status == "expired":
            raise AppError("expired")
        if job.status != "ready":
            raise AppError("not_ready")
        return job

    def mark_delivered(self, job_id: str) -> None:
        job = self._jobs.get(job_id) if _is_job_id(job_id) else None
        if job is not None and job.status == "ready":
            self._expire(job, "delivered")

    async def sweep(self) -> None:
        now = self._clock()
        ttl = self._settings.job_ttl_seconds
        for job in list(self._jobs.values()):
            if job.status == "ready" and now - job.ready_at >= ttl:
                self._expire(job, "ttl")
            elif (job.status in _TERMINAL
                  and now - job.finished_at >= RECORD_RETENTION_SECONDS):
                del self._jobs[job.id]
        live = {j.id for j in self._jobs.values() if j.status not in _TERMINAL}
        for path in _job_dirs(self._temp_dir):
            if path.name not in live:
                logger.info("removing orphan job directory %s", path.name)
                _remove_dir(path)

    # --- Disk budget ----------------------------------------------------------

    def _fits(self, *, other_active: int) -> bool:
        usage = self._disk_usage(self._temp_dir)
        needed = (other_active + 1) * self._reserve
        return usage + needed <= self._settings.max_total_disk_bytes

    def _make_room(self, fits: Callable[[], bool]) -> bool:
        """Evict unsaved ready files, oldest first, until fits() holds."""
        while not fits():
            ready = [j for j in self._jobs.values() if j.status == "ready"]
            if not ready:
                return False
            oldest = min(ready, key=lambda j: j.ready_at)
            logger.info("evicting ready job %s to free disk space", oldest.id)
            self._expire(oldest, "evicted")
        return True

    async def _acquire_slot(self, job: Job) -> None:
        """Hold a concurrency slot with room on disk, or raise disk_full at the deadline."""
        deadline = job.created_at + self._settings.max_job_seconds
        while True:
            await self._slots.acquire()
            others = sum(j.status in _ACTIVE for j in self._jobs.values() if j is not job)
            if self._make_room(lambda: self._fits(other_active=others)):
                return
            self._slots.release()
            if self._clock() >= deadline:
                raise AppError("disk_full")
            job.detail = "Waiting for storage space"
            await asyncio.sleep(DISK_RETRY_SECONDS)

    # --- Job task -------------------------------------------------------------

    async def _run_job(self, job: Job) -> None:
        try:
            await self._acquire_slot(job)
        except AppError as exc:
            self._fail(job, exc)
            return
        try:
            await self._download(job)
        finally:
            self._slots.release()

    async def _download(self, job: Job) -> None:
        settings = self._settings
        try:
            job.status = "downloading"
            job.detail = "Starting download"
            job.started_at = self._clock()
            job.workdir.mkdir()
            result = await self._run_worker(
                {"mode": "download", "url": job.url, "output": job.output,
                 "quality": job.quality, "workdir": str(job.workdir),
                 "max_bytes": settings.max_file_bytes},
                cmd=self._worker_cmd,
                timeout=settings.max_job_seconds,
                on_event=lambda event: self._on_event(job, event),
                workdir=job.workdir,
                max_workdir_bytes=self._reserve,
            )
            self._make_ready(job, result)
        except AppError as exc:
            self._fail(job, exc)
        except Exception:
            logger.exception("job %s failed unexpectedly", job.id)
            self._fail(job, AppError("internal"))
        finally:
            if job.status != "ready":
                _remove_dir(job.workdir)

    def _on_event(self, job: Job, event: dict) -> None:
        if job.status not in _ACTIVE:
            return
        detail = event.get("detail")
        if event.get("event") == "progress":
            job.status = "downloading"
            if isinstance(detail, str):
                job.detail = detail
            job.downloaded_bytes = _count(event.get("downloaded_bytes"))
            job.total_bytes = _count(event.get("total_bytes"))
            job.progress_percent = _percent(event.get("percent"))
        elif event.get("event") == "phase" and event.get("phase") == "converting":
            job.status = "converting"
            job.detail = detail if isinstance(detail, str) else None
            job.progress_percent = None

    def _make_ready(self, job: Job, result: dict) -> None:
        ext, name = result.get("ext"), result.get("file")
        if not isinstance(ext, str) or name != f"media.{ext}":
            logger.error("job %s: worker returned an unexpected file entry", job.id)
            raise AppError("internal")
        try:
            media_type = media_type_for(ext)
        except KeyError:
            logger.error("job %s: worker returned unsupported extension %r", job.id, ext)
            raise AppError("internal") from None
        path = job.workdir / name
        if path.is_symlink() or not path.is_file():
            logger.error("job %s: worker result file is missing", job.id)
            raise AppError("internal")
        title, notice = result.get("title"), result.get("notice")
        job.title = title if isinstance(title, str) else None
        job.notice = notice if isinstance(notice, str) else None
        job.filename = sanitize_filename(job.title, ext)
        job.media_type = media_type
        job.file_path = path
        job.status = "ready"
        job.detail = None
        job.ready_at = self._clock()
        logger.info("job %s ready", job.id)
        self._enforce_ready_cap()

    def _enforce_ready_cap(self) -> None:
        ready = sorted((j for j in self._jobs.values() if j.status == "ready"),
                       key=lambda j: j.ready_at)
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
