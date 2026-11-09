from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


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

    raw_hosts = env.get("ALLOWED_HOSTS")
    if raw_hosts is None or raw_hosts == "":
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
        temp_dir=temp_dir,
        log_level=log_level,
        allowed_hosts=hosts,
    )
