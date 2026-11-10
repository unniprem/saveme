"""HTTP API: the page, inspect, jobs and streamed file delivery.

Every HTTP request first passes _Guard, a pure ASGI middleware (so streamed
downloads and client disconnects go straight through it). It:
- adds the security headers to every response, error responses included;
- answers 400 when the Host header's host (port and case ignored) isn't in ALLOWED_HOSTS;
- answers 403 forbidden_origin when a POST carries an Origin other than the origin the
  request itself was sent to: its scheme, the Host header's host, and the effective port
  (no Origin header is fine: curl and scripts don't send one);
- answers 503 config_error for /api/* while the app is degraded;
- reads a POST body itself, before any JSON parsing, and answers 413 request_too_large
  once it passes MAX_BODY_BYTES (Content-Length or not);
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
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import ConfigError, Settings, load_settings
from app.errors import CATALOG, AppError
from app.filenames import content_disposition
from app.jobs import APP_DIR, JobManager
from app.netguard import (IpPolicy, Resolver, check_source_address, check_url_syntax, is_public_ip,
                          system_resolver)
from app.runner import DEFAULT_WORKER_CMD, RunWorker, run_worker
from app.schemas import InspectRequest, JobRequest, Output, Quality

logger = logging.getLogger(__name__)

STATIC_DIR = APP_DIR / "static"
CHUNK_SIZE = 1024 * 1024
# A download that takes nothing for this long, or runs longer than the second limit,
# is cut off (the file stays for another try), so a slow client can't hold a file.
DELIVERY_IDLE_SECONDS = 60.0
DELIVERY_MAX_SECONDS = 3600.0
# The API's bodies are a link and two choices; this leaves room for pasted share text.
MAX_BODY_BYTES = 64 * 1024

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
_AUTHORITY = re.compile(r"(?:\[(?P<v6>[^\[\]]+)\]|(?P<name>[^:\[\]]+))(?::(?P<port>\d{1,5}))?")
_DEFAULT_PORTS = {"http": 80, "https": 443}
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


def _host_and_port(authority: str) -> tuple[str, int | None] | None:
    """The lowercased host (without IPv6 brackets) and port of a `host[:port]`."""
    match = _AUTHORITY.fullmatch(authority.strip())
    if match is None:
        return None
    port = match["port"]
    return (match["v6"] or match["name"]).lower(), None if port is None else int(port)


def _host_of(authority: str) -> str | None:
    """The lowercased host of a Host-header style `host[:port]`, without IPv6 brackets."""
    parsed = _host_and_port(authority)
    return None if parsed is None else parsed[0]


def _origin(scheme: str, authority: str) -> tuple[str, str, int] | None:
    """(scheme, host, effective port) for an http(s) scheme and a `host[:port]`."""
    scheme = scheme.lower()
    parsed = _host_and_port(authority)
    if scheme not in _DEFAULT_PORTS or parsed is None:
        return None
    host, port = parsed
    return scheme, host, _DEFAULT_PORTS[scheme] if port is None else port


def _origin_of_header(origin: str) -> tuple[str, str, int] | None:
    """The parts of an `http(s)://host[:port]` Origin; None for `null` or anything else."""
    scheme, sep, authority = origin.partition("://")
    return _origin(scheme, authority) if sep else None


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
        if refusal is None and scope["method"] == "POST":
            refusal, receive = await self._read_body(scope, receive)
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

    @staticmethod
    async def _read_body(scope: Scope, receive: Receive) -> tuple[Response | None, Receive]:
        """Read the whole body up to MAX_BODY_BYTES; a refusal past that, else a receive
        that replays it. Chunked bodies are counted as they arrive, so no Content-Length
        is needed for the limit to hold."""
        too_large = _app_error_response(AppError("request_too_large"))
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return too_large, receive
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":  # the client left: let the app see it
                return None, _replay([message], receive)
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return too_large, receive
            chunks.append(chunk)
            if not message.get("more_body", False):
                body = {"type": "http.request", "body": b"".join(chunks), "more_body": False}
                return None, _replay([body], receive)

    def _refusal(self, scope: Scope) -> Response | None:
        headers = Headers(scope=scope)
        # GET /health skips the host check: the container healthcheck sends Host
        # 127.0.0.1:8000 whatever ALLOWED_HOSTS says, and it reveals only the status
        # or the configuration message.
        is_health_check = scope["method"] == "GET" and scope["path"] == "/health"
        if not is_health_check and _host_of(headers.get("host", "")) not in self.allowed:
            return _error_response(400, "invalid_request", "Invalid host header.")
        # The Origin must be the page's own origin, not just an allowed host: another
        # service on the same host but another port or scheme is a different origin.
        origin = headers.get("origin")
        if scope["method"] == "POST" and origin is not None:
            own = _origin(scope.get("scheme", "http"), headers.get("host", ""))
            if own is None or _origin_of_header(origin) != own:
                return _app_error_response(AppError("forbidden_origin"))
        if scope["path"].startswith("/api/"):
            error = self.unavailable()
            if error is not None:
                return _app_error_response(error)
        return None


def _replay(messages: list[Message], receive: Receive) -> Receive:
    """A receive that hands out `messages` first, then defers to `receive`."""
    pending = list(messages)

    async def replay() -> Message:
        return pending.pop(0) if pending else await receive()

    return replay


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


class _FileDelivery(Response):
    """Sends a ready file to one client, then ends its delivery claim.

    Claiming, opening, sending and releasing all happen in __call__, inside one
    try/finally, so no claim outlives its request. While the claim is held, a second
    download gets 409 delivery_in_progress and the file stays on disk and counted (see
    JobManager). The job expires as delivered once the last byte is handed over. A client
    that hangs up, takes nothing for `idle` seconds, or is still downloading after
    `limit` seconds is cut off, and the file stays ready for another try until its TTL.
    A Range header is ignored: the reply is always the whole file with 200 and no
    Accept-Ranges, so a response that runs to the end is a complete delivery.
    """

    def __init__(self, jobs: JobManager, job_id: str, *, idle: float, limit: float) -> None:
        self.jobs, self.job_id, self.idle, self.limit = jobs, job_id, idle, limit
        self.background = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            job = self.jobs.claim_delivery(self.job_id)
        except AppError as exc:
            await _app_error_response(exc)(scope, receive, send)
            return
        complete = False
        try:
            path, filename = job.file_path, job.filename
            try:
                if path is None or filename is None:
                    raise FileNotFoundError
                file = open(path, "rb")
            except FileNotFoundError:
                await _app_error_response(AppError("expired"))(scope, receive, send)
                return
            with file:
                async with anyio.create_task_group() as tasks:
                    async def until_disconnect() -> None:
                        while (await receive())["type"] != "http.disconnect":
                            pass
                        tasks.cancel_scope.cancel()  # the client hung up: stop sending

                    tasks.start_soon(until_disconnect)
                    complete = await self._send_file(job, file, filename, send)
                    tasks.cancel_scope.cancel()
        finally:
            self.jobs.finish_delivery(job, complete=complete)

    async def _send_file(self, job, file, filename: str, send: Send) -> bool:
        """True once every byte is handed over; False if the client was too slow or the
        file shrank."""
        size = os.fstat(file.fileno()).st_size
        self.status_code, self.media_type = 200, job.media_type
        self.init_headers({"Content-Length": str(size),
                           "Content-Disposition": content_disposition(filename)})
        deadline = anyio.current_time() + self.limit
        await send({"type": "http.response.start", "status": 200, "headers": self.raw_headers})
        remaining = size
        while True:
            chunk = b""
            if remaining > 0:
                chunk = await anyio.to_thread.run_sync(file.read, min(CHUNK_SIZE, remaining))
                if not chunk:  # the file shrank: not a complete delivery
                    return False
                remaining -= len(chunk)
            sent = False
            left = deadline - anyio.current_time()
            if left > 0:
                with anyio.move_on_after(min(self.idle, left)):
                    await send({"type": "http.response.body", "body": chunk,
                                "more_body": remaining > 0})
                    sent = True
            if not sent:
                late = anyio.current_time() >= deadline
                logger.warning("job %s: stopped sending the file to a client that %s",
                               job.id, "took too long" if late else "stopped reading")
                return False
            if remaining == 0:
                return True


def _api_router(settings: Settings, runtime: _Runtime, *, worker_cmd: tuple[str, ...],
                ip_policy: IpPolicy, resolver: Resolver, run_worker: RunWorker,
                delivery_idle_seconds: float, delivery_max_seconds: float) -> APIRouter:
    router = APIRouter(prefix="/api")
    inspections = asyncio.Semaphore(settings.max_active_inspections)
    checking_jobs = 0  # job requests whose links are being resolved

    # The cheap syntax check comes first, so a malformed link never takes a slot. The DNS
    # lookup runs inside the slot (or queue place), so the caps bound lookups too.

    @router.post("/inspect")
    async def inspect(body: InspectRequest):
        url = check_url_syntax(body.url)
        if inspections.locked():
            raise AppError("busy")
        async with inspections:  # not locked, so this acquires without waiting
            await check_source_address(url, policy=ip_policy, resolver=resolver)
            return await run_worker({"mode": "inspect", "url": url}, cmd=worker_cmd,
                                    timeout=settings.inspect_timeout_seconds)

    @router.post("/jobs", status_code=202)
    async def create_job(body: JobRequest):
        nonlocal checking_jobs
        url = check_url_syntax(body.url)
        jobs = runtime.jobs()
        jobs.ensure_queue_room(reserved=checking_jobs)
        checking_jobs += 1
        try:
            await check_source_address(url, policy=ip_policy, resolver=resolver)
        finally:
            checking_jobs -= 1
        job = jobs.submit(url, body.output, body.quality)
        return {"job_id": job.id, "status": job.status}

    @router.get("/jobs/{job_id}")
    async def job_status(job_id: str):
        jobs = runtime.jobs()
        return jobs.public_view(jobs.get(job_id))

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        jobs = runtime.jobs()
        return jobs.public_view(jobs.cancel(job_id))

    @router.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: str):
        jobs = runtime.jobs()
        return jobs.public_view(jobs.resume(job_id))

    @router.get("/jobs/{job_id}/file")
    async def job_file(job_id: str):
        return _FileDelivery(runtime.jobs(), job_id, idle=delivery_idle_seconds,
                             limit=delivery_max_seconds)

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
    delivery_idle_seconds: float = DELIVERY_IDLE_SECONDS,
    delivery_max_seconds: float = DELIVERY_MAX_SECONDS,
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
                                       run_worker=run_worker,
                                       delivery_idle_seconds=delivery_idle_seconds,
                                       delivery_max_seconds=delivery_max_seconds))
    return app


def _settings_or_error() -> Settings | ConfigError:
    try:
        return load_settings(os.environ)
    except ConfigError as exc:
        return exc


app = create_app(_settings_or_error())
