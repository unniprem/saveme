"""Clip Studio HTTP API: sources, preview and clips (spec §6.3), in-process with StudioStub."""
import asyncio
import json
import re
import socket
import time
from pathlib import Path

import httpx
import pytest

from app.config import ConfigError, Settings, load_settings
from app.netguard import is_public_ip
from tests.support.api import (URL, StudioStub, error_body, live_server, make_app, poll_terminal,
                               running)

ORIGIN = {"Origin": "http://127.0.0.1"}


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "media-jobs"


@pytest.fixture
def settings(temp_dir: Path) -> Settings:
    return load_settings({"TEMP_DIR": str(temp_dir)})


@pytest.fixture
def stub() -> StudioStub:
    return StudioStub()


@pytest.fixture
async def client(settings, stub):
    async with running(make_app(settings, run_worker=stub)) as c:
        yield c


async def poll_source(client: httpx.AsyncClient, source_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/sources/{source_id}")
        assert r.status_code == 200, r.text
        view = r.json()
        if view["status"] in ("ready", "failed", "expired"):
            return view
        assert time.monotonic() < deadline, view
        await asyncio.sleep(0.02)


async def ready_source(client: httpx.AsyncClient) -> str:
    r = await client.post("/api/sources", json={"url": URL, "quality": "720"})
    assert r.status_code == 202, r.text
    source_id = r.json()["source_id"]
    assert (await poll_source(client, source_id))["status"] == "ready"
    return source_id


async def test_studio_limits(client, settings):
    r = await client.get("/api/studio")
    assert r.status_code == 200
    assert r.json() == {"max_upload_bytes": settings.max_file_bytes,
                        "idle_minutes": settings.job_ttl_minutes, "min_clip_seconds": 0.1}


async def test_link_source_flow(client, stub, temp_dir):
    r = await client.post("/api/sources", json={"url": URL, "quality": "720"})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"source_id", "status"} and body["status"] == "queued"
    view = await poll_source(client, body["source_id"])
    assert view == {"source_id": body["source_id"], "status": "ready", "origin": "link",
                    "title": "Stub Video", "duration_seconds": 12.5, "detail": None,
                    "progress_percent": None, "downloaded_bytes": None, "total_bytes": None,
                    "notice": None, "error": None, "reason": None}
    assert stub.calls[0]["mode"] == "download" and stub.calls[0]["quality"] == "720"
    assert str(temp_dir) not in json.dumps(view)

    media = f"/api/sources/{body['source_id']}/media"
    r = await client.get(media)
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == (temp_dir / body["source_id"] / "media.mp4").read_bytes()
    r = await client.get(media, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert r.headers["content-range"] == "bytes 0-99/4096"


async def test_media_before_ready_is_409(settings):
    stub = StudioStub(block=("download",))
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.post("/api/sources", json={"url": URL})
        source_id = r.json()["source_id"]
        r = await client.get(f"/api/sources/{source_id}/media")
        assert (r.status_code, r.json()) == (409, error_body("source_not_ready"))
        r = await client.post(f"/api/sources/{source_id}/clips", json={"start": 0, "end": 1})
        assert (r.status_code, r.json()) == (409, error_body("source_not_ready"))
        stub.gate.set()


async def test_clip_flow_end_to_end_with_stub(client, stub):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/clips", json={"start": 1, "end": 3.5},
                          headers=ORIGIN)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    view = await poll_terminal(client, job_id)
    assert (view["kind"], view["status"]) == ("clip", "ready")
    assert view["filename"] == "Stub Video (clip 0m01s-0m03.5s).mp4"
    clip = stub.calls[-1]
    assert (clip["mode"], clip["start"], clip["end"], clip["keep_tags"]) == ("clip", 1.0, 3.5, True)
    r = await client.get(f"/api/jobs/{job_id}/file")
    assert r.status_code == 200 and r.content == b"clip bytes"
    assert "Stub%20Video%20%28clip%200m01s-0m03.5s%29.mp4" in r.headers["content-disposition"]
    view = (await client.get(f"/api/jobs/{job_id}")).json()
    assert (view["status"], view["reason"]) == ("expired", "delivered")
    assert (await client.get(f"/api/sources/{source_id}")).json()["status"] == "ready"


@pytest.mark.parametrize("body, status, code, message", [
    ({"start": "1", "end": 2}, 400, "invalid_request", "start must be a number of seconds, 0 or more"),
    ({"start": True, "end": 2}, 400, "invalid_request", "start must be a number of seconds, 0 or more"),
    ({"start": -1, "end": 2}, 400, "invalid_request", "start must be a number of seconds, 0 or more"),
    ({"start": 1, "end": None}, 400, "invalid_request", "end must be a number of seconds, 0 or more"),
    ({"start": 1}, 400, "invalid_request", "end is required"),
    ({"start": 1, "end": 2, "x": 1}, 400, "invalid_request", "x is not an accepted field"),
    ({"start": 5, "end": 2}, 400, "invalid_clip", "The end must be after the start."),
    ({"start": 1, "end": 100}, 400, "invalid_clip", "The end can't be after the end of the video."),
], ids=["str", "bool", "negative", "null", "missing", "extra", "reversed", "past_end"])
async def test_clip_request_validation(client, body, status, code, message):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/clips", json=body)
    assert (r.status_code, r.json()) == (status, error_body(code, message))


async def test_clip_request_refuses_infinity(client):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/clips", content=b'{"start": 0, "end": Infinity}',
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


async def test_unknown_source_and_cross_kind_ids(client):
    source_id = await ready_source(client)
    r = await client.post("/api/jobs", json={"url": URL, "output": "video"})
    job_id = r.json()["job_id"]
    await poll_terminal(client, job_id)
    for path in (f"/api/sources/{job_id}", f"/api/sources/{job_id}/media", "/api/sources/bad"):
        r = await client.get(path)
        assert (r.status_code, r.json()) == (404, error_body("source_not_found")), path
    for path in (f"/api/sources/{job_id}/close", f"/api/sources/{job_id}/clips"):
        r = await client.post(path, json={"start": 0, "end": 1})
        assert (r.status_code, r.json()) == (404, error_body("source_not_found")), path
    for path in (f"/api/jobs/{source_id}", f"/api/jobs/{source_id}/file"):
        r = await client.get(path)
        assert (r.status_code, r.json()) == (404, error_body("job_not_found")), path
    r = await client.post(f"/api/jobs/{source_id}/cancel", json={})
    assert (r.status_code, r.json()) == (404, error_body("job_not_found"))


async def test_close_route_is_post_with_origin_check(client):
    source_id = await ready_source(client)
    close = f"/api/sources/{source_id}/close"
    r = await client.post(close, json={}, headers={"Origin": "http://evil.example"})
    assert (r.status_code, r.json()) == (403, error_body("forbidden_origin"))
    assert (await client.delete(f"/api/sources/{source_id}")).status_code == 405
    r = await client.post(close, json={}, headers=ORIGIN)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["reason"]) == ("expired", "closed")
    r = await client.get(f"/api/sources/{source_id}/media")
    assert (r.status_code, r.json()) == (404, error_body("source_not_found"))
    assert (await client.post(close, json={})).json()["status"] == "expired"  # a repeat is harmless


async def test_source_gone_after_restart(settings, stub):
    async with running(make_app(settings, run_worker=stub)) as client:
        source_id = await ready_source(client)
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.get(f"/api/sources/{source_id}")
        assert (r.status_code, r.json()) == (404, error_body("source_not_found"))


async def test_link_source_url_checks(settings, stub):
    async with running(make_app(settings, run_worker=stub, ip_policy=is_public_ip)) as client:
        r = await client.post("/api/sources", json={"url": "http://192.168.1.1/x"})
        assert (r.status_code, r.json()) == (403, error_body("blocked_address"))
        r = await client.post("/api/sources", json={"url": "ftp://example.com/x"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_url"
        r = await client.post("/api/sources", json={"url": URL, "quality": "4k"})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert stub.calls == []


async def test_link_source_takes_a_queue_place(settings):
    stub = StudioStub(block=("download",))
    tight = load_settings({"TEMP_DIR": str(settings.temp_dir), "MAX_ACTIVE_JOBS": "1",
                           "MAX_QUEUED_JOBS": "0"})
    async with running(make_app(tight, run_worker=stub)) as client:
        assert (await client.post("/api/sources", json={"url": URL})).status_code == 202
        r = await client.post("/api/jobs", json={"url": URL, "output": "video"})
        assert (r.status_code, r.json()) == (429, error_body("busy"))
        r = await client.post("/api/sources", json={"url": URL})
        assert (r.status_code, r.json()) == (429, error_body("busy"))
        stub.gate.set()


# --- Uploads ------------------------------------------------------------------

UPLOAD = "/api/sources/upload"
OCTET = {"Content-Type": "application/octet-stream"}


def job_dirs(temp_dir: Path) -> list[str]:
    return sorted(p.name for p in temp_dir.iterdir() if p.is_dir())


async def test_upload_success(client, stub, media, temp_dir):
    data = media["sample.mp4"].read_bytes()
    r = await client.post(UPLOAD, content=data,
                          headers={**OCTET, **ORIGIN, "X-SaveMe-Filename": "My%20Clip.mp4"})
    assert r.status_code == 202, r.text
    source_id = r.json()["source_id"]
    assert r.json()["status"] == "queued"
    view = await poll_source(client, source_id)
    assert (view["status"], view["origin"], view["title"], view["duration_seconds"]) == (
        "ready", "upload", "My Clip", 12.5)
    assert stub.calls[-1] == {"mode": "probe", "source": str(temp_dir / source_id / "upload.bin"),
                              "workdir": str(temp_dir / source_id)}
    assert (temp_dir / source_id / "media.mp4").read_bytes() == data
    r = await client.post(f"/api/sources/{source_id}/clips", json={"start": 0, "end": 1})
    assert r.status_code == 202
    await poll_terminal(client, r.json()["job_id"])
    assert stub.calls[-1]["keep_tags"] is False


async def chunks(*parts: bytes):
    for part in parts:
        yield part


@pytest.mark.parametrize("make_request, status, code, message", [
    (lambda: {"content": b"x" * 10, "headers": {"Content-Type": "video/mp4"}},
     415, "invalid_request", "Send the file as application/octet-stream."),
    (lambda: {"content": chunks(b"x" * 10), "headers": OCTET},
     411, "invalid_request", "The upload needs a Content-Length header."),
    (lambda: {"content": b"", "headers": OCTET}, 400, "invalid_request", "The file is empty."),
    (lambda: {"content": b"x" * (1024 * 1024 + 1), "headers": OCTET},
     413, "too_large", "This file is larger than the 1 MB upload limit."),
], ids=["content_type", "chunked", "empty", "too_large"])
async def test_upload_header_checks(temp_dir, stub, make_request, status, code, message):
    small = load_settings({"TEMP_DIR": str(temp_dir), "MAX_FILE_SIZE_MB": "1"})
    async with running(make_app(small, run_worker=stub)) as client:
        r = await client.post(UPLOAD, **make_request())
        assert (r.status_code, r.json()) == (status, error_body(code, message))
    assert job_dirs(temp_dir) == [] and stub.calls == []


async def test_upload_bypasses_only_its_own_body_cap(client):
    r = await client.post(UPLOAD, content=b"\0" * (100 * 1024), headers=OCTET)
    assert r.status_code == 202
    r = await client.post("/api/sources", content=b"{" + b" " * (100 * 1024) + b"}",
                          headers={"Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (413, error_body("request_too_large"))


async def test_upload_origin_and_host_checks_still_apply(client, temp_dir):
    r = await client.post(UPLOAD, content=b"x", headers={**OCTET, "Origin": "http://evil.example"})
    assert (r.status_code, r.json()) == (403, error_body("forbidden_origin"))
    r = await client.post(UPLOAD, content=b"x", headers={**OCTET, "Host": "evil.example"})
    assert r.status_code == 400 and r.json()["error"]["message"] == "Invalid host header."
    assert job_dirs(temp_dir) == []


async def test_upload_refused_while_degraded():
    degraded = ConfigError("MAX_ACTIVE_JOBS", "must be between 1 and 16, got 0")
    async with running(make_app(degraded, run_worker=StudioStub())) as client:
        r = await client.post(UPLOAD, content=b"x" * (100 * 1024), headers=OCTET)
        assert r.status_code == 503 and r.json()["error"]["code"] == "config_error"


@pytest.mark.parametrize("header, title", [
    ("My%20Clip.mp4", "My Clip"),
    ("%E2%82%AC%20report.final.MOV", "€ report.final"),
    ("no-extension", "no-extension"),
    (".mp4", None),
    ("C%3A%5Cfolder%5Cclip.mkv", "clip"),
    ("%FF%FE.mp4", "��"),
    ("a" * 2000, "a" * 1024),
    ("", None),
    (None, None),
])
def test_upload_title(header, title):
    from app.studio_routes import upload_title
    assert upload_title(header) == title


def raw_upload(port: int, declared: int, body: bytes) -> socket.socket:
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    raw.sendall(f"POST {UPLOAD} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Content-Type: application/octet-stream\r\nContent-Length: {declared}\r\n\r\n".encode()
                + body)
    return raw


def read_reply(raw: socket.socket) -> bytes:
    """The whole HTTP reply, headers and Content-Length body (one recv() may return only the
    headers)."""
    reply = b""
    while chunk := raw.recv(65536):
        reply += chunk
        head, sep, body = reply.partition(b"\r\n\r\n")
        length = re.search(rb"(?im)^content-length:\s*(\d+)", head)
        if sep and length and len(body) >= int(length[1]):
            break
    return reply


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.02)


def test_upload_short_body_or_disconnect_cleans_up(temp_dir):
    tight = load_settings({"TEMP_DIR": str(temp_dir), "MAX_ACTIVE_JOBS": "1", "MAX_QUEUED_JOBS": "0"})
    with live_server(make_app(tight, run_worker=StudioStub())) as port, \
            httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as http:
        raw = raw_upload(port, 100_000, b"\0" * 1000)
        wait_for(lambda: job_dirs(temp_dir) != [])  # the upload holds a folder and a queue place
        assert http.post("/api/jobs", json={"url": URL, "output": "video"}).status_code == 429
        raw.close()  # the client leaves mid-body
        wait_for(lambda: job_dirs(temp_dir) == [])
        r = http.post("/api/jobs", json={"url": URL, "output": "video"})
        assert r.status_code == 202, r.text  # its place is free again


def test_upload_idle_timeout(temp_dir):
    settings = load_settings({"TEMP_DIR": str(temp_dir)})
    app = make_app(settings, run_worker=StudioStub(), upload_idle_seconds=0.3)
    with live_server(app) as port:
        with raw_upload(port, 100_000, b"\0" * 1000) as raw:
            reply = read_reply(raw)  # nothing more is sent
        assert reply.startswith(b"HTTP/1.1 504"), reply[:100]
        assert b'"code":"timeout"' in reply
        wait_for(lambda: job_dirs(temp_dir) == [])


def test_upload_closed_mid_transfer(temp_dir):
    settings = load_settings({"TEMP_DIR": str(temp_dir)})
    with live_server(make_app(settings, run_worker=StudioStub())) as port, \
            httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as http:
        with raw_upload(port, 100_000, b"\0" * 1000) as raw:
            wait_for(lambda: job_dirs(temp_dir) != [])
            source_id = job_dirs(temp_dir)[0]
            r = http.post(f"/api/sources/{source_id}/close", json={})
            assert r.json()["status"] == "expired"
            raw.sendall(b"\0" * 2000)
            reply = read_reply(raw)
        assert reply.startswith(b"HTTP/1.1 404"), reply[:100]
        assert b'"code":"source_not_found"' in reply
        wait_for(lambda: job_dirs(temp_dir) == [])
