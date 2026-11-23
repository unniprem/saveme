"""embed_cover on real MP3 and MP4 files, checked back with mutagen."""
import shutil

from mutagen.id3 import ID3
from mutagen.mp4 import MP4, MP4Cover

from app.cover import embed_cover


def copy(media, name: str, tmp_path, as_name: str | None = None):
    target = tmp_path / (as_name or name)
    shutil.copy(media[name], target)
    return target


def test_mp3_gets_one_front_cover(media, tmp_path):
    path = copy(media, "sample.mp3", tmp_path)
    jpeg = media["cover.jpg"].read_bytes()
    assert embed_cover(path, jpeg, "jpeg") is True
    assert embed_cover(path, jpeg, "jpeg") is True  # replaces, never stacks
    frames = ID3(path).getall("APIC")
    assert len(frames) == 1
    assert frames[0].mime == "image/jpeg" and frames[0].type == 3 and frames[0].data == jpeg


def test_mp3_without_id3_header(media, tmp_path):
    path = tmp_path / "bare.mp3"
    path.write_bytes(media["sample.mp3"].read_bytes()[-4000:])  # frames only
    png = media["cover.png"].read_bytes()
    assert embed_cover(path, png, "png") is True
    assert ID3(path).getall("APIC")[0].mime == "image/png"


def test_mp4_and_m4a_get_png_cover(media, tmp_path):
    png = media["cover.png"].read_bytes()
    for name in ("a.mp4", "a.m4a"):
        path = copy(media, "sample.mp4", tmp_path, name)
        assert embed_cover(path, png, "png") is True
        cover = MP4(path).tags["covr"][0]
        assert bytes(cover) == png and cover.imageformat == MP4Cover.FORMAT_PNG


def test_other_extension_is_untouched(media, tmp_path):
    path = copy(media, "sample.mp4", tmp_path, "a.mkv")
    before = path.read_bytes()
    assert embed_cover(path, media["cover.jpg"].read_bytes(), "jpeg") is False
    assert path.read_bytes() == before


def test_missing_file_is_false(media, tmp_path):
    assert embed_cover(tmp_path / "missing.mp3", media["cover.jpg"].read_bytes(), "jpeg") is False
