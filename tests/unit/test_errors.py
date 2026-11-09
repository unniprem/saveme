import pytest
from app.errors import AppError, classify_ytdlp_error, too_large_message, BLOCKED_MARKER

@pytest.mark.parametrize("text,code", [
    ("ERROR: Unsupported URL: https://example.com/", "unsupported"),
    ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", "login_required"),
    ("ERROR: [youtube] x: Video unavailable. This video is private", "login_required"),
    ("ERROR: [Instagram] x: Requested content is not available, rate-limit reached or login required", "login_required"),
    ("ERROR: [youtube] x: Video unavailable", "unavailable"),
    ("ERROR: [generic] Unable to download webpage: HTTP Error 404: Not Found", "unavailable"),
    ("ERROR: [generic] Unable to download webpage: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: [youtube] x: The uploader has not made this video available in your country", "geo_blocked"),
    ("ERROR: [x] y: This video is DRM protected", "drm"),
    ("ERROR: [youtube] x: This live event will begin in 3 hours", "live_unsupported"),
    ("ERROR: [generic] Unable to download webpage: <urlopen error [Errno 8] nodename nor servname provided>", "unreachable"),
    (f"ERROR: [generic] Unable to download webpage: {BLOCKED_MARKER} 10.0.0.1 is not public", "blocked_address"),
    ("ERROR: something nobody anticipated", "extraction_failed"),
])
def test_classify(text, code):
    assert classify_ytdlp_error(text).code == code

def test_catalogue_defaults_and_shape():
    e = AppError("busy")
    assert e.status == 429 and e.message and e.to_dict() == {"code": "busy", "message": e.message}
    assert AppError("no_audio").message == "This media has no audio track."

def test_too_large_message():
    m = too_large_message(2048 * 1024 * 1024)
    assert "2 GB" in m and "lower quality" in m
    assert "1 MB" in too_large_message(1024 * 1024)
