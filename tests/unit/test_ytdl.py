import copy
from pathlib import Path

import pytest
import yt_dlp

from app.errors import AppError
from app.ytdl import (
    FFMPEG_WRAPPER_DIR, NATIVE_PROTOCOLS, base_opts, chosen_formats, downgrade_notice,
    inspect_payload, keep_safe_formats, unwrap_first_entry, video_opts, audio_opts,
    TAGS_PP, output_opts, without_source_link, track_available, best_label, codec_family,
    MAX_DESCRIPTION_TAG_BYTES, with_capped_description,
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
    assert p["video"]["qualities"][0]["label"] == "Best (2160p · VP9)" and p["video"]["default"] == "1080"
    assert p["source"] == "Youtube · www.youtube.com"


def test_vertical_uses_short_side():
    p = inspect_payload(info(VERTICAL))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720"]
    assert p["video"]["qualities"][0]["label"] == "Best (1080p · VP9/H.264)"


def test_low_res_offers_best_only_with_notice():
    p = inspect_payload(info(LOW))
    assert [q["id"] for q in p["video"]["qualities"]] == ["best"] and p["video"]["default"] == "best"
    assert "Only 360p is available." in p["notices"]


def test_direct_link_unknown_codecs():
    p = inspect_payload(info(DIRECT))
    assert p["video"]["available"] and p["audio"]["available"] and [q["id"] for q in p["video"]["qualities"]] == ["best"]
    assert p["video"]["qualities"][0]["label"] == "Best"
    assert p["notices"] == ["Details for this link are unknown until download."]


def test_youtube_hls_audio_with_unknown_acodec_has_no_unknown_notice():
    # YouTube's HLS audio formats: vcodec "none" (known: no video), acodec None.
    hls_audio = f("233", None, None, "none", None, "mp4", proto="m3u8_native", tbr=48)
    p = inspect_payload(info(YT + [hls_audio]))
    assert "Details for this link are unknown until download." not in p["notices"]
    assert [q["id"] for q in p["video"]["qualities"]] == ["best", "1080", "720", "480"]


@pytest.mark.parametrize("extra, expected", [
    ({"thumbnail": "https://i.example/main.jpg",
      "thumbnails": [{"url": "https://i.example/other.jpg"}]}, "https://i.example/main.jpg"),
    ({"thumbnails": [{"url": "https://i.example/small.jpg"}, {"id": "no-url"},
                     {"url": "https://i.example/large.jpg"}]}, "https://i.example/large.jpg"),
    ({"thumbnails": [{"url": "//i.example/rel.jpg"}]}, "https://i.example/rel.jpg"),
    ({"thumbnail": "//i.example/rel.jpg"}, "https://i.example/rel.jpg"),
    ({"thumbnails": []}, None),
    ({}, None),
], ids=["thumbnail_wins", "last_thumbnails_entry", "relative_entry", "relative_thumbnail",
        "empty_list", "none"])
def test_thumbnail_falls_back_to_thumbnails_list(extra, expected):
    assert inspect_payload({**info(LOW), **extra})["thumbnail_url"] == expected


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


def test_unwrap_takes_the_first_entry_lazily():
    e1, e2 = info(LOW), info(LOW)

    def entries():
        yield None  # yt-dlp leaves holes for entries it skipped
        yield e1
        yield e2
        raise AssertionError("read past the second entry")

    first, notice = unwrap_first_entry({"_type": "playlist", "entries": entries()})
    assert first is e1
    assert notice == "This post has more than one video; the first will be downloaded."


def test_unwrap_lazy_single_entry_has_no_notice():
    e1 = info(LOW)
    assert unwrap_first_entry({"_type": "playlist", "entries": iter([e1])}) == (e1, None)


def test_unwrap_stops_scanning_empty_entries():
    def holes():
        while True:
            yield None

    with pytest.raises(AppError) as e:
        unwrap_first_entry({"_type": "playlist", "entries": holes()})
    assert e.value.code == "playlist_unsupported"


def test_output_opts_add_tags_last():
    for opts in (output_opts("video", "best"), output_opts("audio", "best")):
        assert opts["postprocessors"][-1] == TAGS_PP
    assert output_opts("audio", "best")["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert output_opts("video", "720")["format_sort"] == video_opts("720")["format_sort"]
    assert TAGS_PP["add_chapters"] is True and TAGS_PP["add_infojson"] is False


def test_without_source_link_blanks_purl_and_comment():
    out = without_source_link({"title": "t", "webpage_url": "https://x/y"})
    assert out["meta_purl"] == "" and out["meta_comment"] == "" and out["webpage_url"] == "https://x/y"


def test_long_description_tags_are_cut_on_a_character_boundary():
    description = "\u20ac" * 70_000  # 205 KiB of 3-byte characters; byte 16384 is mid-character
    info = {"title": "t", "description": description}
    out = with_capped_description(info)
    tag = out["meta_description"]
    assert MAX_DESCRIPTION_TAG_BYTES == 16 * 1024
    assert len(tag.encode("utf-8")) == 16383 and description.startswith(tag)
    assert out["meta_synopsis"] == tag and out["description"] == description
    assert info == {"title": "t", "description": description}  # the input is untouched


def test_description_cut_survives_lone_surrogates():
    # The worker replaces lone surrogates after this step, so it must not trip on them.
    assert with_capped_description({"description": "\ud800" * 10_000})["meta_description"] == "\ud800" * 5461


def test_short_or_missing_description_adds_no_tags():
    for info in ({"description": "x" * MAX_DESCRIPTION_TAG_BYTES}, {"description": None}, {}):
        assert with_capped_description(info) == info


def test_ffmpeg_metadata_writes_the_capped_description():
    from yt_dlp.postprocessor import FFmpegMetadataPP

    info = with_capped_description(without_source_link(
        {"title": "t", "description": "a" * 200_000, "webpage_url": "https://x/y"}))
    pp = FFmpegMetadataPP(yt_dlp.YoutubeDL({"quiet": True}), add_chapters=False)
    args = dict(v.split("=", 1) for k, v in pp._get_metadata_opts(info) if k == "-metadata")
    assert args["description"] == args["synopsis"] == "a" * MAX_DESCRIPTION_TAG_BYTES
    assert args["purl"] == args["comment"] == ""


def test_m4a_flag_needs_known_aac():
    f = lambda **k: {"url": "https://x/f", "protocol": "https", **k}
    assert inspect_payload({"formats": [f(vcodec="none", acodec="mp4a.40.2")]})["audio"]["m4a"] is True
    assert inspect_payload({"formats": [f(vcodec="none", acodec="opus")]})["audio"]["m4a"] is False
    assert inspect_payload({"formats": [f(vcodec=None, acodec=None)]})["audio"]["m4a"] is False


def test_m4a_opts_copy_aac():
    o = output_opts("m4a", "best")
    assert o["format"] == "ba[acodec^=mp4a]/b[acodec^=mp4a]"
    assert o["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}, TAGS_PP]


def test_track_available():
    p = {"video": {"available": False}, "audio": {"available": True, "m4a": False}}
    assert [track_available(p, o) for o in ("video", "audio", "m4a")] == [False, True, False]


V = lambda side, codec, fps=None: {"url": "https://x/v", "protocol": "https", "width": side * 16 // 9,
                                   "height": side, "vcodec": codec, "acodec": "none", "fps": fps}


def test_best_label_cases():
    assert best_label([V(2160, "av01.0.12M.08", 60), V(2160, "vp09.00.51.08", 60),
                       V(2160, "vp09.00.51.08", 30), V(1080, "avc1.640028", 60)]) == "Best (2160p60 · AV1/VP9)"
    assert best_label([V(1080, "avc1.64001F", 30)]) == "Best (1080p · H.264)"
    assert best_label([V(1080, "avc1.64001F", 50)]) == "Best (1080p50 · H.264)"
    assert best_label([V(1080, "avc1.64001F", 59.94)]) == "Best (1080p60 · H.264)"
    assert best_label([V(1080, None)]) == "Best (1080p)"
    assert best_label([V(1080, "avc1"), V(1080, "vp8")]) == "Best (1080p)"
    assert best_label([{"url": "https://x/v", "vcodec": None}]) == "Best"


def test_codec_family():
    assert [codec_family(c) for c in ("AV01.0", "vp9", "hvc1.1", "hevc", "avc3.x", "h264", "vp8", None)] == [
        "AV1", "VP9", "H.265", "H.265", "H.264", "H.264", None, None]
