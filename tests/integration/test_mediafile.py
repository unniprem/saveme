"""app.mediafile: local-media helpers, run through the app/bin wrappers against the real
ffmpeg and ffprobe on PATH."""
import shutil

from mutagen.mp4 import MP4, MP4Cover

from app.mediafile import Probe, probe


def test_probe_reports_kinds_duration_and_codecs(media):
    found = probe(media["sample.mp4"])
    assert isinstance(found, Probe)
    assert found.kinds == {"video", "audio"}
    assert abs(found.duration - 2.0) < 0.1
    assert "mov" in found.format_name
    assert found.codecs == ("h264", "aac")


def test_probe_returns_none_for_junk(media):
    # Random bytes. (junk.jpg won't do: a full ffprobe opens it as a still image by its name.)
    assert probe(media["big.mp4"]) is None


def test_probe_ignores_cover_art(media, tmp_path):
    path = tmp_path / "media.mp4"
    shutil.copy(media["sample.mp4"], path)
    tagged = MP4(path)
    tagged["covr"] = [MP4Cover(media["cover.png"].read_bytes(), imageformat=MP4Cover.FORMAT_PNG)]
    tagged.save()
    found = probe(path)
    assert found.kinds == {"video", "audio"}
    assert found.codecs == ("h264", "aac")
