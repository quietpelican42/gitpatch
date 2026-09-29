"""Applying text hunks and binary payloads to file content."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import ApplyError

if TYPE_CHECKING:
    from .model import FileChange


def _split(content: bytes) -> list[bytes]:
    parts = content.split(b"\n")
    out = [p + b"\n" for p in parts[:-1]]
    if parts[-1]:
        out.append(parts[-1])
    return out


def apply_file(fc: FileChange, content: bytes | None) -> bytes | None:
    if fc.is_combined:
        raise ApplyError("combined diffs cannot be applied")
    if fc.parts:
        result = content
        for part in fc.parts:
            result = apply_file(part, result)
        return result
    if fc.is_binary:
        if fc.binary is None:
            raise ApplyError("the patch does not carry the binary content")
        if fc.status == "deleted":
            return None
        return fc.binary.forward.apply(content)
    if fc.status == "added" and content is not None:
        raise ApplyError(f"{fc.path}: already exists")
    if fc.status != "added" and content is None:
        raise ApplyError(f"{fc.path}: no content to patch")
    lines = _split(content or b"")
    out: list[bytes] = []
    pos = 0
    for hunk in fc.hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if start < pos or start > len(lines):
            raise ApplyError(f"{fc.path}: hunk at line {hunk.old_start} is out of order or range")
        out.extend(lines[pos:start])
        pos = start
        for ln in hunk.lines:
            if ln.kind == "add":
                out.append(ln.raw)
                continue
            if pos >= len(lines) or lines[pos] != ln.raw:
                raise ApplyError(f"{fc.path}: hunk at line {hunk.old_start} does not match")
            pos += 1
            if ln.kind == "context":
                out.append(ln.raw)
    out.extend(lines[pos:])
    if fc.status == "deleted":
        if out:
            raise ApplyError(f"{fc.path}: deletion patch leaves content behind")
        return None
    return b"".join(out)
