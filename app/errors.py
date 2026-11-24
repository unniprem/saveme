"""Error catalogue and yt-dlp error classification."""
import re

BLOCKED_MARKER = "[saveme-blocked]"

CATALOG: dict[str, tuple[int, str]] = {
    "invalid_url": (400, "That doesn't look like a valid web link. Paste a single http or https URL."),
    "invalid_request": (400, "The request wasn't valid. Check the options and try again."),
    "request_too_large": (413, "The request is too large."),
    "blocked_address": (403, "This link points to a private or local network address, which isn't allowed."),
    "forbidden_origin": (403, "This request came from an origin that isn't allowed."),
    "job_not_found": (404, "That download couldn't be found. It may have expired or the server may have restarted."),
    "not_ready": (409, "The file isn't ready yet. Please wait for the download to finish."),
    "delivery_in_progress": (409, "This file is already being saved. If that save stopped, try again in a moment."),
    "expired": (410, "This download has expired. Please start again."),
    "cancelled": (410, "This download was cancelled. Please start again."),
    "not_resumable": (410, "This download can no longer be resumed. Please start again."),
    "source_not_found": (404, "This video is no longer open for clipping. It may have been closed, "
                              "or removed after a while unused. Load it again."),
    "source_not_ready": (409, "The video is still loading. Wait until it's ready, then try again."),
    "invalid_clip": (400, "Choose a start before the end, inside the video."),
    "upload_incomplete": (400, "The upload stopped before the whole file arrived. Please try again."),
    "invalid_subtitles": (400, "SaveMe can't read this as subtitles. Use an SRT or WebVTT (.vtt) file."),
    "unsupported": (422, "This link isn't supported, or it doesn't contain downloadable media."),
    "login_required": (422, "This media requires a login or is private, so it can't be downloaded."),
    "unavailable": (422, "This media is unavailable. It may have been removed or doesn't exist."),
    "geo_blocked": (422, "This media isn't available in the server's region."),
    "drm": (422, "This media is DRM protected and can't be downloaded."),
    "live_unsupported": (422, "Live streams aren't supported."),
    "playlist_unsupported": (422, "Playlists aren't supported. Paste a link to a single video or audio item."),
    "no_audio": (422, "This media has no audio track."),
    "no_video": (422, "This media has no video track."),
    "extraction_failed": (422, "Couldn't get this media from the link. Try again later or try a different link."),
    "source_too_large": (422, "The page at this link is too large to read. Try a link to the media itself."),
    "unreadable_media": (422, "SaveMe can't read this file as a video. Try an MP4, MOV, MKV or WebM file."),
    "clip_failed": (422, "Couldn't cut this clip. Try a different start or end."),
    "transcription_failed": (422, "Couldn't transcribe this video's audio. Try again, or import its subtitles."),
    "video_too_long": (422, "This video is too long for AI Highlights."),
    "whisper_model_missing": (503, "The transcription model isn't installed on the server."),
    "too_large": (413, "This file is larger than the allowed size limit. Try a lower quality or audio only."),
    "busy": (429, "The server is busy right now. Please try again shortly."),
    "rate_limited": (429, "The source site is limiting requests right now. Please try again later."),
    "internal": (500, "Something went wrong on the server. Please try again."),
    "unreachable": (502, "Couldn't reach the source site. Check the link or try again later."),
    "disk_full": (503, "Server storage is full, try again later."),
    "config_error": (503, "The server is misconfigured. Please contact the owner."),
    "ai_not_configured": (503, "AI analysis isn't set up. Add OPENROUTER_API_KEY to the server's .env "
                               "and restart SaveMe."),
    "ai_config_error": (503, "AI Highlights is turned off because a setting is invalid."),
    "model_unsupported": (503, "The configured AI model can't be used for AI Highlights."),
    "openrouter_auth": (502, "OpenRouter didn't accept the API key. Check OPENROUTER_API_KEY in the "
                             "server's .env."),
    "openrouter_credits": (502, "The OpenRouter account or key is out of credits. Add credits or raise "
                                "the key's limit, then try again."),
    "openrouter_rate_limited": (429, "OpenRouter is limiting requests right now. Wait a minute, then try again."),
    "openrouter_refused": (502, "OpenRouter refused the request."),
    "openrouter_unavailable": (502, "Couldn't get an answer from OpenRouter. Check the connection, or try "
                                    "again later."),
    "ai_invalid_response": (502, "The AI model returned an answer SaveMe couldn't use. Try again, or "
                                 "choose another model."),
    "timeout": (504, "The operation took too long and was stopped. Please try again."),
}


class AppError(Exception):
    def __init__(self, code: str, message: str | None = None, status: int | None = None):
        default_status, default_message = CATALOG[code]
        self.code = code
        self.message = message if message is not None else default_message
        self.status = status if status is not None else default_status
        super().__init__(self.message)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("drm", ("drm",)),
    ("live_unsupported", ("live event", "is live", "live stream")),
    ("login_required", ("sign in", "login", "log in", "private", "cookies",
                        "members-only", "confirm your age")),
    ("geo_blocked", ("your country", "geo")),
    ("rate_limited", ("429", "too many requests")),
    ("unavailable", ("video unavailable", "404", "has been removed", "does not exist")),
    ("unsupported", ("unsupported url", "no video formats", "no media")),
    ("unreachable", ("nodename", "name or service not known", "connection refused",
                     "network is unreachable", "timed out", "unable to download webpage")),
]


# Text that names the request rather than describing the failure: URLs,
# yt-dlp's "[extractor] <id>:" prefix (not "[generic] Unable ..." or "[Errno 8] ..."),
# and urllib3's endpoint details: host='...', port=N, and quoted space-free tokens
# containing "." or "/" ('login.example.com'), so prose like "isn't ... it's" stays.
_URL_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_ID_PREFIX = re.compile(r"\[[^\]]+\]\s*[^\s:]+:")
_ENDPOINT_TEXT = re.compile(r"""\bhost=(['"])[^'"]*\1|\bport=\d+|(['"])[^'"\s]*[./][^'"\s]*\2""")


def classify_ytdlp_error(text: str) -> AppError:
    if BLOCKED_MARKER in text.lower():
        return AppError("blocked_address")
    for pattern in (_URL_TEXT, _ID_PREFIX, _ENDPOINT_TEXT):
        text = pattern.sub(" ", text)
    low = text.lower()
    for code, keys in _RULES:
        if any(k in low for k in keys):
            return AppError(code)
    return AppError("extraction_failed")


def size_text(limit_bytes: int) -> str:
    """A size limit in whole MB, or GB from 1 GB up: "500 MB", "2 GB"."""
    mb = limit_bytes // (1024 * 1024)
    if mb >= 1024:
        return f"{mb // 1024} GB"
    return f"{mb} MB"


def too_large_message(limit_bytes: int) -> str:
    return f"This file is larger than the {size_text(limit_bytes)} limit. Try a lower quality or audio only."
