"""Shared logic for the ffmpeg/ffprobe wrappers (stdlib only).

Pins `-protocol_whitelist file,pipe` so hostile media (e.g. an HLS playlist
disguised as MP4) cannot make ffmpeg fetch network URLs.
"""
import os
import sys
from pathlib import Path
from typing import Literal

WHITELIST = ["-protocol_whitelist", "file,pipe"]


def inject_whitelist(argv: list[str], tool: Literal["ffmpeg", "ffprobe"]) -> list[str]:
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "-protocol_whitelist":
            i += 2
            continue
        cleaned.append(argv[i])
        i += 1
    if tool == "ffprobe":
        return [*WHITELIST, *cleaned]
    out: list[str] = []
    for a in cleaned:
        if a == "-i":
            out.extend(WHITELIST)
        out.append(a)
    return out


def find_real(name: str, wrapper_dir: Path, path_env: str) -> str:
    override = os.environ.get("SAVEME_FFMPEG_DIR")
    if override:
        return str(Path(override) / name)
    skip = os.path.realpath(wrapper_dir)
    for d in path_env.split(os.pathsep):
        if not d or os.path.realpath(d) == skip:
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    sys.stderr.write(f"saveme wrapper: real {name} not found on PATH\n")
    sys.exit(127)


def main(tool: Literal["ffmpeg", "ffprobe"], wrapper_dir: Path) -> None:
    real = find_real(tool, wrapper_dir, os.environ.get("PATH", ""))
    args = inject_whitelist(sys.argv[1:], tool)
    os.execv(real, [real, *args])
