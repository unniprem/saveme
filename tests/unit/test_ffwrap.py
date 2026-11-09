from app.bin._ffwrap import inject_whitelist

W = ["-protocol_whitelist", "file,pipe"]


def test_ffmpeg_before_every_input():
    assert inject_whitelist(["-y", "-i", "a.mp4", "-i", "b.m4a", "out.mp4"], "ffmpeg") == [
        "-y", *W, "-i", "a.mp4", *W, "-i", "b.m4a", "out.mp4"]


def test_ffmpeg_version_untouched():
    assert inject_whitelist(["-version"], "ffmpeg") == ["-version"]


def test_ffprobe_prefixed():
    assert inject_whitelist(["-show_streams", "x.mp4"], "ffprobe") == [*W, "-show_streams", "x.mp4"]


def test_existing_whitelist_overridden():
    assert inject_whitelist(["-protocol_whitelist", "http,file", "-i", "x"], "ffmpeg") == [*W, "-i", "x"]
