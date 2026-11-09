"""Static checks on the Docker packaging files (the image itself is checked by scripts/docker-smoke.sh)."""
import dataclasses
import re
from pathlib import Path

from app.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _parse_env_file(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        assert sep, f"not a KEY=value line: {line!r}"
        pairs.append((key.strip(), value.strip()))
    return pairs


def test_dockerfile_ytdlp_arg_matches_lock():
    arg = re.search(r"^ARG YTDLP_VERSION=(\S+)\s*$", _read("Dockerfile"), re.M)
    pin = re.search(r"^yt-dlp(?:\[[^\]]*\])?==(\S+)\s*$", _read("requirements.txt"), re.M)
    assert arg and pin
    assert arg.group(1) == pin.group(1)


def test_env_example_matches_settings_defaults():
    pairs = _parse_env_file(_read(".env.example"))
    keys = [k for k, _ in pairs]
    settings_vars = sorted(f.name.upper() for f in dataclasses.fields(Settings))
    assert sorted(keys) == settings_vars  # exactly the Settings variables, no duplicates
    assert all(v for _, v in pairs), "every variable carries its default value"
    assert load_settings(dict(pairs)) == load_settings({})


def test_compose_binds_loopback_only():
    text = _read("compose.yaml")
    assert '"127.0.0.1:8000:8000"' in text
    ports = [v.strip() for v in re.findall(r"^\s*ports:(.*)$", text, re.M)]
    assert ports == ['["127.0.0.1:8000:8000"]']
    assert "network_mode" not in text


def test_no_curl_cffi_in_lock():
    assert "curl" not in _read("requirements.txt").lower()
