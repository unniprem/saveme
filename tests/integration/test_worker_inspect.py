"""Worker inspect mode. Subprocess tests run the real worker (guard installed
first) against the local media server; the in-process tests cover resolve()
and serve()'s error mapping without any network."""
import copy
import io
import json
import subprocess
import sys

import pytest
from yt_dlp.utils import DownloadError, ExtractorError

from app.errors import CATALOG, AppError
from app.netguard import BlockedAddressError
from tests.support import procs
from tests.support.procs import ROOT, run_worker_proc, worker_env


def inspect(url: str, **kwargs) -> tuple[list[dict], int]:
    return run_worker_proc({"mode": "inspect", "url": url}, **kwargs)


def result_of(events: list[dict], rc: int) -> dict:
    assert rc == 0 and events and events[-1]["event"] == "result", events
    return events[-1]["data"]


def error_code_of(events: list[dict], rc: int) -> str:
    assert rc == 1 and events and events[-1]["event"] == "error", events
    error = events[-1]
    assert error["message"] == CATALOG[error["code"]][1]
    return error["code"]


def run_python(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=ROOT, env=worker_env(), timeout=30)


# --- In-process: import order, resolve(), serve() -------------------------

def test_worker_import_does_not_load_ytdlp():
    r = run_python(
        "import sys\nimport app.worker\n"
        "print([m for m in ('yt_dlp', 'app.ytdl', 'requests', 'urllib3') if m in sys.modules])\n"
    )
    assert r.returncode == 0 and r.stdout.strip() == "[]", r.stdout + r.stderr


STRAY = r"""
import subprocess, sys
from app import worker
out = worker.protocol_out()
print("stray print", flush=True)
child = (
    "import os, sys\n"
    "print('child print', flush=True)\n"
    "try:\n"
    "    os.write(int(sys.argv[1]), b'leak\\n')\n"
    "except OSError:\n"
    "    print('protocol fd closed', flush=True)\n"
)
subprocess.run([sys.executable, "-c", child, str(out.fileno())], close_fds=False, check=True)
out.write('{"event": "result", "data": {}}\n')
out.flush()
"""


def test_protocol_fd_is_private():
    r = run_python(STRAY)
    assert r.returncode == 0, r.stderr
    assert r.stdout == '{"event": "result", "data": {}}\n'
    assert "stray print" in r.stderr and "child print" in r.stderr
    assert "protocol fd closed" in r.stderr


class StubYDL:
    def __init__(self, results: dict[str, dict]):
        self.results = results
        self.calls: list[tuple[str, str | None]] = []

    def extract_info(self, url, download=True, ie_key=None, extra_info=None, process=True):
        assert download is False and process is False
        self.calls.append((url, ie_key))
        return copy.deepcopy(self.results[url])


SAFE = {"format_id": "hls-360", "url": "https://cdn.example/360.m3u8", "protocol": "m3u8_native",
        "width": 640, "height": 360, "vcodec": "avc1", "acodec": "mp4a"}
RTMP = {"format_id": "rtmp", "url": "rtmp://cdn.example/live", "protocol": "rtmp"}


def test_resolve_follows_url_and_transparent_results():
    from app.worker import resolve

    ydl = StubYDL({
        "https://a.example/post": {"_type": "playlist", "playlist_count": 2, "entries": [
            {"_type": "url", "url": "https://b.example/v", "ie_key": "B", "title": "Dropped"},
            {"_type": "url", "url": "https://b.example/w", "ie_key": "B"},
        ]},
        "https://b.example/v": {"_type": "url_transparent", "url": "https://c.example/v", "ie_key": "C",
                                "id": "b1", "extractor_key": "B", "title": "Outer", "thumbnail": None},
        "https://c.example/v": {"id": "c1", "extractor_key": "C", "title": "Inner",
                                "thumbnail": "https://c.example/t.jpg", "formats": [SAFE, RTMP]},
    })
    info, notice = resolve(ydl, "https://a.example/post")
    assert ydl.calls == [("https://a.example/post", None), ("https://b.example/v", "B"),
                         ("https://c.example/v", "C")]
    assert notice == "This post has 2 videos; the first will be downloaded."
    assert info["title"] == "Outer"  # url_transparent: outer non-None fields win...
    assert info["thumbnail"] == "https://c.example/t.jpg"
    assert (info["id"], info["extractor_key"]) == ("c1", "C")  # ...except identity
    assert "_type" not in info and "url" not in info
    assert info["formats"] == [SAFE]  # keep_safe_formats ran last


def test_resolve_gives_up_on_endless_url_chains():
    from app.worker import resolve

    ydl = StubYDL({"https://loop.example/": {"_type": "url", "url": "https://loop.example/"}})
    with pytest.raises(AppError) as ei:
        resolve(ydl, "https://loop.example/")
    assert ei.value.code == "extraction_failed"


def serve_inprocess(request) -> tuple[list[dict], int]:
    from app import worker

    out = io.StringIO()
    rc = worker.serve(io.StringIO(request if isinstance(request, str) else json.dumps(request)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()], rc


@pytest.mark.parametrize("exc, code", [
    (AppError("drm"), "drm"),
    (BlockedAddressError("[saveme-blocked] 10.0.0.1 is not a public address"), "blocked_address"),
    (DownloadError("ERROR: Unsupported URL: https://x.example/"), "unsupported"),
    (ExtractorError("Video unavailable", expected=True), "unavailable"),
    (RuntimeError("boom at /secret/path"), "internal"),
], ids=["app_error", "blocked", "download_error", "extractor_error", "unexpected"])
def test_serve_maps_errors_to_catalogue(monkeypatch, capsys, exc, code):
    from app import worker

    def fail(url):
        raise exc

    monkeypatch.setattr(worker, "run_inspect", fail)
    events, rc = serve_inprocess({"mode": "inspect", "url": "https://x.example/"})
    assert rc == 1
    assert events == [{"event": "error", "code": code, "message": CATALOG[code][1]}]
    if code == "internal":
        assert "RuntimeError: boom" in capsys.readouterr().err


@pytest.mark.parametrize("request_text", [
    "not json", json.dumps(["inspect"]), json.dumps({"mode": "inspect"}),
    json.dumps({"mode": "teleport", "url": "https://x.example/"}),
], ids=["not_json", "not_object", "no_url", "unknown_mode"])
def test_serve_malformed_request_is_internal(request_text):
    events, rc = serve_inprocess(request_text)
    assert rc == 1 and [e["code"] for e in events] == ["internal"]


def test_serve_rejects_non_http_url():
    events, rc = serve_inprocess({"mode": "inspect", "url": "file:///etc/passwd"})
    assert rc == 1 and [e["code"] for e in events] == ["invalid_url"]


# --- Subprocess: the real worker against the local media server -----------

def test_inspect_direct_mp4(media_server):
    data = result_of(*inspect(media_server.url("/media/sample.mp4")))
    assert data["video"]["available"] is True
    assert data["audio"]["available"] is True
    assert data["video"]["qualities"][0]["id"] == "best"


def test_inspect_hls_master(media_server):
    data = result_of(*inspect(media_server.url("/hls/master.m3u8")))
    assert [q["id"] for q in data["video"]["qualities"]] == ["best"]
    assert data["video"]["qualities"][0]["label"] == "Best (360p)"


def test_redirect_to_media_is_followed(media_server):
    target = media_server.url("/media/sample.mp4")
    data = result_of(*inspect(media_server.url(f"/redirect?to={target}")))
    assert data["video"]["available"] is True


def test_redirect_to_metadata_ip_blocked(media_server):
    url = media_server.url("/redirect?to=http://169.254.169.254/latest/meta-data/")
    assert error_code_of(*inspect(url)) == "blocked_address"


def test_production_entry_blocks_loopback(media_server):
    before = media_server.hits["/media/sample.mp4"]
    events, rc = inspect(media_server.url("/media/sample.mp4"), entry="app.worker")
    assert error_code_of(events, rc) == "blocked_address"
    assert media_server.hits["/media/sample.mp4"] == before


def test_unsupported_and_missing(media_server):
    assert error_code_of(*inspect(media_server.url("/plain"))) == "unsupported"
    assert error_code_of(*inspect(media_server.url("/missing"))) == "unavailable"


def test_url_text_does_not_drive_classification(media_server):
    # yt-dlp echoes the URL in "Unsupported URL: ..."; its "429" is not a rate limit.
    assert error_code_of(*inspect(media_server.url("/plain?x=429"))) == "unsupported"


def test_private_thumbnail_dropped(media_server):
    url = media_server.url("/page?title=T&video=/media/sample.mp4&thumb=http://10.0.0.1/x.jpg")
    data = result_of(*inspect(url))
    assert data["title"] == "T"
    assert data["thumbnail_url"] is None


def test_public_thumbnail_kept(media_server):
    url = media_server.url("/page?title=T&video=/media/sample.mp4&thumb=http://8.8.8.8/x.jpg")
    assert result_of(*inspect(url))["thumbnail_url"] == "http://8.8.8.8/x.jpg"


THUMBNAILS_ONLY = r"""
import copy, json, sys
from app.netguard import install_connect_guard
install_connect_guard()  # the production policy, before yt-dlp loads
from app import worker
import yt_dlp

INFO = {"id": "r1", "extractor_key": "Reddit", "title": "T", "webpage_url": "https://r.example/p",
        "formats": [{"format_id": "hls-360", "url": "https://cdn.example/360.m3u8",
                     "protocol": "m3u8_native", "width": 640, "height": 360,
                     "vcodec": "avc1", "acodec": "mp4a"}],
        "thumbnails": [{"url": "https://8.8.4.4/small.jpg"}, {"url": sys.argv[1]}]}

def extract_info(self, url, download=True, ie_key=None, extra_info=None, process=True):
    return copy.deepcopy(INFO)

yt_dlp.YoutubeDL.extract_info = extract_info
print(json.dumps(worker.run_inspect("https://r.example/p")["thumbnail_url"]))
"""


@pytest.mark.parametrize("thumb, expected", [
    ("https://8.8.8.8/large.jpg", "https://8.8.8.8/large.jpg"),
    ("//8.8.8.8/large.jpg", "https://8.8.8.8/large.jpg"),
    ("https://10.0.0.1/large.jpg", None),
], ids=["public", "protocol_relative", "private"])
def test_thumbnails_list_only(thumb, expected):
    # Reddit / X give only `thumbnails`; process=False skips yt-dlp's own fallback.
    r = subprocess.run([sys.executable, "-c", THUMBNAILS_ONLY, thumb], capture_output=True,
                       text=True, cwd=ROOT, env=worker_env(), timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == expected


def test_stdout_is_protocol_only(media_server):
    # Fresh success and failure runs, plus every run made by the tests above.
    inspect(media_server.url("/media/sample.mp4"))
    inspect(media_server.url("/plain"))
    assert procs.STDOUT_LINES
    for line in procs.STDOUT_LINES:
        event = json.loads(line)
        assert isinstance(event, dict) and "event" in event, line


# --- Memory: page-size cap ---------------------------------------------------

@pytest.mark.parametrize("path", ["/big-page", "/gzip-bomb"])
def test_oversized_page_is_refused(media_server, path):
    # The cap counts decoded bytes, so a small compressed body can't expand past it.
    assert error_code_of(*inspect(media_server.url(path))) == "source_too_large"


# --- Memory: per-worker limit ------------------------------------------------

LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux",
                                reason="RLIMIT_DATA is enforced (and settable) on Linux only")


@LINUX_ONLY
def test_limit_memory_caps_the_data_segment():
    r = run_python("import resource\nfrom app.worker import limit_memory\n"
                   "limit_memory(768 << 20)\nprint(resource.getrlimit(resource.RLIMIT_DATA))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str((768 << 20, 768 << 20))


@LINUX_ONLY
def test_limit_memory_stops_a_runaway_allocation():
    r = run_python("from app.worker import limit_memory\nlimit_memory(256 << 20)\n"
                   "try:\n    bytearray(512 << 20)\nexcept MemoryError:\n    print('refused')")
    assert r.returncode == 0 and r.stdout.strip() == "refused", r.stdout + r.stderr


def test_limit_memory_is_skipped_where_refused(monkeypatch, capsys):
    # macOS refuses RLIMIT_DATA changes; the worker still runs, and says so on stderr.
    import resource

    from app.worker import limit_memory

    def refuse(*args):
        raise ValueError("current limit exceeds maximum limit")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    limit_memory()
    assert "memory limit not applied" in capsys.readouterr().err


def test_main_limits_memory_before_serving(monkeypatch):
    from app import worker

    order = []
    monkeypatch.setattr(worker, "limit_memory", lambda: order.append("limit"))
    monkeypatch.setattr(worker, "install_connect_guard", lambda: order.append("guard"))
    monkeypatch.setattr(worker, "protocol_out", lambda: io.StringIO())
    monkeypatch.setattr(worker, "serve", lambda stdin, out: order.append("serve") or 0)
    with pytest.raises(SystemExit):
        worker.main()
    assert order == ["limit", "guard", "serve"]


def test_memory_error_maps_to_source_too_large(monkeypatch, capsys):
    from app import worker

    def boom(url):
        raise MemoryError

    monkeypatch.setattr(worker, "run_inspect", boom)
    out = io.StringIO()
    assert worker.serve(io.StringIO(json.dumps({"mode": "inspect", "url": "https://example.com/"})), out) == 1
    event = json.loads(out.getvalue().splitlines()[-1])
    assert event["code"] == "source_too_large"
