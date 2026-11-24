"""JobManager: Clip Studio sources (spec §6.1, §6.2), driven by StubRunner."""
import dataclasses
import shutil

import pytest

from app.config import ConfigError, load_settings
from app.errors import AppError
from app.jobs import UPLOAD_NAME
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


# --- Clips --------------------------------------------------------------------

def clip_call(runner: StubRunner, clip):
    return next(c for c in runner.calls if c.workdir == clip.workdir)


async def started(runner: StubRunner, job):
    await wait_until(lambda: any(c.workdir == job.workdir for c in runner.calls))
    return clip_call(runner, job)


async def test_clip_runs_and_becomes_a_normal_ready_job(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_source(manager, runner)
    clip = manager.submit_clip(source.id, 1.23456, 4.5)
    assert manager.get(clip.id) is clip and clip.status == "queued"
    call = await started(runner, clip)
    assert call.request == {"mode": "clip", "source": str(source.workdir / "media.mp4"),
                            "workdir": str(clip.workdir), "start": 1.235, "end": 4.5,
                            "keep_tags": True, "max_bytes": settings.max_file_bytes}
    assert call.kwargs["max_workdir_bytes"] == 2 * settings.max_file_bytes
    call.emit({"event": "progress", "detail": "Cutting the clip", "percent": 50.0,
               "downloaded_bytes": 100, "total_bytes": None})
    view = manager.public_view(clip)
    assert (view["kind"], view["status"], view["progress_percent"], view["detail"]) == (
        "clip", "cutting", 50.0, "Cutting the clip")
    call.finish()
    await wait_until(lambda: clip.status == "ready")
    assert clip.filename == "My Video (clip 0m01.2s-0m04.5s).mp4"
    assert clip.media_type == "video/mp4"
    claimed = manager.claim_delivery(clip.id)
    manager.finish_delivery(claimed, complete=True)
    assert (clip.status, clip.reason) == ("expired", "delivered")
    assert not clip.workdir.exists() and source.status == "ready"


@pytest.mark.parametrize("start, end, message", [
    (1.0, 1.05, "A clip must be at least 0.1 seconds long."),
    (5.0, 4.0, "The end must be after the start."),
    (5.0, 5.0, "The end must be after the start."),
    (-1.0, 2.0, "The start can't be before 0:00."),
    (12.5, 13.0, "The start must be before the end of the video."),
    (3.0, 12.6, "The end can't be after the end of the video."),
])
async def test_clip_validation(make_manager, start, end, message):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_source(manager, runner)
    with pytest.raises(AppError) as ei:
        manager.submit_clip(source.id, start, end)
    assert (code_of(ei), ei.value.message, ei.value.status) == ("invalid_clip", message, 400)


async def test_clip_end_just_past_the_duration_is_clamped(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_source(manager, runner)
    clip = manager.submit_clip(source.id, 10.0, DURATION + 0.04)
    assert (clip.start, clip.end) == (10.0, DURATION)


async def test_clip_needs_a_ready_source(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    loading = manager.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        manager.submit_clip(loading.id, 0, 1)
    assert code_of(ei) == "source_not_ready"
    with pytest.raises(AppError) as ei:
        manager.submit_clip("A" * 22, 0, 1)
    assert code_of(ei) == "source_not_found"
    manager.close_source(loading.id)
    with pytest.raises(AppError) as ei:
        manager.submit_clip(loading.id, 0, 1)
    assert code_of(ei) == "source_not_found"


async def test_clip_cancel_has_no_resume(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_source(manager, runner)
    clip = manager.submit_clip(source.id, 0, 5)
    call = await started(runner, clip)
    manager.cancel(clip.id)
    view = manager.public_view(clip)
    assert (view["status"], view["resume_seconds"]) == ("cancelled", None)
    await wait_until(lambda: not clip.runs)
    assert call.cancelled and not clip.workdir.exists()
    with pytest.raises(AppError) as ei:
        manager.resume(clip.id)
    assert code_of(ei) == "not_resumable"


async def test_closing_source_cancels_its_clips(make_manager, settings, temp_dir):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_active_jobs=1), run_worker=runner)
    source = await ready_source(manager, runner)
    running = manager.submit_clip(source.id, 0, 5)
    call = await started(runner, running)
    queued = manager.submit_clip(source.id, 5, 10)
    assert queued.status == "queued"
    manager.close_source(source.id)
    assert (running.status, queued.status) == ("cancelled", "cancelled")
    await wait_until(lambda: not running.runs and not queued.runs)
    assert call.cancelled
    assert len(runner.calls) == 2  # the queued clip never started
    await manager.sweep()
    assert job_dirs(temp_dir) == []


async def test_source_in_use_does_not_expire(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    manager = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(manager, runner)
    clip = manager.submit_clip(source.id, 0, 5)
    call = await started(runner, clip)
    clock.advance(settings.job_ttl_seconds + 1)
    await manager.sweep()
    assert source.status == "ready"
    call.finish()
    await wait_until(lambda: clip.status == "ready")
    assert source.last_used == clock()  # a clip ending is a use
    clock.advance(settings.job_ttl_seconds - 1)
    await manager.sweep()
    assert source.status == "ready"
    clock.advance(2)
    await manager.sweep()
    assert (source.status, source.reason) == ("expired", "idle")
    assert (clip.status, clip.reason) == ("expired", "ttl")  # its own TTL, not the source's


async def test_upload_clips_drop_tags(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_upload(manager, runner, "Holiday")
    clip = manager.submit_clip(source.id, 0, 1)
    call = await started(runner, clip)
    assert call.request["keep_tags"] is False
    call.finish()
    await wait_until(lambda: clip.status == "ready")
    assert clip.filename == "Holiday (clip 0m00s-0m01s).mp4"


# --- Uploads ------------------------------------------------------------------

async def ready_upload(manager, runner: StubRunner, title: str | None):
    source = manager.begin_upload(title)
    (source.workdir / UPLOAD_NAME).write_bytes(b"\0" * 2048)
    manager.finish_upload(source)
    call = await started(runner, source)
    (source.workdir / UPLOAD_NAME).rename(source.workdir / "media.mp4")
    call.finish({"file": "media.mp4", "ext": "mp4", "duration": 3.0, "notice": None})
    await wait_until(lambda: source.status == "ready")
    return source


async def test_upload_lifecycle(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.begin_upload("Holiday")
    assert source.status == "uploading" and source.workdir.is_dir()
    (source.workdir / UPLOAD_NAME).write_bytes(b"\0" * 2048)
    manager.finish_upload(source)
    call = await started(runner, source)
    assert call.request == {"mode": "probe", "source": str(source.workdir / UPLOAD_NAME),
                            "workdir": str(source.workdir)}
    assert call.kwargs["max_workdir_bytes"] == 2 * settings.max_file_bytes
    assert source.status == "checking"
    (source.workdir / UPLOAD_NAME).rename(source.workdir / "media.mp4")
    call.finish({"file": "media.mp4", "ext": "mp4", "duration": 3.0, "notice": None})
    await wait_until(lambda: source.status == "ready")
    view = manager.source_view(source)
    assert (view["origin"], view["title"], view["duration_seconds"]) == ("upload", "Holiday", 3.0)


async def test_upload_without_a_name_is_called_uploaded_video(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = await ready_upload(manager, runner, None)
    assert manager.source_view(source)["title"] == "Uploaded video"


async def test_upload_that_isnt_a_video_fails_and_cleans_up(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.begin_upload("notes")
    (source.workdir / UPLOAD_NAME).write_bytes(b"text")
    manager.finish_upload(source)
    call = await started(runner, source)
    call.fail(AppError("unreadable_media"))
    await wait_until(lambda: source.status == "failed")
    assert manager.source_view(source)["error"]["code"] == "unreadable_media"
    assert not source.workdir.exists()


async def test_abort_upload_frees_place(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_active_jobs=1, max_queued_jobs=0),
                                 run_worker=runner)
    source = manager.begin_upload("x")
    with pytest.raises(AppError) as ei:
        manager.submit(URL, "video", "best")
    assert code_of(ei) == "busy"
    manager.abort_upload(source)
    assert not source.workdir.exists()
    with pytest.raises(AppError) as ei:
        manager.get_source(source.id)
    assert code_of(ei) == "source_not_found"
    manager.submit(URL, "video", "best")


async def test_finish_upload_after_close_is_refused(make_manager):
    runner = StubRunner()
    manager = await make_manager(run_worker=runner)
    source = manager.begin_upload("x")
    manager.close_source(source.id)
    with pytest.raises(AppError) as ei:
        manager.finish_upload(source)
    assert code_of(ei) == "source_not_found"
    manager.abort_upload(source)
    assert not source.workdir.exists() and runner.calls == []


async def test_upload_admission_counts_active_jobs(make_manager, settings):
    runner = StubRunner()
    tight = dataclasses.replace(settings, max_file_size_mb=1, max_total_disk_mb=3)
    manager = await make_manager(tight, run_worker=runner, disk_usage=lambda path: 0)
    job = manager.submit(URL, "video", "best")
    await wait_until(lambda: job.status == "downloading")
    with pytest.raises(AppError) as ei:
        manager.begin_upload("x")  # the running job's 2 MiB reservation plus 2 MiB is over 3 MiB
    assert code_of(ei) == "disk_full"


async def test_upload_counts_against_the_source_cap(make_manager, settings):
    runner = StubRunner()
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=1), run_worker=runner)
    manager.begin_upload("x")
    with pytest.raises(AppError) as ei:
        manager.open_link_source(URL, "best")
    assert code_of(ei) == "busy"


# --- Final review fixes ---------------------------------------------------------

def minimum_budget(temp_dir):
    return load_settings({"TEMP_DIR": str(temp_dir), "MAX_FILE_SIZE_MB": "1", "MAX_TOTAL_DISK_MB": "2"})


async def test_upload_at_the_minimum_budget_starts_its_check(make_manager, temp_dir):
    runner = StubRunner()
    manager = await make_manager(minimum_budget(temp_dir), run_worker=runner)  # real disk usage
    source = manager.begin_upload("big")
    (source.workdir / UPLOAD_NAME).write_bytes(b"\0" * (1024 * 1024))
    manager.finish_upload(source)
    # Its own file is inside its reservation, so the check it was admitted for can start.
    await wait_until(lambda: any(c.workdir == source.workdir for c in runner.calls), timeout=3)


async def test_clip_at_the_minimum_budget(make_manager, temp_dir):
    runner = StubRunner()
    manager = await make_manager(minimum_budget(temp_dir), run_worker=runner)
    source = manager.open_link_source(URL, "best")
    call = await started(runner, source)
    (call.workdir / "media.mp4").write_bytes(b"\0" * (1024 * 1024 - 4096))
    call.finish({"file": "media.mp4", "ext": "mp4", "title": "T", "duration": 10.0, "notice": None})
    await wait_until(lambda: source.status == "ready")
    clip = manager.submit_clip(source.id, 0, 5)  # a clip reserves MAX_FILE_SIZE_MB (its -fs cap)
    await started(runner, clip)


async def test_upload_whose_folder_cant_be_made_leaves_nothing(make_manager, settings, temp_dir):
    manager = await make_manager(dataclasses.replace(settings, max_clip_sources=1), run_worker=StubRunner())
    shutil.rmtree(temp_dir)
    with pytest.raises(AppError) as ei:
        manager.begin_upload("x")
    assert code_of(ei) == "internal"
    temp_dir.mkdir()
    manager.begin_upload("y")  # the failed upload holds no queue or source-cap place
