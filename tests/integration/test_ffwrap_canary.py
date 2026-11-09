import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

# FFmpeg 8 defaults local-file inputs to file,crypto,data, so a bare run never
# fetches. The control opts in explicitly to prove the canary can fire; the
# wrapped run passes the same permissive flag, which the wrapper must override.
PERMISSIVE = ["-protocol_whitelist", "file,http,https,tcp,tls,crypto,data"]
BIN = Path(__file__).resolve().parents[2] / "app" / "bin"


@pytest.fixture
def canary(tmp_path):
    state = {"hits": 0}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/canary.ts":
                state["hits"] += 1
            self.send_response(404)
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    evil = tmp_path / "evil.m3u8"
    evil.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\n"
        f"http://127.0.0.1:{port}/canary.ts\n#EXT-X-ENDLIST\n"
    )
    yield state, evil
    srv.shutdown()
    srv.server_close()


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, timeout=60)


def test_control_real_ffprobe_fetches(canary):
    state, evil = canary
    _run([shutil.which("ffprobe"), *PERMISSIVE, "-show_streams", str(evil)])
    assert state["hits"] > 0


def test_wrapped_ffprobe_and_ffmpeg_do_not_fetch(canary):
    state, evil = canary
    state["hits"] = 0
    _run([str(BIN / "ffprobe"), *PERMISSIVE, "-show_streams", str(evil)])
    _run([str(BIN / "ffmpeg"), *PERMISSIVE, "-i", str(evil), "-f", "null", "-"])
    assert state["hits"] == 0


def test_wrapper_passthrough(tmp_path):
    assert _run([str(BIN / "ffmpeg"), "-version"]).returncode == 0
    mp4 = tmp_path / "t.mp4"
    gen = _run([shutil.which("ffmpeg"), "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10",
                "-pix_fmt", "yuv420p", str(mp4)])
    assert gen.returncode == 0
    assert _run([str(BIN / "ffprobe"), "-show_streams", str(mp4)]).returncode == 0
