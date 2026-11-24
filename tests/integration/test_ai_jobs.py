"""AiManager: the analysis lifecycle on top of JobManager sources, with the transcriber worker
stubbed (tests/support/ai.py) and OpenRouter mocked (tests/support/openrouter.py)."""
import asyncio
import dataclasses

import pytest

from app.ai_jobs import AiManager
from app.config import ConfigError, load_ai_settings
from app.errors import AppError
from app.schemas import AnalysisRequest
from app.transcript import load_transcript
from tests.integration.test_sources import ready_source
from tests.support.ai import TranscriberStub, clips_answer, speech_transcript
from tests.support.jobs import (URL, FakeClock, StubRunner, make_manager, ready_job,  # noqa: F401
                                settings, temp_dir, wait_until)
from tests.support.openrouter import KEY, MockOpenRouter, completion

AI = load_ai_settings({"OPENROUTER_API_KEY": KEY})
TERMINAL = ("ready", "failed", "cancelled")


@pytest.fixture
async def setup(make_manager):
    """(jobs, source, make_ai): a JobManager with one ready 12.5 s source; make_ai builds an
    AiManager over it."""
    runner = StubRunner()
    jobs = await make_manager(run_worker=runner)
    source = await ready_source(jobs, runner)
    source.duration = 300.0
    managers = []

    def make_ai(*, ai=AI, transcriber=None, server=None, **kwargs) -> tuple[AiManager, TranscriberStub, MockOpenRouter]:
        transcriber = transcriber or TranscriberStub()
        server = server or MockOpenRouter()

        async def no_sleep(_seconds):
            return None

        manager = AiManager(ai, jobs, run_worker=transcriber, transport=server.transport(), sleep=no_sleep,
                            **kwargs)
        managers.append(manager)
        return manager, transcriber, server

    yield jobs, source, make_ai, runner
    for manager in managers:
        await manager.shutdown()


def request(**change) -> AnalysisRequest:
    return AnalysisRequest(**change)


async def finished(analysis) -> None:
    await wait_until(lambda: analysis.status in TERMINAL)


async def test_analysis_runs_every_stage_and_caches_the_transcript(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, server = make_ai(transcriber=TranscriberStub(block=True))
    server.chat.append(clips_answer((10, 15, 95), (30, 35, 70)))
    analysis = ai.start(source.id, request(instruction="Find the best bits", count=3))
    assert analysis.status == "queued"
    await transcriber.started.wait()
    assert analysis.status == "transcribing"
    assert (source.workdir / "ai" / analysis.id / "audio.wav").exists()
    assert list(jobs._held_disk) == [analysis.id]  # the audio's disk is held while it runs
    transcriber.gate.set()
    await finished(analysis)
    assert analysis.status == "ready", ai.view(analysis)
    view = ai.view(analysis)
    assert [(c["id"], c["title"], c["start"], c["end"], c["score"]) for c in view["candidates"]] == [
        ("h1", "Clip 10-15", 49.7, 80.5, 95), ("h2", "Clip 30-35", 149.7, 180.5, 70)]
    assert view["candidates"][0]["tags"] == ["Tag"] and view["candidates"][0]["summary"] == "A summary."
    assert view["transcript"] == {"origin": "whisper", "language": "en", "segments": 60, "cached": False}
    assert view["skipped"] == [] and view["error"] is None
    assert view["usage"]["calls"] == 1 and view["usage"]["cost_reported"] is True
    assert view["usage"]["limit_usd"] == 0.25 and view["usage"]["max_usd"] > 0
    assert view["options"]["instruction"] == "Find the best bits" and view["options"]["count"] == 3
    # The worker ran the transcriber on the source, in a folder inside the source's own.
    call = transcriber.calls[0]
    assert call == {"mode": "transcribe", "source": str(source.file_path),
                    "workdir": str(source.workdir / "ai" / analysis.id), "model": "base",
                    "language": "auto", "cpu_threads": 2, "duration": 300.0, "max_seconds": 180 * 60}
    assert transcriber.kwargs[0]["cmd"][-2:] == ("-m", "app.transcriber")
    assert not (source.workdir / "ai" / analysis.id).exists()
    assert load_transcript(source.workdir / "ai" / "transcript.json") == transcriber.transcript
    assert source.status == "ready"


async def test_a_second_analysis_reuses_the_transcript(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, server = make_ai()
    server.chat.extend([clips_answer((10, 15, 95)), clips_answer((40, 45, 80))])
    first = ai.start(source.id, request())
    await finished(first)
    second = ai.start(source.id, request(instruction="Something else"))
    await finished(second)
    assert second.status == "ready" and len(transcriber.calls) == 1
    view = ai.view(second)
    assert view["skipped"] == ["extracting", "transcribing"] and view["transcript"]["cached"] is True
    assert view["candidates"][0]["start"] == 199.7


@pytest.mark.parametrize("change", [{"retranscribe": True}, {"language": "ml"}])
async def test_retranscribe_or_another_language_transcribes_again(setup, change):
    jobs, source, make_ai, _ = setup
    ai, transcriber, server = make_ai()
    server.chat.extend([clips_answer((10, 15, 95)), clips_answer((10, 15, 95))])
    await finished(ai.start(source.id, request()))
    again = ai.start(source.id, request(**change))
    await finished(again)
    assert again.status == "ready" and len(transcriber.calls) == 2
    assert transcriber.calls[1]["language"] == change.get("language", "auto")


async def test_transcribe_only_needs_no_key_and_calls_no_model(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, server = make_ai(ai=load_ai_settings({}))
    analysis = ai.start(source.id, request(transcribe_only=True))
    await finished(analysis)
    assert analysis.status == "ready" and server.requests == []
    view = ai.view(analysis)
    assert view["candidates"] == [] and view["usage"] is None and view["transcript"]["segments"] == 60


async def test_analysis_without_a_key_is_refused(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai(ai=load_ai_settings({}))
    with pytest.raises(AppError) as ei:
        ai.start(source.id, request())
    assert ei.value.code == "ai_not_configured"


async def test_ai_config_error_turns_everything_off(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai(ai=ConfigError("AI_MAX_CLIPS", "must be between 1 and 20, got 0"))
    status = ai.status()
    assert status["available"] is False and status["configured"] is False
    assert status["error"] == {"code": "ai_config_error", "message": "AI_MAX_CLIPS must be between 1 and 20, got 0."}
    with pytest.raises(AppError) as ei:
        ai.start(source.id, request(transcribe_only=True))
    assert ei.value.code == "ai_config_error"


async def test_status_names_the_settings_but_never_the_key(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai()
    status = ai.status()
    assert KEY not in str(status)
    assert status["configured"] and status["available"] and status["error"] is None
    assert status["model"] == "anthropic/claude-haiku-5.5" and status["whisper_model"] == "base"
    assert (status["max_clips"], status["max_cost_usd"], status["max_source_minutes"]) == (10, 0.25, 180)
    assert status["data_collection"] == "deny"
    assert {"code": "ml", "name": "Malayalam"} in status["languages"]


async def test_one_pending_analysis_per_source(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(transcriber=TranscriberStub(block=True))
    ai.start(source.id, request())
    with pytest.raises(AppError) as ei:
        ai.start(source.id, request())
    assert ei.value.code == "analysis_running"


async def test_count_is_capped_by_ai_max_clips(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai(ai=dataclasses.replace(AI, ai_max_clips=3))
    with pytest.raises(AppError) as ei:
        ai.start(source.id, request(count=4))
    assert ei.value.code == "invalid_request" and "1 to 3" in ei.value.message


async def test_analysis_needs_a_ready_source(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai()
    loading = jobs.open_link_source(URL, "best")
    with pytest.raises(AppError) as ei:
        ai.start(loading.id, request())
    assert ei.value.code == "source_not_ready"
    with pytest.raises(AppError) as ei:
        ai.start("x" * 22, request())
    assert ei.value.code == "source_not_found"


async def test_cancel_while_transcribing_kills_the_run_and_cleans_up(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(transcriber=TranscriberStub(block=True))
    analysis = ai.start(source.id, request())
    await transcriber.started.wait()
    assert analysis.status == "transcribing" and ai.view(analysis)["progress_percent"] == 40.0
    assert ai.view(analysis)["detail"] == "Transcribing (English)"
    assert ai.cancel(analysis.id) is analysis and analysis.status == "cancelled"
    await wait_until(lambda: transcriber.cancelled and not analysis.task_running())
    assert not (source.workdir / "ai" / analysis.id).exists()
    assert ai.cancel(analysis.id).status == "cancelled"  # a repeat is harmless
    assert not jobs._in_use(source)  # the hold is released
    assert jobs._held_disk == {}  # and the disk reservation


async def test_cancel_while_analyzing(setup):
    jobs, source, make_ai, _ = setup
    server = MockOpenRouter()
    server.chat_delay = 30
    ai, _, _ = make_ai(server=server)
    analysis = ai.start(source.id, request())
    await wait_until(lambda: analysis.status == "analyzing" and len(server.requests) >= 2)
    ai.cancel(analysis.id)
    await wait_until(lambda: not analysis.task_running())
    view = ai.view(analysis)
    assert view["status"] == "cancelled" and view["candidates"] == []


async def test_closing_the_source_cancels_its_analysis(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(transcriber=TranscriberStub(block=True))
    analysis = ai.start(source.id, request())
    await transcriber.started.wait()
    jobs.close_source(source.id)
    assert analysis.status == "cancelled"
    await wait_until(lambda: transcriber.cancelled)
    assert not source.workdir.exists()


async def test_a_held_source_does_not_idle_out_while_analysing(make_manager, settings):
    clock, runner = FakeClock(), StubRunner()
    jobs = await make_manager(run_worker=runner, clock=clock, sweep_interval=3600)
    source = await ready_source(jobs, runner)
    transcriber = TranscriberStub(block=True)
    ai = AiManager(AI, jobs, run_worker=transcriber, transport=MockOpenRouter().transport())
    try:
        ai.start(source.id, request(transcribe_only=True))
        await transcriber.started.wait()
        clock.advance(settings.job_ttl_seconds + 10)
        await jobs.sweep()
        assert source.status == "ready"
    finally:
        await ai.shutdown()


async def test_ai_jobs_run_one_at_a_time_and_never_block_downloads(make_manager, settings):
    runner = StubRunner()
    jobs = await make_manager(run_worker=runner)
    first, second = await ready_source(jobs, runner), await ready_source(jobs, runner)
    transcriber = TranscriberStub(block=True)
    ai = AiManager(AI, jobs, run_worker=transcriber, transport=MockOpenRouter().transport())
    try:
        a = ai.start(first.id, request(transcribe_only=True))
        b = ai.start(second.id, request(transcribe_only=True))
        await transcriber.started.wait()
        await asyncio.sleep(0.05)
        assert a.status == "transcribing" and b.status == "queued" and len(transcriber.calls) == 1
        # A download starts and finishes while the AI slot is taken.
        runner.auto_finish = True
        job = await ready_job(jobs, runner)
        assert job.status == "ready"
        transcriber.gate.set()
        await finished(a)
        await finished(b)
        assert (a.status, b.status) == ("ready", "ready") and len(transcriber.calls) == 2
    finally:
        await ai.shutdown()


async def test_a_job_over_the_spending_limit_is_refused_before_any_call(setup):
    jobs, source, make_ai, _ = setup
    ai, _, server = make_ai(ai=dataclasses.replace(AI, ai_max_cost_usd_per_job=0.0001))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.status == "failed" and analysis.error.code == "cost_limit"
    assert "$0.0001" in analysis.error.message and server.bodies() == []


async def test_no_speech_fails_clearly(setup):
    jobs, source, make_ai, _ = setup
    empty = speech_transcript().model_copy(update={"segments": []})
    ai, _, server = make_ai(transcriber=TranscriberStub(empty))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.status == "failed" and analysis.error.code == "no_speech"
    assert server.bodies() == []


async def test_transcriber_errors_fail_the_analysis_and_clean_up(setup):
    jobs, source, make_ai, _ = setup
    error = AppError("whisper_model_missing", "The transcription model isn't installed.")
    ai, _, _ = make_ai(transcriber=TranscriberStub(error=error))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.status == "failed" and analysis.error is error
    assert not (source.workdir / "ai" / analysis.id).exists()
    assert ai.view(analysis)["error"] == {"code": "whisper_model_missing",
                                          "message": "The transcription model isn't installed."}


async def test_a_bad_transcript_file_is_an_internal_error(setup):
    jobs, source, make_ai, _ = setup

    class Broken(TranscriberStub):
        async def __call__(self, request, *, workdir=None, **kwargs):
            (workdir / "transcript.json").write_text("{not json")
            return {"file": "transcript.json"}

    ai, _, _ = make_ai(transcriber=Broken())
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.error.code == "internal"


async def test_the_whole_analysis_has_a_time_limit(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai(ai=dataclasses.replace(AI, ai_max_job_seconds=0.2),
                       transcriber=TranscriberStub(block=True))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.status == "failed" and analysis.error.code == "timeout"


async def test_too_long_a_video_is_refused_before_transcribing(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(ai=dataclasses.replace(AI, ai_max_source_minutes=4))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.error.code == "video_too_long" and "4-minute" in analysis.error.message
    assert transcriber.calls == []


async def test_no_disk_room_for_the_audio(setup):
    jobs, source, make_ai, _ = setup
    jobs.reserve_disk = lambda key, path, nbytes: False
    ai, transcriber, _ = make_ai()
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.error.code == "disk_full" and transcriber.calls == []


async def test_an_unusable_model_fails_before_transcribing(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(ai=dataclasses.replace(AI, openrouter_model="openrouter/auto"))
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.error.code == "model_unsupported" and transcriber.calls == []


async def test_imported_subtitles_become_the_transcript(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, server = make_ai()
    srt = "1\n00:00:01,000 --> 00:00:30,000\nHello there\n\n2\n00:00:31,000 --> 00:01:05,000\nMore\n"
    transcript = ai.import_subtitles(source.id, srt.encode(), filename="talk.srt", language="en")
    assert transcript.origin == "subtitles" and len(transcript.segments) == 2
    assert ai.transcript(source.id) == transcript
    server.chat.append(clips_answer((0, 1, 90)))
    analysis = ai.start(source.id, request(language="ml"))  # subtitles win over the language choice
    await finished(analysis)
    assert analysis.status == "ready" and transcriber.calls == []
    assert ai.view(analysis)["transcript"]["origin"] == "subtitles"


async def test_subtitles_cannot_replace_a_transcript_being_made(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(transcriber=TranscriberStub(block=True))
    ai.start(source.id, request())
    with pytest.raises(AppError) as ei:
        ai.import_subtitles(source.id, b"1\n00:00:01,000 --> 00:00:02,000\nx\n", filename=None, language="auto")
    assert ei.value.code == "analysis_running"


async def test_no_transcript_yet(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai()
    with pytest.raises(AppError) as ei:
        ai.transcript(source.id)
    assert ei.value.code == "transcript_not_found"


async def test_estimates_use_the_transcript_when_there_is_one(setup):
    jobs, source, make_ai, _ = setup
    ai, _, server = make_ai()
    before = await ai.estimate(source.id, request())
    assert before["basis"] == "duration" and before["transcript_cached"] is False
    assert before["limit_usd"] == 0.25 and before["within_limit"] is True
    assert 0 < before["estimated_usd"] <= before["max_usd"]
    server.chat.append(clips_answer((10, 15, 95)))
    await finished(ai.start(source.id, request()))
    after = await ai.estimate(source.id, request())
    assert after["basis"] == "transcript" and after["transcript_cached"] is True
    assert after["max_usd"] <= before["max_usd"]


async def test_test_connection_reports_key_and_model(setup):
    jobs, source, make_ai, _ = setup
    ai, _, server = make_ai()
    result = await ai.test_connection()
    assert result["ok"] is True and result["key"]["limit_usd"] == 10.0
    assert result["model"] == {"id": "anthropic/claude-haiku-5.5", "name": "Anthropic: Claude Haiku 5.5",
                               "context_length": 1_000_000, "prompt_usd_per_million": 0.1,
                               "completion_usd_per_million": 0.5, "structured_outputs": True}
    server.key_status = 401
    with pytest.raises(AppError) as ei:
        await ai.test_connection()
    assert ei.value.code == "openrouter_auth"


async def test_openrouter_failures_fail_the_analysis_with_their_message(setup):
    jobs, source, make_ai, _ = setup
    server = MockOpenRouter()
    server.chat.append(completion({"clips": []}))
    import httpx
    server.chat[0] = httpx.Response(402, json={"error": {"code": 402, "message": "no credits"}})
    ai, _, _ = make_ai(server=server)
    analysis = ai.start(source.id, request())
    await finished(analysis)
    assert analysis.error.code == "openrouter_credits"
    assert ai.view(analysis)["usage"]["calls"] == 0


async def test_shutdown_cancels_running_analyses(setup):
    jobs, source, make_ai, _ = setup
    ai, transcriber, _ = make_ai(transcriber=TranscriberStub(block=True))
    analysis = ai.start(source.id, request())
    await transcriber.started.wait()
    await ai.shutdown()
    assert transcriber.cancelled and analysis.status == "cancelled"


async def test_unknown_analysis_ids(setup):
    jobs, source, make_ai, _ = setup
    ai, _, _ = make_ai()
    for bad in ("x" * 22, "../etc", source.id):
        with pytest.raises(AppError) as ei:
            ai.get(bad)
        assert ei.value.code == "analysis_not_found"
