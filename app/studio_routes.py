"""Clip Studio API routes (spec §6.3): sources, their preview, and clips.

A source is a video kept on the server while a studio is open; a clip is a job cut out
of it, then followed, cancelled and saved through the existing /api/jobs routes.
"""
from collections.abc import Awaitable, Callable

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.config import Settings
from app.jobs import MIN_CLIP_SECONDS, JobManager
from app.netguard import check_url_syntax
from app.schemas import ClipRequest, SourceRequest

CheckLink = Callable[[str, JobManager], Awaitable[None]]


def add_studio_routes(router: APIRouter, settings: Settings, jobs: Callable[[], JobManager], *,
                      check_link: CheckLink) -> None:
    """Add the studio routes to the /api router. `check_link` resolves a link's host inside
    a held queue place, exactly as for POST /api/jobs."""

    @router.get("/studio")
    async def studio_limits():
        return {"max_upload_bytes": settings.max_file_bytes,
                "idle_minutes": settings.job_ttl_minutes,
                "min_clip_seconds": MIN_CLIP_SECONDS}

    @router.post("/sources", status_code=202)
    async def open_source(body: SourceRequest):
        url = check_url_syntax(body.url)
        manager = jobs()
        await check_link(url, manager)
        source = manager.open_link_source(url, body.quality)
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

    @router.post("/sources/{source_id}/close")
    async def close_source(source_id: str):
        manager = jobs()
        return manager.source_view(manager.close_source(source_id))
