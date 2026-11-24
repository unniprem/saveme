"""AI Highlights HTTP API, in-process: StudioStub runs every worker mode (the transcriber
included) and OpenRouter is mocked. Covers the acceptance flow end to end: a source, an
analysis, its highlights, then clips and a ZIP cut from them through the Phase 1 routes."""
import asyncio
import time
from pathlib import Path

import pytest

from app.config import ConfigError, Settings, load_ai_settings, load_settings
from tests.support.ai import clips_answer
from tests.support.api import URL, StudioStub, error_body, make_app, poll_terminal, running
from tests.support.openrouter import KEY, MockOpenRouter

ORIGIN = {"Origin": "http://127.0.0.1"}
AI = load_ai_settings({"OPENROUTER_API_KEY": KEY})
SRT = "1\n00:00:01,000 --> 00:00:30,000\nHello there\n\n2\n00:00:31,000 --> 00:01:05,000\nMore talk\n"


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "media-jobs"


@pytest.fixture
def settings(temp_dir: Path) -> Settings:
    return load_settings({"TEMP_DIR": str(temp_dir)})


@pytest.fixture
def server() -> MockOpenRouter:
    return MockOpenRouter()


@pytest.fixture
def stub() -> StudioStub:
    return StudioStub(duration=300.0)


@pytest.fixture
async def client(settings, stub, server):
    app = make_app(settings, run_worker=stub, ai_settings=AI, openrouter_transport=server.transport())
    async with running(app) as c:
        yield c


async def ready_source(client) -> str:
    r = await client.post("/api/sources", json={"url": URL, "quality": "720"})
    assert r.status_code == 202, r.text
    source_id = r.json()["source_id"]
    deadline = time.monotonic() + 10
    while (await client.get(f"/api/sources/{source_id}")).json()["status"] != "ready":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)
    return source_id


async def poll_analysis(client, analysis_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/analyses/{analysis_id}")
        assert r.status_code == 200, r.text
        view = r.json()
        if view["status"] in ("ready", "failed", "cancelled"):
            return view
        assert time.monotonic() < deadline, view
        await asyncio.sleep(0.02)


# --- status and connection ------------------------------------------------------------------

async def test_status_without_a_key(settings, stub):
    async with running(make_app(settings, run_worker=stub)) as client:
        r = await client.get("/api/ai/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["configured"] is False and body["error"] is None
    assert body["model"] == "anthropic/claude-haiku-5.5" and body["whisper_model"] == "base"


async def test_status_never_shows_the_key(client):
    r = await client.get("/api/ai/status")
    assert r.json()["configured"] is True and KEY not in r.text


async def test_test_connection(client, server):
    r = await client.post("/api/ai/test", json={}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["model"]["id"] == "anthropic/claude-haiku-5.5"
    assert KEY not in r.text
    server.key_status = 401
    r = await client.post("/api/ai/test", json={})
    assert r.status_code == 502 and r.json()["error"]["code"] == "openrouter_auth"
    r = await client.post("/api/ai/test", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == error_body("forbidden_origin")


async def test_an_ai_config_error_leaves_downloads_working(settings, stub):
    error = ConfigError("AI_MAX_CLIPS", "must be between 1 and 20, got 0")
    async with running(make_app(settings, run_worker=stub, ai_settings=error)) as client:
        status = (await client.get("/api/ai/status")).json()
        assert status["available"] is False
        assert status["error"]["message"] == "AI_MAX_CLIPS must be between 1 and 20, got 0."
        source_id = await ready_source(client)
        r = await client.post(f"/api/sources/{source_id}/analyses", json={"transcribe_only": True})
        assert r.status_code == 503 and r.json()["error"]["code"] == "ai_config_error"
        r = await client.post("/api/jobs", json={"url": URL, "output": "video"})
        assert r.status_code == 202
        assert (await poll_terminal(client, r.json()["job_id"]))["status"] == "ready"
        assert (await client.get("/health")).status_code == 200


# --- the acceptance flow ----------------------------------------------------------------------

async def test_highlights_become_clips_and_a_zip(client, server, stub):
    source_id = await ready_source(client)
    server.chat.append(clips_answer((10, 15, 95), (30, 35, 70)))
    r = await client.post(f"/api/sources/{source_id}/analyses", headers=ORIGIN,
                          json={"instruction": "Find the most interesting moments", "min_seconds": 20,
                                "max_seconds": 90, "count": 2, "min_score": 50, "language": "auto"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert set(body) == {"analysis_id", "status"} and body["status"] == "queued"
    view = await poll_analysis(client, body["analysis_id"])
    assert view["status"] == "ready", view
    highlights = view["candidates"]
    assert [(h["start"], h["end"], h["score"]) for h in highlights] == [(49.7, 80.5, 95), (149.7, 180.5, 70)]
    assert view["usage"]["calls"] == 1 and view["usage"]["cost_reported"] is True
    assert stub.calls[-1]["mode"] == "transcribe"
    # The transcript is readable and searchable in the page.
    transcript = (await client.get(f"/api/sources/{source_id}/transcript")).json()["transcript"]
    assert transcript["origin"] == "whisper" and len(transcript["segments"]) == 60
    assert transcript["segments"][0] == {"start": 0.0, "end": 5.0, "text": "words at 0", "language": "en"}
    # One highlight exported as a clip, both as a ZIP, through the Phase 1 routes.
    h = highlights[0]
    r = await client.post(f"/api/sources/{source_id}/clips", json={"start": h["start"], "end": h["end"]})
    assert r.status_code == 202, r.text
    assert (await poll_terminal(client, r.json()["job_id"]))["status"] == "ready"
    r = await client.post(f"/api/sources/{source_id}/batch",
                          json={"clips": [{"start": x["start"], "end": x["end"]} for x in highlights]})
    assert r.status_code == 202, r.text
    job = await poll_terminal(client, r.json()["job_id"])
    assert job["status"] == "ready" and job["filename"].endswith("(2 clips).zip")
    file = await client.get(f"/api/jobs/{job['job_id']}/file")
    assert file.status_code == 200 and file.headers["content-type"] == "application/zip"
    # Regenerating with another instruction reuses the transcript.
    server.chat.append(clips_answer((40, 45, 88)))
    r = await client.post(f"/api/sources/{source_id}/analyses", json={"instruction": "Only the jokes"})
    view = await poll_analysis(client, r.json()["analysis_id"])
    assert view["skipped"] == ["extracting", "transcribing"]
    assert sum(1 for call in stub.calls if call["mode"] == "transcribe") == 1


async def test_estimate(client):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/analyses/estimate", json={"count": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["basis"] == "duration" and body["within_limit"] is True and body["limit_usd"] == 0.25


async def test_transcribe_only_works_without_a_key(settings, stub):
    async with running(make_app(settings, run_worker=stub)) as client:
        source_id = await ready_source(client)
        r = await client.post(f"/api/sources/{source_id}/analyses", json={})
        assert r.status_code == 503 and r.json() == error_body("ai_not_configured")
        r = await client.post(f"/api/sources/{source_id}/analyses", json={"transcribe_only": True})
        assert r.status_code == 202, r.text
        view = await poll_analysis(client, r.json()["analysis_id"])
        assert view["status"] == "ready" and view["transcript"]["segments"] == 60


@pytest.mark.parametrize("body,message", [
    ({"count": 0}, "count must be a whole number from 1 to 20"),
    ({"count": 2.5}, "count must be a whole number from 1 to 20"),
    ({"count": 11}, "Ask for 1 to 10 clips."),
    ({"min_seconds": 1}, "min_seconds must be a number of seconds from 3 to 600"),
    ({"max_seconds": "90"}, "max_seconds must be a number of seconds from 5 to 900"),
    ({"min_seconds": 50, "max_seconds": 40}, "max_seconds must be at least 1 more than min_seconds"),
    ({"min_score": 101}, "min_score must be a whole number from 0 to 100"),
    ({"language": "xx"}, "language must be auto or a language from the list"),
    ({"instruction": "x" * 501}, "instruction can be at most 500 characters"),
    ({"retranscribe": "yes"}, "retranscribe must be true or false"),
    ({"model": "openai/gpt"}, "model is not an accepted field"),
])
async def test_analysis_request_validation(client, body, message):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/analyses", json=body)
    assert r.status_code == 400, r.text
    assert r.json() == error_body("invalid_request", message)


async def test_one_analysis_at_a_time_per_source_and_cancel(settings, server):
    stub = StudioStub(duration=300.0, block=("transcribe",))
    app = make_app(settings, run_worker=stub, ai_settings=AI, openrouter_transport=server.transport())
    async with running(app) as client:
        source_id = await ready_source(client)
        first = (await client.post(f"/api/sources/{source_id}/analyses", json={})).json()["analysis_id"]
        r = await client.post(f"/api/sources/{source_id}/analyses", json={})
        assert r.status_code == 409 and r.json() == error_body("analysis_running")
        r = await client.post(f"/api/analyses/{first}/cancel", json={}, headers=ORIGIN)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        r = await client.post(f"/api/sources/{source_id}/analyses", json={})
        assert r.status_code == 202
        # Closing the studio's source cancels its analysis.
        second = r.json()["analysis_id"]
        await client.post(f"/api/sources/{source_id}/close", json={})
        assert (await client.get(f"/api/analyses/{second}")).json()["status"] == "cancelled"


async def test_unknown_ids(client):
    r = await client.get(f"/api/analyses/{'x' * 22}")
    assert r.status_code == 404 and r.json() == error_body("analysis_not_found")
    r = await client.post(f"/api/sources/{'x' * 22}/analyses", json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "source_not_found"
    source_id = await ready_source(client)
    # No transcript yet is a normal state the page asks about, not an error.
    r = await client.get(f"/api/sources/{source_id}/transcript")
    assert r.status_code == 200 and r.json() == {"transcript": None}


# --- subtitle import ----------------------------------------------------------------------------

def subtitle_headers(**more) -> dict:
    return {"Content-Type": "text/plain; charset=utf-8", **ORIGIN, **more}


async def test_import_subtitles(client, server):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/transcript?language=en", content=SRT.encode(),
                          headers=subtitle_headers(**{"X-SaveMe-Filename": "talk.srt"}))
    assert r.status_code == 200, r.text
    assert r.json() == {"origin": "subtitles", "language": "en", "segments": 2, "duration": 300.0}
    transcript = (await client.get(f"/api/sources/{source_id}/transcript")).json()["transcript"]
    assert transcript["origin"] == "subtitles" and transcript["segments"][1]["text"] == "More talk"
    server.chat.append(clips_answer((0, 1, 90)))
    r = await client.post(f"/api/sources/{source_id}/analyses", json={"min_seconds": 20, "max_seconds": 90})
    view = await poll_analysis(client, r.json()["analysis_id"])
    assert view["status"] == "ready" and view["transcript"]["origin"] == "subtitles"


async def test_import_takes_files_larger_than_the_json_body_cap(client):
    source_id = await ready_source(client)
    cues = "".join(f"{i}\n00:{i // 60:02d}:{i % 60:02d},000 --> 00:{i // 60:02d}:{i % 60:02d},900\n"
                   f"{'word ' * 45}\n\n" for i in range(1, 290))
    assert len(cues) > 64 * 1024
    r = await client.post(f"/api/sources/{source_id}/transcript", content=cues.encode(),
                          headers=subtitle_headers())
    assert r.status_code == 200, r.text
    assert r.json()["segments"] == 289


@pytest.mark.parametrize("headers,content,status,code", [
    ({"Content-Type": "application/json"}, SRT.encode(), 415, "invalid_request"),
    ({}, b"no cues at all", 400, "invalid_subtitles"),
    ({"Content-Length": str(5 * 1024 * 1024)}, SRT.encode(), 413, "request_too_large"),
    ({"Origin": "http://evil.example"}, SRT.encode(), 403, "forbidden_origin"),
])
async def test_import_refusals(client, headers, content, status, code):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/transcript", content=content,
                          headers={**subtitle_headers(), **headers})
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


async def test_import_refuses_an_unknown_language(client):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/transcript?language=klingon", content=SRT.encode(),
                          headers=subtitle_headers())
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
