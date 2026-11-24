"""`python -m app.whisper_models [--list] [MODEL ...]`: install faster-whisper models.

The transcriber never downloads anything (it can't open a network connection); it loads models
from the local Hugging Face cache (`HF_HOME`, by default ~/.cache/huggingface). This command
fills that cache. With no MODEL it installs WHISPER_MODEL, or `base`. The Docker image runs it
at build time (build argument WHISPER_MODELS).
"""
import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence

from app.transcript import WHISPER_MODELS

DEFAULT_MODEL = "base"


def _hub_download(name: str, local_files_only: bool = False) -> str:
    from faster_whisper.utils import download_model

    return download_model(name, local_files_only=local_files_only)


def _installed(name: str, download: Callable[..., str]) -> bool:
    try:
        download(name, local_files_only=True)
        return True
    except Exception:
        return False


def main(argv: Sequence[str] | None = None, *, download: Callable[..., str] = _hub_download,
         env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.whisper_models",
                                     description="Install faster-whisper models for AI Highlights.")
    parser.add_argument("models", nargs="*", metavar="MODEL",
                        help=f"model names (default: WHISPER_MODEL or {DEFAULT_MODEL})")
    parser.add_argument("--list", action="store_true", help="list the models and whether each is installed")
    args = parser.parse_args(argv)
    if args.list:
        for name in WHISPER_MODELS:
            print(f"{name}  installed" if _installed(name, download) else name)
        return 0
    names = args.models or [(env.get("WHISPER_MODEL") or "").strip() or DEFAULT_MODEL]
    unknown = [name for name in names if name not in WHISPER_MODELS]
    if unknown:
        print(f"Unknown model: {', '.join(unknown)}. Choose from: {', '.join(WHISPER_MODELS)}",
              file=sys.stderr)
        return 2
    for name in names:
        try:
            path = download(name)
        except Exception as exc:
            print(f"Couldn't install {name}: {exc}", file=sys.stderr)
            return 1
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
