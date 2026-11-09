import pytest
from app.filenames import sanitize_filename, content_disposition, media_type_for

@pytest.mark.parametrize("title,ext,expected", [
    ("My Video", "mp4", "My Video.mp4"),
    ("../../etc/passwd", "mp4", "etc passwd.mp4"),
    ("a\nb\tc\x00d", "mp3", "a b c d.mp3"),
    ('x<>:"|?*\\/y', "mp4", "x y.mp4"),
    ("CON", "mp4", "CON_.mp4"),
    (None, "mp3", "download.mp3"), ("", "mp4", "download.mp4"), ("...", "mp4", "download.mp4"),
])
def test_sanitize(title, ext, expected): assert sanitize_filename(title, ext) == expected

def test_hostile_shell_title_has_no_separator():
    name = sanitize_filename("../../x; $(rm -rf /)", "mp3")
    assert "/" not in name and "\\" not in name and name.endswith(".mp3") and not name.startswith(".")

def test_long_title_truncated_on_codepoint_boundary():
    name = sanitize_filename("é" * 500, "mp4")
    stem = name.removesuffix(".mp4")
    assert len(stem.encode("utf-8")) <= 150 and stem == "é" * len(stem)

def test_bad_ext_rejected():
    with pytest.raises(ValueError): sanitize_filename("x", "sh")

def test_unicode_title_disposition():
    name = sanitize_filename("日本語 🎵 title", "mp4")
    assert name == "日本語 🎵 title.mp4"
    cd = content_disposition(name)
    assert cd.startswith("attachment; ") and 'filename="title.mp4"' in cd
    assert "filename*=UTF-8''%E6%97%A5%E6%9C%AC%E8%AA%9E%20%F0%9F%8E%B5%20title.mp4" in cd

def test_all_non_ascii_falls_back():
    assert 'filename="download.mp4"' in content_disposition("日本語.mp4")

def test_media_types(): assert media_type_for("mp3") == "audio/mpeg" and media_type_for("mkv") == "video/x-matroska"
