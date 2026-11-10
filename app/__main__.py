"""`python -m app [--host ADDRESS]`: serve saveme with uvicorn on port 8000.

The app has no login, so it listens on loopback unless told otherwise. The Docker image
passes `--host 0.0.0.0`: compose publishes that port on the host's 127.0.0.1 only.
"""
import argparse
import logging
import os
import sys

import uvicorn

_LEVELS = ("debug", "info", "warning", "error")


def _log_level() -> str:
    # An invalid LOG_LEVEL puts the app in degraded mode (app.main says so); log at info.
    level = os.environ.get("LOG_LEVEL", "").strip().lower()
    return level if level in _LEVELS else "info"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default: %(default)s)")
    args = parser.parse_args(argv)
    level = _log_level()
    # uvicorn configures only its own loggers; this sends the app's to stdout too.
    logging.basicConfig(level=level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("app.main:app", host=args.host, port=8000, log_level=level,
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
