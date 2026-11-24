"""Shared helpers for the HTTP API tests: an in-process app with its lifespan, and a
stub run_worker for the Clip Studio modes."""
import asyncio
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from ipaddress import ip_address
from pathlib import Path

import httpx
import uvicorn

from app.config import ConfigError, Settings
from app.errors import CATALOG
from app.main import create_app
from tests.support.policy import allow_loopback

TEST_WORKER_CMD = (sys.executable, "-P", "-m", "tests.support.worker_entry")
URL = "http://127.0.0.1:9/watch?v=1"


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


def error_body(code: str, message: str | None = None) -> dict:
    return {"error": {"code": code, "message": message or CATALOG[code][1]}}


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


class StudioStub:
    """Stand-in for run_worker covering every mode. A download writes media.mp4 and reports
    `duration`; a probe renames the upload to media.mp4; a clip emits one progress event
    and writes a small media.mp4, and a batch the same with media.zip; a transcribe writes a
    transcript of 5 s segments over the whole duration. Modes in `block` wait on `gate` first."""

    def __init__(self, *, duration: float | None = 12.5, block: tuple[str, ...] = ()):
        self.duration = duration
        self.block = block
        self.gate = asyncio.Event()
        self.calls: list[dict] = []

    async def __call__(self, request, *, cmd, timeout, on_event=None, workdir=None,
                       max_workdir_bytes=None, poll_interval=1.0):
        self.calls.append(request)
        mode = request["mode"]
        if mode in self.block:
            await self.gate.wait()
        if mode == "download":
            (workdir / "media.mp4").write_bytes(os.urandom(4096))
            return {"file": "media.mp4", "ext": "mp4", "title": "Stub Video",
                    "duration": self.duration, "notice": None}
        if mode == "probe":
            Path(request["source"]).rename(workdir / "media.mp4")
            return {"file": "media.mp4", "ext": "mp4", "duration": self.duration, "notice": None}
        if mode == "clip":
            on_event({"event": "progress", "detail": "Cutting the clip", "percent": 50.0,
                      "downloaded_bytes": 10, "total_bytes": None})
            (workdir / "media.mp4").write_bytes(b"clip bytes")
            return {"file": "media.mp4", "ext": "mp4"}
        if mode == "transcribe":
            from tests.support.ai import speech_transcript
            from app.transcript import save_transcript

            on_event({"event": "phase", "phase": "extracting", "detail": "Extracting audio"})
            on_event({"event": "phase", "phase": "transcribing", "detail": "Transcribing"})
            transcript = speech_transcript(request["duration"] or self.duration or 12.5,
                                           requested=request["language"])
            save_transcript(workdir / "transcript.json", transcript)
            return {"file": "transcript.json", "segments": len(transcript.segments),
                    "language": transcript.language, "duration": transcript.duration}
        if mode == "batch":
            on_event({"event": "progress", "detail": f"Cutting clip 1 of {len(request['clips'])}",
                      "percent": 25.0, "downloaded_bytes": 10, "total_bytes": None})
            (workdir / "media.zip").write_bytes(b"zip bytes")
            return {"file": "media.zip", "ext": "zip", "count": len(request["clips"])}
        return {"title": "Stub", "video": {"available": True, "qualities": []},
                "audio": {"available": True, "m4a": False}}
