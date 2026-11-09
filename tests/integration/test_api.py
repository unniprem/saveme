"""HTTP API: routes, streamed delivery, host/origin checks, headers, degraded mode.

Most tests drive the app in-process through httpx's ASGITransport (lifespan run
explicitly) with StubWorker standing in for run_worker. The real-worker tests run
the test worker entry against the local media server; the aborted-download test
runs a real uvicorn server so a client can hang up mid-stream.
"""
import asyncio
import dataclasses
import logging
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from ipaddress import ip_address
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.config import ConfigError, Settings, load_settings
from app.errors import CATALOG, AppError
from app.jobs import JOB_ID_RE, MARKER_NAME
from app.main import STATIC_DIR, create_app
from app.netguard import is_public_ip, system_resolver
from tests.support.policy import allow_loopback

TEST_WORKER_CMD = (sys.executable, "-P", "-m", "tests.support.worker_entry")
URL = "http://127.0.0.1:9/watch?v=1"
MiB = 1024 * 1024
CSP = ("default-src 'self'; img-src 'self' https: http: data:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")
FOOTER = ("Only download media you have the right to save. Works with many sites, not all "
          "— compatibility depends on the source.")
INSPECT_RESULT = {"title": "Stub", "video": {"available": True,
                                              "qualities": [{"id": "best", "label": "Best"}]},
                  "audio": {"available": True}}


# --- Helpers ---------------------------------------------------------------

class StubWorker:
    """Stand-in for run_worker. Records (request, cmd, timeout); waits on `gate`
    when `block`; then raises `error`, or for a download writes `file_bytes`
    random bytes as media.mp4, or returns INSPECT_RESULT."""

    def __init__(self, *, error: BaseException | None = None, block: bool = False,
                 file_bytes: int = 4096):
        self.error = error
        self.block = block
        self.file_bytes = file_bytes
        self.gate = asyncio.Event()
        self.calls: list[tuple[dict, tuple, float]] = []

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None,
                       max_workdir_bytes=None, poll_interval=1.0):
        self.calls.append((request, tuple(cmd), timeout))
        if self.block:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if request["mode"] == "download":
            (workdir / "media.mp4").write_bytes(os.urandom(self.file_bytes))
            return {"file": "media.mp4", "ext": "mp4", "title": "Stub Video", "notice": None}
        return INSPECT_RESULT


async def literal_resolver(host: str, port: int) -> list:
    return [ip_address(host)]


def make_app(settings: Settings | ConfigError, **overrides):
    options = {"ip_policy": allow_loopback, "resolver": literal_resolver,
               "worker_cmd": TEST_WORKER_CMD}
    options.update(overrides)
    return create_app(settings, **options)


@asynccontextmanager
async def running(app):
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


async def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def poll_terminal(client: httpx.AsyncClient, job_id: str, timeout: float = 30.0,
                        seen: list | None = None) -> dict:
    """Poll the job until it is ready, failed or expired; return its view."""
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/jobs/{job_id}")
        if seen is not None:
            seen.append(r)
        assert r.status_code == 200, r.text
        view = r.json()
        if view["status"] in ("ready", "failed", "expired"):
            return view
        assert time.monotonic() < deadline, f"job not finished after {timeout}s: {view}"
        await asyncio.sleep(0.05)


async def ready_job(client: httpx.AsyncClient, url: str = URL) -> str:
    r = await client.post("/api/jobs", json={"url": url, "output": "video", "quality": "best"})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    view = await poll_terminal(client, job_id)
    assert view["status"] == "ready", view
    return job_id


def error_body(code: str, message: str | None = None) -> dict:
    return {"error": {"code": code, "message": message or CATALOG[code][1]}}


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "media-jobs"


@pytest.fixture
def settings(temp_dir: Path) -> Settings:
    return load_settings({"TEMP_DIR": str(temp_dir)})


@pytest.fixture
def stub() -> StubWorker:
    return StubWorker()


@pytest.fixture
async def client(settings, stub):
    async with running(make_app(settings, run_worker=stub)) as c:
        yield c


# --- Health, page, static ---------------------------------------------------

async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.content == b'{"status":"ok"}'  # the Docker smoke test compares this exactly
    assert r.headers["content-type"] == "application/json"


async def test_index_page_contract(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    html = r.text
    for needle in ('<label for="url"', 'id="url"', 'aria-live="polite"', FOOTER,
                   "/static/app.js"):
        assert needle in html, needle
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
    assert scripts
    for attrs, body in scripts:
        assert "src=" in attrs and body.strip() == "", (attrs, body)


async def test_app_js_served(client):
    r = await client.get("/static/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.content == (STATIC_DIR / "app.js").read_bytes()


@pytest.mark.parametrize("path", ["/", "/health", "/static/app.js", "/nope", "/api/jobs/x"])
async def test_security_headers_on_index(client, path):
    r = await client.get(path)
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


async def test_unknown_route_and_wrong_method_error_shape(client):
    for path in ("/nope", "/static/missing.js", "/api"):
        r = await client.get(path)
        assert r.status_code == 404, path
        assert r.json() == error_body("invalid_request", "Not found."), path
    r = await client.get("/api/inspect")
    assert r.status_code == 405
    assert r.json() == error_body("invalid_request", "Method not allowed.")
    assert "POST" in r.headers["allow"]
    r = await client.delete("/api/jobs/AAAAAAAAAAAAAAAAAAAAAA")
    assert r.status_code == 405
    assert r.json() == error_body("invalid_request", "Method not allowed.")


# --- Inspect ------------------------------------------------------------------

async def test_inspect_invalid_400_shape(client, stub):
    r = await client.post("/api/inspect", json={"url": "not a url"})
    assert r.status_code == 400
    body = r.json()
    assert list(body) == ["error"] and sorted(body["error"]) == ["code", "message"]
    assert body["error"]["code"] == "invalid_url"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert stub.calls == []


@pytest.mark.parametrize("url", ["http://169.254.169.254/", "http://127.0.0.1:8000/",
                                 "http://[::1]/"])
async def test_inspect_blocked_403(settings, stub, url):
    app = make_app(settings, ip_policy=is_public_ip, resolver=system_resolver, run_worker=stub)
    async with running(app) as client:
        r = await client.post("/api/inspect", json={"url": url})
    assert r.status_code == 403
    assert r.json() == error_body("blocked_address")
    assert stub.calls == []


async def test_inspect_file_scheme_400(client, stub):
    r = await client.post("/api/inspect", json={"url": "file:///etc/passwd"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_url"
    assert stub.calls == []


@pytest.mark.parametrize("path, body", [
    ("/api/inspect", {"url": URL, "format": "bestvideo"}),
    ("/api/jobs", {"url": URL, "output": "video", "format": "bestvideo"}),
])
async def test_extra_fields_rejected(client, stub, path, body):
    r = await client.post(path, json=body)
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert "format" in error["message"]
    assert stub.calls == []


@pytest.mark.parametrize("body, message", [
    ({"url": URL, "output": "gif"}, "output must be one of: video, audio"),
    ({"url": URL, "output": "video", "quality": "4k"},
     "quality must be one of: best, 1080, 720, 480"),
    ({"url": URL}, "output is required"),
    ({"url": 7, "output": "video"}, "url must be a string"),
])
async def test_bad_enum_message_names_field(client, stub, body, message):
    r = await client.post("/api/jobs", json=body)
    assert r.status_code == 400
    assert r.json() == error_body("invalid_request", message)
    assert stub.calls == []


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b""])
async def test_malformed_body_400(client, content):
    r = await client.post("/api/inspect", content=content,
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


async def test_inspect_success(settings, media_server):
    async with running(make_app(settings)) as client:
        r = await client.post("/api/inspect", json={"url": media_server.url("/media/sample.mp4")})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["video"]["available"] is True and data["audio"]["available"] is True
    qualities = data["video"]["qualities"]
    assert qualities and qualities[0]["id"] == "best"
    assert all(set(q) == {"id", "label"} and isinstance(q["label"], str) for q in qualities)


# "A&#xD800;B": yt-dlp unescapes the entity to a lone surrogate, which JSON can't encode as UTF-8.
SURROGATE_PAGE = "/page?title_html=A%26%23xD800%3BB&video=/media/sample.mp4"


async def test_inspect_title_with_lone_surrogate(settings, media_server):
    async with running(make_app(settings)) as client:
        r = await client.post("/api/inspect", json={"url": media_server.url(SURROGATE_PAGE)})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "A\ufffdB"


async def test_job_title_with_lone_surrogate(settings, media_server):
    async with running(make_app(settings)) as client:
        r = await client.post("/api/jobs", json={"url": media_server.url(SURROGATE_PAGE),
                                                 "output": "video", "quality": "best"})
        assert r.status_code == 202, r.text
        view = await poll_terminal(client, r.json()["job_id"], timeout=30)
        assert view["status"] == "ready", view
        assert view["filename"] == "A\ufffdB.mp4"
        r = await client.get(f"/api/jobs/{view['job_id']}/file")
        assert r.status_code == 200
        assert r.headers["content-disposition"] == (
            'attachment; filename="AB.mp4"; filename*=UTF-8\'\'A%EF%BF%BDB.mp4')


async def test_inspect_busy_429(settings):
    stub = StubWorker(block=True)
    app = make_app(dataclasses.replace(settings, max_active_inspections=1), run_worker=stub)
    async with running(app) as client:
        first = asyncio.create_task(client.post("/api/inspect", json={"url": URL}))
        await wait_until(lambda: stub.calls)
        r = await client.post("/api/inspect", json={"url": URL})
        assert r.status_code == 429
        assert r.json() == error_body("busy")
        # URL validation comes first, so a bad link never waits for (or takes) a slot.
        r = await client.post("/api/inspect", json={"url": "not a url"})
        assert r.status_code == 400
        stub.gate.set()
        assert (await first).status_code == 200
        r = await client.post("/api/inspect", json={"url": URL})  # the slot was released
        assert r.status_code == 200
    assert len(stub.calls) == 2


async def test_inspect_timeout_504(settings):
    stub = StubWorker(error=AppError("timeout"))
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.post("/api/inspect", json={"url": URL})
    assert r.status_code == 504
    assert r.json() == error_body("timeout")
    assert stub.calls == [({"mode": "inspect", "url": URL}, TEST_WORKER_CMD,
                           settings.inspect_timeout_seconds)]


async def test_unexpected_exception_500_no_traceback(settings, caplog):
    stub = StubWorker(error=RuntimeError("secret /tmp/x"))
    async with running(make_app(settings, run_worker=stub)) as client:
        with caplog.at_level(logging.ERROR, logger="app.main"):
            r = await client.post("/api/inspect", json={"url": URL})
    assert r.status_code == 500
    assert r.json() == error_body("internal")
    for leak in ("secret", "/tmp/x", "Traceback"):
        assert leak not in r.text
    assert r.headers["content-security-policy"] == CSP
    logged = [rec for rec in caplog.records if rec.name == "app.main" and rec.exc_info]
    assert len(logged) == 1 and "secret /tmp/x" in str(logged[0].exc_info[1])


# --- Jobs and delivery ----------------------------------------------------------

async def test_job_end_to_end(settings, temp_dir, media_server):
    async with running(make_app(settings)) as client:
        r = await client.post("/api/jobs", json={"url": media_server.url("/media/sample.mp4"),
                                                 "output": "video", "quality": "best"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        assert r.json() == {"job_id": job_id, "status": "queued"}
        assert JOB_ID_RE.fullmatch(job_id)

        view = await poll_terminal(client, job_id, timeout=30)
        assert view["status"] == "ready", view
        expected = (temp_dir / job_id / "media.mp4").read_bytes()

        r = await client.get(f"/api/jobs/{job_id}/file")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        disposition = r.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert 'filename="' in disposition and "filename*=UTF-8''" in disposition
        assert r.headers["content-length"] == str(len(expected))
        assert r.content == expected

        view = (await client.get(f"/api/jobs/{job_id}")).json()
        assert (view["status"], view["reason"]) == ("expired", "delivered")
        assert not (temp_dir / job_id).exists()

        r = await client.get(f"/api/jobs/{job_id}/file")
        assert r.status_code == 410
        assert r.json() == error_body("expired")


async def test_unknown_job_is_404_job_not_found(client):
    for job_id in ("AAAAAAAAAAAAAAAAAAAAAA", "not-a-job", "A" * 23):
        for suffix in ("", "/file"):
            r = await client.get(f"/api/jobs/{job_id}{suffix}")
            assert r.status_code == 404, (job_id, suffix)
            assert r.json() == error_body("job_not_found")


async def test_file_not_ready_409(settings):
    stub = StubWorker(block=True)
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.post("/api/jobs", json={"url": URL, "output": "audio"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        await wait_until(lambda: stub.calls)
        r = await client.get(f"/api/jobs/{job_id}/file")
        assert r.status_code == 409
        assert r.json() == error_body("not_ready")
        assert stub.calls[0][0]["mode"] == "download"


async def test_range_request_gets_whole_file(settings, temp_dir):
    stub = StubWorker(file_bytes=2 * MiB + 5)  # several 1 MiB chunks plus a remainder
    async with running(make_app(settings, run_worker=stub)) as client:
        job_id = await ready_job(client)
        expected = (temp_dir / job_id / "media.mp4").read_bytes()
        r = await client.get(f"/api/jobs/{job_id}/file", headers={"Range": "bytes=0-9"})
        assert r.status_code == 200
        assert "content-range" not in r.headers and "accept-ranges" not in r.headers
        assert r.headers["content-length"] == str(len(expected))
        assert r.content == expected
        view = (await client.get(f"/api/jobs/{job_id}")).json()
        assert (view["status"], view["reason"]) == ("expired", "delivered")


async def test_no_temp_dir_in_any_response(settings, temp_dir, media_server):
    seen: list[httpx.Response] = []
    url = media_server.url("/media/sample.mp4")
    async with running(make_app(settings)) as client:
        seen.append(await client.post("/api/inspect", json={"url": url}))
        seen.append(await client.post("/api/inspect", json={"url": media_server.url("/missing")}))
        seen.append(r := await client.post("/api/jobs", json={"url": url, "output": "audio"}))
        job_id = r.json()["job_id"]
        view = await poll_terminal(client, job_id, seen=seen)
        assert view["status"] == "ready", view
        seen.append(await client.get(f"/api/jobs/{job_id}/file"))
        seen.append(await client.get(f"/api/jobs/{job_id}"))
        seen.append(await client.get(f"/api/jobs/{job_id}/file"))
        seen.append(await client.get("/api/jobs/AAAAAAAAAAAAAAAAAAAAAA"))
    assert [r.status_code for r in seen[-4:]] == [200, 200, 410, 404]
    assert seen[1].status_code == 422
    blob = b"".join(r.content + repr(r.headers.multi_items()).encode() for r in seen)
    for secret in {str(temp_dir), str(temp_dir.resolve())}:
        assert secret.encode() not in blob
    assert b"Traceback" not in blob


def _free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def live_server(app) -> Iterator[int]:
    """Serve `app` with a real uvicorn server in a background thread; yield its port."""
    sock = _free_socket()
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_config=None,
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(15)
        sock.close()


def test_aborted_download_keeps_file(settings, temp_dir):
    size = 3 * MiB
    app = make_app(settings, run_worker=StubWorker(file_bytes=size))
    with live_server(app) as port, httpx.Client(base_url=f"http://127.0.0.1:{port}",
                                               trust_env=False, timeout=10) as http:
        r = http.post("/api/jobs", json={"url": URL, "output": "video"})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        deadline = time.monotonic() + 10
        while http.get(f"/api/jobs/{job_id}").json()["status"] != "ready":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        path = temp_dir / job_id / "media.mp4"
        assert path.stat().st_size == size

        with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
            raw.sendall(f"GET /api/jobs/{job_id}/file HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n\r\n".encode())
            received = 0
            while received < 64 * 1024:
                chunk = raw.recv(64 * 1024)
                assert chunk, "server closed the connection early"
                received += len(chunk)
        time.sleep(1)

        view = http.get(f"/api/jobs/{job_id}").json()
        assert view["status"] == "ready", view
        assert path.is_file()

        # The file is still there for a complete download, which then removes it.
        r = http.get(f"/api/jobs/{job_id}/file")
        assert r.status_code == 200 and len(r.content) == size
        view = http.get(f"/api/jobs/{job_id}").json()
        assert (view["status"], view["reason"]) == ("expired", "delivered")
        assert not path.exists()


# --- Host and origin checks -----------------------------------------------------

@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000",
                                  "127.0.0.1.evil.example", "localhost.evil:8000",
                                  "evil.example@127.0.0.1", "[::1]:8000", "127.0.0.1:x", ""])
async def test_bad_host_header_400(client, stub, host):
    for method, path in (("GET", "/"), ("GET", "/static/app.js"), ("POST", "/api/inspect"),
                         ("GET", "/api/jobs/AAAAAAAAAAAAAAAAAAAAAA"),
                         # Only exactly GET /health skips the host check.
                         ("POST", "/health"), ("HEAD", "/health"), ("GET", "/health/"),
                         ("GET", "/healthz")):
        r = await client.request(method, path, headers={"Host": host}, json={"url": URL})
        assert r.status_code == 400, (host, method, path)
        if method != "HEAD":
            assert r.json() == error_body("invalid_request", "Invalid host header.")
        assert r.headers["content-security-policy"] == CSP
    assert stub.calls == []


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:x", ""])
async def test_health_skips_host_check(client, host):
    # The container healthcheck sends Host 127.0.0.1:8000 whatever ALLOWED_HOSTS says.
    r = await client.get("/health", headers={"Host": host})
    assert r.status_code == 200
    assert r.content == b'{"status":"ok"}'
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_custom_allowed_hosts_keep_container_healthy(temp_dir, stub):
    settings = load_settings({"TEMP_DIR": str(temp_dir), "ALLOWED_HOSTS": "saveme.lan"})
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.get("/health", headers={"Host": "127.0.0.1:8000"})
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert (await client.get("/", headers={"Host": "127.0.0.1:8000"})).status_code == 400
        assert (await client.get("/", headers={"Host": "saveme.lan:8000"})).status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost:8000", "LocalHost"])
async def test_allowed_host_header_ignores_port_and_case(client, host):
    r = await client.get("/", headers={"Host": host})
    assert r.status_code == 200


@pytest.mark.parametrize("allowed", ["[::1]", "::1"])
@pytest.mark.parametrize("host", ["[::1]", "[::1]:8000"])
async def test_ipv6_allowed_host(temp_dir, stub, allowed, host):
    settings = load_settings({"TEMP_DIR": str(temp_dir), "ALLOWED_HOSTS": f"localhost,{allowed}"})
    async with running(make_app(settings, run_worker=stub)) as client:
        assert (await client.get("/", headers={"Host": host})).status_code == 200
        r = await client.get("/", headers={"Host": "127.0.0.1"})
        assert r.status_code == 400


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1.evil.example",
                                    "https://evil.example:8000", "file://", "http://localhost/"])
async def test_cross_origin_post_403(client, stub, origin):
    for path, body in (("/api/inspect", {"url": URL}),
                       ("/api/jobs", {"url": URL, "output": "video"})):
        r = await client.post(path, json=body, headers={"Origin": origin})
        assert r.status_code == 403, (origin, path)
        assert r.json() == error_body("forbidden_origin")
    assert stub.calls == []


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8000", "http://LOCALHOST:5173",
                                    "https://localhost"])
async def test_same_origin_or_no_origin_post_allowed(client, origin):
    headers = {"Origin": origin} if origin else {}
    r = await client.post("/api/inspect", json={"url": "not a url"}, headers=headers)
    assert r.status_code == 400  # past the origin check, rejected by URL validation
    assert r.json()["error"]["code"] == "invalid_url"


# --- Degraded mode ----------------------------------------------------------------

async def test_degraded_mode(caplog):
    app = create_app(ConfigError("MAX_ACTIVE_JOBS", "must be 1-16"))
    with caplog.at_level(logging.INFO, logger="app.main"):
        async with running(app) as client:
            r = await client.get("/health")
            assert r.status_code == 503
            body = r.json()
            assert body["status"] == "error"
            assert body["error"]["code"] == "config_error"
            assert "MAX_ACTIVE_JOBS" in body["error"]["message"]
            assert r.headers["content-security-policy"] == CSP

            for method, path, payload in (
                ("POST", "/api/inspect", {"url": URL}),
                ("POST", "/api/jobs", {"url": URL, "output": "video"}),
                ("POST", "/api/jobs", {"bad": "body"}),
                ("GET", "/api/jobs/AAAAAAAAAAAAAAAAAAAAAA", None),
                ("GET", "/api/jobs/AAAAAAAAAAAAAAAAAAAAAA/file", None),
            ):
                r = await client.request(method, path, json=payload)
                assert r.status_code == 503, path
                assert r.json() == {"error": body["error"]}, path

            r = await client.get("/")
            assert r.status_code == 200 and FOOTER in r.text
            assert (await client.get("/static/app.js")).status_code == 200
            # The host check still applies, except to GET /health.
            r = await client.get("/", headers={"Host": "evil.example"})
            assert r.status_code == 400
            r = await client.get("/health", headers={"Host": "evil.example"})
            assert r.status_code == 503 and r.json() == body
    errors = [rec for rec in caplog.records
              if rec.name == "app.main" and rec.levelno >= logging.ERROR]
    assert len(errors) == 1 and "MAX_ACTIVE_JOBS" in errors[0].getMessage()


async def test_degraded_when_temp_dir_is_not_dedicated(tmp_path, stub):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "notes.txt").write_text("keep me")
    app = make_app(load_settings({"TEMP_DIR": str(shared)}), run_worker=stub)
    async with running(app) as client:
        r = await client.get("/health")
        assert r.status_code == 503
        assert "TEMP_DIR" in r.json()["error"]["message"]
        assert str(shared) not in r.text
        r = await client.post("/api/inspect", json={"url": URL})
        assert r.status_code == 503 and r.json()["error"]["code"] == "config_error"
        assert (await client.get("/")).status_code == 200
    assert (shared / "notes.txt").read_text() == "keep me"
    assert not (shared / MARKER_NAME).exists()
    assert stub.calls == []
