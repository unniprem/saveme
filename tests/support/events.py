"""Small helpers for reading a worker run's protocol events and its folder."""
from pathlib import Path


def result_of(events: list[dict], rc: int) -> dict:
    assert rc == 0 and events and events[-1]["event"] == "result", events
    return events[-1]["data"]


def error_of(events: list[dict], rc: int) -> dict:
    assert rc == 1 and events and events[-1]["event"] == "error", events
    return events[-1]


def listing(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())
