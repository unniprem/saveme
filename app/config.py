from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(Exception):
    def __init__(self, var: str, message: str):
        self.var = var
        self.message = message
        super().__init__(f"{var}: {message}")


@dataclass(frozen=True)
class Settings:
    max_active_jobs: int
    max_queued_jobs: int
    max_active_inspections: int
    inspect_timeout_seconds: int
    max_file_size_mb: int
    max_job_seconds: int
    job_ttl_minutes: int
    max_ready_files: int
    max_total_disk_mb: int
    max_clip_sources: int
    temp_dir: Path
    log_level: str
    allowed_hosts: tuple[str, ...]

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def job_ttl_seconds(self) -> int:
        return self.job_ttl_minutes * 60

    @property
    def max_total_disk_bytes(self) -> int:
        return self.max_total_disk_mb * 1024 * 1024


_LOG_LEVELS = {"debug", "info", "warning", "error"}


def _get(env: Mapping[str, str], var: str) -> str | None:
    value = env.get(var)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _int(env: Mapping[str, str], var: str, default: int, lo: int, hi: int) -> int:
    raw = _get(env, var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(var, f"must be an integer, got {raw!r}") from None
    if not lo <= value <= hi:
        raise ConfigError(var, f"must be between {lo} and {hi}, got {value}")
    return value


def load_settings(env: Mapping[str, str]) -> Settings:
    max_file_size_mb = _int(env, "MAX_FILE_SIZE_MB", 2048, 1, 102400)
    max_total_disk_mb = _int(env, "MAX_TOTAL_DISK_MB", 10240, 1, 10_485_760)
    if max_total_disk_mb < 2 * max_file_size_mb:
        raise ConfigError(
            "MAX_TOTAL_DISK_MB", "must be at least 2 x MAX_FILE_SIZE_MB"
        )

    raw_dir = _get(env, "TEMP_DIR")
    temp_dir = Path(raw_dir) if raw_dir is not None else Path("/tmp/media-jobs")
    if not temp_dir.is_absolute() or temp_dir == Path("/"):
        raise ConfigError("TEMP_DIR", "must be an absolute path other than /")

    log_level = (_get(env, "LOG_LEVEL") or "info").lower()
    if log_level not in _LOG_LEVELS:
        raise ConfigError("LOG_LEVEL", f"must be one of {sorted(_LOG_LEVELS)}")

    raw_hosts = _get(env, "ALLOWED_HOSTS")
    if raw_hosts is None:
        hosts = ("localhost", "127.0.0.1")
    else:
        hosts = tuple(h.strip() for h in raw_hosts.split(",") if h.strip())
        if not hosts:
            raise ConfigError("ALLOWED_HOSTS", "must list at least one host")

    return Settings(
        max_active_jobs=_int(env, "MAX_ACTIVE_JOBS", 2, 1, 16),
        max_queued_jobs=_int(env, "MAX_QUEUED_JOBS", 4, 0, 100),
        max_active_inspections=_int(env, "MAX_ACTIVE_INSPECTIONS", 4, 1, 32),
        inspect_timeout_seconds=_int(env, "INSPECT_TIMEOUT_SECONDS", 30, 5, 300),
        max_file_size_mb=max_file_size_mb,
        max_job_seconds=_int(env, "MAX_JOB_SECONDS", 1800, 10, 86400),
        job_ttl_minutes=_int(env, "JOB_TTL_MINUTES", 30, 1, 1440),
        max_ready_files=_int(env, "MAX_READY_FILES", 6, 1, 100),
        max_total_disk_mb=max_total_disk_mb,
        max_clip_sources=_int(env, "MAX_CLIP_SOURCES", 2, 1, 20),
        temp_dir=temp_dir,
        log_level=log_level,
        allowed_hosts=hosts,
    )


# --- AI Highlights ---------------------------------------------------------------------------
# Loaded apart from Settings: an invalid AI value turns off AI Highlights only, never downloads
# or manual clipping. A blank OPENROUTER_API_KEY isn't an error: analysis is off, transcription
# still works.

DEFAULT_OPENROUTER_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._:-]+")
_KEY = re.compile(r"[\x21-\x7e]{1,512}")  # visible ASCII, no spaces


@dataclass(frozen=True)
class AiSettings:
    openrouter_api_key: str = field(repr=False)
    openrouter_model: str
    openrouter_base_url: str
    openrouter_data_collection: str  # deny | allow
    ai_max_clips: int
    ai_max_concurrent_jobs: int
    ai_max_cost_usd_per_job: float
    ai_max_source_minutes: int
    ai_max_job_seconds: int
    whisper_model: str
    whisper_cpu_threads: int

    @property
    def configured(self) -> bool:
        """True when an OpenRouter key is set (analysis can run)."""
        return bool(self.openrouter_api_key)


def _usd(env: Mapping[str, str], var: str, default: float, lo: float, hi: float) -> float:
    raw = _get(env, var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(var, f"must be a number of US dollars, got {raw!r}") from None
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ConfigError(var, f"must be between {lo} and {hi}")
    return value


def _base_url(env: Mapping[str, str]) -> str:
    raw = _get(env, "OPENROUTER_BASE_URL")
    if raw is None:
        return DEFAULT_OPENROUTER_BASE_URL
    invalid = ConfigError("OPENROUTER_BASE_URL", "must be an https:// URL with no login, query or fragment")
    try:
        import httpx

        parts = urlsplit(raw)
        _port = parts.port  # a bad port only raises when read
        httpx.URL(raw)  # the client must take it too (no spaces, valid host)
    except (ValueError, httpx.InvalidURL):
        raise invalid from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or " " in raw):
        raise invalid
    return raw.rstrip("/")


def load_ai_settings(env: Mapping[str, str]) -> AiSettings:
    from app.mediafile import MAX_BATCH_CLIPS
    from app.transcript import WHISPER_MODELS

    key = _get(env, "OPENROUTER_API_KEY") or ""
    if key and not _KEY.fullmatch(key):
        # Never echo the value: it is a secret.
        raise ConfigError("OPENROUTER_API_KEY", "must be the key as OpenRouter shows it, with no spaces")
    model = _get(env, "OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    if len(model) > 128 or not _MODEL_ID.fullmatch(model):
        raise ConfigError("OPENROUTER_MODEL", "must be an OpenRouter model id like author/model")
    collection = (_get(env, "OPENROUTER_DATA_COLLECTION") or "deny").lower()
    if collection not in ("deny", "allow"):
        raise ConfigError("OPENROUTER_DATA_COLLECTION", "must be deny or allow")
    whisper = _get(env, "WHISPER_MODEL") or "base"
    if whisper not in WHISPER_MODELS:
        raise ConfigError("WHISPER_MODEL", f"must be one of {', '.join(WHISPER_MODELS)}")
    return AiSettings(
        openrouter_api_key=key,
        openrouter_model=model,
        openrouter_base_url=_base_url(env),
        openrouter_data_collection=collection,
        ai_max_clips=_int(env, "AI_MAX_CLIPS", 10, 1, MAX_BATCH_CLIPS),
        ai_max_concurrent_jobs=_int(env, "AI_MAX_CONCURRENT_JOBS", 1, 1, 4),
        ai_max_cost_usd_per_job=_usd(env, "AI_MAX_COST_USD_PER_JOB", 0.25, 0.001, 100),
        ai_max_source_minutes=_int(env, "AI_MAX_SOURCE_MINUTES", 180, 1, 1440),
        ai_max_job_seconds=_int(env, "AI_MAX_JOB_SECONDS", 3600, 60, 86400),
        whisper_model=whisper,
        whisper_cpu_threads=_int(env, "WHISPER_CPU_THREADS", 2, 1, 16),
    )
