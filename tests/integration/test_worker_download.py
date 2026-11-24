"""Worker download mode. Subprocess tests run the real worker (guard installed
first) against the local media server, each into a fresh `tmp_path / "job"`
workdir; the in-process tests cover the progress hook, the MP4 fallback and
request validation without any network."""
import io
import json
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

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


def tags(path: Path) -> dict[str, str]:
    """Container tags by the real ffprobe, keys lower-cased; {} when there are none."""
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format_tags",
                           "-of", "json", str(path)], capture_output=True, text=True, timeout=30, check=True)
    found = json.loads(proc.stdout).get("format", {}).get("tags", {})
    return {k.lower(): v for k, v in found.items()}


def probe_streams_with_bitrate(path: Path) -> list[dict]:
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries",
                           "stream=codec_name,bit_rate", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=30, check=True)
    return json.loads(proc.stdout)["streams"]


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


def test_download_result_includes_duration(media_server, job):
    data = result_of(*download(media_server, "/media/sample.mp4", job))
    assert abs(data["duration"] - 2.0) < 0.1


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


def test_m4a_is_a_stream_copy_of_aac(media_server, job):
    events, rc = download(media_server, "/hls/aac/master.m3u8", job, output="m4a")
    data = result_of(events, rc)
    assert data["file"] == "media.m4a" and data["ext"] == "m4a"
    s = probe_streams_with_bitrate(job / "media.m4a")  # 64k source; a re-encode would be ~128k
    assert [x["codec_name"] for x in s] == ["aac"] and int(s[0]["bit_rate"]) < 80_000
    assert "Preparing M4A" in phases_of(events) and "Converting to MP3" not in phases_of(events)
    assert "Converting to MP4" not in phases_of(events)
    assert listing(job) == ["media.m4a"]


def test_m4a_without_aac_fails_before_download(media_server, job):
    before = media_server.hits["/hls/aac/a_000.ts"]
    assert error_of(*download(media_server, "/hls/opus/master.m3u8", job, output="m4a"))["code"] == "no_audio"
    assert media_server.hits["/hls/aac/a_000.ts"] == before and listing(job) == []


def test_m4a_from_unknown_codec_direct_link_is_refused(media_server, job):
    assert error_of(*download(media_server, "/media/sample.mp4", job, output="m4a"))["code"] == "no_audio"


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


PHRASE = "Requested%20format%20is%20not%20available"


@pytest.mark.parametrize("output", ["video", "audio"])
def test_missing_track_phrase_in_url_is_not_a_missing_track(media_server, job, output):
    # yt-dlp echoes the URL (and an id made from it) in its errors; that text must
    # not turn a 404 into no_video / no_audio.
    error = error_of(*download(media_server, f"/nope/{PHRASE}.mp4", job, output=output))
    assert error["code"] == "unavailable"
    assert listing(job) == []


def test_blocked_marker_wins_over_missing_track_phrase(media_server, job):
    path = f"/redirect?to=http://169.254.169.254/{PHRASE}.mp4"
    assert error_of(*download(media_server, path, job))["code"] == "blocked_address"


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
    assert tags(job / "media.mp4")["title"] == HOSTILE_TITLE
    assert listing(tmp_path) == ["job"]
    assert {d: listing(d) for d in outside} == outside


def test_mp3_and_mp4_carry_title_and_no_source_link(media_server, job, tmp_path):
    page = "/page?title=Tagged%20Song&video=/media/sample.mp4"
    events, rc = download(media_server, page, job, output="audio")
    result_of(events, rc)
    t = tags(job / "media.mp3")
    assert t["title"] == "Tagged Song"
    assert not any(media_server.url("") in v for v in t.values())
    assert "Adding tags" in phases_of(events)
    assert listing(job) == ["media.mp3"]          # no .meta / .temp files left
    other = tmp_path / "v"
    other.mkdir()
    result_of(*download(media_server, page, other))
    t = tags(other / "media.mp4")
    assert t["title"] == "Tagged Song" and not any(media_server.url("") in v for v in t.values())


def test_huge_description_is_cut_in_the_tags(media_server, job):
    # 205 KiB in one ffmpeg argument is over Linux's 128 KiB limit (E2BIG). macOS has no
    # such limit, so this checks the cut rather than the failure.
    page = "/page?title=T&video=/media/sample.mp4&desc=%E2%82%AC&desc_times=70000"
    result_of(*download(media_server, page, job))
    t = tags(job / "media.mp4")
    assert t["description"] == t["synopsis"] == "\u20ac" * 5461  # 16383 bytes of UTF-8


def test_source_comment_link_is_removed(media_server, job):
    result_of(*download(media_server, "/media/tagged.mp4", job))
    assert not any("leak.example" in v for v in tags(job / "media.mp4").values())


def cover_page(media_server, thumb: str) -> str:
    return f"/page?title=T&video=/media/sample.mp4&thumb={quote(thumb, safe='')}"


def test_mp3_gets_jpeg_cover(media_server, media, job):
    result_of(*download(media_server, cover_page(media_server, media_server.url("/media/cover.jpg")),
                        job, output="audio"))
    assert [f.data for f in ID3(job / "media.mp3").getall("APIC")] == [media["cover.jpg"].read_bytes()]
    assert listing(job) == ["media.mp3"]


def test_mp4_gets_png_cover(media_server, media, job):
    result_of(*download(media_server, cover_page(media_server, media_server.url("/media/cover.png")), job))
    assert [bytes(c) for c in MP4(job / "media.mp4").tags["covr"]] == [media["cover.png"].read_bytes()]


def test_webp_thumbnail_is_never_fetched(media_server, job):
    result_of(*download(media_server, cover_page(media_server, media_server.url("/media/cover.webp")), job))
    assert "covr" not in (MP4(job / "media.mp4").tags or {})
    assert media_server.hits["/media/cover.webp"] == 0


@pytest.mark.parametrize("thumb", ["/media/junk.jpg", "/missing.jpg"])
def test_junk_and_missing_thumbnails_still_deliver(media_server, job, thumb):
    result_of(*download(media_server, cover_page(media_server, media_server.url(thumb)), job))
    assert "covr" not in (MP4(job / "media.mp4").tags or {})
    assert listing(job) == ["media.mp4"]


def test_blocked_thumbnail_still_delivers(media_server, job):
    events, rc = download(media_server, cover_page(media_server, "http://169.254.169.254/x.jpg"), job)
    result_of(events, rc)
    assert not any(e["event"] == "error" for e in events)
    assert "covr" not in (MP4(job / "media.mp4").tags or {})


def test_trickling_thumbnail_still_delivers_on_time(media_server, job, monkeypatch):
    # /slow/ sends 1 KiB every 0.5 s, so no read ever times out and the 64 KiB image would
    # take 32 s. The worker gives up after its cover budget (1 s here) instead.
    monkeypatch.setenv("SAVEME_TEST_COVER_BUDGET", "1")
    start = time.monotonic()
    result_of(*download(media_server, cover_page(media_server, media_server.url("/slow/big.jpg")), job))
    assert time.monotonic() - start < 20
    assert media_server.hits["/slow/big.jpg"] == 1
    assert "covr" not in (MP4(job / "media.mp4").tags or {})
    assert listing(job) == ["media.mp4"]


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


def test_kept_track_counts_toward_the_cap():
    from app.worker import _Progress, _TooLarge

    events: list[dict] = []
    hook = _Progress(events.append, max_bytes=1000)
    # A track a cancelled run finished: yt-dlp reports only its size, as total_bytes.
    hook({"status": "finished", "total_bytes": 900, "filename": "/job/media.fv.mp4",
          "info_dict": {"format_id": "v"}})
    assert events[-1]["downloaded_bytes"] == 900
    with pytest.raises(_TooLarge):
        hook({"status": "downloading", "downloaded_bytes": 101, "total_bytes": 200,
              "filename": "/job/media.fa.m4a", "info_dict": {"format_id": "a"}})


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


def test_missing_format_judged_by_ytdlp_message_only():
    from yt_dlp.utils import DownloadError, ExtractorError

    from app.worker import _is_missing_format

    real = ExtractorError("Requested format is not available. Use --list-formats for a list of "
                          "available formats", expected=True, video_id="v1", ie="generic")
    echoed = ExtractorError("Unable to download webpage: HTTP Error 404: Not Found",
                            video_id="Requested format is not available", ie="generic")
    assert _is_missing_format(real)
    assert _is_missing_format(DownloadError(f"ERROR: {real}", (ExtractorError, real, None)))
    assert not _is_missing_format(echoed)
    assert not _is_missing_format(DownloadError(f"ERROR: {echoed}", (ExtractorError, echoed, None)))
    assert not _is_missing_format(DownloadError("ERROR: Requested format is not available"))


@pytest.mark.parametrize("name, outcome", [
    ("media.mkv", "Delivered as MKV (couldn't convert to MP4)."),
    ("media.ogv", "unsupported"),  # no MIME type to deliver it with
], ids=["kept_with_notice", "undeliverable"])
def test_failed_mp4_remux(tmp_path, name, outcome):
    from app.mediafile import remux_to_mp4 as _to_mp4

    src = tmp_path / name
    src.write_bytes(b"not a media file")
    if outcome == "unsupported":
        with pytest.raises(AppError) as ei:
            _to_mp4(src)
        assert ei.value.code == "unsupported"
    else:
        assert _to_mp4(src) == (src, outcome)
    assert listing(tmp_path) == [name]  # no half-written media.mp4


def test_remux_to_mp4_keeps_chapters_and_title(tmp_path):
    from app.mediafile import remux_to_mp4 as _to_mp4

    meta = tmp_path / "ch.meta"
    meta.write_text(";FFMETADATA1\ntitle=Kept\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
                    "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n")
    src = tmp_path / "media.mkv"
    subprocess.run([real_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25", "-i", str(meta),
                    "-map_metadata", "1", "-map_chapters", "1", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
                   check=True, capture_output=True, timeout=60)
    dst, notice = _to_mp4(src)
    assert dst.name == "media.mp4" and notice is None
    proc = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_chapters", "-of", "json", str(dst)],
                          capture_output=True, text=True, timeout=30, check=True)
    assert len(json.loads(proc.stdout)["chapters"]) == 2
    assert tags(dst)["title"] == "Kept"


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
