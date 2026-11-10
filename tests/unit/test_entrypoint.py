"""`python -m app` listens on loopback unless told otherwise (the app has no login)."""
import pytest

import app.__main__ as entry


@pytest.fixture
def served(monkeypatch):
    """The keyword arguments of each uvicorn.run call; no server is started."""
    calls = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(entry.logging, "basicConfig", lambda **kwargs: None)
    return calls


def test_listens_on_loopback_by_default(served):
    entry.main([])
    assert [call["host"] for call in served] == ["127.0.0.1"]


def test_host_flag_sets_the_listen_address(served):
    entry.main(["--host", "0.0.0.0"])
    assert [call["host"] for call in served] == ["0.0.0.0"]
