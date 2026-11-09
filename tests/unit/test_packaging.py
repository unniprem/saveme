"""Checks on the Docker packaging files and on how scripts/docker-smoke.sh fails.

The image itself is checked by running scripts/docker-smoke.sh against a real Docker daemon.
"""
import dataclasses
import os
import re
import subprocess
import sys
import textwrap
import time
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


def test_deno_stage_pinned_by_index_digest():
    text = _read("Dockerfile")
    assert re.search(r"^ARG DENO_VERSION=2\.\d+\.\d+$", text, re.M)
    assert re.search(r"^ARG DENO_DIGEST=sha256:[0-9a-f]{64}$", text, re.M)
    froms = re.findall(r"^FROM (\S+) AS deno$", text, re.M)
    assert froms == ["denoland/deno:bin-${DENO_VERSION}@${DENO_DIGEST}"]
    # The digest wins over the tag, so the build checks the binary is the version named.
    assert re.search(r'^RUN deno --version \| grep -q "\^deno \$\{DENO_VERSION\} "$', text, re.M)


# A fake `docker` for running the smoke script without a daemon. `docker inspect -f TEMPLATE`
# renders {{.Field.Path}} from STATE ({{if ...}}/{{end}} markers are dropped).
_FAKE_DOCKER = textwrap.dedent("""\
    #!{python}
    import os, re, sys
    STATE = {{"State.Status": "exited", "State.Running": "false", "State.ExitCode": "1",
             "State.Health.Status": "starting", "RestartCount": "0"}}
    args = sys.argv[1:]
    if args[:2] == ["compose", "ps"]:
        rc = int(os.environ.get("FAKE_PS_RC", "0"))
        if not rc:
            print("c0ffee")
        sys.exit(rc)
    if args[:1] == ["inspect"]:
        template = re.sub(r"{{{{(if [^}}]*|end)}}}}", "", args[args.index("-f") + 1])
        print(re.sub(r"{{{{\\s*\\.([\\w.]+)\\s*}}}}", lambda m: STATE.get(m[1], "<no value>"), template))
    sys.exit(0)
""")


def _run_smoke(tmp_path, **env_extra):
    fake = tmp_path / "docker"
    fake.write_text(_FAKE_DOCKER.format(python=sys.executable))
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}", **env_extra}
    started = time.monotonic()
    proc = subprocess.run([str(ROOT / "scripts" / "docker-smoke.sh")], env=env,
                          capture_output=True, text=True, timeout=30)
    return proc, time.monotonic() - started


def test_smoke_reports_compose_ps_failure(tmp_path):
    proc, _ = _run_smoke(tmp_path, FAKE_PS_RC="3")
    assert proc.returncode == 1
    assert "SMOKE FAIL" in proc.stderr


def test_smoke_fails_fast_when_container_exits(tmp_path):
    proc, elapsed = _run_smoke(tmp_path)
    assert proc.returncode == 1
    assert "SMOKE FAIL" in proc.stderr and "exited" in proc.stderr
    assert elapsed < 10


def test_readme_hedges_the_reddit_cause():
    # Observed: Reddit answered 403 and yt-dlp reported login_required. That the cause is the
    # missing curl_cffi impersonation is inferred, so the README must not state it as fact.
    text = _read("README.md")
    causal = [line for line in text.splitlines()
              if "Reddit" in line and ("curl_cffi" in line or "browser" in line)]
    assert causal
    for line in causal:
        assert re.search(r"\b(likely|appears?)\b", line), line
    assert "Reddit blocks" not in text
    assert "This is why some Reddit links fail" not in text
