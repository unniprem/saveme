"""AI Highlights API routes (docs/AI_CLIPPING_DESIGN.md §4).

GET  /api/ai/status                          settings the page needs; never the key
POST /api/ai/test                            check the key and the model with OpenRouter
POST /api/sources/{id}/analyses/estimate     cost estimate for a request
POST /api/sources/{id}/analyses              start an analysis (202)
GET  /api/analyses/{id}                      follow it: stage, progress, highlights, usage
POST /api/analyses/{id}/cancel               cancel it
GET  /api/sources/{id}/transcript            the cached transcript
POST /api/sources/{id}/transcript            import an SRT/WebVTT file as the transcript

The subtitle import is, with the video upload, one of the two routes whose body the Guard
doesn't read first (its 64 KiB cap is too small for subtitles): it takes the raw file with a
Content-Length of at most MAX_SUBTITLE_BYTES, reads it with its own idle timeout, and never
reads past that size. Highlights are cut through the existing /clips and /batch routes.
"""
import re
from collections.abc import Callable

import anyio
from fastapi import APIRouter, Request
from starlette.requests import ClientDisconnect

from app.ai_jobs import AiManager
from app.errors import AppError
from app.schemas import AnalysisRequest
from app.studio_routes import upload_title
from app.transcript import AUTO, is_language

MAX_SUBTITLE_BYTES = 4 * 1024 * 1024
TRANSCRIPT_PATH = re.compile(r"/api/sources/[A-Za-z0-9_-]{22}/transcript")
_SUBTITLE_TYPES = frozenset({"text/plain", "text/vtt", "application/x-subrip", "text/srt",
                             "application/octet-stream"})


async def read_small_body(request: Request, *, limit: int, idle: float) -> bytes:
    """The whole body, refusing more than `limit` bytes (request_too_large) and timing out
    when nothing arrives for `idle` seconds."""
    length = request.headers.get("content-length", "")
    if not length.isdigit() or "transfer-encoding" in request.headers:
        raise AppError("invalid_request", "The upload needs a Content-Length header.", status=411)
    if int(length) > limit:
        raise AppError("request_too_large", "Subtitle files can be up to 4 MB.")
    body = bytearray()
    stream = request.stream()
    try:
        while True:
            try:
                with anyio.fail_after(idle):
                    chunk = await anext(stream)
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise AppError("timeout") from None
            except ClientDisconnect:
                raise AppError("upload_incomplete") from None
            body += chunk
            if len(body) > limit:
                raise AppError("request_too_large", "Subtitle files can be up to 4 MB.")
    finally:
        await stream.aclose()
    return bytes(body)


def add_ai_routes(router: APIRouter, ai: Callable[[], AiManager], *, idle_seconds: float) -> None:
    @router.get("/ai/status")
    async def ai_status():
        return ai().status()

    @router.post("/ai/test")
    async def ai_test():
        return await ai().test_connection()

    @router.post("/sources/{source_id}/analyses/estimate")
    async def estimate(source_id: str, body: AnalysisRequest):
        return await ai().estimate(source_id, body)

    @router.post("/sources/{source_id}/analyses", status_code=202)
    async def start_analysis(source_id: str, body: AnalysisRequest):
        analysis = ai().start(source_id, body)
        return {"analysis_id": analysis.id, "status": analysis.status}

    @router.get("/analyses/{analysis_id}")
    async def analysis_view(analysis_id: str):
        manager = ai()
        return manager.view(manager.get(analysis_id))

    @router.post("/analyses/{analysis_id}/cancel")
    async def cancel_analysis(analysis_id: str):
        manager = ai()
        return manager.view(manager.cancel(analysis_id))

    @router.get("/sources/{source_id}/transcript")
    async def transcript(source_id: str):
        return ai().transcript(source_id).model_dump()

    @router.post("/sources/{source_id}/transcript")
    async def import_transcript(source_id: str, request: Request, language: str = AUTO):
        manager = ai()
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type not in _SUBTITLE_TYPES:
            raise AppError("invalid_request", "Send the subtitle file as text/plain.", status=415)
        if language != AUTO and not is_language(language):
            raise AppError("invalid_request", "language must be auto or a language from the list")
        data = await read_small_body(request, limit=MAX_SUBTITLE_BYTES, idle=idle_seconds)
        result = manager.import_subtitles(source_id, data, filename=upload_title(request.headers.get(
            "x-saveme-filename")), language=language)
        return {"origin": result.origin, "language": result.language, "segments": len(result.segments),
                "duration": result.duration}
