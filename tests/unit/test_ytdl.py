import copy
from pathlib import Path

import pytest
import yt_dlp

from app.errors import AppError
from app.ytdl import (
    FFMPEG_WRAPPER_DIR, NATIVE_PROTOCOLS, base_opts, chosen_formats, downgrade_notice,
    inspect_payload, keep_safe_formats, unwrap_first_entry, video_opts, audio_opts,
)


def f(id, w, h, vcodec, acodec, ext, proto="https", tbr=1000):
    return {"format_id": id, "url": f"https://cdn.example/{id}", "protocol": proto, "ext": ext,
            "width": w, "height": h, "vcodec": vcodec, "acodec": acodec, "tbr": tbr}


YT = [
    f("313", 3840, 2160, "vp9", "none", "webm", tbr=20000),
    f("248", 1920, 1080, "vp9", "none", "webm", tbr=3000),
    f("137", 1920, 1080, "avc1.640028", "none", "mp4", tbr=4000),
    f("247", 1280, 720, "vp9", "none", "webm", tbr=1500),
    f("136", 1280, 720, "avc1.4d401f", "none", "mp4", tbr=2000),
    f("135", 854, 480, "avc1.4d401e", "none", "mp4", tbr=800),
    f("140", None, None, "none", "mp4a.40.2", "m4a", tbr=128),
    f("251", None, None, "none", "opus", "webm", tbr=130),
]
VERTICAL = [
    f("v1", 1080, 1920, "avc1.640028", "none", "mp4", tbr=4000),
    f("v2", 1080, 1920, "vp9", "none", "webm", tbr=3000),
    f("v3", 720, 1280, "avc1.4d401f", "none", "mp4", tbr=2000),
    f("a1", None, None, "none", "mp4a.40.2", "m4a", tbr=128),
]
LOW = [f("l", 640, 360, "avc1.4d401e", "mp4a.40.2", "mp4")]
DIRECT = [{"format_id": "0", "url": "https://cdn.example/x.mp4", "protocol": "https", "ext": "mp4"}]


def info(formats):
    return {"id": "x", "title": "t", "extractor_key": "Youtube",
            "webpage_url": "https://www.youtube.com/watch?v=x", "formats": formats}


def select(formats, q):
    ydl = yt_dlp.YoutubeDL({**base_opts(None), **video_opts(q), "quiet": True})
    return ydl.process_ie_result(copy.deepcopy(info(formats)), download=False)


def test_base_opts_lock_down():
    o = base_opts(Path("/w"))
    assert o["noplaylist"] is True and o["playlist_items"] == "1" and o["cachedir"] is False
    assert o["proxy"] == "" and o["external_downloader"] == {"default": "native"} and o["hls_prefer_native"] is True
    assert o["outtmpl"] == {"default": "media.%(ext)s"} and o["paths"] == {"home": "/w", "temp": "/w"}
    assert o["ffmpeg_location"] == str(FFMPEG_WRAPPER_DIR) and o["remote_components"] == set()
    assert "cookiefile" not in o and o.get("usenetrc") is False


def test_opts_video_audio():
    assert video_opts("best")["format"] == "bv*+ba/b" and "format_sort" not in video_opts("best")
    assert video_opts("720")["format_sort"] == ["res:720", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"]
    assert audio_opts()["format"] == "ba/b"


def test_tiers_listed_only_when_present():
    p = inspect_payload(info(YT))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720", "480"]
    assert p["video"]["qualities"][0]["label"] == "Best (2160p)" and p["video"]["default"] == "1080"
    assert p["source"] == "Youtube · www.youtube.com"


def test_vertical_uses_short_side():
    p = inspect_payload(info(VERTICAL))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720"]
    assert p["video"]["qualities"][0]["label"] == "Best (1080p)"


def test_low_res_offers_best_only_with_notice():
    p = inspect_payload(info(LOW))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best"] and p["video"]["default"] == "best"
    assert "Only 360p is available." in p["notices"]


def test_direct_link_unknown_codecs():
    p = inspect_payload(info(DIRECT))
    assert p["video"]["available"] and p["audio"]["available"] and [q["id"] for q in p["video"]["qualities"]] == ["best"]
    assert p["video"]["qualities"][0]["label"] == "Best"
    assert p["notices"] == ["Details for this link are unknown until download."]


def test_generic_source_label():
    i = {**info(LOW), "extractor_key": "Generic", "webpage_url": "https://a.example/p"}
    assert inspect_payload(i)["source"] == "Web page · a.example"


def test_audio_only_and_video_only_sources():
    assert inspect_payload(info([f("a", None, None, "none", "mp4a", "m4a")]))["video"]["available"] is False
    assert inspect_payload(info([f("v", 640, 360, "avc1", "none", "mp4")]))["audio"]["available"] is False


def test_keep_safe_formats():
    out = keep_safe_formats(info(YT + [f("r", 1920, 1080, "avc1", "mp4a", "flv", proto="rtmp")]))
    assert all(x["protocol"] in NATIVE_PROTOCOLS for x in out["formats"])
    for bad, code in [({"is_live": True}, "live_unsupported"), ({"live_status": "is_upcoming"}, "live_unsupported")]:
        with pytest.raises(AppError) as e:
            keep_safe_formats({**info(YT), **bad})
        assert e.value.code == code
    with pytest.raises(AppError) as e:
        keep_safe_formats(info([{**f("d", 640, 360, "avc1", "mp4a", "mp4"), "has_drm": True}]))
    assert e.value.code == "drm"
    with pytest.raises(AppError) as e:
        keep_safe_formats(info([f("r", 1, 1, "avc1", "mp4a", "flv", proto="rtmp")]))
    assert e.value.code == "unsupported"


def test_unwrap_playlist():
    e1 = info(LOW)
    assert unwrap_first_entry({"_type": "playlist", "entries": [e1], "playlist_count": 3}) == (e1, "This post has 3 videos; the first will be downloaded.")
    assert unwrap_first_entry(e1) == (e1, None)
    with pytest.raises(AppError) as e:
        unwrap_first_entry({"_type": "playlist", "entries": []})
    assert e.value.code == "playlist_unsupported"


def test_select_720_prefers_h264_and_aac():
    ids = [x["format_id"] for x in chosen_formats(select(YT, "720"))]
    assert ids == ["136", "140"]


def test_select_1080_vertical_is_h264():
    assert chosen_formats(select(VERTICAL, "1080"))[0]["vcodec"].startswith("avc1")


def test_select_best_any_codec():
    assert chosen_formats(select(YT, "best"))[0]["format_id"] == "313"


def test_downgrade_notice():
    assert downgrade_notice([{"width": 1280, "height": 720, "vcodec": "avc1"}], "1080") == "1080p wasn't available; downloaded 720p."
    assert downgrade_notice([{"width": 1920, "height": 1080, "vcodec": "avc1"}], "1080") is None
    assert downgrade_notice([{"width": 640, "height": 360, "vcodec": "avc1"}], "best") is None


def _raw(url, **kw):
    return {"format_id": "r", "url": url, **kw}


def test_single_format_protocol_inferred():
    for url in ["rtmp://h/x", "https://h/a.f4m", "ftp://h/a.mp4"]:
        with pytest.raises(AppError) as e:
            keep_safe_formats({"id": "x", "url": url})
        assert e.value.code == "unsupported"
    assert keep_safe_formats({"id": "x", "url": "https://h/a.m3u8"})["url"] == "https://h/a.m3u8"
    assert keep_safe_formats({"id": "x", "url": "https://h/a.mp4"})["url"] == "https://h/a.mp4"


def test_multi_format_protocol_inferred():
    fmts = [_raw("https://h/a.mp4"), _raw("https://h/a.m3u8"), _raw("https://h/a.f4m"), _raw("rtmp://h/x")]
    out = keep_safe_formats({"id": "x", "formats": fmts})
    assert [x["url"] for x in out["formats"]] == ["https://h/a.mp4", "https://h/a.m3u8"]


def test_mixed_drm_keeps_clean_only():
    out = keep_safe_formats(info([{**f("d", 640, 360, "avc1", "mp4a", "mp4"), "has_drm": True},
                                  f("c", 640, 360, "avc1", "mp4a", "mp4")]))
    assert [x["format_id"] for x in out["formats"]] == ["c"]
