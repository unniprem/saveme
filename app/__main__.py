"""`python -m app`: serve saveme with uvicorn on port 8000."""
import logging
import os
import sys

import uvicorn

_LEVELS = ("debug", "info", "warning", "error")


def _log_level() -> str:
    # An invalid LOG_LEVEL puts the app in degraded mode (app.main says so); log at info.
    level = os.environ.get("LOG_LEVEL", "").strip().lower()
    return level if level in _LEVELS else "info"


def main() -> None:
    level = _log_level()
    # uvicorn configures only its own loggers; this sends the app's to stdout too.
    logging.basicConfig(level=level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, log_level=level,
                timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
