"""Opt-in checks against real public sites and a running container.

Run with `docker compose up -d --build && .venv/bin/pytest -m network -v`.
Set SAVEME_BASE_URL to test another address (default http://127.0.0.1:8000).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from tests.network import urls

BASE_URL = os.environ.get("SAVEME_BASE_URL", "http://127.0.0.1:8000")
TERMINAL = {"ready", "failed", "expired"}

pytestmark = pytest.mark.network


def _reachable() -> bool:
    try:
        return httpx.get(f"{BASE_URL}/health", timeout=3, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
def client():
    if not _reachable():
        pytest.skip(f"saveme is not reachable at {BASE_URL}/health")
    # No Origin header; Host comes from the base URL (127.0.0.1).
    with httpx.Client(base_url=BASE_URL, timeout=60, trust_env=False) as c:
        yield c


def _inspect(client: httpx.Client, url: str) -> dict:
    r = client.post("/api/inspect", json={"url": url})
    assert r.status_code == 200, r.text
    return r.json()


def _run_job(client, url, output, quality="best", timeout=300) -> dict:
    r = client.post("/api/jobs", json={"url": url, "output": output, "quality": quality})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    deadline = time.monotonic() + timeout
    while True:
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in TERMINAL:
            break
        assert time.monotonic() < deadline, f"job {job_id} still {status['status']} after {timeout}s"
        time.sleep(2)
    assert status["status"] == "ready", status
    return status


def _download(client, status: dict, tmp_path: Path) -> Path:
    r = client.get(f"/api/jobs/{status['job_id']}/file")
    assert r.status_code == 200, r.text[:200]
    path = tmp_path / "download.bin"
    path.write_bytes(r.content)
    assert path.stat().st_size > 0
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def _codecs(info: dict, kind: str) -> list[str]:
    return [s["codec_name"] for s in info["streams"] if s["codec_type"] == kind]


def test_youtube_inspect_and_480p_and_mp3(client, tmp_path):
    info = _inspect(client, urls.YOUTUBE)
    assert info["video"]["available"] and info["audio"]["available"]
    ids = [q["id"] for q in info["video"]["qualities"]]
    assert "best" in ids and "480" in ids

    video = _run_job(client, urls.YOUTUBE, "video", "480")
    probe = _probe(_download(client, video, tmp_path))
    assert "h264" in _codecs(probe, "video")

    audio = _run_job(client, urls.YOUTUBE, "audio")
    probe = _probe(_download(client, audio, tmp_path))
    assert _codecs(probe, "audio") == ["mp3"]
    assert "mp3" in probe["format"]["format_name"]


def test_youtu_be_short_link_inspects(client):
    info = _inspect(client, urls.YOUTU_BE)
    assert info["title"]
    assert info["video"]["available"]


def test_reddit_video(client, tmp_path):
    info = _inspect(client, urls.REDDIT)
    assert info["video"]["available"]
    status = _run_job(client, urls.REDDIT, "video")
    assert _codecs(_probe(_download(client, status, tmp_path)), "video")


def test_direct_mp4(client, tmp_path):
    info = _inspect(client, urls.DIRECT_MP4)
    assert info["video"]["available"]
    status = _run_job(client, urls.DIRECT_MP4, "video")
    assert _codecs(_probe(_download(client, status, tmp_path)), "video")


def test_unsupported(client):
    r = client.post("/api/inspect", json={"url": urls.UNSUPPORTED})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unsupported"


def test_private_ip_blocked(client):
    r = client.post("/api/inspect", json={"url": urls.PRIVATE_IP})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "blocked_address"
