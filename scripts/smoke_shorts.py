"""Auto Shorts smoke check, run inside the container by scripts/docker-smoke.sh
(`docker compose exec -T saveme python - < scripts/smoke_shorts.py`).

The container's own FFmpeg makes a 4-second test video (so the host needs no FFmpeg). The check
uploads it, imports English and Malayalam subtitles, renders a captioned 9:16 draft short, downloads
it and probes it. Exit 0 and one summary line on success; an AssertionError otherwise.
"""
import json
import math
import struct
import subprocess
import tempfile
import time
import urllib.request
import wave

BASE = "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WORK = tempfile.mkdtemp()


def call(method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    with OPENER.open(req, timeout=60) as res:
        return json.loads(res.read())


def make_video(path: str, seconds: int = 4) -> None:
    width, height, fps = 320, 240, 25
    with open(f"{WORK}/frames.rgb", "wb") as frames:
        for n in range(fps * seconds):
            frames.write(bytes([(n * 6) % 256, 90, 160]) * (width * height))
    with wave.open(f"{WORK}/tone.wav", "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * i / 48000)))
                                   for i in range(48000 * seconds)))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pixel_format", "rgb24",
                    "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", f"{WORK}/frames.rgb",
                    "-i", f"{WORK}/tone.wav", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", path], check=True)


def main() -> None:
    make_video(f"{WORK}/src.mp4")
    with open(f"{WORK}/src.mp4", "rb") as f:
        data = f.read()
    source = call("POST", "/api/sources/upload", data, {"Content-Type": "application/octet-stream",
                                                        "X-SaveMe-Filename": "smoke.mp4"})["source_id"]
    for _ in range(60):
        if call("GET", f"/api/sources/{source}")["status"] == "ready":
            break
        time.sleep(0.5)
    srt = ("1\n00:00:00,200 --> 00:00:01,800\nHello from the smoke test\n\n"
           "2\n00:00:02,000 --> 00:00:03,800\nഇലക്ട്രിക് വാഹനങ്ങൾ\n")
    call("POST", f"/api/sources/{source}/transcript", srt.encode(), {"Content-Type": "text/plain"})
    body = json.dumps({"clips": [{"start": 0, "end": 4}],
                       "spec": {"template": "educational", "quality": "draft", "captions": {"export": ["srt"]}}})
    render = call("POST", f"/api/sources/{source}/renders", body.encode(),
                  {"Content-Type": "application/json"})["render_id"]
    view = {}
    for _ in range(240):
        view = call("GET", f"/api/renders/{render}")
        if view["status"] not in ("queued", "running"):
            break
        time.sleep(0.5)
    assert view.get("status") == "ready", view
    clip = view["clips"][0]
    assert clip["encoder"] == "libx264" and clip["captions_timing"] == "estimated", clip
    with OPENER.open(f"{BASE}/api/renders/{render}/clips/1/file", timeout=60) as res, \
            open(f"{WORK}/short.mp4", "wb") as out:
        out.write(res.read())
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                       "stream=codec_name,width,height,pix_fmt:format=duration", "-of", "json",
                                       f"{WORK}/short.mp4"], check=True, capture_output=True).stdout)
    codecs = sorted(s["codec_name"] for s in probe["streams"])
    video = next(s for s in probe["streams"] if s["codec_name"] == "h264")
    assert codecs == ["aac", "h264"] and (video["width"], video["height"]) == (540, 960), probe
    assert video["pix_fmt"] == "yuv420p" and abs(float(probe["format"]["duration"]) - 4.0) < 0.2, probe
    call("POST", f"/api/sources/{source}/close", b"{}", {"Content-Type": "application/json"})
    print(f"    short: {video['width']}x{video['height']} h264+aac, {float(probe['format']['duration']):.2f} s")


if __name__ == "__main__":
    main()
