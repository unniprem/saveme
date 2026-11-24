"""Shared helpers for the JobManager tests: a stub runner, a fake clock and fixtures.

StubRunner is a run_worker stand-in whose calls block until the test releases them.
"""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.jobs import JOB_ID_RE, JobManager

URL = "https://example.com/watch?v=1"


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
