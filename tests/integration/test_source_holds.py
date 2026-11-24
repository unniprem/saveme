"""JobManager hooks for AI Highlights: an analysis holds its source (no idle expiry, no
replacement), hears when the source ends, and reserves disk through the shared budget."""
import dataclasses

import pytest

from app.errors import AppError
from tests.integration.test_sources import ready_source
from tests.support.jobs import (URL, FakeClock, StubRunner, make_manager, ready_job,  # noqa: F401
                                settings, temp_dir, wait_until)


async def test_a_held_source_does_not_idle_out(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(manager, runner)
    assert manager.hold_source(source.id) is source
    clock.advance(settings.job_ttl_seconds + 5)
    await manager.sweep()
    assert source.status == "ready"
    manager.release_source(source)
    assert source.last_used == clock()  # the analysis ending is a use
    clock.advance(settings.job_ttl_seconds + 1)
    await manager.sweep()
    assert (source.status, source.reason) == ("expired", "idle")


async def test_holds_are_counted(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(manager, runner)
    manager.hold_source(source.id)
    manager.hold_source(source.id)
    manager.release_source(source)
    clock.advance(settings.job_ttl_seconds + 1)
    await manager.sweep()
    assert source.status == "ready"
    manager.release_source(source)
    manager.release_source(source)  # an extra release is harmless
    clock.advance(settings.job_ttl_seconds + 1)
    await manager.sweep()
    assert source.status == "expired"


async def test_a_held_source_is_not_replaced(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=1), run_worker=runner)
    source = await ready_source(manager, runner)
    manager.hold_source(source.id)
    with pytest.raises(AppError) as ei:
        manager.open_link_source(URL, "best")
    assert ei.value.code == "busy"
    manager.release_source(source)
    manager.open_link_source(URL, "best")
    assert (source.status, source.reason) == ("expired", "replaced")


async def test_hold_needs_a_ready_source(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    loading = manager.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        manager.hold_source(loading.id)
    assert ei.value.code == "source_not_ready"
    with pytest.raises(AppError) as ei:
        manager.hold_source("x" * 22)
    assert ei.value.code == "source_not_found"
    with pytest.raises(AppError) as ei:
        manager.ready_source(loading.id)
    assert ei.value.code == "source_not_ready"


@pytest.mark.parametrize("ending", ["closed", "idle", "replaced"])
async def test_listeners_hear_a_source_end_before_its_folder_goes(make_manager, settings, ending):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=1), run_worker=runner,
                                 clock=clock, sweep_interval=3600)
    heard = []
    manager.add_source_listener(lambda source_id: heard.append((source_id, manager.get_source(source_id)
                                                                .workdir.exists())))
    source = await ready_source(manager, runner)
    if ending == "closed":
        manager.close_source(source.id)
    elif ending == "idle":
        clock.advance(settings.job_ttl_seconds + 1)
        await manager.sweep()
    else:
        manager.open_link_source(URL, "best")
    assert heard == [(source.id, True)]
    assert not source.workdir.exists()


async def test_a_failing_listener_does_not_stop_the_close(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)

    def broken(_source_id):
        raise RuntimeError("boom")

    manager.add_source_listener(broken)
    source = await ready_source(manager, runner)
    manager.close_source(source.id)
    assert source.status == "expired" and not source.workdir.exists()


async def test_listeners_ignore_jobs_that_are_not_sources(make_manager):
    runner = StubRunner(auto_finish=True)
    manager = await make_manager(run_worker=runner)
    heard = []
    manager.add_source_listener(heard.append)
    job = await ready_job(manager, runner)
    manager.cancel(job.id)
    assert heard == []


async def test_a_disk_reservation_holds_until_released(make_manager, settings, temp_dir):
    tight = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=4)
    MiB = 1024 * 1024
    runner = StubRunner()
    manager = await make_manager(tight, run_worker=runner, disk_usage=lambda path: 0)
    assert manager.reserve_disk("analysis-1", temp_dir / "x" / "ai" / "a", 3 * MiB)
    assert not manager.reserve_disk("analysis-2", temp_dir / "y" / "ai" / "b", 2 * MiB)  # 3 + 2 > 4
    # A download needs 2 MiB while it runs: with 3 MiB held it waits for room.
    job = manager.submit(URL, "video", "best")
    await wait_until(lambda: job.detail == "Waiting for storage space")
    assert runner.calls == []
    manager.release_disk("analysis-1")
    manager.release_disk("analysis-1")  # a repeat is harmless
    await wait_until(lambda: runner.calls)
    runner.calls[0].finish()
    await wait_until(lambda: job.status == "ready")


async def test_a_reservation_counts_its_own_files_once(make_manager, settings, temp_dir):
    tight = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=4)
    MiB = 1024 * 1024
    sizes: dict[str, int] = {}
    manager = await make_manager(tight, run_worker=StubRunner(auto_finish=True),
                                 disk_usage=lambda path: sizes.get(path.name, 0))
    assert manager.reserve_disk("analysis-1", temp_dir / "src" / "ai" / "a", 2 * MiB)
    # Its WAV grows to 1.5 MiB inside TEMP_DIR: counted as the 2 MiB held, not 2 + 1.5.
    sizes[temp_dir.name] = sizes["a"] = int(1.5 * MiB)
    assert manager.reserve_disk("analysis-2", temp_dir / "src2" / "ai" / "b", 2 * MiB)


async def test_a_reservation_may_evict_unsaved_files(make_manager, settings, temp_dir):
    tight = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=4)
    sizes: dict[str, int] = {}
    runner = StubRunner(auto_finish=True)
    manager = await make_manager(tight, run_worker=runner, disk_usage=lambda path: sizes.get(path.name, 0))
    job = await ready_job(manager, runner)
    sizes[temp_dir.name] = sizes[job.workdir.name] = 2 * 1024 * 1024
    assert manager.reserve_disk("analysis-1", temp_dir / "s" / "ai" / "a", 3 * 1024 * 1024)
    assert (job.status, job.reason) == ("expired", "evicted")
