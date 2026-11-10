"""Test-only worker entry (`python -m tests.support.worker_entry`).

Same as `python -m app.worker` (memory limit included) except that the guard's
policy also allows the loopback media server (127.0.0.1). Production never accepts an
injected policy.
"""
import os
import sys

from app.netguard import install_connect_guard
from tests.support.policy import allow_loopback


def main() -> None:
    from app.worker import limit_memory

    limit_memory()
    install_connect_guard(allow_loopback)
    ffmpeg_dir = os.environ.get("SAVEME_TEST_FFMPEG_DIR")
    if ffmpeg_dir:  # where the app/bin wrappers should find the real ffmpeg/ffprobe
        os.environ["SAVEME_FFMPEG_DIR"] = ffmpeg_dir
    from app import worker

    with worker.protocol_out() as out:
        code = worker.serve(sys.stdin, out)
    sys.exit(code)


if __name__ == "__main__":
    main()
