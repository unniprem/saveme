"""`python -m app.whisper_models`: install faster-whisper models into the local cache."""
import pytest

from app import whisper_models
from app.transcript import WHISPER_MODELS


class FakeHub:
    def __init__(self, cached=()):
        self.cached = set(cached)
        self.downloads = []

    def __call__(self, name, local_files_only=False):
        if local_files_only:
            if name not in self.cached:
                raise FileNotFoundError(name)
        else:
            self.downloads.append(name)
            self.cached.add(name)
        return f"/cache/{name}"


def test_model_names_match_faster_whisper():
    utils = pytest.importorskip("faster_whisper.utils")
    assert set(WHISPER_MODELS) == set(utils.available_models())


def test_downloads_the_named_models(capsys):
    hub = FakeHub()
    assert whisper_models.main(["base", "small"], download=hub, env={}) == 0
    assert hub.downloads == ["base", "small"]
    assert "/cache/small" in capsys.readouterr().out


def test_defaults_to_whisper_model_then_base():
    hub = FakeHub()
    whisper_models.main([], download=hub, env={"WHISPER_MODEL": "tiny"})
    whisper_models.main([], download=hub, env={"WHISPER_MODEL": " "})
    assert hub.downloads == ["tiny", "base"]


def test_refuses_unknown_names_before_downloading(capsys):
    hub = FakeHub()
    assert whisper_models.main(["base", "huge"], download=hub, env={}) == 2
    assert hub.downloads == []
    assert "huge" in capsys.readouterr().err


def test_list_shows_what_is_installed(capsys):
    assert whisper_models.main(["--list"], download=FakeHub(cached={"base"}), env={}) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "base  installed" in lines and "small" in lines


def test_reports_a_failed_download(capsys):
    def failing(name, local_files_only=False):
        raise OSError("network down")

    assert whisper_models.main(["base"], download=failing, env={}, sleep=lambda s: None) == 1
    assert "network down" in capsys.readouterr().err


def test_retries_a_failed_download_before_giving_up(capsys):
    attempts, waits = [], []

    def flaky(name, local_files_only=False):
        attempts.append(name)
        if len(attempts) < 3:
            raise OSError("Name or service not known")
        return f"/cache/{name}"

    assert whisper_models.main(["base"], download=flaky, env={}, sleep=waits.append) == 0
    assert attempts == ["base"] * 3 and waits == [2, 4]
    attempts.clear()

    def down(name, local_files_only=False):
        attempts.append(name)
        raise OSError("network down")

    assert whisper_models.main(["base"], download=down, env={}, sleep=waits.append) == 1
    assert len(attempts) == whisper_models.ATTEMPTS
