from pathlib import Path
import pytest
from app.config import load_settings, ConfigError

def test_defaults_when_env_empty():
    s = load_settings({})
    assert (s.max_active_jobs, s.max_queued_jobs, s.max_active_inspections) == (2, 4, 4)
    assert (s.inspect_timeout_seconds, s.max_file_size_mb, s.max_job_seconds) == (30, 2048, 1800)
    assert (s.job_ttl_minutes, s.max_ready_files, s.max_total_disk_mb) == (30, 6, 10240)
    assert s.temp_dir == Path("/tmp/media-jobs") and s.log_level == "info"
    assert s.allowed_hosts == ("localhost", "127.0.0.1")
    assert s.max_file_bytes == 2048 * 1024 * 1024 and s.job_ttl_seconds == 1800

def test_env_overrides_and_blank_means_default():
    s = load_settings({"MAX_ACTIVE_JOBS": "3", "TEMP_DIR": "/data/jobs", "MAX_QUEUED_JOBS": "",
                       "ALLOWED_HOSTS": "localhost, saveme.lan"})
    assert s.max_active_jobs == 3 and s.temp_dir == Path("/data/jobs") and s.max_queued_jobs == 4
    assert s.allowed_hosts == ("localhost", "saveme.lan")

@pytest.mark.parametrize("value", ["", "  ", "\t \n"])
def test_blank_allowed_hosts_means_default(value):
    assert load_settings({"ALLOWED_HOSTS": value}).allowed_hosts == ("localhost", "127.0.0.1")

@pytest.mark.parametrize("var,value", [
    ("MAX_ACTIVE_JOBS", "0"), ("MAX_ACTIVE_JOBS", "abc"), ("MAX_FILE_SIZE_MB", "-1"),
    ("INSPECT_TIMEOUT_SECONDS", "1"), ("TEMP_DIR", "relative/path"), ("TEMP_DIR", "/"),
    ("LOG_LEVEL", "loud"), ("ALLOWED_HOSTS", " , "),
])
def test_invalid_values_name_the_variable(var, value):
    with pytest.raises(ConfigError) as e:
        load_settings({var: value})
    assert e.value.var == var and var in str(e.value)

def test_disk_budget_must_hold_one_reservation():
    with pytest.raises(ConfigError) as e:
        load_settings({"MAX_FILE_SIZE_MB": "2048", "MAX_TOTAL_DISK_MB": "4000"})
    assert e.value.var == "MAX_TOTAL_DISK_MB"
