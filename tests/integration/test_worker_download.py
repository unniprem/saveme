"""Worker download mode. Subprocess tests run the real worker (guard installed
first) against the local media server, each into a fresh `tmp_path / "job"`
workdir; the in-process tests cover the progress hook, the MP4 fallback and
request validation without any network."""
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.errors import CATALOG, AppError, too_large_message
from tests.support.media import real_ffmpeg
from tests.support.procs import ROOT, run_worker_proc

MiB = 1024 * 1024
HOSTILE_TITLE = "../../x; $(rm -rf /)"


@pytest.fixture
def job(tmp_path) -> Path:
    workdir = tmp_path / "job"  # the parent (JobManager) creates the workdir
    workdir.mkdir()
    return workdir


def download(media_server, path: str, workdir: Path, *, output: str = "video",
             quality: str = "best", max_bytes: int = 50 * MiB) -> tuple[list[dict], int]:
    return run_worker_proc({"mode": "download", "url": media_server.url(path), "output": output,
                            "quality": quality, "workdir": str(workdir), "max_bytes": max_bytes})


def result_of(events: list[dict], rc: int) -> dict:
    assert rc == 0 and events and events[-1]["event"] == "result", events
    return events[-1]["data"]


def error_of(events: list[dict], rc: int) -> dict:
    assert rc == 1 and events and events[-1]["event"] == "error", events
    return events[-1]


def listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def probe(path: Path) -> dict:
    """ffprobe (the real one on PATH) as JSON: streams and container duration."""
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_name,codec_type:format=duration", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)


def progress_of(events: list[dict]) -> list[dict]:
    return [e for e in events if e["event"] == "progress"]


def phases_of(events: list[dict]) -> list[str]:
    return [e["detail"] for e in events if e["event"] == "phase" and e["phase"] == "converting"]


# --- Subprocess: the real worker against the local media server -----------

def test_video_direct_mp4(media_server, job):
    data = result_of(*download(media_server, "/media/sample.mp4", job))
    assert data["file"] == "media.mp4" and data["ext"] == "mp4" and data["notice"] is None
    assert sorted(s["codec_name"] for s in probe(job / "media.mp4")["streams"]) == ["aac", "h264"]
    assert listing(job) == ["media.mp4"]


def test_audio_mp3(media_server, job):
    events, rc = download(media_server, "/media/sample.mp4", job, output="audio")
    data = result_of(events, rc)
    assert data["file"] == "media.mp3" and data["ext"] == "mp3"
    info = probe(job / "media.mp3")
    assert [s["codec_name"] for s in info["streams"]] == ["mp3"]
    assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.3)
    assert "Converting to MP3" in phases_of(events)
    assert listing(job) == ["media.mp3"]


def test_hls_merge_to_mp4(media_server, job):
    events, rc = download(media_server, "/hls/master.m3u8", job)
    data = result_of(events, rc)
    details = [e["detail"] for e in progress_of(events)]
    # The fixture has one video rendition and one audio rendition: two tracks.
    assert "Downloading video track (1 of 2)" in details
    assert "Downloading audio track (2 of 2)" in details
    assert "Merging video and audio" in phases_of(events)
    assert data["file"] == "media.mp4" and data["ext"] == "mp4"
    assert sorted(s["codec_type"] for s in probe(job / "media.mp4")["streams"]) == ["audio", "video"]
    assert listing(job) == ["media.mp4"]


def test_quality_downgrade_notice(media_server, job):
    data = result_of(*download(media_server, "/hls/master.m3u8", job, quality="720"))
    assert data["notice"] == "720p wasn't available; downloaded 360p."


def test_audio_from_video_only_fails(media_server, job):
    error = error_of(*download(media_server, "/media/video_only.mp4", job, output="audio"))
    assert error["code"] == "no_audio" and error["message"] == CATALOG["no_audio"][1]
    assert listing(job) == []


def test_video_from_audio_only_fails_before_download(media_server, job):
    # audio/mpeg is known to have no video track: refused after extraction alone.
    before = media_server.hits["/media/sample.mp3"]
    error = error_of(*download(media_server, "/media/sample.mp3", job))
    assert error["code"] == "no_video"
    assert media_server.hits["/media/sample.mp3"] - before == 1  # the extractor's request only
    assert listing(job) == []


@pytest.fixture(scope="module")
def audio_in_mp4(media) -> str:
    """An AAC-only MP4: served as video/mp4 with unknown codecs, so only probing
    the downloaded file shows it has no video."""
    root = media["sample.mp4"].parent
    subprocess.run([real_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:a", "aac",
                    str(root / "audio_only.mp4")], capture_output=True, timeout=60, check=True)
    return "/media/audio_only.mp4"


def test_video_without_video_stream_fails(media_server, job, audio_in_mp4):
    error = error_of(*download(media_server, audio_in_mp4, job))
    assert error["code"] == "no_video" and error["message"] == CATALOG["no_video"][1]
    assert listing(job) == []


@pytest.mark.parametrize("path", ["/media/big.mp4", "/chunked/big.mp4"],
                         ids=["content_length", "chunked"])
def test_size_limit_aborts(media_server, job, path):
    error = error_of(*download(media_server, path, job, max_bytes=MiB))
    assert error == {"event": "error", "code": "too_large", "message": too_large_message(MiB)}
    assert listing(job) == []  # no media.*, .part or fragment files left behind


def test_hostile_title_stays_in_workdir(media_server, job, tmp_path):
    outside = {d: listing(d) for d in (tmp_path.parent, ROOT)}  # ../../x lands in tmp_path.parent
    path = "/page?title=../../x;%20$(rm%20-rf%20/)&video=/media/sample.mp4"
    data = result_of(*download(media_server, path, job))
    assert data["file"] == "media.mp4" and data["title"] == HOSTILE_TITLE
    assert listing(job) == ["media.mp4"]
    assert listing(tmp_path) == ["job"]
    assert {d: listing(d) for d in outside} == outside


def test_percent_only_with_exact_total(media_server, tmp_path):
    chunked, plain = tmp_path / "chunked", tmp_path / "plain"
    chunked.mkdir()
    plain.mkdir()
    events, rc = download(media_server, "/chunked/sample.mp4", chunked)
    result_of(events, rc)
    progress = progress_of(events)
    assert progress and all(e["percent"] is None and e["total_bytes"] is None for e in progress)
    events, rc = download(media_server, "/media/sample.mp4", plain)
    result_of(events, rc)
    percents = [e["percent"] for e in progress_of(events)]
    assert percents and None not in percents and percents[-1] >= 99


def test_side_door_canary(media_server, job):
    # /evil.mp4 is an HLS playlist served as video/mp4; converting it to MP3 must
    # not let ffprobe/ffmpeg follow it to /canary.ts. The job's outcome doesn't matter.
    before = media_server.hits["/evil.mp4"]
    download(media_server, "/evil.mp4", job, output="audio")
    assert media_server.hits["/evil.mp4"] > before
    assert media_server.hits["/canary.ts"] == 0


# --- In-process: progress hook, MP4 fallback, request validation ----------

def test_progress_hook_throttles_and_caps():
    from app.worker import _Progress, _TooLarge

    now = [0.0]
    events: list[dict] = []
    hook = _Progress(events.append, max_bytes=1000, clock=lambda: now[0])
    hook.parts = [{"format_id": "v", "vcodec": "avc1"}, {"format_id": "a", "vcodec": "none"}]

    def tick(fid: str, status: str, done: int, **extra) -> None:
        hook({"status": status, "downloaded_bytes": done, "filename": f"/job/media.f{fid}.mp4",
              "info_dict": {"format_id": fid}, **extra})

    tick("v", "downloading", 100, total_bytes=400)  # first of a part: always sent
    tick("v", "downloading", 200, total_bytes=400)  # < 0.25 s later: dropped
    now[0] = 0.3
    tick("v", "downloading", 300, total_bytes=400)
    tick("v", "finished", 400, total_bytes=400)  # last of a part: always sent
    tick("a", "downloading", 100, fragment_index=1, fragment_count=4, total_bytes_estimate=900)
    tick("a", "downloading", 200, fragment_index=2, fragment_count=4)  # throttled
    assert [(e["detail"], e["downloaded_bytes"], e["total_bytes"], e["percent"]) for e in events] == [
        ("Downloading video track (1 of 2)", 100, 400, 25.0),
        ("Downloading video track (1 of 2)", 300, 400, 75.0),
        ("Downloading video track (1 of 2)", 400, 400, 100.0),
        ("Downloading audio track (2 of 2)", 100, None, 25.0),
    ]
    assert all(e["event"] == "progress" for e in events)
    with pytest.raises(_TooLarge):  # 400 (video) + 601 (audio) > 1000, across parts
        tick("a", "downloading", 601, fragment_index=2, fragment_count=4)


def test_estimate_alone_gives_no_percent():
    from app.worker import _Progress

    events: list[dict] = []
    hook = _Progress(events.append, max_bytes=1000)
    base = {"filename": "/job/media.mp4", "info_dict": {}, "total_bytes_estimate": 50}
    hook({**base, "status": "downloading", "downloaded_bytes": 10})
    # A chunked "finished" reports its byte count as total_bytes; that is not a known total.
    hook({**base, "status": "finished", "downloaded_bytes": 20, "total_bytes": 20})
    assert [(e["detail"], e["total_bytes"], e["percent"]) for e in events] == [
        ("Downloading", None, None), ("Downloading", None, None)]


@pytest.mark.parametrize("name, outcome", [
    ("media.mkv", "Delivered as MKV (couldn't convert to MP4)."),
    ("media.ogv", "unsupported"),  # no MIME type to deliver it with
], ids=["kept_with_notice", "undeliverable"])
def test_failed_mp4_remux(tmp_path, name, outcome):
    from app.worker import _to_mp4

    src = tmp_path / name
    src.write_bytes(b"not a media file")
    if outcome == "unsupported":
        with pytest.raises(AppError) as ei:
            _to_mp4(src)
        assert ei.value.code == "unsupported"
    else:
        assert _to_mp4(src) == (src, outcome)
    assert listing(tmp_path) == [name]  # no half-written media.mp4


@pytest.mark.parametrize("change", [
    {"output": "gif"}, {"quality": "4k"}, {"workdir": "relative/job"}, {"workdir": None},
    {"max_bytes": "1"}, {"max_bytes": 0}, {"max_bytes": True},
], ids=["output", "quality", "relative_workdir", "no_workdir", "str_limit", "zero_limit", "bool_limit"])
def test_malformed_download_request_is_internal(tmp_path, change):
    from app import worker

    req = {"mode": "download", "url": "https://x.example/v", "output": "video", "quality": "best",
           "workdir": str(tmp_path), "max_bytes": MiB, **change}
    out = io.StringIO()
    rc = worker.serve(io.StringIO(json.dumps(req)), out)
    assert rc == 1 and [json.loads(line)["code"] for line in out.getvalue().splitlines()] == ["internal"]
