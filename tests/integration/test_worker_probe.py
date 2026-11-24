"""Worker `probe` mode: an uploaded file is checked and becomes media.<ext> (spec §6.5).
Each test runs the real worker subprocess on a copy of a generated file."""
import os
import shutil
from pathlib import Path

import pytest

from app.mediafile import Probe, container_ext, probe
from tests.support.events import error_of, listing, result_of
from tests.support.procs import run_worker_proc


@pytest.fixture
def job(tmp_path) -> Path:
    workdir = tmp_path / "job"
    workdir.mkdir()
    return workdir


def check_upload(src: Path, job: Path) -> tuple[list[dict], int]:
    dest = job / "upload.bin"
    shutil.copy(src, dest)
    return run_worker_proc({"mode": "probe", "source": str(dest), "workdir": str(job)})


def test_probe_keeps_mp4(media, job):
    data = result_of(*check_upload(media["sample.mp4"], job))
    assert (data["file"], data["ext"], data["notice"]) == ("media.mp4", "mp4", None)
    assert abs(data["duration"] - 2.0) < 0.1
    assert listing(job) == ["media.mp4"]


@pytest.mark.parametrize("name", ["sample.mkv", "sample.ts"])
def test_probe_remuxes_mkv_and_ts_to_mp4(media, job, name):
    data = result_of(*check_upload(media[name], job))
    assert (data["file"], data["ext"]) == ("media.mp4", "mp4")
    assert abs(data["duration"] - 2.0) < 0.2
    assert listing(job) == ["media.mp4"]
    assert probe(job / "media.mp4").codecs == ("h264", "aac")


def test_probe_webm_becomes_mp4_or_stays_webm(media, job):
    data = result_of(*check_upload(media["sample.webm"], job))
    assert data["ext"] in {"mp4", "webm"}
    assert listing(job) == [data["file"]]


def test_probe_finds_the_duration_of_a_live_webm(media, job):
    # A browser recording has no duration in its header; the MP4 remux writes one.
    assert probe(media["live.webm"]).duration is None
    data = result_of(*check_upload(media["live.webm"], job))
    assert data["ext"] == "mp4" and abs(data["duration"] - 2.0) < 0.2
    assert listing(job) == ["media.mp4"]


def test_probe_refuses_audio_only(media, job):
    assert error_of(*check_upload(media["sample.mp3"], job))["code"] == "no_video"
    assert listing(job) == []


def test_probe_refuses_junk(media, job):
    assert error_of(*check_upload(media["big.mp4"], job))["code"] == "unreadable_media"
    assert listing(job) == []


def test_probe_refuses_source_outside_workdir(media, job, tmp_path):
    outside = tmp_path / "upload.bin"
    shutil.copy(media["sample.mp4"], outside)
    events = run_worker_proc({"mode": "probe", "source": str(outside), "workdir": str(job)})
    assert error_of(*events)["code"] == "internal"
    assert outside.is_file()


def test_probe_refuses_symlinked_source(media, job):
    os.symlink(media["sample.mp4"], job / "upload.bin")
    events = run_worker_proc({"mode": "probe", "source": str(job / "upload.bin"), "workdir": str(job)})
    assert error_of(*events)["code"] == "internal"
    assert media["sample.mp4"].is_file()


def _probe(format_name: str, *codecs: str) -> Probe:
    return Probe(kinds=frozenset({"video"}), duration=1.0, format_name=format_name, codecs=codecs)


@pytest.mark.parametrize("found, ext", [
    (_probe("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac"), "mp4"),
    (_probe("matroska,webm", "vp9", "opus"), "webm"),
    (_probe("matroska,webm", "av1", "vorbis"), "webm"),
    (_probe("matroska,webm", "vp8"), "webm"),
    (_probe("matroska,webm", "h264", "aac"), "mkv"),
    (_probe("matroska,webm", "vp9", "aac"), "mkv"),
    (_probe("mpegts", "h264", "aac"), "ts"),
    (_probe("flv", "h264", "aac"), "flv"),
    (_probe("avi", "mpeg4", "mp3"), "avi"),
    (_probe("ogg", "theora", "vorbis"), None),
    (_probe("image2", "mjpeg"), None),
    (_probe("", "h264"), None),
])
def test_container_ext_mapping(found, ext):
    assert container_ext(found) == ext
