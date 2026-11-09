"""JobManager: lifecycle, queue, disk budget, TTL and cleanup.

Most tests drive the manager with StubRunner (a run_worker stand-in whose calls
block until the test releases them). The real-runner tests at the end spawn the
test worker entry against the local media server.
"""
import asyncio
import dataclasses
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, load_settings
from app.errors import AppError
from app.jobs import APP_DIR, JOB_ID_RE, MARKER_NAME, JobManager, prepare_temp_dir
from app.runner import DEFAULT_WORKER_CMD

URL = "https://example.com/watch?v=1"
MiB = 1024 * 1024
TEST_WORKER_CMD = (sys.executable, "-P", "-m", "tests.support.worker_entry")
TERMINAL = {"ready", "failed", "expired"}


# --- Helpers ---------------------------------------------------------------

@dataclass
class Call:
    request: dict
    kwargs: dict
    workdir: Path
    on_event: Callable[[dict], None]
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    error: BaseException | None = None
    result: dict | None = None
    cancelled: bool = False

    def emit(self, event: dict) -> None:
        self.on_event(event)

    def finish(self, result: dict | None = None) -> None:
        self.result = result
        self.gate.set()

    def fail(self, error: BaseException) -> None:
        self.error = error
        self.gate.set()


class StubRunner:
    """Async stand-in for run_worker. Each call writes a partial file, waits on
    its gate (unless auto_finish), then raises or returns a media.mp4 result."""

    def __init__(self, *, auto_finish: bool = False):
        self.auto_finish = auto_finish
        self.calls: list[Call] = []
        self.inflight = 0
        self.max_inflight = 0

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None,
                       max_workdir_bytes=None, poll_interval=1.0):
        call = Call(request, {"cmd": cmd, "timeout": timeout,
                              "max_workdir_bytes": max_workdir_bytes}, workdir, on_event)
        self.calls.append(call)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            part = workdir / "media.mp4.part"
            part.write_bytes(b"\0" * 512)
            if not self.auto_finish:
                await call.gate.wait()
            if call.error is not None:
                raise call.error
            if call.result is not None:
                return call.result
            part.rename(workdir / "media.mp4")
            return {"file": "media.mp4", "ext": "mp4", "title": "My Video", "notice": None}
        except asyncio.CancelledError:
            call.cancelled = True
            raise
        finally:
            self.inflight -= 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def wait_until(predicate: Callable[[], object], timeout: float = 5.0,
                     on_poll: Callable[[], None] | None = None) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if on_poll is not None:
            on_poll()
        if predicate():
            return
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def job_dirs(temp_dir: Path) -> list[str]:
    return sorted(p.name for p in temp_dir.iterdir() if JOB_ID_RE.fullmatch(p.name))


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "media-jobs"


@pytest.fixture
def settings(temp_dir: Path) -> Settings:
    return load_settings({"TEMP_DIR": str(temp_dir)})


@pytest.fixture
async def make_manager(settings: Settings):
    managers: list[JobManager] = []

    async def make(custom: Settings | None = None, **kwargs) -> JobManager:
        manager = JobManager(custom or settings, **kwargs)
        await manager.start()
        managers.append(manager)
        return manager

    yield make
    for manager in managers:
        await manager.shutdown()


async def ready_job(manager: JobManager, runner: StubRunner) -> object:
    job = manager.submit(URL, "video", "best")
    if not runner.auto_finish:
        await wait_until(lambda: len(runner.calls) and runner.calls[-1].workdir == job.workdir)
        runner.calls[-1].finish()
    await wait_until(lambda: job.status == "ready")
    return job


# --- Lifecycle ---------------------------------------------------------------

async def test_lifecycle_to_ready(make_manager, settings, temp_dir):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    job = manager.submit(URL, "video", "best")
    assert job.status == "queued"
    assert JOB_ID_RE.fullmatch(job.id) and job.workdir == temp_dir / job.id

    await wait_until(lambda: runner.calls)
    call = runner.calls[0]
    assert call.request == {"mode": "download", "url": URL, "output": "video", "quality": "best",
                            "workdir": str(job.workdir), "max_bytes": settings.max_file_bytes}
    assert call.kwargs == {"cmd": DEFAULT_WORKER_CMD, "timeout": settings.max_job_seconds,
                           "max_workdir_bytes": 2 * settings.max_file_bytes}
    assert call.workdir == job.workdir and job.workdir.is_dir()

    call.emit({"event": "progress", "detail": "Downloading", "downloaded_bytes": 100,
               "total_bytes": 400, "percent": 25.0})
    assert job.status == "downloading"
    assert (job.detail, job.downloaded_bytes, job.total_bytes, job.progress_percent) == (
        "Downloading", 100, 400, 25.0)

    call.emit({"event": "phase", "phase": "converting", "detail": "Converting to MP4"})
    assert job.status == "converting"
    assert job.detail == "Converting to MP4" and job.progress_percent is None

    call.finish()
    await wait_until(lambda: job.status == "ready")
    assert job.filename == "My Video.mp4"
    assert job.media_type == "video/mp4"
    assert job.title == "My Video"
    assert job.file_path == job.workdir / "media.mp4" and job.file_path.is_file()
    assert job.ready_at is not None and job.error is None

    view = manager.public_view(job)
    assert set(view) == {"job_id", "status", "detail", "progress_percent", "downloaded_bytes",
                         "total_bytes", "notice", "error", "filename", "reason"}
    assert view["job_id"] == job.id and view["status"] == "ready"
    assert view["filename"] == "My Video.mp4" and view["error"] is None
    assert str(temp_dir) not in json.dumps(view)


@pytest.mark.parametrize("error, code", [
    (AppError("unsupported"), "unsupported"),
    (RuntimeError("boom in /secret/path"), "internal"),
])
async def test_failure_cleans_workdir(make_manager, temp_dir, error, code):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    job = manager.submit(URL, "audio", "best")
    await wait_until(lambda: runner.calls)
    assert any(job.workdir.iterdir())
    runner.calls[0].fail(error)
    await wait_until(lambda: job.status == "failed")
    assert job.error.code == code
    assert not job.workdir.exists()
    assert job.finished_at is not None
    view = manager.public_view(job)
    assert view["error"] == {"code": code, "message": job.error.message}
    assert "secret" not in json.dumps(view) and str(temp_dir) not in json.dumps(view)


@pytest.mark.parametrize("result", [
    {"file": "../escape.mp4", "ext": "mp4", "title": "x", "notice": None},
    {"file": "media.exe", "ext": "exe", "title": "x", "notice": None},
    {"file": "media.webm", "ext": "mp4", "title": "x", "notice": None},
    {"file": "media.mp4", "ext": "mp4", "title": "x", "notice": None},  # never written
])
async def test_bad_worker_result_is_internal(make_manager, result):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    job = manager.submit(URL, "video", "best")
    await wait_until(lambda: runner.calls)
    runner.calls[0].finish(result)
    await wait_until(lambda: job.status == "failed")
    assert job.error.code == "internal"
    assert not job.workdir.exists()


async def test_concurrency_and_queue_limit(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    jobs = [manager.submit(URL, "video", "best") for _ in range(2)]
    await wait_until(lambda: len(runner.calls) == 2)
    # Submissions don't count active jobs' reservations (R4): with two active
    # jobs under the defaults, more submissions still queue instead of 503ing.
    jobs += [manager.submit(URL, "video", "best") for _ in range(4)]
    await asyncio.sleep(0.1)  # a broken limit would let more jobs start meanwhile

    seen_active: list[int] = []

    def observe() -> None:
        active = sum(j.status in ("downloading", "converting") for j in jobs)
        seen_active.append(active)
        assert runner.inflight <= 2

    observe()
    assert Counter(j.status for j in jobs) == {"downloading": 2, "queued": 4}
    with pytest.raises(AppError) as ei:
        manager.submit(URL, "video", "best")
    assert ei.value.code == "busy" and ei.value.status == 429

    runner.calls[0].finish()
    await wait_until(lambda: len(runner.calls) == 3, on_poll=observe)
    await wait_until(lambda: Counter(j.status for j in jobs)
                     == {"ready": 1, "downloading": 2, "queued": 3}, on_poll=observe)

    for i in range(1, 6):
        await wait_until(lambda: len(runner.calls) > i, on_poll=observe)
        runner.calls[i].finish()
        await wait_until(lambda: jobs[i].status == "ready", on_poll=observe)
    assert all(j.status == "ready" for j in jobs)
    assert max(seen_active) <= 2 and runner.max_inflight == 2


async def test_ready_cap_evicts_oldest(make_manager, settings):
    clock = FakeClock()
    runner = StubRunner(auto_finish=True)
    manager = await make_manager(dataclasses.replace(settings, max_ready_files=2),
                                 run_worker=runner, clock=clock)
    jobs = []
    for _ in range(3):
        jobs.append(await ready_job(manager, runner))
        clock.advance(1)
    oldest, *rest = jobs
    assert oldest.status == "expired" and oldest.reason == "evicted"
    assert not oldest.workdir.exists()
    assert all(j.status == "ready" and j.file_path.is_file() for j in rest)
    assert manager.public_view(oldest)["reason"] == "evicted"


async def test_ttl_expiry(make_manager, settings):
    clock = FakeClock()
    runner = StubRunner(auto_finish=True)
    manager = await make_manager(run_worker=runner, clock=clock)
    job = await ready_job(manager, runner)
    path = job.file_path

    clock.advance(settings.job_ttl_seconds - 1)
    await manager.sweep()
    assert job.status == "ready" and path.is_file()

    clock.advance(2)
    await manager.sweep()
    assert job.status == "expired" and job.reason == "ttl"
    assert not path.exists() and not job.workdir.exists()


async def test_delivery_states(make_manager, tmp_path):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    job = manager.submit(URL, "video", "best")
    with pytest.raises(AppError) as ei:
        manager.file_for_delivery(job.id)
    assert (ei.value.code, ei.value.status) == ("not_ready", 409)

    await wait_until(lambda: runner.calls)
    with pytest.raises(AppError) as ei:
        manager.file_for_delivery(job.id)
    assert ei.value.code == "not_ready"

    runner.calls[0].finish()
    await wait_until(lambda: job.status == "ready")
    assert manager.file_for_delivery(job.id) is job

    manager.mark_delivered(job.id)
    assert job.status == "expired" and job.reason == "delivered"
    assert not job.workdir.exists()
    with pytest.raises(AppError) as ei:
        manager.file_for_delivery(job.id)
    assert (ei.value.code, ei.value.status) == ("expired", 410)

    with pytest.raises(AppError) as ei:
        manager.file_for_delivery("A" * 22)
    assert (ei.value.code, ei.value.status) == ("job_not_found", 404)

    before = sorted(str(p) for p in tmp_path.rglob("*"))
    for bad in ("../../etc", "A" * 22 + "\n", "", "../" + "A" * 19):
        with pytest.raises(AppError) as ei:
            manager.get(bad)
        assert (ei.value.code, ei.value.status) == ("job_not_found", 404)
        with pytest.raises(AppError):
            manager.file_for_delivery(bad)
        manager.mark_delivered(bad)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


# --- Disk budget -------------------------------------------------------------
# Defaults: 10240 MiB budget; each job reserves 2 x 2048 = 4096 MiB.

class FakeDisk:
    """Per-path disk_usage stub. Each workdir in `sizes` reports its size while the
    directory exists; TEMP_DIR reports those sizes plus `other` bytes."""

    def __init__(self, temp_dir: Path):
        self.temp_dir = temp_dir
        self.sizes: dict[Path, int] = {}
        self.other = 0

    def __call__(self, path: Path) -> int:
        if path == self.temp_dir:
            return self.other + sum(self(p) for p in self.sizes)
        return self.sizes.get(path, 0) if path.exists() else 0


async def test_disk_pressure_evicts_just_enough(make_manager, temp_dir):
    # Three 2500 MiB ready files + 4096 reserved = 11596 > 10240: evicting the
    # oldest one (-> 9096) is enough, so the newer two are kept.
    clock = FakeClock()
    runner = StubRunner(auto_finish=True)
    disk = FakeDisk(temp_dir)
    manager = await make_manager(run_worker=runner, disk_usage=disk, clock=clock)
    ready = []
    for _ in range(3):
        job = await ready_job(manager, runner)
        disk.sizes[job.workdir] = 2500 * MiB
        ready.append(job)
        clock.advance(1)

    newest = manager.submit(URL, "video", "best")
    oldest, *kept = ready
    assert oldest.status == "expired" and oldest.reason == "evicted"
    assert not oldest.workdir.exists()
    await wait_until(lambda: newest.status == "ready")
    assert all(j.status == "ready" and j.file_path.is_file() for j in kept)


async def test_disk_full_when_eviction_cannot_help(make_manager, temp_dir):
    runner = StubRunner(auto_finish=True)
    disk = FakeDisk(temp_dir)
    manager = await make_manager(run_worker=runner, disk_usage=disk)
    ready = [await ready_job(manager, runner) for _ in range(2)]
    for job in ready:
        disk.sizes[job.workdir] = 100 * MiB
    disk.other = 20000 * MiB  # freeing both ready files (200 MiB) can't make room

    with pytest.raises(AppError) as ei:
        manager.submit(URL, "video", "best")
    assert (ei.value.code, ei.value.status) == ("disk_full", 503)
    assert ei.value.message == "Server storage is full, try again later."
    assert all(j.status == "ready" and j.file_path.is_file() for j in ready)
    assert len(runner.calls) == 2


@pytest.mark.parametrize("partial_mib, ready_mib, evicted", [
    (3000, 1, False),  # the partial counts once (as its 4096 reservation): fits, keep the file
    (0, 3000, True),   # 3000 + 4096 + 4096 > 10240: evicting the ready file makes room
], ids=["partial-counted-once", "eviction-makes-room"])
async def test_start_evicts_only_when_eviction_makes_room(make_manager, partial_mib, ready_mib,
                                                          evicted):
    # Real dir_size; the big files are sparse, so they cost no real disk.
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    ready = await ready_job(manager, runner)
    os.truncate(ready.file_path, ready_mib * MiB)
    active = manager.submit(URL, "video", "best")
    await wait_until(lambda: len(runner.calls) == 2)
    os.truncate(active.workdir / "media.mp4.part", partial_mib * MiB)

    second = manager.submit(URL, "video", "best")
    await wait_until(lambda: len(runner.calls) == 3, timeout=5)
    assert second.status == "downloading" and active.status == "downloading"
    if evicted:
        assert ready.status == "expired" and ready.reason == "evicted"
        assert not ready.workdir.exists()
    else:
        assert ready.status == "ready" and ready.file_path.is_file()


async def test_queued_job_waits_for_disk(make_manager, temp_dir):
    runner = StubRunner()
    disk = FakeDisk(temp_dir)
    manager = await make_manager(run_worker=runner, disk_usage=disk)
    kept = await ready_job(manager, runner)
    disk.sizes[kept.workdir] = 100 * MiB
    job = manager.submit(URL, "video", "best")
    disk.other = 20000 * MiB  # over budget by the time the job tries to start

    await asyncio.sleep(0.5)
    assert job.status == "queued" and job.detail == "Waiting for storage space"
    assert len(runner.calls) == 1 and not job.workdir.exists()
    assert kept.status == "ready"  # evicting it couldn't have made room

    disk.other = 0
    await wait_until(lambda: len(runner.calls) == 2, timeout=5)
    assert job.status == "downloading" and kept.status == "ready"
    runner.calls[1].finish()
    await wait_until(lambda: job.status == "ready")


async def test_start_counts_other_active_reservations(make_manager, settings):
    # 1 MB files, 3 MB budget: one submission fits on its own, but two active
    # jobs (2 x 2 MB reserved) don't, so the second waits for the first to end.
    runner = StubRunner()
    custom = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=3)
    manager = await make_manager(custom, run_worker=runner)
    first = manager.submit(URL, "video", "best")
    second = manager.submit(URL, "video", "best")
    await wait_until(lambda: runner.calls)
    await asyncio.sleep(0.3)
    assert first.status == "downloading" and second.status == "queued"
    assert len(runner.calls) == 1

    runner.calls[0].finish()
    await wait_until(lambda: len(runner.calls) == 2, timeout=5)
    assert second.status == "downloading"
    runner.calls[1].finish()
    await wait_until(lambda: second.status == "ready")


async def test_disk_wait_gives_up_at_job_deadline(make_manager, settings, temp_dir):
    clock = FakeClock()
    runner = StubRunner()
    disk = FakeDisk(temp_dir)
    manager = await make_manager(run_worker=runner, clock=clock, disk_usage=disk)
    job = manager.submit(URL, "video", "best")
    disk.other = 20000 * MiB
    await asyncio.sleep(0.3)
    assert job.status == "queued"

    clock.advance(settings.max_job_seconds + 1)
    await wait_until(lambda: job.status == "failed", timeout=5)
    assert job.error.code == "disk_full"
    assert runner.calls == [] and not job.workdir.exists()


# --- Temp dir, startup, shutdown, sweeper ----------------------------------

async def test_startup_wipe_and_prepare(make_manager, temp_dir, tmp_path):
    temp_dir.mkdir()
    (temp_dir / MARKER_NAME).touch()
    stale = ["A" * 22, "b_-" + "c" * 19]
    for name in stale:
        (temp_dir / name).mkdir()
        (temp_dir / name / "media.mp4").write_bytes(b"x")
    (temp_dir / "keep.txt").write_text("keep")
    (temp_dir / "not-a-job").mkdir()

    await make_manager(run_worker=StubRunner())
    assert job_dirs(temp_dir) == []
    assert (temp_dir / "keep.txt").read_text() == "keep"
    assert (temp_dir / "not-a-job").is_dir() and (temp_dir / MARKER_NAME).is_file()

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (tmp_path / "root-link").symlink_to("/")
    (tmp_path / "app-link").symlink_to(app_dir)
    unmarked = tmp_path / "unmarked"
    unmarked.mkdir()
    (unmarked / "notes.txt").write_text("mine")
    for bad in (Path("/"), tmp_path / "root-link", app_dir, app_dir / "jobs",
                tmp_path / "app-link" / "jobs", unmarked):
        with pytest.raises(ConfigError) as ei:
            prepare_temp_dir(bad, app_dir=app_dir)
        assert ei.value.var == "TEMP_DIR"
    assert not (app_dir / "jobs").exists() and not (unmarked / MARKER_NAME).exists()

    assert APP_DIR == Path(__file__).resolve().parents[2]
    with pytest.raises(ConfigError):
        prepare_temp_dir(APP_DIR / "media-jobs", app_dir=APP_DIR)

    fresh = tmp_path / "fresh" / "jobs"
    prepare_temp_dir(fresh, app_dir=app_dir)
    assert (fresh / MARKER_NAME).is_file()
    empty = tmp_path / "empty"
    empty.mkdir()
    prepare_temp_dir(empty, app_dir=app_dir)
    assert (empty / MARKER_NAME).is_file()
    (empty / "x").mkdir()
    prepare_temp_dir(empty, app_dir=app_dir)  # marked: non-empty is fine


async def test_shutdown_kills_and_cleans(make_manager, temp_dir):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    done = await ready_job(manager, runner)
    running = [manager.submit(URL, "video", "best") for _ in range(3)]
    await wait_until(lambda: len(runner.calls) == 3)
    assert [j.status for j in running] == ["downloading", "downloading", "queued"]

    await manager.shutdown()
    assert all(call.cancelled for call in runner.calls[1:])
    for job in (done, *running):
        assert not job.workdir.exists()
    assert job_dirs(temp_dir) == []
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []
    await manager.shutdown()  # safe to call twice


async def test_shutdown_kills_real_worker_group(make_manager, tmp_path):
    pidfile = tmp_path / "pids"
    script = (
        "import os, subprocess, time\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pidfile)!r}, 'w').write('%d %d' % (os.getpid(), child.pid))\n"
        "time.sleep(60)\n"
    )
    manager = await make_manager(worker_cmd=(sys.executable, "-c", script))
    job = manager.submit(URL, "video", "best")
    await wait_until(lambda: pidfile.exists() and pidfile.read_text().count(" ") == 1,
                     timeout=10)
    pids = [int(p) for p in pidfile.read_text().split()]

    await manager.shutdown()
    assert not job.workdir.exists()
    for pid in pids:
        await wait_until(lambda: not alive(pid), timeout=8)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_record_purge(make_manager):
    clock = FakeClock()
    runner = StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock)
    job = manager.submit(URL, "video", "best")
    await wait_until(lambda: runner.calls)
    runner.calls[0].fail(AppError("unavailable"))
    await wait_until(lambda: job.status == "failed")

    clock.advance(3599)
    await manager.sweep()
    assert manager.get(job.id) is job

    clock.advance(2)
    await manager.sweep()
    with pytest.raises(AppError) as ei:
        manager.get(job.id)
    assert (ei.value.code, ei.value.status) == ("job_not_found", 404)


async def test_sweep_removes_orphans(make_manager, temp_dir):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    done = await ready_job(manager, runner)
    running = manager.submit(URL, "video", "best")
    await wait_until(lambda: len(runner.calls) == 2)

    orphan = temp_dir / ("Z" * 22)
    orphan.mkdir()
    (orphan / "media.mp4").write_bytes(b"x")
    (temp_dir / "not-a-job").mkdir()

    await manager.sweep()
    assert not orphan.exists()
    assert done.status == "ready" and done.file_path.is_file()
    assert running.status == "downloading" and running.workdir.is_dir()
    assert (temp_dir / "not-a-job").is_dir() and (temp_dir / MARKER_NAME).is_file()


# --- Real worker -------------------------------------------------------------

async def wait_terminal(job, timeout: float = 60) -> None:
    await wait_until(lambda: job.status in TERMINAL, timeout=timeout)


async def test_oversize_job_fails_cleanly(make_manager, settings, media_server):
    manager = await make_manager(dataclasses.replace(settings, max_file_size_mb=1),
                                 worker_cmd=TEST_WORKER_CMD)
    job = manager.submit(media_server.url("/chunked/big.mp4"), "video", "best")
    await wait_terminal(job)
    assert job.status == "failed", manager.public_view(job)
    assert job.error.code == "too_large"
    assert "1 MB" in job.error.message
    assert not job.workdir.exists()


async def test_timeout_job_fails_cleanly(make_manager, settings, media_server):
    manager = await make_manager(dataclasses.replace(settings, max_job_seconds=3),
                                 worker_cmd=TEST_WORKER_CMD)
    job = manager.submit(media_server.url("/slow/sample.mp4"), "video", "best")
    await wait_terminal(job, timeout=30)
    assert job.status == "failed", manager.public_view(job)
    assert job.error.code == "timeout"
    assert not job.workdir.exists()


async def test_real_video_job_ready(make_manager, media_server):
    manager = await make_manager(worker_cmd=TEST_WORKER_CMD)
    job = manager.submit(media_server.url("/media/sample.mp4"), "video", "best")
    await wait_terminal(job)
    assert job.status == "ready", manager.public_view(job)
    assert job.file_path is not None and job.file_path.is_file()
    assert job.file_path.parent == job.workdir
    assert job.media_type == "video/mp4" and job.filename.endswith(".mp4")
