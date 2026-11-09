"""HTTP API: the page, inspect, jobs and streamed file delivery.

Every HTTP request first passes _Guard, a pure ASGI middleware (so streamed
downloads and client disconnects go straight through it). It:
- adds the security headers to every response, error responses included;
- answers 400 when the Host header's host (port and case ignored) isn't in ALLOWED_HOSTS;
- answers 403 forbidden_origin when a POST carries an Origin whose host isn't allowed
  (no Origin header is fine: curl and scripts don't send one);
- answers 503 config_error for /api/* while the app is degraded;
- turns an unexpected exception into a 500 `internal` body; the traceback goes to the log only.

Degraded mode: when the settings don't load (or TEMP_DIR is refused at startup) the
app keeps serving the page and /health but no API, logging one line that names the
bad variable, so `restart: unless-stopped` can't crash-loop.
"""
import asyncio
import logging
import os
import re
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import get_args

import anyio.to_thread
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import ConfigError, Settings, load_settings
from app.errors import CATALOG, AppError
from app.filenames import content_disposition, media_type_for
from app.jobs import APP_DIR, JobManager
from app.netguard import IpPolicy, Resolver, is_public_ip, system_resolver, validate_source_url
from app.runner import DEFAULT_WORKER_CMD, RunWorker, run_worker
from app.schemas import InspectRequest, JobRequest, Output, Quality

logger = logging.getLogger(__name__)

STATIC_DIR = APP_DIR / "static"
CHUNK_SIZE = 1024 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' https: http: data:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# Degraded by a settings error: the configured hosts are unknown, so use the defaults.
_DEFAULT_ALLOWED_HOSTS = load_settings({}).allowed_hosts
# `host`, `host:port`, `[v6]` or `[v6]:port`.
_AUTHORITY = re.compile(r"(?:\[(?P<v6>[^\[\]]+)\]|(?P<name>[^:\[\]]+))(?::\d{1,5})?")
_HTTP_MESSAGES = {404: "Not found.", 405: "Method not allowed."}
_CHOICES = {"output": get_args(Output), "quality": get_args(Quality)}
_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


# --- Errors -------------------------------------------------------------------

def _error_response(status: int, code: str, message: str,
                    headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}},
                        status_code=status, headers=headers)


def _app_error_response(exc: AppError) -> JSONResponse:
    return _error_response(exc.status, exc.code, exc.message)


def _config_message(exc: ConfigError) -> str:
    return f"{exc.var} {exc.message.rstrip('.')}."


def _validation_message(errors: Sequence[dict]) -> str:
    """A plain message naming the offending field, from FastAPI's first validation error."""
    if not errors:
        return CATALOG["invalid_request"][1]
    error = errors[0]
    kind = error.get("type")
    if kind == "json_invalid":
        return "The request body isn't valid JSON."
    field = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
    if not field:
        return "The request body must be a JSON object."
    if kind == "extra_forbidden":  # the only case where the field name comes from the client
        if _FIELD_NAME.fullmatch(field):
            return f"{field} is not an accepted field"
        return "The request has a field that isn't accepted"
    if kind == "literal_error" and field in _CHOICES:
        return f"{field} must be one of: {', '.join(_CHOICES[field])}"
    if kind == "missing":
        return f"{field} is required"
    if kind == "string_type":
        return f"{field} must be a string"
    return f"{field} is not valid"


async def _on_app_error(request: Request, exc: AppError) -> JSONResponse:
    return _app_error_response(exc)


async def _on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _error_response(400, "invalid_request", _validation_message(exc.errors()))


async def _on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Unknown routes, wrong methods and missing static files, in the standard error shape."""
    if exc.status_code >= 500:
        code, message = "internal", CATALOG["internal"][1]
    else:
        code = "invalid_request"
        message = _HTTP_MESSAGES.get(exc.status_code, CATALOG["invalid_request"][1])
    return _error_response(exc.status_code, code, message, headers=exc.headers)


# --- Host, origin and headers -------------------------------------------------------

def _normalize_host(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def _host_of(authority: str) -> str | None:
    """The lowercased host of a Host-header style `host[:port]`, without IPv6 brackets."""
    match = _AUTHORITY.fullmatch(authority.strip())
    if match is None:
        return None
    return (match["v6"] or match["name"]).lower()


def _origin_host(origin: str) -> str | None:
    """The host of an `http(s)://host[:port]` Origin; None for `null` or anything else."""
    scheme, sep, authority = origin.partition("://")
    if not sep or scheme.lower() not in ("http", "https"):
        return None
    return _host_of(authority)


class _Guard:
    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str],
                 unavailable: Callable[[], AppError | None]) -> None:
        self.app = app
        self.allowed = frozenset(_normalize_host(h) for h in allowed_hosts)
        self.unavailable = unavailable

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        refusal = self._refusal(scope)
        if refusal is not None:
            await refusal(scope, receive, send_with_headers)
            return
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if started:  # mid-stream: nothing more can be sent; the server logs and aborts
                raise
            logger.exception("unexpected error handling %s %s", scope["method"], scope["path"])
            await _app_error_response(AppError("internal"))(scope, receive, send_with_headers)

    def _refusal(self, scope: Scope) -> Response | None:
        headers = Headers(scope=scope)
        if _host_of(headers.get("host", "")) not in self.allowed:
            return _error_response(400, "invalid_request", "Invalid host header.")
        origin = headers.get("origin")
        if (scope["method"] == "POST" and origin is not None
                and _origin_host(origin) not in self.allowed):
            return _app_error_response(AppError("forbidden_origin"))
        if scope["path"].startswith("/api/"):
            error = self.unavailable()
            if error is not None:
                return _app_error_response(error)
        return None


# --- Runtime state and delivery ---------------------------------------------------

class _Runtime:
    """The job manager once started, or the configuration error that stopped it."""

    def __init__(self, config_error: ConfigError | None) -> None:
        self.config_error = config_error
        self.manager: JobManager | None = None

    def unavailable(self) -> AppError | None:
        if self.manager is not None:
            return None
        message = None if self.config_error is None else _config_message(self.config_error)
        return AppError("config_error", message)

    def jobs(self) -> JobManager:
        if self.manager is None:
            raise self.unavailable()
        return self.manager


def _deliver(jobs: JobManager, job_id: str) -> StreamingResponse:
    """Stream a ready file; the job expires as delivered once the last byte is handed over.

    The file is opened right after file_for_delivery, with no await in between, so a
    TTL sweep or eviction can't remove it first; once it is open, a later unlink can't
    break the stream. A Range header is ignored: the reply is always the whole file
    with 200 and no Accept-Ranges, so a response that runs to the end is a complete
    delivery. If the client hangs up, the loop never finishes and the file stays until
    its TTL.
    """
    job = jobs.file_for_delivery(job_id)
    path, filename = job.file_path, job.filename
    if path is None or filename is None:
        raise AppError("expired")
    media_type = media_type_for(path.suffix.removeprefix("."))
    try:
        file = open(path, "rb")
    except FileNotFoundError:
        raise AppError("expired") from None
    size = os.fstat(file.fileno()).st_size

    async def body():
        remaining = size
        try:
            while remaining > 0:
                chunk = await anyio.to_thread.run_sync(file.read, min(CHUNK_SIZE, remaining))
                if not chunk:  # the file shrank: not a complete delivery
                    return
                remaining -= len(chunk)
                yield chunk
        finally:
            file.close()
        jobs.mark_delivered(job_id)

    return StreamingResponse(body(), media_type=media_type, headers={
        "Content-Length": str(size),
        "Content-Disposition": content_disposition(filename),
    })


def _api_router(settings: Settings, runtime: _Runtime, *, worker_cmd: tuple[str, ...],
                ip_policy: IpPolicy, resolver: Resolver, run_worker: RunWorker) -> APIRouter:
    router = APIRouter(prefix="/api")
    inspections = asyncio.Semaphore(settings.max_active_inspections)

    @router.post("/inspect")
    async def inspect(body: InspectRequest):
        # Validate first, so a bad or blocked link never takes an inspection slot.
        url = await validate_source_url(body.url, policy=ip_policy, resolver=resolver)
        if inspections.locked():
            raise AppError("busy")
        async with inspections:  # not locked, so this acquires without waiting
            return await run_worker({"mode": "inspect", "url": url}, cmd=worker_cmd,
                                    timeout=settings.inspect_timeout_seconds)

    @router.post("/jobs", status_code=202)
    async def create_job(body: JobRequest):
        url = await validate_source_url(body.url, policy=ip_policy, resolver=resolver)
        job = runtime.jobs().submit(url, body.output, body.quality)
        return {"job_id": job.id, "status": job.status}

    @router.get("/jobs/{job_id}")
    async def job_status(job_id: str):
        jobs = runtime.jobs()
        return jobs.public_view(jobs.get(job_id))

    @router.get("/jobs/{job_id}/file")
    async def job_file(job_id: str):
        return _deliver(runtime.jobs(), job_id)

    return router


# --- App factory --------------------------------------------------------------------

def create_app(
    settings: Settings | ConfigError,
    *,
    worker_cmd: Sequence[str] = DEFAULT_WORKER_CMD,
    ip_policy: IpPolicy = is_public_ip,
    resolver: Resolver = system_resolver,
    run_worker: RunWorker = run_worker,
    static_dir: Path = STATIC_DIR,
    sweep_interval: float = 60.0,
) -> FastAPI:
    worker_cmd = tuple(worker_cmd)
    healthy_settings = settings if isinstance(settings, Settings) else None
    runtime = _Runtime(settings if healthy_settings is None else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if healthy_settings is not None:
            manager = JobManager(healthy_settings, run_worker=run_worker, worker_cmd=worker_cmd,
                                 sweep_interval=sweep_interval)
            try:
                await manager.start()
            except ConfigError as exc:  # TEMP_DIR refused
                runtime.config_error = exc
            else:
                runtime.manager = manager
        if runtime.config_error is not None:
            logger.error("Invalid configuration: %s saveme is running in degraded mode "
                         "(the API answers 503) until this is fixed and saveme is restarted.",
                         _config_message(runtime.config_error))
        try:
            yield
        finally:
            manager, runtime.manager = runtime.manager, None
            if manager is not None:
                await manager.shutdown()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
        # FastAPI's built-in OpenTelemetry stays off, whatever the environment says.
        telemetry={"auto_configure": False, "tracing": False, "metrics": False,
                   "logs": False, "operation_spans": False},
    )
    app.add_middleware(
        _Guard,
        allowed_hosts=(_DEFAULT_ALLOWED_HOSTS if healthy_settings is None
                       else healthy_settings.allowed_hosts),
        unavailable=runtime.unavailable,
    )
    app.add_exception_handler(AppError, _on_app_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)

    @app.get("/")
    async def index():
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @app.get("/health")
    async def health():
        error = runtime.unavailable()
        if error is None:
            return {"status": "ok"}
        return JSONResponse({"status": "error", "error": error.to_dict()}, status_code=error.status)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if healthy_settings is not None:
        app.include_router(_api_router(healthy_settings, runtime, worker_cmd=worker_cmd,
                                       ip_policy=ip_policy, resolver=resolver,
                                       run_worker=run_worker))
    return app


def _settings_or_error() -> Settings | ConfigError:
    try:
        return load_settings(os.environ)
    except ConfigError as exc:
        return exc


app = create_app(_settings_or_error())
