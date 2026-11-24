"""AI settings: loaded apart from the download settings, so a bad AI value turns off only AI."""
import pytest

from app.config import AiSettings, ConfigError, load_ai_settings

KEY = "sk-or-v1-0123456789abcdef"


def test_defaults():
    s = load_ai_settings({})
    assert s == AiSettings(
        openrouter_api_key="", openrouter_model="anthropic/claude-haiku-5.5",
        openrouter_base_url="https://openrouter.ai/api/v1", openrouter_data_collection="deny",
        ai_max_clips=10, ai_max_concurrent_jobs=1, ai_max_cost_usd_per_job=0.25,
        ai_max_source_minutes=180, ai_max_job_seconds=3600, whisper_model="base",
        whisper_cpu_threads=2)
    assert not s.configured


def test_values_are_read_and_trimmed():
    s = load_ai_settings({
        "OPENROUTER_API_KEY": f"  {KEY} ", "OPENROUTER_MODEL": "deepseek/deepseek-v4.1-flash-1017",
        "OPENROUTER_BASE_URL": "https://gateway.example/api/v1/", "OPENROUTER_DATA_COLLECTION": "Allow",
        "AI_MAX_CLIPS": "20", "AI_MAX_CONCURRENT_JOBS": "4", "AI_MAX_COST_USD_PER_JOB": "1.5",
        "AI_MAX_SOURCE_MINUTES": "60", "AI_MAX_JOB_SECONDS": "600", "WHISPER_MODEL": "small",
        "WHISPER_CPU_THREADS": "4"})
    assert s.configured and s.openrouter_api_key == KEY
    assert s.openrouter_base_url == "https://gateway.example/api/v1"
    assert s.openrouter_data_collection == "allow"
    assert (s.ai_max_clips, s.ai_max_concurrent_jobs, s.ai_max_cost_usd_per_job) == (20, 4, 1.5)
    assert (s.ai_max_source_minutes, s.ai_max_job_seconds) == (60, 600)
    assert (s.whisper_model, s.whisper_cpu_threads) == ("small", 4)


def test_the_key_never_shows_in_repr_or_errors():
    s = load_ai_settings({"OPENROUTER_API_KEY": KEY})
    assert KEY not in repr(s) and KEY not in str(s)
    with pytest.raises(ConfigError) as exc:
        load_ai_settings({"OPENROUTER_API_KEY": "sk-or bad key"})
    assert exc.value.var == "OPENROUTER_API_KEY" and "sk-or" not in str(exc.value)


@pytest.mark.parametrize("var,value", [
    ("OPENROUTER_API_KEY", "abc\ndef"),
    ("OPENROUTER_MODEL", "no-slash"), ("OPENROUTER_MODEL", "a/b c"), ("OPENROUTER_MODEL", "x/" + "y" * 200),
    ("OPENROUTER_BASE_URL", "http://openrouter.ai/api/v1"),
    ("OPENROUTER_BASE_URL", "https://user:pw@openrouter.ai/api/v1"),
    ("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1?x=1"),
    ("OPENROUTER_BASE_URL", "https:///api"), ("OPENROUTER_BASE_URL", "ftp://x"),
    ("OPENROUTER_BASE_URL", "https://[openrouter.ai/api/v1"),  # urlsplit raises
    ("OPENROUTER_BASE_URL", "https://openrouter.ai:abc/api/v1"),  # bad port, only seen on access
    ("OPENROUTER_BASE_URL", "https://openrouter.ai:99999/api/v1"),
    ("OPENROUTER_BASE_URL", "https://open router.ai/api/v1"),
    ("OPENROUTER_DATA_COLLECTION", "maybe"),
    ("AI_MAX_CLIPS", "0"), ("AI_MAX_CLIPS", "21"), ("AI_MAX_CLIPS", "ten"),
    ("AI_MAX_CONCURRENT_JOBS", "5"),
    ("AI_MAX_COST_USD_PER_JOB", "0"), ("AI_MAX_COST_USD_PER_JOB", "101"),
    ("AI_MAX_COST_USD_PER_JOB", "nan"), ("AI_MAX_COST_USD_PER_JOB", "inf"),
    ("AI_MAX_COST_USD_PER_JOB", "$1"),
    ("AI_MAX_SOURCE_MINUTES", "0"), ("AI_MAX_JOB_SECONDS", "59"),
    ("WHISPER_MODEL", "huge"), ("WHISPER_CPU_THREADS", "17"),
])
def test_invalid_values_name_the_variable(var, value):
    with pytest.raises(ConfigError) as exc:
        load_ai_settings({var: value})
    assert exc.value.var == var


def test_a_bad_ai_setting_never_stops_the_app_starting():
    from app.main import _ai_settings_or_error
    import os
    from unittest import mock

    for value in ("https://[openrouter.ai/api/v1", "https://openrouter.ai:abc/api/v1"):
        with mock.patch.dict(os.environ, {"OPENROUTER_BASE_URL": value}):
            result = _ai_settings_or_error()
        assert isinstance(result, ConfigError) and result.var == "OPENROUTER_BASE_URL", value


def test_an_accepted_base_url_is_one_httpx_takes():
    import httpx

    url = load_ai_settings({"OPENROUTER_BASE_URL": "https://gateway.example:8443/api/v1"}).openrouter_base_url
    assert httpx.URL(url + "/models").port == 8443
