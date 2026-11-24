"""JobManager: Clip Studio sources (spec §6.1, §6.2), driven by StubRunner."""
import dataclasses

import pytest

from app.config import ConfigError, load_settings
from app.errors import AppError
from tests.support.jobs import (URL, FakeClock, StubRunner, job_dirs, make_manager,  # noqa: F401
                                ready_job, settings, temp_dir, wait_until)

DURATION = 12.5


def finish_source(call, *, duration: float | None = DURATION, title: str = "My Video") -> None:
    (call.workdir / "media.mp4").write_bytes(b"\0" * 1024)
    call.finish({"file": "media.mp4", "ext": "mp4", "title": title, "duration": duration,
                 "notice": None})


async def ready_source(manager, runner: StubRunner, url: str = URL):
    source = manager.open_link_source(url, "720")
    await wait_until(lambda: any(c.workdir == source.workdir for c in runner.calls))
    finish_source(next(c for c in runner.calls if c.workdir == source.workdir))
    await wait_until(lambda: source.status == "ready")
    return source


def code_of(excinfo) -> str:
    return excinfo.value.code


async def test_link_source_downloads_and_stays_ready(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.open_link_source(URL, "720")
    await wait_until(lambda: runner.calls)
    assert runner.calls[0].request == {"mode": "download", "url": URL, "output": "video",
                                       "quality": "720", "workdir": str(source.workdir),
                                       "max_bytes": settings.max_file_bytes}
    finish_source(runner.calls[0])
    await wait_until(lambda: source.status == "ready")
    view = manager.source_view(source)
    assert view == {"source_id": source.id, "status": "ready", "origin": "link",
                    "title": "My Video", "duration_seconds": DURATION, "detail": None,
                    "progress_percent": None, "downloaded_bytes": None, "total_bytes": None,
                    "notice": None, "error": None, "reason": None}
    assert manager.source_media(source.id) == (source.workdir / "media.mp4", "video/mp4")
    with pytest.raises(AppError) as ei:
        manager.get(source.id)
    assert code_of(ei) == "job_not_found"


async def test_source_progress_shows_in_its_view(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.open_link_source(URL, "best")
    await wait_until(lambda: runner.calls)
    runner.calls[0].emit({"event": "progress", "detail": "Downloading", "downloaded_bytes": 10,
                          "total_bytes": 40, "percent": 25.0})
    view = manager.source_view(source)
    assert (view["status"], view["progress_percent"], view["downloaded_bytes"]) == ("downloading", 25.0, 10)
    runner.calls[0].fail(AppError("unavailable"))
    await wait_until(lambda: source.status == "failed")
    assert manager.source_view(source)["error"]["code"] == "unavailable"
    assert not source.workdir.exists()


async def test_job_ids_are_not_source_ids(make_manager):
    runner = StubRunner(auto_finish=True)
    manager = await make_manager(run_worker=runner)
    job = await ready_job(manager, runner)
    with pytest.raises(AppError) as ei:
        manager.get_source(job.id)
    assert code_of(ei) == "source_not_found"
    with pytest.raises(AppError) as ei:
        manager.get_source("not-an-id")
    assert code_of(ei) == "source_not_found"
    assert manager.public_view(job)["kind"] == "download"


async def test_source_is_not_delivered_or_counted_as_ready_file(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_ready_files=1), run_worker=runner)
    source = await ready_source(manager, runner)
    runner.auto_finish = True
    first = await ready_job(manager, runner)
    second = await ready_job(manager, runner)
    assert (source.status, first.status, second.status) == ("ready", "expired", "ready")
    with pytest.raises(AppError) as ei:
        manager.claim_delivery(source.id)
    assert code_of(ei) == "job_not_found"


async def test_source_is_not_evicted_for_disk_room(make_manager, settings):
    sizes = {}
    runner = StubRunner()
    tight = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=3)
    manager = await make_manager(tight, run_worker=runner,
                                 disk_usage=lambda path: sizes.get(path.name, 0))
    source = await ready_source(manager, runner)
    sizes[source.workdir.parent.name] = 2 * 1024 * 1024  # TEMP_DIR holds a 2 MiB source
    with pytest.raises(AppError) as ei:
        manager.submit(URL, "video", "best")  # needs 2 MiB more; only 1 MiB is free
    assert code_of(ei) == "disk_full"
    assert source.status == "ready"


async def test_idle_source_expires_after_ttl(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(manager, runner)
    clock.advance(settings.job_ttl_seconds - 1)
    manager.source_media(source.id)  # a preview request is a use
    clock.advance(settings.job_ttl_seconds - 1)
    await manager.sweep()
    assert source.status == "ready"
    clock.advance(2)
    await manager.sweep()
    view = manager.source_view(source)
    assert (view["status"], view["reason"]) == ("expired", "idle")
    assert not source.workdir.exists()
    with pytest.raises(AppError) as ei:
        manager.source_media(source.id)
    assert code_of(ei) == "source_not_found"


async def test_touch_keeps_a_source_alive(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(manager, runner)
    for _ in range(3):
        clock.advance(settings.job_ttl_seconds - 1)
        manager.touch(source)
        await manager.sweep()
    assert source.status == "ready"


@pytest.mark.parametrize("state", ["queued", "downloading", "ready"])
async def test_close_source_in_each_state(make_manager, settings, temp_dir, state):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_active_jobs=1), run_worker=runner)
    blocker = manager.submit(URL, "video", "best") if state == "queued" else None
    if state == "ready":
        source = await ready_source(manager, runner)
    else:
        source = manager.open_link_source(URL, "best")
        if state == "downloading":
            await wait_until(lambda: runner.calls)
            runner.calls[0].emit({"event": "progress", "detail": "Downloading", "percent": 5.0})
    assert source.status == state
    closed = manager.close_source(source.id)
    assert closed is source
    view = manager.source_view(source)
    assert (view["status"], view["reason"]) == ("expired", "closed")
    await wait_until(lambda: not source.workdir.exists() and not source.runs)
    assert manager.close_source(source.id).status == "expired"  # a repeat is harmless
    if blocker is not None:
        runner.calls[0].finish()
        await wait_until(lambda: blocker.status == "ready")
    await manager.sweep()
    assert source.id not in job_dirs(temp_dir)


async def test_source_cap_replaces_least_recently_used(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=2),
                                 run_worker=runner, clock=clock)
    older = await ready_source(manager, runner)
    clock.advance(5)
    newer = await ready_source(manager, runner)
    clock.advance(5)
    manager.touch(older)  # now `newer` is the least recently used
    third = manager.open_link_source(URL, "best")
    assert (older.status, newer.status) == ("ready", "expired")
    assert manager.source_view(newer)["reason"] == "replaced"
    assert third.status == "queued"


async def test_source_cap_is_busy_when_nothing_can_be_replaced(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=2), run_worker=runner)
    manager.open_link_source(URL, "best")
    manager.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        manager.open_link_source(URL, "best")
    assert code_of(ei) == "busy"


async def test_source_media_requires_ready(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        manager.source_media(source.id)
    assert code_of(ei) == "source_not_ready"


async def test_sources_take_queue_places(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_active_jobs=1, max_queued_jobs=0),
                                 run_worker=runner)
    manager.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        manager.submit(URL, "video", "best")
    assert code_of(ei) == "busy"


def test_config_max_clip_sources():
    assert load_settings({}).max_clip_sources == 2
    assert load_settings({"MAX_CLIP_SOURCES": "20"}).max_clip_sources == 20
    for bad in ("0", "21", "two"):
        with pytest.raises(ConfigError) as ei:
            load_settings({"MAX_CLIP_SOURCES": bad})
        assert ei.value.var == "MAX_CLIP_SOURCES"
