"""Error catalogue and yt-dlp error classification."""
import re

BLOCKED_MARKER = "[saveme-blocked]"

CATALOG: dict[str, tuple[int, str]] = {
    "invalid_url": (400, "That doesn't look like a valid web link. Paste a single http or https URL."),
    "invalid_request": (400, "The request wasn't valid. Check the options and try again."),
    "blocked_address": (403, "This link points to a private or local network address, which isn't allowed."),
    "forbidden_origin": (403, "This request came from an origin that isn't allowed."),
    "job_not_found": (404, "That download couldn't be found. It may have expired or the server may have restarted."),
    "not_ready": (409, "The file isn't ready yet. Please wait for the download to finish."),
    "expired": (410, "This download has expired. Please start again."),
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
    "too_large": (413, "This file is larger than the allowed size limit. Try a lower quality or audio only."),
    "busy": (429, "The server is busy right now. Please try again shortly."),
    "rate_limited": (429, "The source site is limiting requests right now. Please try again later."),
    "internal": (500, "Something went wrong on the server. Please try again."),
    "unreachable": (502, "Couldn't reach the source site. Check the link or try again later."),
    "disk_full": (503, "Server storage is full, try again later."),
    "config_error": (503, "The server is misconfigured. Please contact the owner."),
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


# Text that names the request rather than describing the failure: URLs, and
# yt-dlp's "[extractor] <id>:" prefix (not "[generic] Unable ..." or "[Errno 8] ...").
_URL_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
_ID_PREFIX = re.compile(r"\[[^\]]+\]\s*[^\s:]+:")


def classify_ytdlp_error(text: str) -> AppError:
    if BLOCKED_MARKER in text.lower():
        return AppError("blocked_address")
    low = _ID_PREFIX.sub(" ", _URL_TEXT.sub(" ", text)).lower()
    for code, keys in _RULES:
        if any(k in low for k in keys):
            return AppError(code)
    return AppError("extraction_failed")


def too_large_message(limit_bytes: int) -> str:
    mb = limit_bytes // (1024 * 1024)
    if mb >= 1024:
        n, unit = mb // 1024, "GB"
    else:
        n, unit = mb, "MB"
    return f"This file is larger than the {n} {unit} limit. Try a lower quality or audio only."
