"""Cover-art selection and fetching, with a fake urlopen (no network)."""
from app.cover import COVER_MAX_BYTES, MAX_COVER_TRIES, cover_urls, fetch_cover, image_kind

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class _Response:
    def __init__(self, data: bytes):
        self._data, self.closed = data, False

    def read(self, n: int = -1) -> bytes:
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def close(self) -> None:
        self.closed = True


def make_opener(responses: dict):
    calls, opened = [], []

    def opener(url: str):
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        opened.append(_Response(result))
        return opened[-1]

    return opener, calls, opened


def info_of(*urls: str) -> dict:
    # yt-dlp sorts thumbnails worst to best, so the last URL is the best.
    return {"thumbnails": [{"url": u} for u in urls]}


def test_cover_urls_judges_the_path_not_the_query():
    info = {"thumbnails": [
        {"url": "https://i.ytimg.com/vi/a/default.jpg"},
        {"url": "https://i.ytimg.com/vi_webp/a/maxresdefault.webp"},
        {"url": "https://x.test/y.webp?fake=.jpg"},
        {"url": "https://i.ytimg.com/vi/a/hqdefault.jpg?sqp=abc"},
    ]}
    assert cover_urls(info) == ["https://i.ytimg.com/vi/a/hqdefault.jpg?sqp=abc",
                                "https://i.ytimg.com/vi/a/default.jpg"]


def test_cover_urls_schemes_and_fallback():
    assert cover_urls({"thumbnail": "//cdn.test/c.PNG"}) == ["https://cdn.test/c.PNG"]
    assert cover_urls({"thumbnails": [{"url": "file:///etc/a.jpg"}, {"url": "data:image/jpeg,x.jpg"}]}) == []
    assert len(cover_urls({"thumbnails": [{"url": f"https://c.test/{i}.jpg"} for i in range(9)]})) == MAX_COVER_TRIES
    assert cover_urls({}) == []


def test_cover_urls_removes_duplicates():
    info = {"thumbnails": [{"url": "https://c.test/a.jpg"}, {"url": "https://c.test/a.jpg"}]}
    assert cover_urls(info) == ["https://c.test/a.jpg"]


def test_image_kind():
    assert image_kind(JPEG) == "jpeg" and image_kind(PNG) == "png"
    assert image_kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None and image_kind(b"") is None


def test_fetch_cover_takes_first_good_image():
    a, b = "https://c.test/a.jpg", "https://c.test/b.png"
    opener, calls, opened = make_opener({a: OSError("down"), b: PNG})
    assert fetch_cover(opener, info_of(a, b)) == (PNG, "png")
    assert calls == [b]  # best (last) first, stops at the first good one
    assert all(r.closed for r in opened)


def test_fetch_cover_falls_through_a_failure():
    a, b = "https://c.test/a.jpg", "https://c.test/b.png"
    opener, calls, _ = make_opener({a: PNG, b: OSError("down")})
    assert fetch_cover(opener, info_of(a, b)) == (PNG, "png") and calls == [b, a]


def test_fetch_cover_skips_oversize_and_junk():
    big, junk = "https://c.test/big.jpg", "https://c.test/junk.jpg"
    opener, calls, opened = make_opener({big: JPEG + b"\0" * COVER_MAX_BYTES, junk: b"not an image"})
    assert fetch_cover(opener, info_of(junk, big)) is None and calls == [big, junk]
    assert all(r.closed for r in opened)


def test_fetch_cover_stops_after_max_tries():
    urls = [f"https://c.test/{i}.jpg" for i in range(5)]
    opener, calls, _ = make_opener({u: OSError("down") for u in urls})
    assert fetch_cover(opener, info_of(*urls)) is None and len(calls) == MAX_COVER_TRIES


def test_fetch_cover_never_raises():
    def opener(url):
        raise RuntimeError("boom")

    assert fetch_cover(opener, info_of("https://c.test/a.jpg")) is None


def _listed(n: int, **extra) -> dict:
    return {"thumbnails": [{"url": f"https://c.test/{i}.jpg"} for i in range(n)], **extra}


def test_cover_urls_always_tries_the_sources_own_thumbnail():
    own = "https://i.ytimg.com/vi/a/hqdefault.jpg?sqp=abc"
    assert cover_urls(_listed(5, thumbnail=own)) == ["https://c.test/4.jpg", "https://c.test/3.jpg", own]


def test_cover_urls_own_thumbnail_already_in_top_three():
    assert cover_urls(_listed(5, thumbnail="https://c.test/3.jpg")) == [
        f"https://c.test/{i}.jpg" for i in (4, 3, 2)]


def test_cover_urls_unqualified_own_thumbnail_is_ignored():
    assert cover_urls(_listed(5, thumbnail="https://c.test/x.webp")) == [
        f"https://c.test/{i}.jpg" for i in (4, 3, 2)]
