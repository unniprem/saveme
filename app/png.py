"""A small, strict PNG decoder for Auto Shorts logos (docs/AUTO_SHORTS_DESIGN.md §3.2).

FFmpeg in SaveMe never parses an image (cover art is embedded by mutagen; the image's FFmpeg build has no
image decoders). A logo is therefore decoded here, in Python, inside the renderer worker (memory-capped,
no network), into raw 8-bit RGBA that FFmpeg reads as `rawvideo`.

Accepted: non-interlaced PNGs, every standard colour type (grey, RGB, palette, grey+alpha, RGBA) at
their bit depths (16-bit samples keep their high byte), with tRNS transparency, at most MAX_SIDE pixels a
side. Chunk CRCs are checked, the decompressed size is bounded before inflating, and anything malformed
raises PngError.
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SIDE = 2048
MAX_CHUNKS = 10_000
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}


class PngError(ValueError):
    pass


def _chunks(data: bytes):
    if not data.startswith(SIGNATURE):
        raise PngError("not a PNG")
    pos, count = len(SIGNATURE), 0
    while pos + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if length > len(data) or end > len(data):
            raise PngError("truncated chunk")
        body = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:end])
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise PngError("bad chunk CRC")
        yield kind, body
        count += 1
        if kind == b"IEND" or count > MAX_CHUNKS:
            return
        pos = end
    raise PngError("no IEND chunk")


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> np.ndarray:
    """Undo the per-row filters (None, Sub, Up, Average, Paeth). Returns height×stride uint8."""
    out = np.zeros((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.int32)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        row = np.frombuffer(raw, dtype=np.uint8, count=stride, offset=start + 1).astype(np.int32)
        if kind == 0:
            cur = row
        elif kind == 1:  # Sub: a running sum per byte position modulo the pixel size
            cur = (np.cumsum(np.pad(row, (0, (-stride) % bpp)).reshape(-1, bpp), axis=0).reshape(-1)[:stride]) & 0xFF
        elif kind == 2:  # Up
            cur = (row + prev) & 0xFF
        elif kind in (3, 4):
            cur_list = row.tolist()
            up = prev.tolist()
            for i in range(stride):
                left = cur_list[i - bpp] if i >= bpp else 0
                if kind == 3:
                    cur_list[i] = (cur_list[i] + ((left + up[i]) >> 1)) & 0xFF
                else:
                    upleft = up[i - bpp] if i >= bpp else 0
                    p = left + up[i] - upleft
                    pa, pb, pc = abs(p - left), abs(p - up[i]), abs(p - upleft)
                    pred = left if pa <= pb and pa <= pc else up[i] if pb <= pc else upleft
                    cur_list[i] = (cur_list[i] + pred) & 0xFF
            cur = np.array(cur_list, dtype=np.int32)
        else:
            raise PngError("unknown row filter")
        out[y] = cur
        prev = cur
    return out


def decode(data: bytes, *, max_side: int = MAX_SIDE) -> tuple[int, int, bytes]:
    """(width, height, RGBA bytes) of a PNG."""
    header = palette = transparency = None
    idat = []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            if header is not None or len(body) != 13:
                raise PngError("bad IHDR")
            header = struct.unpack(">IIBBBBB", body)
        elif header is None:
            raise PngError("IHDR must come first")
        elif kind == b"PLTE":
            if len(body) % 3 or not 3 <= len(body) <= 768:
                raise PngError("bad palette")
            palette = np.frombuffer(body, dtype=np.uint8).reshape(-1, 3)
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            idat.append(body)
    if header is None or not idat:
        raise PngError("no image data")
    width, height, depth, color, compression, filtering, interlace = header
    if not (1 <= width <= max_side and 1 <= height <= max_side):
        raise PngError(f"the image must be at most {max_side} pixels a side")
    if color not in _CHANNELS or depth not in _DEPTHS[color] or compression or filtering:
        raise PngError("unsupported PNG format")
    if interlace:
        raise PngError("interlaced PNGs aren't supported; save it without interlacing")
    if color == 3 and palette is None:
        raise PngError("missing palette")
    channels = _CHANNELS[color]
    bits = depth * channels
    stride = (width * bits + 7) // 8
    bpp = max(1, bits // 8)
    expected = height * (stride + 1)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(idat), expected + 1)
    except zlib.error as exc:
        raise PngError("corrupt image data") from exc
    if len(raw) != expected:
        raise PngError("image data has the wrong size")
    rows = _unfilter(raw, height, stride, bpp)
    if depth == 16:
        samples = rows.reshape(height, width * channels, 2)[:, :, 0]
    elif depth < 8:
        bits_per_row = np.unpackbits(rows, axis=1)[:, :width * depth]
        groups = bits_per_row.reshape(height, width, depth)
        weights = (1 << np.arange(depth - 1, -1, -1)).astype(np.uint16)
        samples = (groups * weights).sum(axis=2).astype(np.uint8)
    else:
        samples = rows
    samples = samples.reshape(height, width, channels if color != 3 or depth == 8 else 1)
    rgba = np.empty((height, width, 4), dtype=np.uint8)
    if color == 3:
        index = samples.reshape(height, width)
        if int(index.max()) >= len(palette):
            raise PngError("palette index out of range")
        rgba[:, :, :3] = palette[index]
        alpha = np.full(256, 255, dtype=np.uint8)
        if transparency:
            alpha[:min(len(transparency), 256)] = np.frombuffer(transparency[:256], dtype=np.uint8)
        rgba[:, :, 3] = alpha[index]
    else:
        if color in (0, 4) and depth < 8:  # scale 1/2/4-bit grey to 0–255
            samples = (samples.astype(np.uint16) * 255 // ((1 << depth) - 1)).astype(np.uint8)
        grey = color in (0, 4)
        rgba[:, :, :3] = samples[:, :, :1] if grey else samples[:, :, :3]
        rgba[:, :, 3] = samples[:, :, -1] if color in (4, 6) else 255
        if transparency and color in (0, 2):  # one colour is fully transparent
            key = struct.unpack(">" + "H" * (len(transparency) // 2), transparency)
            if depth == 16:
                key = tuple(v >> 8 for v in key)
            elif depth < 8:
                key = tuple(v * 255 // ((1 << depth) - 1) for v in key)
            match = np.all(rgba[:, :, :3] == np.array(key[:1] * 3 if color == 0 else key[:3], dtype=np.uint8),
                           axis=2)
            rgba[match, 3] = 0
    return width, height, rgba.tobytes()
