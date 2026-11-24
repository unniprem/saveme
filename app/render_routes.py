"""Auto Shorts API routes (docs/AUTO_SHORTS_DESIGN.md §8).

GET  /api/shorts/options                          presets, encoder, limits, availability
POST /api/shorts/validate                         check a spec (a saved or imported preset)
POST /api/sources/{id}/shorts/captions            the automatic cues for a range
POST /api/sources/{id}/shorts/assets?kind=…       upload a logo (PNG) or an intro/outro video
POST /api/sources/{id}/shorts/previews            a preview still or low-res draft (202)
GET  /api/shorts/previews/{id}                    follow it
GET  /api/shorts/previews/{id}/file               the JPEG or MP4
POST /api/sources/{id}/renders                    render 1–20 clips (202)
GET  /api/renders/{id}                            follow it: per clip stage, measured %, estimated time left
POST /api/renders/{id}/cancel                     cancel the render
POST /api/renders/{id}/clips/{n}/cancel           cancel one clip
GET  /api/renders/{id}/clips/{n}/file?kind=…      a finished clip's MP4 (or SRT/ASS); inline=true to play it
GET  /api/renders/{id}/zip?subtitles=…            every finished clip in one ZIP

Render and preview bodies may carry edited captions for every clip, more than the Guard's 64 KiB
cap, so those routes (and the asset upload) read their own bodies, with limits of their own.
"""
import re
from collections.abc import Callable
from typing import Literal

import anyio
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from app.errors import AppError, size_text
from app.filenames import content_disposition
from app.render_jobs import RenderManager
from app.render_spec import PreviewBody, RenderBody, RenderSpec
from app.studio_routes import receive_upload

MAX_JSON_BYTES = 2 * 1024 * 1024
_ID = r"[A-Za-z0-9_-]{22}"
OWN_BODY_PATHS = re.compile(rf"/api/sources/{_ID}/(renders|shorts/previews|shorts/assets)")
_ASSET_TYPES = frozenset({"application/octet-stream", "image/png", "video/mp4", "video/quicktime", "video/webm",
                          "video/x-matroska"})


class CaptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: float = Field(strict=True, ge=0, allow_inf_nan=False)
    end: float = Field(strict=True, gt=0, allow_inf_nan=False)
    spec: RenderSpec = RenderSpec()




class _ValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: RenderSpec


async def read_json_body(request: Request, *, limit: int, idle: float) -> bytes:
    """The whole body (JSON), refusing more than `limit` bytes and timing out when nothing arrives for
    `idle` seconds. Chunked bodies are counted as they arrive."""
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise AppError("invalid_request", "Send the request as application/json.", status=415)
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise AppError("request_too_large")
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
                raise AppError("request_too_large")
    finally:
        await stream.aclose()
    return bytes(body)


def _parse(model, data: bytes):
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from None


def add_render_routes(router: APIRouter, render: Callable[[], RenderManager], *, idle_seconds: float) -> None:
    @router.get("/shorts/options")
    async def shorts_options():
        return render().options()

    @router.post("/shorts/validate")
    async def validate_spec(request: Request):
        """{"valid": true, "spec": <normalised>} or {"valid": false, "error": {...}}: a question about a
        preset, so a bad one is an answer (200), not a failed request."""
        data = await read_json_body(request, limit=64 * 1024, idle=idle_seconds)
        try:
            body = _parse(_ValidateBody, data)
        except RequestValidationError as exc:
            from app.main import _validation_message

            return {"valid": False, "error": {"code": "invalid_request",
                                              "message": _validation_message(exc.errors())}}
        return {"valid": True, "spec": body.spec.model_dump()}

    @router.post("/sources/{source_id}/shorts/captions")
    async def captions(source_id: str, body: CaptionsRequest):
        if body.end <= body.start:
            raise AppError("invalid_clip", "The end must be after the start.")
        return render().captions(source_id, body.start, body.end, body.spec)

    @router.post("/sources/{source_id}/shorts/assets")
    async def upload_asset(source_id: str, request: Request, kind: Literal["logo", "intro", "outro"]):
        manager = render()
        limit = manager.asset_limits(kind)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type not in _ASSET_TYPES:
            raise AppError("invalid_request", "Send the file as application/octet-stream.", status=415)
        length = request.headers.get("content-length", "")
        if not length.isdigit() or "transfer-encoding" in request.headers:
            raise AppError("invalid_request", "The upload needs a Content-Length header.", status=411)
        size = int(length)
        if size == 0:
            raise AppError("invalid_request", "The file is empty.")
        if size > limit:
            raise AppError("too_large", f"This file is larger than the {size_text(limit)} limit for a {kind}.")
        source, path, asset_id = manager.begin_asset(source_id, kind, size)
        try:
            await receive_upload(request, path, size, idle=idle_seconds, limit=600.0,
                                 is_open=lambda: source.status == "ready")
            asset = await manager.finish_asset(source, path, asset_id, kind)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            manager.end_asset(source, asset_id)
        return manager.asset_view(asset)

    @router.post("/sources/{source_id}/shorts/previews", status_code=202)
    async def start_preview(source_id: str, request: Request):
        body = _parse(PreviewBody, await read_json_body(request, limit=MAX_JSON_BYTES, idle=idle_seconds))
        preview = render().start_preview(source_id, body)
        return {"preview_id": preview.id, "status": preview.status}

    @router.get("/shorts/previews/{preview_id}")
    async def preview_view(preview_id: str):
        manager = render()
        return manager.preview_view(manager.get_preview(preview_id))

    @router.get("/shorts/previews/{preview_id}/file")
    async def preview_file(preview_id: str):
        path, media_type = render().preview_file(preview_id)
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.post("/sources/{source_id}/renders", status_code=202)
    async def start_render(source_id: str, request: Request):
        body = _parse(RenderBody, await read_json_body(request, limit=MAX_JSON_BYTES, idle=idle_seconds))
        manager = render()
        job = manager.start_render(source_id, body)
        return {"render_id": job.id, "status": job.status}

    @router.get("/renders/{render_id}")
    async def render_view(render_id: str):
        manager = render()
        return manager.render_view(manager.get_render(render_id))

    @router.post("/renders/{render_id}/cancel")
    async def cancel_render(render_id: str):
        manager = render()
        return manager.render_view(manager.cancel_render(render_id))

    @router.post("/renders/{render_id}/clips/{number}/cancel")
    async def cancel_clip(render_id: str, number: int):
        manager = render()
        return manager.render_view(manager.cancel_clip(render_id, number))

    @router.get("/renders/{render_id}/clips/{number}/file")
    async def clip_file(render_id: str, number: int, kind: Literal["mp4", "srt", "ass"] = "mp4",
                        inline: bool = False):
        path, media_type, name = render().clip_file(render_id, number, kind)
        disposition = content_disposition(name)
        if inline and kind == "mp4":
            disposition = "inline" + disposition[len("attachment"):]
        # Range requests (seeking in the page's player) are answered by FileResponse with 206.
        return FileResponse(path, media_type=media_type,
                            headers={"Content-Disposition": disposition, "Cache-Control": "no-store"})

    @router.get("/renders/{render_id}/zip")
    async def render_zip(render_id: str, subtitles: bool = True):
        path, name = await render().zip_file(render_id, subtitles)
        return FileResponse(path, media_type="application/zip",
                            headers={"Content-Disposition": content_disposition(name), "Cache-Control": "no-store"})
