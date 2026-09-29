"""C-style path quoting as used by git (core.quotePath)."""

from __future__ import annotations

from .errors import PatchError

_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("f"): 12,
    ord("n"): 10,
    ord("r"): 13,
    ord("t"): 9,
    ord("v"): 11,
    ord("\\"): 92,
    ord('"'): 34,
}
_OUT_ESCAPES = {7: "a", 8: "b", 12: "f", 10: "n", 13: "r", 9: "t", 11: "v", 92: "\\", 34: '"'}


def unquote(data: bytes, start: int, line: int) -> tuple[bytes, int]:
    """Decode the quoted string that begins at ``data[start] == '"'``.

    Returns the decoded bytes and the index just past the closing quote.
    """
    out = bytearray()
    i = start + 1
    n = len(data)
    while i < n:
        c = data[i]
        if c == 0x22:
            return bytes(out), i + 1
        if c != 0x5C:
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = data[i]
        if e in _ESCAPES:
            out.append(_ESCAPES[e])
            i += 1
        elif 0x30 <= e <= 0x33:
            digits = data[i : i + 3]
            if len(digits) != 3 or any(not 0x30 <= d <= 0x37 for d in digits):
                raise PatchError("invalid octal escape in quoted path", line)
            out.append(int(digits, 8))
            i += 3
        else:
            raise PatchError("invalid escape in quoted path", line)
    raise PatchError("unterminated quoted path", line)


def quote(name: bytes, *, quote_high: bool = True) -> str:
    """Render ``name`` the way git prints a path in non-``-z`` output."""
    needs = any(
        b < 0x20 or b == 0x7F or b in (0x22, 0x5C) or (quote_high and b >= 0x80) for b in name
    )
    if not needs:
        return name.decode("utf-8", "surrogateescape")
    parts = ['"']
    for b in name:
        if b in _OUT_ESCAPES:
            parts.append("\\" + _OUT_ESCAPES[b])
        elif b < 0x20 or b == 0x7F or (quote_high and b >= 0x80):
            parts.append("\\%03o" % b)
        else:
            parts.append(chr(b))
    parts.append('"')
    return "".join(parts)
