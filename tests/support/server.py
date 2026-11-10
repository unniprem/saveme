"""A local media HTTP server for the offline tests (127.0.0.1, random port).

Routes:
  /media/<name>        file from the media dir, with Content-Type and Content-Length
  /chunked/<name>      the same file, chunked transfer encoding (no Content-Length)
  /slow/<name>         the same file, trickled 1 KiB every 0.5 s
  /resumable/<name>    the same file, trickled 1 KiB every 0.05 s; a `Range: bytes=N-`
                       request (N > 0) gets the rest at once as a 206 (recorded in .ranges)
  /hls/<name>          files under the media dir's hls/
  /redirect?to=<url>   302 to <url>
  /page?title=&video=&thumb=   HTML with <title>, og:title, og:video, og:image
                       (title_html= instead of title= is inserted unescaped, entities and all)
  /plain               HTML with no media
  /evil.mp4            an HLS playlist pointing at /canary.ts, served as video/mp4
  /canary.ts           counts as a hit if anything fetches it
  anything else        404 (e.g. /missing)
"""
import html
import re
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}
SLOW_CHUNK = 1024
SLOW_DELAY = 0.5
RESUMABLE_DELAY = 0.05
_RANGE = re.compile(r"bytes=(\d+)-")


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    media: "MediaServer"

    def handle_error(self, request, client_address):
        # A client that hangs up mid-body (yt-dlp probing a direct link, a killed
        # worker) is normal here; anything else still gets the default traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.close_connection = True
        parts = urlsplit(self.path)
        path, query = parts.path, parse_qs(parts.query)
        media = self.server.media
        media.record_hit(path)
        for prefix, mode in (("/media/", "plain"), ("/chunked/", "chunked"), ("/slow/", "slow"),
                             ("/resumable/", "resumable")):
            if path.startswith(prefix):
                return self._file(path[len(prefix):], mode)
        if path.startswith("/hls/"):
            return self._file(path[1:], "plain")
        if path == "/redirect" and "to" in query:
            return self._send(302, b"", "text/plain", location=query["to"][0])
        if path == "/page":
            return self._send(200, self._page(query).encode(), "text/html; charset=utf-8")
        if path == "/plain":
            body = b"<!doctype html><html><head><title>Plain</title></head><body><p>No media.</p></body></html>"
            return self._send(200, body, "text/html; charset=utf-8")
        if path == "/evil.mp4":
            playlist = ("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
                        f"{media.url('/canary.ts')}\n#EXT-X-ENDLIST\n")
            return self._send(200, playlist.encode(), "video/mp4")
        if path == "/canary.ts":
            return self._send(200, b"\x47" * 188, "video/mp2t")
        return self._send(404, b"not found", "text/plain")

    def _page(self, query: dict[str, list[str]]) -> str:
        def meta(prop: str, value: str) -> str:
            return f'<meta property="og:{prop}" content="{html.escape(value)}">\n'

        if "title_html" in query:
            title_html = query["title_html"][0]
            head = (f"<title>{title_html}</title>\n"
                    f'<meta property="og:title" content="{title_html}">\n')
        else:
            title = query.get("title", ["Test page"])[0]
            head = f"<title>{html.escape(title)}</title>\n" + meta("title", title)
        if "video" in query:
            video = query["video"][0]
            head += meta("video", self.server.media.url(video) if video.startswith("/") else video)
        if "thumb" in query:
            head += meta("image", query["thumb"][0])
        return f"<!doctype html>\n<html><head>\n<meta charset=\"utf-8\">\n{head}</head><body></body></html>\n"

    def _file(self, rel: str, mode: str) -> None:
        root = self.server.media.root
        target = (root / rel).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        data = target.read_bytes()
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        if mode == "plain":
            return self._send(200, data, ctype)
        delay = SLOW_DELAY
        if mode == "resumable":
            delay = RESUMABLE_DELAY
            match = _RANGE.fullmatch(self.headers.get("Range", ""))
            start = int(match[1]) if match else 0
            if 0 < start < len(data):
                self.server.media.record_range(start)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Connection", "close")
        if mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(data), 64 * 1024):
                chunk = data[i:i + 64 * 1024]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        for i in range(0, len(data), SLOW_CHUNK):
            self.wfile.write(data[i:i + SLOW_CHUNK])
            time.sleep(delay)

    def _send(self, status: int, body: bytes, ctype: str, *, location: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)


class MediaServer:
    """Serves `root` on 127.0.0.1 from a daemon thread until close()."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.hits: Counter[str] = Counter()
        self.ranges: list[int] = []  # start offsets of the 206s /resumable/ sent
        self._lock = threading.Lock()
        self._httpd = _HTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.media = self
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def record_hit(self, path: str) -> None:
        with self._lock:
            self.hits[path] += 1

    def record_range(self, start: int) -> None:
        with self._lock:
            self.ranges.append(start)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()
