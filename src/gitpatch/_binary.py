"""``GIT binary patch`` payloads: base85 lines, zlib, and the pack delta format."""

from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from typing import Literal

from .errors import ApplyError, PatchError


@dataclass(frozen=True, slots=True)
class BinaryData:
    """One direction of a binary patch.

    ``kind == "literal"``: ``data`` is the complete new content.
    ``kind == "delta"``: ``data`` is a pack-format delta against the old content.
    ``size`` is the size declared in the patch (of the resulting content for a
    literal, of the delta for a delta).
    """

    kind: Literal["literal", "delta"]
    size: int
    data: bytes

    def apply(self, base: bytes | None) -> bytes:
        """Return the content this payload produces from ``base``."""
        if self.kind == "literal":
            return self.data
        if base is None:
            raise ApplyError("a binary delta needs the old content")
        return apply_delta(base, self.data)


@dataclass(frozen=True, slots=True)
class BinaryPatch:
    """The forward (old to new) and reverse (new to old) payloads."""

    forward: BinaryData
    reverse: BinaryData | None


def _varint(delta: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(delta):
            raise ApplyError("truncated delta header")
        b = delta[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a git pack-format delta to ``base``."""
    src_size, pos = _varint(delta, 0)
    dst_size, pos = _varint(delta, pos)
    if src_size != len(base):
        raise ApplyError(f"delta expects a {src_size}-byte base, got {len(base)} bytes")
    out = bytearray()
    n = len(delta)
    while pos < n:
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if cmd & (1 << bit):
                    if pos >= n:
                        raise ApplyError("truncated delta copy")
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if cmd & (0x10 << bit):
                    if pos >= n:
                        raise ApplyError("truncated delta copy")
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(base):
                raise ApplyError("delta copy is out of range")
            out += base[offset : offset + size]
        elif cmd:
            if pos + cmd > n:
                raise ApplyError("truncated delta insert")
            out += delta[pos : pos + cmd]
            pos += cmd
        else:
            raise ApplyError("reserved delta opcode 0")
    if len(out) != dst_size:
        raise ApplyError("delta produced the wrong size")
    return bytes(out)


def _line_length(ch: int) -> int:
    if 0x41 <= ch <= 0x5A:
        return ch - 0x41 + 1
    if 0x61 <= ch <= 0x7A:
        return ch - 0x61 + 27
    return -1


def parse_block(lines: list[bytes], i: int) -> tuple[BinaryData, int]:
    """Parse ``literal N`` / ``delta N`` and its base85 lines starting at index ``i``.

    Returns the payload and the index of the first line after the block
    (a blank separator line, if any, is consumed).
    """
    n = len(lines)
    header = lines[i]
    if header.startswith(b"literal "):
        kind: Literal["literal", "delta"] = "literal"
        size_text = header[8:]
    elif header.startswith(b"delta "):
        kind = "delta"
        size_text = header[6:]
    else:
        raise PatchError("expected 'literal' or 'delta' in binary patch", i + 1)
    if not size_text.isdigit():
        raise PatchError("invalid size in binary patch", i + 1)
    size = int(size_text)
    start = i
    i += 1
    chunks: list[bytes] = []
    while i < n and lines[i]:
        raw = lines[i]
        want = _line_length(raw[0])
        body = raw[1:]
        if want < 0 or len(body) != 5 * ((want + 3) // 4):
            raise PatchError("malformed base85 line in binary patch", i + 1)
        try:
            decoded = base64.b85decode(body)
        except ValueError as exc:
            raise PatchError(f"invalid base85 data in binary patch ({exc})", i + 1) from None
        chunks.append(decoded[:want])
        i += 1
    if i < n:
        i += 1  # blank separator
    try:
        inflater = zlib.decompressobj()
        data = inflater.decompress(b"".join(chunks), size + 1)
        if not inflater.eof and len(data) <= size:
            raise zlib.error("incomplete stream")
    except zlib.error as exc:
        raise PatchError(f"invalid zlib data in binary patch ({exc})", start + 1) from None
    if len(data) != size:
        raise PatchError(f"binary patch declares {size} bytes but inflates to a different size", start + 1)
    return BinaryData(kind, size, data), i
