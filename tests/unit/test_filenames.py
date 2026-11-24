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


@pytest.mark.parametrize("title, expected", [
    ("a\ud800b", "a b.mp4"), ("\udfff", "download.mp4"), ("x\udc80\ud83d", "x.mp4"),
], ids=["lone_high", "lone_low_only", "trailing_surrogates"])
def test_lone_surrogates_replaced(title, expected):
    name = sanitize_filename(title, "mp4")
    assert name == expected
    name.encode("utf-8")
    header = content_disposition(name)
    header.encode("latin-1")  # a header value must be encodable


def test_m4a_name_and_type():
    assert sanitize_filename("Song", "m4a") == "Song.m4a"
    assert media_type_for("m4a") == "audio/mp4"


@pytest.mark.parametrize("title, start, end, ext, expected", [
    ("Talk", 65, 100.5, "mp4", "Talk (clip 1m05s-1m40.5s).mp4"),
    (None, 0, 12, "mkv", "download (clip 0m00s-0m12s).mkv"),
    ("Long talk", 3725, 3725.04, "webm", "Long talk (clip 1h02m05s-1h02m05s).webm"),
    ("a/b: c", 1.23456, 4.5, "mp4", "a b c (clip 0m01.2s-0m04.5s).mp4"),
])
def test_clip_filename(title, start, end, ext, expected):
    from app.filenames import clip_filename
    assert clip_filename(title, start, end, ext) == expected


def test_clip_filename_keeps_its_range_when_the_title_is_long():
    from app.filenames import clip_filename
    name = clip_filename("é" * 500, 0, 30, "mp4")
    assert name.endswith(" (clip 0m00s-0m30s).mp4")
    assert len(name.removesuffix(".mp4").encode("utf-8")) <= 150


# --- Batch export (a ZIP of clips) ----------------------------------------------

def test_batch_names():
    from app.filenames import batch_entry_name, batch_filename, is_plain_filename
    entry = batch_entry_name(3, "a/b: c", 65, 100.5, "mp4")
    assert entry == "03 a b c (clip 1m05s-1m40.5s).mp4" and is_plain_filename(entry, "mp4")
    assert batch_filename("Talk", 5) == "Talk (5 clips).zip"
    assert batch_filename(None, 1) == "download (1 clip).zip"
    long = batch_filename("é" * 500, 20)
    assert long.endswith(" (20 clips).zip") and len(long.removesuffix(".zip").encode("utf-8")) <= 150


def test_zip_is_never_a_download_media_type():
    from app.filenames import ZIP_MEDIA_TYPE, clip_filename
    assert ZIP_MEDIA_TYPE == "application/zip"
    with pytest.raises(KeyError):
        media_type_for("zip")
    with pytest.raises(ValueError):
        sanitize_filename("x", "zip")
    with pytest.raises(ValueError):
        clip_filename("x", 0, 1, "zip")


@pytest.mark.parametrize("name, ok", [
    ("01 Talk (clip 0m00s-0m05s).mp4", True), ("日本語.mp4", True),
    ("a/b.mp4", False), ("a\\b.mp4", False), ("../x.mp4", False), (".x.mp4", False), (".mp4", False),
    ("x.mkv", False), ("x.MP4", False), ("a\x00.mp4", False), ("a\x1f.mp4", False), ("a:b.mp4", False),
    (" x.mp4", False), ("x" * 252 + ".mp4", False), ("x" * 251 + ".mp4", True), ("\ud800.mp4", False),
    (None, False), (5, False),
])
def test_is_plain_filename(name, ok):
    from app.filenames import is_plain_filename
    assert is_plain_filename(name, "mp4") is ok
