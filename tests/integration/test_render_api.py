"""Auto Shorts HTTP API and RenderManager, in-process. Most tests use RendererStub (no FFmpeg) to cover
the lifecycle, captions, cancellation, limits and cleanup; the acceptance tests render for real with the
renderer worker and check every output with ffprobe."""
import asyncio
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from app.config import ConfigError, load_render_settings, load_settings
from app.encoders import CAPTION_FILTER, REQUIRED_FILTERS, Capabilities
from tests.support.api import URL, error_body, make_app, running
from tests.support.render import colour_at, ffprobe, frame_rgb
from tests.support.renderstub import RealRenderRunner, RendererStub, put_transcript, worded_transcript

ORIGIN = {"Origin": "http://127.0.0.1"}
FULL = Capabilities(encoders=frozenset({"libx264", "aac", "mjpeg"}),
                    filters=frozenset(REQUIRED_FILTERS | {CAPTION_FILTER}))


@pytest.fixture
def temp_dir(tmp_path: Path) -> Path:
    return tmp_path / "media-jobs"


@pytest.fixture
def settings(temp_dir: Path):
    return load_settings({"TEMP_DIR": str(temp_dir)})


@pytest.fixture
def stub() -> RendererStub:
    return RendererStub()


def app_for(settings, runner, *, caps=FULL, render_settings=None):
    return make_app(settings, run_worker=runner, render_capabilities=(lambda: caps) if caps else None,
                    render_settings=render_settings)


@pytest.fixture
async def client(settings, stub):
    async with running(app_for(settings, stub)) as c:
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


async def poll_render(client, render_id: str, timeout: float = 20.0, until=("ready", "failed", "cancelled")) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        r = await client.get(f"/api/renders/{render_id}")
        assert r.status_code == 200, r.text
        view = r.json()
        if view["status"] in until:
            return view
        assert time.monotonic() < deadline, view
        await asyncio.sleep(0.02)


async def start(client, source_id, clips, spec=None, **extra) -> str:
    body = {"clips": clips, **({"spec": spec} if spec is not None else {}), **extra}
    r = await client.post(f"/api/sources/{source_id}/renders", json=body)
    assert r.status_code == 202, r.text
    return r.json()["render_id"]


def renders(stub) -> list[dict]:
    return [c for c in stub.calls if c["mode"] == "render"]


# --- options and validation ---------------------------------------------------------------------

async def test_options_list_presets_encoder_and_limits(client):
    data = (await client.get("/api/shorts/options")).json()
    assert data["available"] is True and data["error"] is None
    assert data["encoder"] == {"name": "libx264", "hardware": False} and data["captions_available"]
    assert {t["id"] for t in data["templates"]} >= {"talking_head", "podcast_split"}
    assert data["limits"]["max_clips"] == 20 and data["limits"]["max_clip_seconds"] == 300


async def test_options_explain_an_ffmpeg_that_cant_render(settings, stub):
    caps = Capabilities(encoders=frozenset({"aac"}), filters=frozenset())
    async with running(app_for(settings, stub, caps=caps)) as client:
        data = (await client.get("/api/shorts/options")).json()
        assert data["available"] is False and data["error"]["code"] == "render_unavailable"
        assert "libx264" in data["error"]["message"]
        source_id = await ready_source(client)
        r = await client.post(f"/api/sources/{source_id}/renders", json={"clips": [{"start": 0, "end": 5}]})
        assert r.status_code == 503 and r.json()["error"]["code"] == "render_unavailable"


async def test_a_bad_render_setting_turns_off_only_auto_shorts(settings, stub):
    bad = ConfigError("RENDER_ENCODER", "must be one of auto, libx264, h264_videotoolbox")
    async with running(app_for(settings, stub, render_settings=bad)) as client:
        data = (await client.get("/api/shorts/options")).json()
        assert data["available"] is False and data["error"]["code"] == "render_config_error"
        assert "RENDER_ENCODER" in data["error"]["message"]
        assert (await client.get("/health")).status_code == 200
        await ready_source(client)  # Clip Studio still works


async def test_validate_normalises_a_preset_and_refuses_a_bad_one(client):
    r = await client.post("/api/shorts/validate", json={"spec": {"template": "educational", "fps": 30}})
    assert r.status_code == 200 and r.json()["spec"]["template"] == "educational" and r.json()["spec"]["fps"] == 30
    r = await client.post("/api/shorts/validate", json={"spec": {"captions": {"color": "javascript:x"}}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    r = await client.post("/api/shorts/validate", json={"spec": {"template": "x"}, "extra": 1})
    assert r.status_code == 400


# --- captions -----------------------------------------------------------------------------------

async def test_captions_are_null_without_a_transcript_and_cues_with_one(client, temp_dir):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/shorts/captions", json={"start": 2, "end": 8})
    assert r.status_code == 200 and r.json() == {"captions": None}
    put_transcript(temp_dir / source_id, worded_transcript(30.0))
    data = (await client.post(f"/api/sources/{source_id}/shorts/captions",
                              json={"start": 2, "end": 8, "spec": {"captions": {"style": "bold_creator"}}})).json()
    caps = data["captions"]
    assert caps["timing"] == "words" and caps["has_words"] and caps["language"] == "en"
    assert caps["cues"][0]["start"] == 0.0 and all(len(c["words"]) <= 3 for c in caps["cues"])
    assert all(0 <= c["start"] < c["end"] <= 6.0 for c in caps["cues"])
    put_transcript(temp_dir / source_id, worded_transcript(30.0, with_words=False))
    caps = (await client.post(f"/api/sources/{source_id}/shorts/captions", json={"start": 2, "end": 8})).json()
    assert caps["captions"]["timing"] == "estimated"


# --- renders ------------------------------------------------------------------------------------

async def test_a_render_runs_each_clip_in_turn_with_the_resolved_spec(client, stub, temp_dir):
    source_id = await ready_source(client)
    put_transcript(temp_dir / source_id, worded_transcript(30.0))
    render_id = await start(client, source_id, [{"start": 2, "end": 8, "title": "First"}, {"start": 10, "end": 14}],
                            {"template": "talking_head", "captions": {"export": ["srt", "ass"]}})
    view = await poll_render(client, render_id)
    assert view["status"] == "ready" and view["ready"] == 2 and view["count"] == 2 and view["percent"] == 100.0
    first, second = view["clips"]
    assert first["status"] == "ready" and first["files"] == ["ass", "mp4", "srt"]
    assert first["encoder"] == "libx264" and first["captions_timing"] == "words"
    assert first["percent_measured"] is True and first["eta_estimated"] is False
    calls = renders(stub)
    assert [(c["start"], c["end"]) for c in calls] == [(2.0, 8.0), (10.0, 14.0)]
    req = calls[0]
    assert (req["aspect"], req["width"], req["height"], req["quality"]) == ("9:16", 1080, 1920, "standard")
    assert req["layout"]["mode"] == "face" and req["analysis"] is not None  # analysed first
    assert req["captions"]["plan"]["style"]["style"] == "bold_creator"
    assert req["captions"]["cues"][0]["words"][0][0] == 0.0  # clip time
    assert req["speech"] and req["speech"][0][0] == 0.0
    assert req["logo"] is None and req["intro"] is None and req["outro"] is None
    assert [c["mode"] for c in stub.calls].count("analyze") == 2
    folder = temp_dir / source_id / "render" / render_id
    assert sorted(p.name for p in folder.iterdir()) == ["01.ass", "01.mp4", "01.srt", "02.ass", "02.mp4", "02.srt"]
    srt = (await client.get(f"/api/renders/{render_id}/clips/1/file?kind=srt")).text
    assert srt.startswith("1\n00:00:00,000 --> ") and "W4" in srt  # uppercase style, words from 2 s on
    mp4 = await client.get(f"/api/renders/{render_id}/clips/1/file")
    assert mp4.status_code == 200 and mp4.headers["content-type"] == "video/mp4"
    assert "attachment" in mp4.headers["content-disposition"] and "First (short 1, 0m02s-0m08s).mp4" in \
        mp4.headers["content-disposition"].replace("%20", " ").replace("%28", "(").replace("%29", ")")
    inline = await client.get(f"/api/renders/{render_id}/clips/2/file?inline=true")
    assert inline.headers["content-disposition"].startswith("inline")


async def test_face_analysis_is_cached_per_range(client, stub):
    source_id = await ready_source(client)
    for template in ("talking_head", "interview"):
        await poll_render(client, await start(client, source_id, [{"start": 2, "end": 8}], {"template": template}))
    assert [c["mode"] for c in stub.calls].count("analyze") == 1
    await poll_render(client, await start(client, source_id, [{"start": 2, "end": 8}], {"template": "educational"}))
    assert [c["mode"] for c in stub.calls].count("analyze") == 1  # blur needs no faces


async def test_captions_off_edited_or_missing(client, stub, temp_dir):
    source_id = await ready_source(client)
    view = await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                                 {"template": "educational"}))
    clip = view["clips"][0]
    assert "No transcript yet" in clip["notice"] and clip["captions_timing"] is None
    assert renders(stub)[-1]["captions"] is None
    put_transcript(temp_dir / source_id, worded_transcript(30.0))
    await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                          {"template": "educational", "captions": {"enabled": False}}))
    assert renders(stub)[-1]["captions"] is None
    cues = [{"start": 0.5, "end": 2.0, "text": "Edited {\\b1} line", "speaker": "Ann"}]
    view = await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5, "cues": cues}],
                                                 {"template": "educational"}))
    assert view["clips"][0]["captions_timing"] == "edited"
    sent = renders(stub)[-1]["captions"]["cues"]
    assert len(sent) == 1 and sent[0]["speaker"] == "Ann" and [w[2] for w in sent[0]["words"]] == \
        ["Edited", "{\\b1}", "line"]  # escaped when the worker writes the ASS


async def test_branding_only_burns_a_watermark_and_title_bar(client, stub):
    source_id = await ready_source(client)
    await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5, "title": "Headline"}],
                                          {"template": "news_commentary", "captions": {"enabled": False},
                                           "branding": {"watermark": "@me", "brand_color": "#112233"}}))
    plan = renders(stub)[-1]["captions"]["plan"]
    assert plan["enabled"] is False and plan["title"] == "Headline" and plan["watermark"] == "@me"
    assert plan["brand_color"] == "#112233" and renders(stub)[-1]["layout"]["pad_color"] == "#112233"


async def test_one_failed_clip_doesnt_stop_the_batch(settings):
    from app.errors import AppError

    stub = RendererStub(fail={1: AppError("render_failed")})
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        view = await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5},
                                                                         {"start": 5, "end": 9}],
                                                     {"template": "educational"}))
        assert view["status"] == "ready" and [c["status"] for c in view["clips"]] == ["failed", "ready"]
        assert view["clips"][0]["error"]["code"] == "render_failed"
        stub2 = RendererStub(fail={1: AppError("render_failed")})
    async with running(app_for(settings, stub2)) as client:
        source_id = await ready_source(client)
        view = await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                                     {"template": "educational"}))
        assert view["status"] == "failed" and view["error"]["code"] == "render_failed"


async def test_cancelling_a_render_stops_its_worker_and_keeps_finished_clips(settings, temp_dir):
    stub = RendererStub(block=(2,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        render_id = await start(client, source_id, [{"start": 0, "end": 5}, {"start": 5, "end": 9},
                                                    {"start": 10, "end": 12}], {"template": "educational"})
        await poll_render(client, render_id, until=("running",))
        deadline = time.monotonic() + 5
        while stub.renders < 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        r = await client.post(f"/api/renders/{render_id}/cancel", headers=ORIGIN)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        view = await poll_render(client, render_id)
        assert [c["status"] for c in view["clips"]] == ["ready", "cancelled", "cancelled"]
        await asyncio.sleep(0.1)
        assert stub.cancelled == [2] and stub.renders == 2
        folder = temp_dir / source_id / "render" / render_id
        assert sorted(p.name for p in folder.iterdir()) == ["01.mp4"]  # no work folders, no parts
        assert (await client.get(f"/api/renders/{render_id}/clips/1/file")).status_code == 200
        r = await client.get(f"/api/renders/{render_id}/clips/2/file")
        assert r.status_code == 409


async def test_cancelling_one_clip_lets_the_rest_finish(settings):
    stub = RendererStub(block=(1,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        render_id = await start(client, source_id, [{"start": 0, "end": 5}, {"start": 5, "end": 9}],
                                {"template": "educational"})
        await asyncio.wait_for(stub.started.wait(), 5)
        r = await client.post(f"/api/renders/{render_id}/clips/1/cancel", headers=ORIGIN)
        assert r.status_code == 200
        view = await poll_render(client, render_id)
        assert view["status"] == "ready" and [c["status"] for c in view["clips"]] == ["cancelled", "ready"]
        assert stub.cancelled == [1]


async def test_closing_the_source_cancels_its_render(settings, temp_dir):
    stub = RendererStub(block=(1,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        render_id = await start(client, source_id, [{"start": 0, "end": 5}], {"template": "educational"})
        await asyncio.wait_for(stub.started.wait(), 5)
        await client.post(f"/api/sources/{source_id}/close", headers=ORIGIN)
        view = await poll_render(client, render_id)
        assert view["status"] == "cancelled" and not (temp_dir / source_id).exists()


async def test_one_render_per_source_and_a_bounded_queue(settings):
    stub = RendererStub(block=(1, 2, 3, 4, 5, 6))
    rs = load_render_settings({"RENDER_MAX_QUEUED_JOBS": "0"})
    async with running(app_for(settings, stub, render_settings=rs)) as client:
        first = await ready_source(client)
        await start(client, first, [{"start": 0, "end": 5}], {"template": "educational"})
        r = await client.post(f"/api/sources/{first}/renders", json={"clips": [{"start": 0, "end": 5}]})
        assert r.status_code == 409 and r.json()["error"]["code"] == "render_running"
        second = await ready_source(client)
        r = await client.post(f"/api/sources/{second}/renders", json={"clips": [{"start": 0, "end": 5}]})
        assert r.status_code == 429 and r.json()["error"]["code"] == "busy"
        stub.gate.set()


async def test_render_slots_never_block_downloads(settings):
    stub = RendererStub(block=(1,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        await start(client, source_id, [{"start": 0, "end": 5}], {"template": "educational"})
        await asyncio.wait_for(stub.started.wait(), 5)
        r = await client.post("/api/jobs", json={"url": URL, "output": "video"})
        assert r.status_code == 202
        deadline = time.monotonic() + 5
        while (await client.get(f"/api/jobs/{r.json()['job_id']}")).json()["status"] != "ready":
            assert time.monotonic() < deadline
            await asyncio.sleep(0.02)
        stub.gate.set()


async def test_refused_renders(client, settings):
    source_id = await ready_source(client)

    async def refused(body, status, code, fragment=None):
        r = await client.post(f"/api/sources/{source_id}/renders", json=body)
        assert r.status_code == status, r.text
        error = r.json()["error"]
        assert error["code"] == code
        if fragment:
            assert fragment in error["message"], error["message"]

    await refused({"clips": []}, 400, "invalid_request", "1 to 20")
    await refused({"clips": [{"start": 5, "end": 2}]}, 400, "invalid_request", "Clip 1")
    await refused({"clips": [{"start": 0, "end": 400}]}, 400, "invalid_clip", "after the end of the video")
    await refused({"clips": [{"start": 40, "end": 45}]}, 400, "invalid_clip", "Clip 1")
    await refused({"clips": [{"start": 0, "end": 5}, {"start": 3, "end": 3.2}]}, 400, "invalid_clip", "Clip 2")
    await refused({"clips": [{"start": 0, "end": 5}], "spec": {"branding": {"logo": "A" * 22}}}, 404, "asset_not_found")
    await refused({"clips": [{"start": 0, "end": 5}], "spec": {"template": "nope"}}, 400, "invalid_request")
    r = await client.post("/api/sources/" + "A" * 22 + "/renders", json={"clips": [{"start": 0, "end": 5}]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "source_not_found"
    r = await client.post(f"/api/sources/{source_id}/renders", content=b"{}", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    r = await client.get("/api/renders/" + "B" * 22)
    assert r.status_code == 404 and r.json() == error_body("render_not_found")


async def test_clips_over_the_length_limit_are_refused(settings, stub):
    rs = load_render_settings({"RENDER_MAX_CLIP_SECONDS": "10"})
    async with running(app_for(settings, stub, render_settings=rs)) as client:
        source_id = await ready_source(client)
        r = await client.post(f"/api/sources/{source_id}/renders", json={"clips": [{"start": 0, "end": 20}]})
        assert r.status_code == 400 and "10-second" in r.json()["error"]["message"]


async def test_render_bodies_have_their_own_cap(client):
    source_id = await ready_source(client)
    cues = [{"start": i * 0.1, "end": i * 0.1 + 0.05, "text": "x" * 190} for i in range(50)]
    body = {"clips": [{"start": 0, "end": 10, "cues": cues}] * 20}
    assert len(json.dumps(body)) > 64 * 1024  # over the Guard's cap, under the route's 2 MiB
    r = await client.post(f"/api/sources/{source_id}/renders", json=body)
    assert r.status_code == 202, r.text
    huge = json.dumps({"clips": [{"start": 0, "end": 1}], "pad": "x" * (3 * 1024 * 1024)})
    r = await client.post(f"/api/sources/{source_id}/renders", content=huge,
                          headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_render_posts_still_need_the_right_origin(client):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/renders", json={"clips": [{"start": 0, "end": 5}]},
                          headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden_origin"


async def test_a_full_disk_fails_the_clip(settings, monkeypatch):
    stub = RendererStub()
    async with running(app_for(settings, stub)) as client:
        from app.jobs import JobManager

        monkeypatch.setattr(JobManager, "reserve_disk", lambda self, key, path, nbytes: False)
        source_id = await ready_source(client)
        view = await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                                     {"template": "educational"}))
        assert view["status"] == "failed" and view["clips"][0]["error"]["code"] == "disk_full"
        assert renders(stub) == []


# --- ZIP ------------------------------------------------------------------------------------------

async def test_the_zip_holds_every_finished_clip_and_its_subtitles(client, stub, temp_dir):
    source_id = await ready_source(client)
    put_transcript(temp_dir / source_id, worded_transcript(30.0))
    render_id = await start(client, source_id, [{"start": 2, "end": 8}, {"start": 10, "end": 14}],
                            {"template": "educational", "captions": {"export": ["srt"]}})
    await poll_render(client, render_id)
    r = await client.get(f"/api/renders/{render_id}/zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "Stub Video (2 shorts).zip" in r.headers["content-disposition"].replace("%20", " ").replace("%28", "(") \
        .replace("%29", ")")
    bundle = zipfile.ZipFile(io.BytesIO(r.content))
    assert bundle.testzip() is None
    assert bundle.namelist() == ["01 Stub Video (short 1, 0m02s-0m08s).mp4", "01 Stub Video (short 1, 0m02s-0m08s).srt",
                                 "02 Stub Video (short 2, 0m10s-0m14s).mp4", "02 Stub Video (short 2, 0m10s-0m14s).srt"]
    assert bundle.getinfo(bundle.namelist()[0]).compress_type == zipfile.ZIP_STORED
    plain = zipfile.ZipFile(io.BytesIO((await client.get(f"/api/renders/{render_id}/zip?subtitles=false")).content))
    assert [n[-4:] for n in plain.namelist()] == [".mp4", ".mp4"]


async def test_no_zip_before_a_clip_is_ready(settings):
    stub = RendererStub(block=(1,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        render_id = await start(client, source_id, [{"start": 0, "end": 5}], {"template": "educational"})
        await asyncio.wait_for(stub.started.wait(), 5)
        r = await client.get(f"/api/renders/{render_id}/zip")
        assert r.status_code == 409
        stub.gate.set()


# --- previews -------------------------------------------------------------------------------------

async def poll_preview(client, preview_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        view = (await client.get(f"/api/shorts/previews/{preview_id}")).json()
        if view["status"] in ("ready", "failed", "cancelled"):
            return view
        assert time.monotonic() < deadline, view
        await asyncio.sleep(0.02)


async def test_a_preview_still_and_a_draft_clip(client, stub):
    source_id = await ready_source(client)
    r = await client.post(f"/api/sources/{source_id}/shorts/previews",
                          json={"clip": {"start": 2, "end": 8}, "spec": {"template": "educational"}, "at": 1.5})
    assert r.status_code == 202
    view = await poll_preview(client, r.json()["preview_id"])
    assert view["status"] == "ready" and view["kind"] == "frame"
    image = await client.get(f"/api/shorts/previews/{view['preview_id']}/file")
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"
    req = renders(stub)[-1]
    assert (req["kind"], req["quality"], req["at"]) == ("frame", "draft", 1.5)
    r = await client.post(f"/api/sources/{source_id}/shorts/previews",
                          json={"clip": {"start": 0, "end": 29}, "spec": {"template": "educational"}, "kind": "clip"})
    view = await poll_preview(client, r.json()["preview_id"])
    assert view["status"] == "ready" and renders(stub)[-1]["end"] == 29.0  # under the 30 s draft cap
    video = await client.get(f"/api/shorts/previews/{view['preview_id']}/file")
    assert video.headers["content-type"] == "video/mp4"


async def test_the_newest_preview_wins(settings):
    stub = RendererStub(block=(1,))
    async with running(app_for(settings, stub)) as client:
        source_id = await ready_source(client)
        body = {"clip": {"start": 2, "end": 8}, "spec": {"template": "educational"}}
        first = (await client.post(f"/api/sources/{source_id}/shorts/previews", json=body)).json()["preview_id"]
        await asyncio.wait_for(stub.started.wait(), 5)
        second = (await client.post(f"/api/sources/{source_id}/shorts/previews", json=body)).json()["preview_id"]
        assert (await poll_preview(client, first))["status"] == "cancelled"
        assert (await poll_preview(client, second))["status"] == "ready"
        r = await client.post(f"/api/sources/{source_id}/shorts/previews", json={**body, "at": 99})
        assert r.status_code == 400


# --- brand assets --------------------------------------------------------------------------------------

async def test_logo_upload_is_decoded_and_used(client, stub, render_media, temp_dir):
    source_id = await ready_source(client)
    data = render_media["logo.png"].read_bytes()
    r = await client.post(f"/api/sources/{source_id}/shorts/assets?kind=logo", content=data,
                          headers={"content-type": "image/png"})
    assert r.status_code == 200, r.text
    asset = r.json()
    assert (asset["kind"], asset["width"], asset["height"]) == ("logo", 200, 100)
    stored = list((temp_dir / source_id / "render" / "assets").iterdir())
    assert [p.suffix for p in stored] == [".rgba"]  # only the decoded pixels are kept
    await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                          {"template": "educational", "branding": {"logo": asset["asset_id"]}}))
    logo = renders(stub)[-1]["logo"]
    assert (logo["width"], logo["height"], logo["position"]) == (200, 100, "top-right")


async def test_unsafe_or_wrong_assets_are_refused(client, stub):
    source_id = await ready_source(client)
    url = f"/api/sources/{source_id}/shorts/assets"
    r = await client.post(f"{url}?kind=logo", content=b"GIF89a" + bytes(100), headers={"content-type": "image/png"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_asset"
    r = await client.post(f"{url}?kind=logo", content=bytes(3 * 1024 * 1024),
                          headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    r = await client.post(f"{url}?kind=logo", content=b"x", headers={"content-type": "text/html"})
    assert r.status_code == 415
    r = await client.post(f"{url}?kind=script", content=b"x", headers={"content-type": "image/png"})
    assert r.status_code == 400
    stub.video_seconds = 40.0
    r = await client.post(f"{url}?kind=intro", content=bytes(1000), headers={"content-type": "video/mp4"})
    assert r.status_code == 400 and "15 seconds" in r.json()["error"]["message"]


async def test_intro_and_outro_go_to_the_renderer(client, stub):
    source_id = await ready_source(client)
    url = f"/api/sources/{source_id}/shorts/assets"
    intro = (await client.post(f"{url}?kind=intro", content=bytes(1000), headers={"content-type": "video/mp4"})).json()
    outro = (await client.post(f"{url}?kind=outro", content=bytes(1000), headers={"content-type": "video/mp4"})).json()
    await poll_render(client, await start(client, source_id, [{"start": 0, "end": 5}],
                                          {"template": "educational",
                                           "branding": {"intro": intro["asset_id"], "outro": outro["asset_id"]}}))
    req = renders(stub)[-1]
    assert req["intro"].endswith(".video") and req["outro"].endswith(".video")
    r = await client.post(f"/api/sources/{source_id}/renders",
                          json={"clips": [{"start": 0, "end": 5}], "spec": {"branding": {"intro": outro["asset_id"]}}})
    assert r.status_code == 404  # an outro isn't an intro


# --- acceptance: real rendering ---------------------------------------------------------------------------

@pytest.fixture
async def real(settings, face_media):
    runner = RealRenderRunner(face_media["speakers.mp4"], 7.0)
    async with running(make_app(settings, run_worker=runner)) as client:
        yield client, runner


async def test_acceptance_a_highlight_becomes_a_captioned_9_16_short(real, temp_dir):
    client, _runner = real
    source_id = await ready_source(client)
    put_transcript(temp_dir / source_id, worded_transcript(7.0, text="Electric vehicles are getting cheaper "
                                                           "every single year now"))
    render_id = await start(client, source_id, [{"start": 1.0, "end": 5.0, "title": "Highlight"}],
                            {"template": "talking_head", "platform": "youtube_shorts", "quality": "draft",
                             "captions": {"export": ["srt"]}})
    view = await poll_render(client, render_id, timeout=120)
    assert view["status"] == "ready", view
    clip = view["clips"][0]
    assert clip["mode"] == "face" and clip["captions_timing"] == "words" and (clip["width"], clip["height"]) == (540, 960)
    r = await client.get(f"/api/renders/{render_id}/clips/1/file")
    assert r.status_code == 200
    out = temp_dir / "out.mp4"
    out.write_bytes(r.content)
    probe = ffprobe(out)
    assert (probe["video"]["codec_name"], probe["video"]["pix_fmt"], probe["audio"]["codec_name"]) == \
        ("h264", "yuv420p", "aac")
    assert (probe["video"]["width"], probe["video"]["height"]) == (540, 960) and abs(probe["duration"] - 4.0) < 0.1
    srt = (await client.get(f"/api/renders/{render_id}/clips/1/file?kind=srt")).text
    first_cue = srt.split("\n")[1]
    start_s = float(first_cue[6:8]) + float(first_cue[9:12]) / 1000
    frame = frame_rgb(out, start_s + 0.2)
    band = frame[int(0.55 * 960):int(0.85 * 960)]
    assert int((band.max(axis=2) > 200).sum()) > 100  # the caption is burned in while it is said


async def test_acceptance_several_highlights_export_as_a_zip(real, temp_dir):
    client, _runner = real
    source_id = await ready_source(client)
    render_id = await start(client, source_id, [{"start": 0.5, "end": 3.0}, {"start": 3.5, "end": 6.5}],
                            {"template": "podcast_split", "quality": "draft"})
    view = await poll_render(client, render_id, timeout=120)
    assert view["status"] == "ready" and view["ready"] == 2, view
    r = await client.get(f"/api/renders/{render_id}/zip")
    assert r.status_code == 200
    bundle = zipfile.ZipFile(io.BytesIO(r.content))
    assert bundle.testzip() is None and len(bundle.namelist()) == 2
    for name, length in zip(bundle.namelist(), (2.5, 3.0)):
        path = temp_dir / "z.mp4"
        path.write_bytes(bundle.read(name))
        probe = ffprobe(path)
        assert (probe["video"]["width"], probe["video"]["height"]) == (540, 960)
        assert abs(probe["duration"] - length) < 0.1
        frame = frame_rgb(path, 1.0)
        assert colour_at(frame, 0.5, 0.5) != "black"
