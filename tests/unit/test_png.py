"""The logo PNG decoder (app.png): every colour type and filter decodes exactly as FFmpeg does, and
malformed, oversized, interlaced or decompression-bomb files are refused."""
import struct
import subprocess
import zlib

import numpy as np
import pytest

from app.png import SIGNATURE, PngError, decode
from tests.support.media import real_ffmpeg

FORMATS = [("rgba", "paeth"), ("rgb24", "sub"), ("gray", "avg"), ("ya8", "up"), ("pal8", "mixed"),
           ("monob", "none"), ("rgb48be", "paeth"), ("rgba64be", "avg"), ("gray16be", "sub")]


@pytest.fixture(scope="module")
def pngs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("png")
    graph = "format=rgba,geq=r=r(X\\,Y):g=g(X\\,Y):b=b(X\\,Y):a=if(gt(X\\,48)\\,255\\,mod(X*5\\,256))"
    out = {}
    for pix_fmt, pred in FORMATS:
        path = folder / f"{pix_fmt}.png"
        subprocess.run([real_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=97x61", "-vf", graph,
                        "-frames:v", "1", "-pix_fmt", pix_fmt, "-pred", pred, str(path)], check=True, timeout=60)
        out[pix_fmt] = path
    return out


def _reference(path) -> np.ndarray:
    raw = subprocess.run([real_ffmpeg(), "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                         capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(raw, np.uint8).astype(int)


@pytest.mark.parametrize("pix_fmt", [f for f, _p in FORMATS])
def test_every_format_decodes_like_ffmpeg(pngs, pix_fmt):
    width, height, rgba = decode(pngs[pix_fmt].read_bytes())
    ours = np.frombuffer(rgba, np.uint8).astype(int)
    assert (width, height) == (96, 60) and len(rgba) == 96 * 60 * 4
    tolerance = 1 if "16" in pix_fmt or "48" in pix_fmt or "64" in pix_fmt else 0  # 16-bit: rounding vs truncation
    assert int(np.abs(ours - _reference(pngs[pix_fmt])).max()) <= tolerance


def _png(width, height, color, depth, rows: bytes, *, interlace=0, extra=(), crc_ok=True) -> bytes:
    def chunk(kind, body):
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc if crc_ok else crc ^ 1)

    ihdr = struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace)
    return SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(chunk(k, b) for k, b in extra) + \
        chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def test_a_tiny_rgba_png():
    data = _png(2, 1, 6, 8, b"\x00" + bytes([255, 0, 0, 255, 0, 0, 255, 128]))
    assert decode(data) == (2, 1, bytes([255, 0, 0, 255, 0, 0, 255, 128]))


def test_palette_transparency():
    rows = b"\x00\x00\x01"
    data = _png(2, 1, 3, 8, rows, extra=[(b"PLTE", bytes([10, 20, 30, 40, 50, 60])), (b"tRNS", bytes([0]))])
    assert decode(data)[2] == bytes([10, 20, 30, 0, 40, 50, 60, 255])


@pytest.mark.parametrize("data,message", [
    (b"GIF89a....", "not a PNG"),
    (_png(1, 1, 6, 8, b"\x00\x00\x00\x00\x00", crc_ok=False), "CRC"),
    (_png(1, 1, 6, 8, b"\x00\x00\x00\x00\x00", interlace=1), "interlaced"),
    (_png(4096, 1, 6, 8, b"\x00" + bytes(4096 * 4)), "at most 2048"),
    (_png(1, 1, 5, 8, b"\x00\x00"), "unsupported"),
    (_png(1, 1, 2, 4, b"\x00\x00"), "unsupported"),  # 4-bit RGB doesn't exist
    (_png(1, 1, 6, 8, b"\x00\x00\x00"), "wrong size"),
    (_png(1, 1, 6, 8, b"\x07\x00\x00\x00\x00"), "filter"),
    (_png(2, 1, 3, 8, b"\x00\x00\x05", extra=[(b"PLTE", bytes(6))]), "palette index"),
    (_png(1, 1, 3, 8, b"\x00\x00"), "missing palette"),
    (SIGNATURE + b"\x00\x00\x00\x0dIHDR", "truncated|no IEND"),
])
def test_malformed_pngs_are_refused(data, message):
    with pytest.raises(PngError, match=message):
        decode(data)


def test_a_decompression_bomb_is_refused_without_inflating_it():
    # 1×1 RGBA needs 5 bytes; this IDAT would inflate to 50 MB.
    data = _png(1, 1, 6, 8, bytes(50 * 1024 * 1024))
    with pytest.raises(PngError, match="wrong size"):
        decode(data)
