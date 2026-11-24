"""Clip Studio API routes (spec §6.3): sources, their preview, and clips.

A source is a video kept on the server while a studio is open; a clip is a job cut out
of it, and a batch is several clips cut out of it into one ZIP. Both are then followed,
cancelled and saved through the existing /api/jobs routes.

POST /api/sources/upload is the one route whose body the Guard doesn't read (and cap at
64 KiB) first. It takes the raw file with a Content-Length no larger than
MAX_FILE_SIZE_MB, and streams it to disk with its own limits: the queue, the source cap
and the disk budget before any byte is written, 60 s without data, MAX_JOB_SECONDS in
all, and not a byte past the declared length. Every failure removes what was written.
"""
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import unquote

import anyio
import anyio.to_thread
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from app.config import Settings
from app.errors import AppError, size_text
from app.jobs import MIN_CLIP_SECONDS, UPLOAD_NAME, JobManager
from app.mediafile import MAX_BATCH_CLIPS
from app.netguard import check_url_syntax
from app.schemas import BatchRequest, ClipRequest, SourceRequest

CheckLink = Callable[[str, JobManager], Awaitable[None]]
UPLOAD_PATH = "/api/sources/upload"
UPLOAD_IDLE_SECONDS = 60.0
_WRITE_BYTES = 1024 * 1024
_MAX_NAME = 1024  # characters of the file name kept for the title


def upload_title(header: str | None) -> str | None:
    """The title for an upload: its file name (X-SaveMe-Filename, percent-encoded UTF-8)
    without any folder or extension, or None. Only ever shown as text or passed through
    sanitize_filename."""
    if not header:
        return None
    name = unquote(header[:3 * _MAX_NAME], errors="replace")[:_MAX_NAME]
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, _ext = name.rpartition(".")
    return (stem if dot else name).strip() or None


async def receive_upload(request: Request, dest: Path, size: int, *, idle: float, limit: float,
                         is_open: Callable[[], bool]) -> None:
    """Write exactly `size` body bytes to `dest`, in large writes off the event loop.

    Raises timeout when nothing arrives for `idle` seconds or the upload passes `limit`,
    upload_incomplete when the body ends short or the client leaves, invalid_request past
    `size`, and source_not_found once `is_open()` is false (the source was closed)."""
    received = 0
    pending = bytearray()
    deadline = anyio.current_time() + limit
    stream = request.stream()
    try:
        with open(dest, "wb") as file:
            while True:
                wait = min(idle, deadline - anyio.current_time())
                try:
                    if wait <= 0:
                        raise TimeoutError
                    with anyio.fail_after(wait):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise AppError("timeout") from None
                except ClientDisconnect:
                    raise AppError("upload_incomplete") from None
                if not is_open():
                    raise AppError("source_not_found")
                received += len(chunk)
                if received > size:
                    raise AppError("invalid_request", "The upload sent more than its Content-Length.")
                pending += chunk
                if len(pending) >= _WRITE_BYTES:
                    await anyio.to_thread.run_sync(file.write, bytes(pending))
                    pending.clear()
            if pending:
                await anyio.to_thread.run_sync(file.write, bytes(pending))
    finally:
        await stream.aclose()
    if received != size:
        raise AppError("upload_incomplete")


def add_studio_routes(router: APIRouter, settings: Settings, jobs: Callable[[], JobManager], *,
                      check_link: CheckLink, upload_idle_seconds: float = UPLOAD_IDLE_SECONDS) -> None:
    """Add the studio routes to the /api router. `check_link` resolves a link's host inside
    a held queue place, exactly as for POST /api/jobs."""

    @router.get("/studio")
    async def studio_limits():
        return {"max_upload_bytes": settings.max_file_bytes,
                "idle_minutes": settings.job_ttl_minutes,
                "min_clip_seconds": MIN_CLIP_SECONDS,
                "max_batch_clips": MAX_BATCH_CLIPS}

    @router.post("/sources", status_code=202)
    async def open_source(body: SourceRequest):
        url = check_url_syntax(body.url)
        manager = jobs()
        await check_link(url, manager)
        source = manager.open_link_source(url, body.quality)
        return {"source_id": source.id, "status": source.status}

    @router.post("/sources/upload", status_code=202)
    async def upload_source(request: Request):
        manager = jobs()
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type != "application/octet-stream":
            raise AppError("invalid_request", "Send the file as application/octet-stream.", status=415)
        length = request.headers.get("content-length", "")
        if not length.isdigit() or "transfer-encoding" in request.headers:
            raise AppError("invalid_request", "The upload needs a Content-Length header.", status=411)
        size = int(length)
        if size == 0:
            raise AppError("invalid_request", "The file is empty.")
        if size > settings.max_file_bytes:
            raise AppError("too_large",
                           f"This file is larger than the {size_text(settings.max_file_bytes)} upload limit.")
        source = manager.begin_upload(upload_title(request.headers.get("x-saveme-filename")))
        try:
            await receive_upload(request, source.workdir / UPLOAD_NAME, size,
                                 idle=upload_idle_seconds, limit=settings.max_job_seconds,
                                 is_open=lambda: source.status == "uploading")
            manager.finish_upload(source)
        except BaseException:
            manager.abort_upload(source)
            raise
        return {"source_id": source.id, "status": source.status}

    @router.get("/sources/{source_id}")
    async def source_status(source_id: str):
        manager = jobs()
        source = manager.get_source(source_id)
        manager.touch(source)
        return manager.source_view(source)

    @router.get("/sources/{source_id}/media")
    async def source_media(source_id: str):
        path, media_type = jobs().source_media(source_id)
        # Range requests (seeking) are answered by FileResponse with 206.
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.post("/sources/{source_id}/clips", status_code=202)
    async def create_clip(source_id: str, body: ClipRequest):
        clip = jobs().submit_clip(source_id, body.start, body.end)
        return {"job_id": clip.id, "status": clip.status}

    @router.post("/sources/{source_id}/batch", status_code=202)
    async def create_batch(source_id: str, body: BatchRequest):
        batch = jobs().submit_batch(source_id, [(clip.start, clip.end) for clip in body.clips])
        return {"job_id": batch.id, "status": batch.status}

    @router.post("/sources/{source_id}/close")
    async def close_source(source_id: str):
        manager = jobs()
        return manager.source_view(manager.close_source(source_id))
