"""fetch_cover through a real YoutubeDL (in process, no guard) against a loopback server."""
import socket
import time

from yt_dlp import YoutubeDL

from app import cover
from app.cover import cover_opener, fetch_cover


def test_yt_dlp_honours_the_cover_request_timeout(monkeypatch, capsys):
    # The server accepts the connection and never answers. Under the download's 20 s
    # socket_timeout the try would run into the whole budget; the request's own
    # timeout extension ends it first.
    monkeypatch.setattr(cover, "COVER_REQUEST_TIMEOUT", 0.5)
    with (socket.create_server(("127.0.0.1", 0)) as server,
          YoutubeDL({"socket_timeout": 20, "proxy": "", "quiet": True}) as ydl):
        url = f"http://127.0.0.1:{server.getsockname()[1]}/c.jpg"
        start = time.monotonic()
        assert fetch_cover(cover_opener(ydl.urlopen), {"thumbnail": url}) is None
        assert time.monotonic() - start < 5
    assert capsys.readouterr().err == "worker: cover: thumbnail fetch failed: TransportError\n"
