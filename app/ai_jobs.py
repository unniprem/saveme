"""AiManager: AI Highlights analyses of Clip Studio sources (docs/AI_CLIPPING_DESIGN.md §5).

An analysis is in-memory, like a job. Its stages, each its status in turn:

    queued → preparing → extracting → transcribing → analyzing → ranking → ready
                                                    (or failed | cancelled at any point)

- queued: waiting for one of AI_MAX_CONCURRENT_JOBS slots. These are separate from download
  slots and the download queue, so analyses never hold up downloads (or the other way round).
- preparing: limits checked, model checked (before spending CPU on transcription), cached
  transcript looked up, disk for the audio reserved through the JobManager's budget.
- extracting, transcribing: the transcriber worker (`python -m app.transcriber`) run through
  run_worker, in <source folder>/ai/<analysis id>/; its transcript becomes the source's cached
  transcript (<source folder>/ai/transcript.json) and the work folder is removed. Skipped when
  the cached transcript can be reused.
- analyzing: app.highlights.analyze, one OpenRouter call per transcript chunk, within the
  spending limit (an analysis whose upper-bound estimate is over the limit is refused first).
- ranking, ready: candidates and usage stay readable until the record is pruned.

From start to end an analysis *holds* its source, so the source can't idle out or be replaced.
Closing the source (or its expiry) cancels the analysis; so does shutdown. Cancelling kills the
transcriber's process group (run_worker) or abandons the OpenRouter request; the work folder is
always removed. The whole run (after its slot is free) is limited to AI_MAX_JOB_SECONDS. One
analysis may be pending per source. Transcripts can also come from a subtitle file.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import httpx

from app.config import AiSettings, ConfigError
from app.errors import AppError
from app.highlights import (CHUNK_TOKENS, Candidate, Options, analyze, answer_budget, estimate_from_duration,
                            estimate_from_transcript, per_chunk_budget, usd_text)
from app.jobs import Job, JobManager
from app.mediafile import WAV_BYTES_PER_SECOND
from app.openrouter import OpenRouterClient, UsageMeter, check_model
from app.runner import RunWorker, run_worker
from app.schemas import AnalysisRequest
from app.transcript import (WHISPER_LANGUAGES, Transcript, load_transcript, parse_subtitles, reusable,
                            save_transcript)

logger = logging.getLogger(__name__)

TRANSCRIBER_CMD: tuple[str, ...] = (sys.executable, "-P", "-m", "app.transcriber")
AI_DIR = "ai"
TRANSCRIPT_FILE = "transcript.json"
MAX_RECORDS = 50
RECORD_SECONDS = 3600
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_PENDING = frozenset({"queued", "preparing", "extracting", "transcribing", "analyzing", "ranking"})
_TRANSCRIBER_STAGES = ("extracting", "transcribing")
_SLACK_BYTES = 64 * 1024 * 1024  # transcript and headers, beside the WAV
_DETAIL = {"queued": "Waiting for the AI to be free", "preparing": "Preparing the video",
           "analyzing": "Analyzing the transcript", "ranking": "Ranking highlights"}


@dataclass(eq=False)
class Analysis:
    id: str
    source_id: str
    request: AnalysisRequest
    created_at: float
    status: str = "queued"
    detail: str | None = _DETAIL["queued"]
    progress_percent: float | None = None
    skipped: list[str] = field(default_factory=list)
    transcript: dict | None = None
    candidates: list[Candidate] = field(default_factory=list)
    notice: str | None = None
    error: AppError | None = None
    meter: UsageMeter | None = None
    estimate: dict | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def task_running(self) -> bool:
        return self.task is not None and not self.task.done()


def _config_message(error: ConfigError) -> str:
    return f"{error.var} {error.message.rstrip('.')}."


class AiManager:
    def __init__(self, ai: AiSettings | ConfigError, jobs: JobManager, *, run_worker: RunWorker = run_worker,
                 transcriber_cmd: Sequence[str] = TRANSCRIBER_CMD,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], object] = asyncio.sleep, clock: Callable[[], float] = time.monotonic):
        self._jobs = jobs
        self._run_worker = run_worker
        self._cmd = tuple(transcriber_cmd)
        self._clock = clock
        self._analyses: dict[str, Analysis] = {}
        if isinstance(ai, ConfigError):
            self._ai: AiSettings | None = None
            self._config_error: AppError | None = AppError("ai_config_error", _config_message(ai))
            self._client: OpenRouterClient | None = None
            self._slots = asyncio.Semaphore(1)
        else:
            self._ai, self._config_error = ai, None
            self._client = OpenRouterClient(ai, transport=transport, sleep=sleep)
            self._slots = asyncio.Semaphore(ai.ai_max_concurrent_jobs)
        jobs.add_source_listener(self._on_source_ended)

    async def shutdown(self) -> None:
        tasks = [a.task for a in self._analyses.values() if a.task_running()]
        for analysis in self._analyses.values():
            if analysis.status in _PENDING:
                self._end(analysis, "cancelled")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    # --- settings and status --------------------------------------------------------------

    def _settings(self) -> AiSettings:
        if self._ai is None:
            raise self._config_error
        return self._ai

    def _openrouter(self) -> OpenRouterClient:
        ai = self._settings()
        if not ai.configured or self._client is None:
            raise AppError("ai_not_configured")
        return self._client

    def status(self) -> dict:
        ai = self._ai
        languages = sorted(({"code": code, "name": name} for code, name in WHISPER_LANGUAGES.items()),
                           key=lambda lang: lang["name"])
        if ai is None:
            return {"available": False, "configured": False, "model": None, "whisper_model": None,
                    "max_clips": None, "max_cost_usd": None, "max_source_minutes": None,
                    "data_collection": None, "languages": languages, "error": self._config_error.to_dict()}
        return {"available": True, "configured": ai.configured, "model": ai.openrouter_model,
                "whisper_model": ai.whisper_model, "max_clips": ai.ai_max_clips,
                "max_cost_usd": ai.ai_max_cost_usd_per_job, "max_source_minutes": ai.ai_max_source_minutes,
                "data_collection": ai.openrouter_data_collection, "languages": languages, "error": None}

    async def test_connection(self) -> dict:
        client = self._openrouter()
        key = await client.key_info()
        model = await client.model()
        check_model(model, needed_tokens=CHUNK_TOKENS + answer_budget(per_chunk_budget(10), model))
        assert model.pricing is not None
        return {"ok": True, "key": key, "model": {
            "id": model.id, "name": model.name, "context_length": model.context_length,
            "prompt_usd_per_million": round(model.pricing.prompt * 1e6, 6),
            "completion_usd_per_million": round(model.pricing.completion * 1e6, 6),
            "structured_outputs": model.structured_outputs}}

    # --- transcripts ----------------------------------------------------------------------

    @staticmethod
    def _cache_path(source: Job):
        return source.workdir / AI_DIR / TRANSCRIPT_FILE

    def _pending_for(self, source_id: str) -> Analysis | None:
        return next((a for a in self._analyses.values()
                     if a.source_id == source_id and a.status in _PENDING), None)

    def transcript(self, source_id: str) -> Transcript:
        self._settings()
        source = self._jobs.ready_source(source_id)
        self._jobs.touch(source)
        cached = load_transcript(self._cache_path(source))
        if cached is None:
            raise AppError("transcript_not_found")
        return cached

    def import_subtitles(self, source_id: str, data: bytes, *, filename: str | None, language: str) -> Transcript:
        self._settings()
        source = self._jobs.ready_source(source_id)
        if self._pending_for(source.id) is not None:
            raise AppError("analysis_running")
        transcript = parse_subtitles(data, filename=filename, duration=source.duration, language=language)
        save_transcript(self._cache_path(source), transcript)
        self._jobs.touch(source)
        logger.info("source %s: subtitles imported (%d cues)", source.id, len(transcript.segments))
        return transcript

    # --- analyses -------------------------------------------------------------------------

    def _options(self, request: AnalysisRequest) -> Options:
        ai = self._settings()
        if request.count > ai.ai_max_clips:
            raise AppError("invalid_request", f"Ask for 1 to {ai.ai_max_clips} clips.")
        return Options(instruction=request.instruction, min_seconds=request.min_seconds,
                       max_seconds=request.max_seconds, count=request.count, min_score=request.min_score)

    async def estimate(self, source_id: str, request: AnalysisRequest) -> dict:
        options = self._options(request)
        client = self._openrouter()
        source = self._jobs.ready_source(source_id)
        model = await client.model()
        cached = load_transcript(self._cache_path(source))
        if reusable(cached, language=request.language, retranscribe=request.retranscribe) and cached.segments:
            estimate = estimate_from_transcript(cached, options, model)
        else:
            estimate = estimate_from_duration(source.duration or 60.0, options, model)
            cached = None
        limit = self._settings().ai_max_cost_usd_per_job
        return {"basis": estimate.basis, "chunks": estimate.chunks, "estimated_usd": estimate.estimated_usd,
                "max_usd": estimate.max_usd, "limit_usd": limit, "within_limit": estimate.max_usd <= limit,
                "transcript_cached": cached is not None, "model": model.id}

    def start(self, source_id: str, request: AnalysisRequest) -> Analysis:
        self._options(request)
        if not request.transcribe_only:
            self._openrouter()
        source = self._jobs.ready_source(source_id)
        if self._pending_for(source.id) is not None:
            raise AppError("analysis_running")
        self._prune()
        self._jobs.hold_source(source.id)
        analysis_id = secrets.token_urlsafe(16)
        while analysis_id in self._analyses:
            analysis_id = secrets.token_urlsafe(16)
        analysis = Analysis(id=analysis_id, source_id=source.id, request=request, created_at=self._clock())
        self._analyses[analysis.id] = analysis
        analysis.task = asyncio.create_task(self._run(analysis, source))
        logger.info("analysis %s queued for source %s", analysis.id, source.id)
        return analysis

    def get(self, analysis_id: str) -> Analysis:
        analysis = self._analyses.get(analysis_id) if _ID_RE.fullmatch(str(analysis_id)) else None
        if analysis is None:
            raise AppError("analysis_not_found")
        return analysis

    def cancel(self, analysis_id: str) -> Analysis:
        analysis = self.get(analysis_id)
        self._cancel(analysis)
        return analysis

    def _cancel(self, analysis: Analysis) -> None:
        if analysis.status not in _PENDING:
            return
        self._end(analysis, "cancelled")
        if analysis.task is not None:
            analysis.task.cancel()
        logger.info("analysis %s cancelled", analysis.id)

    def _on_source_ended(self, source_id: str) -> None:
        for analysis in list(self._analyses.values()):
            if analysis.source_id == source_id:
                self._cancel(analysis)

    def view(self, analysis: Analysis) -> dict:
        usage = None
        if analysis.meter is not None:
            usage = {**analysis.meter.view(), **(analysis.estimate or {}),
                     "limit_usd": self._ai.ai_max_cost_usd_per_job if self._ai else None}
        return {
            "analysis_id": analysis.id, "source_id": analysis.source_id, "status": analysis.status,
            "detail": analysis.detail, "progress_percent": analysis.progress_percent,
            "skipped": list(analysis.skipped), "transcript": analysis.transcript,
            "candidates": [{"id": f"h{n}", "title": c.title, "start": c.start, "end": c.end, "score": c.score,
                            "summary": c.summary, "reason": c.reason, "tags": list(c.tags)}
                           for n, c in enumerate(analysis.candidates, 1)],
            "notice": analysis.notice,
            "error": analysis.error.to_dict() if analysis.error is not None else None,
            "usage": usage, "options": analysis.request.model_dump(),
        }

    # --- the run --------------------------------------------------------------------------

    def _stage(self, analysis: Analysis, status: str, detail: str | None = None,
               percent: float | None = None) -> None:
        if analysis.status not in _PENDING:
            return
        analysis.status = status
        analysis.detail = detail or _DETAIL.get(status)
        analysis.progress_percent = percent

    def _end(self, analysis: Analysis, status: str, error: AppError | None = None) -> None:
        analysis.status = status
        analysis.error = error
        analysis.detail = None
        analysis.progress_percent = None
        analysis.finished_at = self._clock()

    def _prune(self) -> None:
        now = self._clock()
        finished = sorted((a for a in self._analyses.values() if a.status not in _PENDING),
                          key=lambda a: a.finished_at or 0.0)
        for n, analysis in enumerate(finished):
            if len(finished) - n > MAX_RECORDS - 1 or now - (analysis.finished_at or now) >= RECORD_SECONDS:
                del self._analyses[analysis.id]

    async def _run(self, analysis: Analysis, source: Job) -> None:
        ai = self._ai
        assert ai is not None
        workdir = source.workdir / AI_DIR / analysis.id
        try:
            async with self._slots:
                if analysis.status != "queued":
                    return
                async with asyncio.timeout(ai.ai_max_job_seconds):
                    await self._work(analysis, source, workdir, ai)
        except asyncio.CancelledError:
            if analysis.status in _PENDING:
                self._end(analysis, "cancelled")
        except TimeoutError:
            self._end(analysis, "failed", AppError(
                "timeout", f"The analysis took longer than {ai.ai_max_job_seconds} seconds and was stopped."))
        except AppError as exc:
            if analysis.status in _PENDING:
                self._end(analysis, "failed", exc)
            logger.info("analysis %s failed: %s", analysis.id, exc.code)
        except Exception:
            logger.exception("analysis %s failed unexpectedly", analysis.id)
            self._end(analysis, "failed", AppError("internal"))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._jobs.release_disk(analysis.id)
            self._jobs.release_source(source)

    async def _work(self, analysis: Analysis, source: Job, workdir, ai: AiSettings) -> None:
        request = analysis.request
        options = self._options(request)
        self._stage(analysis, "preparing")
        if source.status != "ready" or source.file_path is None:
            raise AppError("source_not_found")
        max_seconds = ai.ai_max_source_minutes * 60
        if source.duration is not None and source.duration > max_seconds:
            raise AppError("video_too_long", f"This video is longer than the {ai.ai_max_source_minutes}-minute "
                           "limit for AI Highlights.")
        model = None
        if not request.transcribe_only:
            model = await self._openrouter().model()
            check_model(model, needed_tokens=CHUNK_TOKENS + answer_budget(per_chunk_budget(request.count), model))

        cache = self._cache_path(source)
        transcript = load_transcript(cache)
        cached = reusable(transcript, language=request.language, retranscribe=request.retranscribe)
        if cached:
            analysis.skipped = list(_TRANSCRIBER_STAGES)
        else:
            transcript = await self._transcribe(analysis, source, workdir, ai, max_seconds)
            save_transcript(cache, transcript)
        assert transcript is not None
        analysis.transcript = {"origin": transcript.origin, "language": transcript.language,
                               "segments": len(transcript.segments), "cached": cached}
        if request.transcribe_only:
            if not transcript.segments:
                analysis.notice = "No speech was found in this video."
            self._finish(analysis)
            return
        if not transcript.segments:
            raise AppError("no_speech")

        assert model is not None and model.pricing is not None
        estimate = estimate_from_transcript(transcript, options, model)
        analysis.estimate = {"estimated_usd": estimate.estimated_usd, "max_usd": estimate.max_usd}
        analysis.meter = UsageMeter(model.pricing)
        limit = ai.ai_max_cost_usd_per_job
        if estimate.max_usd > limit:
            raise AppError("cost_limit", f"This analysis could cost up to {usd_text(estimate.max_usd)}, over "
                           f"the {usd_text(limit)} limit per analysis. Raise AI_MAX_COST_USD_PER_JOB, or "
                           "analyse a shorter video.")
        self._stage(analysis, "analyzing", percent=0.0)

        def progress(done: int, total: int) -> None:
            detail = f"Analyzing part {min(done + 1, total)} of {total}" if total > 1 else _DETAIL["analyzing"]
            self._stage(analysis, "analyzing", detail, round(100 * done / total, 1) if total else None)

        result = await analyze(self._openrouter(), model, transcript, options, limit_usd=limit,
                               meter=analysis.meter, on_progress=progress)
        self._stage(analysis, "ranking")
        analysis.candidates = result.candidates
        analysis.notice = result.notice
        self._finish(analysis)

    def _finish(self, analysis: Analysis) -> None:
        if analysis.status in _PENDING:
            self._end(analysis, "ready")
            logger.info("analysis %s ready (%d highlights)", analysis.id, len(analysis.candidates))

    async def _transcribe(self, analysis: Analysis, source: Job, workdir, ai: AiSettings,
                          max_seconds: int) -> Transcript:
        length = source.duration or max_seconds
        reserve = int(WAV_BYTES_PER_SECOND * length) + _SLACK_BYTES
        if not self._jobs.reserve_disk(analysis.id, workdir, reserve):  # held until the run ends
            raise AppError("disk_full")
        workdir.mkdir(parents=True, exist_ok=True)

        def on_event(event: dict) -> None:
            if event.get("event") == "phase" and event.get("phase") in _TRANSCRIBER_STAGES:
                detail = event.get("detail")
                self._stage(analysis, event["phase"], detail if isinstance(detail, str) else None)
            elif event.get("event") == "progress" and event.get("stage") == analysis.status:
                percent, detail = event.get("percent"), event.get("detail")
                if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                    analysis.progress_percent = min(100.0, max(0.0, float(percent)))
                if isinstance(detail, str):
                    analysis.detail = detail

        result = await self._run_worker(
            {"mode": "transcribe", "source": str(source.file_path), "workdir": str(workdir),
             "model": ai.whisper_model, "language": analysis.request.language,
             "cpu_threads": ai.whisper_cpu_threads, "duration": source.duration, "max_seconds": max_seconds,
             "word_timestamps": ai.whisper_word_timestamps},
            cmd=self._cmd, timeout=ai.ai_max_job_seconds, on_event=on_event, workdir=workdir,
            max_workdir_bytes=reserve)
        transcript = load_transcript(workdir / TRANSCRIPT_FILE) if result.get("file") == TRANSCRIPT_FILE else None
        if transcript is None:
            logger.error("analysis %s: the transcriber returned no valid transcript", analysis.id)
            raise AppError("internal")
        return transcript
