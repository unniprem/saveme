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

# URLs and "[extractor] <id>:" prefixes are not evidence: keywords inside them
# (a port containing 429, a "cookies" path, an id containing "geo404") must not
# decide the code.
@pytest.mark.parametrize("text,code", [
    ("ERROR: Unsupported URL: http://127.0.0.1:54290/plain", "unsupported"),
    ("ERROR: Unsupported URL: https://example.com/recipes/cookies", "unsupported"),
    ("ERROR: [youtube] geo404abcde: Video unavailable", "unavailable"),
    (f"ERROR: [generic] x: Unable to download webpage: {BLOCKED_MARKER} ::1 is not a public address",
     "blocked_address"),
    ("ERROR: [generic] x: Unable to download webpage: HTTPSConnection(host='login.example.com', port=443): "
     "Failed to resolve 'login.example.com' ([Errno 8] nodename nor servname provided, or not known)",
     "unreachable"),
    ("ERROR: [generic] x: Unable to download webpage: HTTPConnection(host='127.0.0.1', port=54290): "
     "Failed to establish a new connection: [Errno 61] Connection refused", "unreachable"),
    # Apostrophes in prose are not quoted host/path tokens.
    ("ERROR: [youtube] x: This video isn't available, it's private", "login_required"),
])
def test_classify_ignores_urls_and_ids(text, code):
    assert classify_ytdlp_error(text).code == code

def test_catalogue_defaults_and_shape():
    e = AppError("busy")
    assert e.status == 429 and e.message and e.to_dict() == {"code": "busy", "message": e.message}
    assert AppError("no_audio").message == "This media has no audio track."

def test_too_large_message():
    m = too_large_message(2048 * 1024 * 1024)
    assert "2 GB" in m and "lower quality" in m
    assert "1 MB" in too_large_message(1024 * 1024)
