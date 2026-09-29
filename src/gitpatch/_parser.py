"""The patch parser."""

from __future__ import annotations

import re
from email.header import decode_header, make_header

from ._binary import BinaryPatch, parse_block
from ._quote import unquote
from .errors import PatchError
from .model import CommitInfo, FileChange, Hunk, HunkLine, Patch, Status

_HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")
_INDEX_RE = re.compile(rb"^index ([0-9a-f]+)\.\.([0-9a-f]+)(?: (\d+))?$")
_MAIL_RE = re.compile(rb"^From ([0-9a-f]{40,64}) ")
_SHOW_RE = re.compile(rb"^commit ([0-9a-f]{40,64})(?: |$)")
_MODE_RE = re.compile(rb"^[0-7]{1,7}$")
_SIM_RE = re.compile(rb"^(\d+)%?$")
_ZERO_RE = re.compile(r"^0+$")

_GIT_HDR = b"diff --git "
_TYPE_MASK = 0o170000


def _dec(b: bytes) -> str:
    return b.decode("utf-8", "surrogateescape")


def _hash(h: bytes) -> str | None:
    s = h.decode("ascii")
    return None if _ZERO_RE.match(s) else s


def _parse_name(data: bytes, line: int) -> bytes:
    """A path as written on ``rename from`` / ``--- `` lines (quoted or bare)."""
    if data.startswith(b'"'):
        name, _ = unquote(data, 0, line)
        return name
    return data


def _diff_name(data: bytes, line: int) -> bytes:
    """A path from a ``---`` / ``+++`` line: quoted, or bare up to the first tab."""
    if data.startswith(b'"'):
        name, _ = unquote(data, 0, line)
        return name
    tab = data.find(b"\t")
    return data if tab < 0 else data[:tab]


def _components_after(name: bytes, k: int) -> bytes | None:
    parts = name.split(b"/", k)
    return parts[k] if len(parts) > k else None


def _split_header_names(rest: bytes, line: int) -> tuple[bytes, bytes] | None:
    """Split the two names of ``diff --git A B`` (each may carry a prefix)."""
    if rest.startswith(b'"'):
        first, end = unquote(rest, 0, line)
        if end >= len(rest) or rest[end] != 0x20:
            return None
        tail = rest[end + 1 :]
        if tail.startswith(b'"'):
            second, tend = unquote(tail, 0, line)
            return (first, second) if tend == len(tail) else None
        return first, tail
    if rest.endswith(b'"'):
        q = rest.find(b' "')
        while q >= 0:
            try:
                second, tend = unquote(rest, q + 1, line)
            except PatchError:
                second, tend = b"", -1
            if tend == len(rest):
                return rest[:q], second
            q = rest.find(b' "', q + 1)
        return None
    n = len(rest)
    mid = n // 2
    if n % 2 == 1 and rest[mid] == 0x20:
        left, right = rest[:mid], rest[mid + 1 :]
        if left == right or _same_modulo_prefix(left, right):
            return left, right
    best: tuple[int, bytes, bytes] | None = None
    p = rest.find(b" ")
    while p >= 0:
        left, right = rest[:p], rest[p + 1 :]
        if left and right and (left == right or _same_modulo_prefix(left, right)):
            dist = abs(p - mid)
            if best is None or dist < best[0]:
                best = (dist, left, right)
        p = rest.find(b" ", p + 1)
    if best is not None:
        return best[1], best[2]
    return None


def _same_modulo_prefix(left: bytes, right: bytes) -> bool:
    a = _components_after(left, 1)
    b = _components_after(right, 1)
    return a is not None and a == b


def _prefix_len(left: bytes, right: bytes) -> tuple[int, int]:
    """Prefix lengths of the two names of a header (0, 0 when they are equal)."""
    if left == right:
        return 0, 0
    a = _components_after(left, 1)
    if a is None:
        return 0, 0
    return len(left) - len(a), len(right) - len(a)


def _strip(name: bytes, n: int) -> bytes:
    return name[n:] if n and len(name) > n else name


class _Parser:
    def __init__(
        self,
        data: bytes,
        strip: int,
        src_prefix: str | None,
        dst_prefix: str | None,
        merge_type_changes: bool,
    ) -> None:
        lines = data.split(b"\n")
        if lines and lines[-1] == b"":
            lines.pop()
        self.lines = lines
        self.strip = strip
        self.src_prefix = None if src_prefix is None else src_prefix.encode("utf-8", "surrogateescape")
        self.dst_prefix = None if dst_prefix is None else dst_prefix.encode("utf-8", "surrogateescape")
        self.merge = merge_type_changes
        self.patch = Patch()
        self.current_commit: int | None = None

    # -- top level -------------------------------------------------------

    def run(self) -> Patch:
        lines = self.lines
        n = len(lines)
        i = 0
        while i < n:
            ln = lines[i]
            if ln.startswith(_GIT_HDR):
                i = self._git_file(i)
                self.patch.files[-1].end_line = i
            elif ln.startswith((b"diff --cc ", b"diff --combined ")):
                i = self._combined(i)
                self.patch.files[-1].end_line = i
            elif ln.startswith(b"--- ") and i + 2 < n and lines[i + 1].startswith(b"+++ ") and lines[i + 2].startswith(b"@@ "):
                i = self._traditional(i)
                self.patch.files[-1].end_line = i
            elif _MAIL_RE.match(ln):
                i = self._mail(i)
            elif _SHOW_RE.match(ln) and self._looks_like_show(i):
                i = self._show(i)
            else:
                i += 1
        if self.merge:
            self._merge_type_changes()
        return self.patch

    def _add(self, fc: FileChange) -> None:
        fc.commit = self.current_commit
        self.patch.files.append(fc)

    # -- commit metadata -------------------------------------------------

    def _mail(self, i: int) -> int:
        lines = self.lines
        m = _MAIL_RE.match(lines[i])
        assert m is not None
        sha = m.group(1).decode("ascii")
        start = i
        i += 1
        headers: dict[str, str] = {}
        last: str | None = None
        n = len(lines)
        while i < n and lines[i].strip(b"\r") != b"":
            ln = lines[i]
            if ln[:1] in (b" ", b"\t") and last is not None:
                headers[last] += _dec(ln)  # RFC 5322 unfolding keeps the folding whitespace
            else:
                k, sep, v = ln.partition(b":")
                if not sep:
                    break
                last = _dec(k).strip().title()
                headers[last] = _dec(v).lstrip()
            i += 1
        headers = {k: v.strip() for k, v in headers.items()}
        if i < n and lines[i].strip(b"\r") == b"":
            i += 1
        body: list[str] = []
        while i < n:
            ln = lines[i]
            if ln.rstrip() == b"---" or ln.startswith((_GIT_HDR, b"diff --cc ", b"diff --combined ")):
                break
            if ln.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
                break
            if _MAIL_RE.match(ln):
                break
            body.append(_dec(ln))
            i += 1
        subject = _decode_words(headers.get("Subject", ""))
        info = CommitInfo(
            kind="mbox",
            sha=sha,
            author=_address(headers["From"]) if "From" in headers else None,
            date=headers.get("Date"),
            subject=subject,
            message="\n".join(body).strip("\n"),
            line=start + 1,
            headers=headers,
        )
        self.patch.commits.append(info)
        self.current_commit = len(self.patch.commits) - 1
        return i

    def _looks_like_show(self, i: int) -> bool:
        # 'commit <sha>' followed (within the header block) by an Author: line
        for j in range(i + 1, min(i + 4, len(self.lines))):
            if self.lines[j].startswith(b"Author:"):
                return True
        return False

    def _show(self, i: int) -> int:
        lines = self.lines
        n = len(lines)
        m = _SHOW_RE.match(lines[i])
        assert m is not None
        start = i
        sha = m.group(1).decode("ascii")
        i += 1
        headers: dict[str, str] = {}
        while i < n and lines[i] != b"":
            k, _, v = lines[i].partition(b":")
            headers[_dec(k).strip().title()] = _dec(v).strip()
            i += 1
        i += 1
        msg: list[str] = []
        while i < n and (lines[i].startswith(b"    ") or lines[i] == b""):
            msg.append(_dec(lines[i][4:]))
            i += 1
        while msg and msg[-1] == "":
            msg.pop()
        info = CommitInfo(
            kind="show",
            sha=sha,
            author=headers.get("Author"),
            date=headers.get("Date"),
            subject=msg[0] if msg else "",
            message="\n".join(msg),
            line=start + 1,
            headers=headers,
        )
        self.patch.commits.append(info)
        self.current_commit = len(self.patch.commits) - 1
        return i

    # -- file entries ----------------------------------------------------

    def _combined(self, i: int) -> int:
        lines = self.lines
        ln = lines[i]
        head = ln.split(b" ", 2)
        path = _parse_name(head[2], i + 1) if len(head) > 2 else b""
        fc = FileChange(
            old_path=_dec(path),
            new_path=_dec(path),
            is_combined=True,
            line=i + 1,
        )
        i += 1
        n = len(lines)
        while i < n:
            nxt = lines[i]
            if nxt.startswith(b"diff ") or _MAIL_RE.match(nxt):
                break
            i += 1
        self._add(fc)
        return i

    def _traditional(self, i: int) -> int:
        lines = self.lines
        start = i
        old = _diff_name(lines[i][4:], i + 1)
        new = _diff_name(lines[i + 1][4:], i + 2)
        fc = FileChange(old_path=None, new_path=None, line=start + 1)
        if old == b"/dev/null":
            fc.status = "added"
            fc.new_path = _dec(_strip_components(new, self.strip))
        elif new == b"/dev/null":
            fc.status = "deleted"
            fc.old_path = _dec(_strip_components(old, self.strip))
        else:
            fc.old_path = _dec(_strip_components(old, self.strip))
            fc.new_path = _dec(_strip_components(new, self.strip))
        i = self._hunks(i + 2, fc)
        self._add(fc)
        return i

    def _git_file(self, i: int) -> int:
        lines = self.lines
        n = len(lines)
        start = i
        rest = lines[i][len(_GIT_HDR) :]
        header = _split_header_names(rest, i + 1)
        i += 1
        fc = FileChange(old_path=None, new_path=None, line=start + 1)
        rename_from: bytes | None = None
        rename_to: bytes | None = None
        copy_from: bytes | None = None
        copy_to: bytes | None = None
        new_file = deleted_file = False
        old_mode: int | None = None
        new_mode: int | None = None
        index_mode: int | None = None

        def mode(text: bytes, at: int) -> int:
            if not _MODE_RE.match(text):
                raise PatchError("invalid file mode", at + 1)
            return int(text, 8)

        while i < n:
            ln = lines[i]
            if ln.startswith(b"old mode "):
                old_mode = mode(ln[9:], i)
            elif ln.startswith(b"new mode "):
                new_mode = mode(ln[9:], i)
            elif ln.startswith(b"deleted file mode "):
                deleted_file = True
                old_mode = mode(ln[18:], i)
            elif ln.startswith(b"new file mode "):
                new_file = True
                new_mode = mode(ln[14:], i)
            elif ln.startswith(b"rename from "):
                rename_from = _parse_name(ln[12:], i + 1)
            elif ln.startswith(b"rename to "):
                rename_to = _parse_name(ln[10:], i + 1)
            elif ln.startswith(b"copy from "):
                copy_from = _parse_name(ln[10:], i + 1)
            elif ln.startswith(b"copy to "):
                copy_to = _parse_name(ln[8:], i + 1)
            elif ln.startswith(b"similarity index "):
                m = _SIM_RE.match(ln[17:])
                if not m:
                    raise PatchError("invalid similarity index", i + 1)
                fc.similarity = int(m.group(1))
            elif ln.startswith(b"dissimilarity index "):
                m = _SIM_RE.match(ln[20:])
                if not m:
                    raise PatchError("invalid dissimilarity index", i + 1)
                fc.dissimilarity = int(m.group(1))
            elif ln.startswith(b"index "):
                m2 = _INDEX_RE.match(ln)
                if not m2:
                    raise PatchError("invalid index line", i + 1)
                fc.old_hash = _hash(m2.group(1))
                fc.new_hash = _hash(m2.group(2))
                if m2.group(3):
                    index_mode = mode(m2.group(3), i)
            else:
                break
            i += 1

        fc.old_mode = old_mode if old_mode is not None else index_mode
        fc.new_mode = new_mode if new_mode is not None else index_mode
        if new_file:
            fc.old_mode = None
        if deleted_file:
            fc.new_mode = None

        # body
        old_name: bytes | None = None
        new_name: bytes | None = None
        if i < n and lines[i].startswith(b"--- "):
            if i + 1 >= n or not lines[i + 1].startswith(b"+++ "):
                raise PatchError("expected '+++' line after '---' line", i + 2)
            old_name = _diff_name(lines[i][4:], i + 1)
            new_name = _diff_name(lines[i + 1][4:], i + 2)
            i = self._hunks(i + 2, fc)
        elif i < n and lines[i].startswith(b"Binary files "):
            fc.is_binary = True
            i += 1
        elif i < n and lines[i] == b"GIT binary patch":
            fc.is_binary = True
            i = self._binary(i + 1, fc)

        # status and names
        if rename_from is not None or rename_to is not None:
            if rename_from is None or rename_to is None:
                raise PatchError("incomplete rename header", start + 1)
            fc.status = "renamed"
            fc.old_path, fc.new_path = _dec(rename_from), _dec(rename_to)
        elif copy_from is not None or copy_to is not None:
            if copy_from is None or copy_to is None:
                raise PatchError("incomplete copy header", start + 1)
            fc.status = "copied"
            fc.old_path, fc.new_path = _dec(copy_from), _dec(copy_to)
        else:
            self._plain_names(fc, header, old_name, new_name, new_file, deleted_file, start)
        if fc.status == "modified" and fc.old_mode is not None and fc.new_mode is not None:
            if (fc.old_mode & _TYPE_MASK) != (fc.new_mode & _TYPE_MASK):
                fc.status = "type-changed"
        self._add(fc)
        return i

    def _plain_names(
        self,
        fc: FileChange,
        header: tuple[bytes, bytes] | None,
        old_name: bytes | None,
        new_name: bytes | None,
        new_file: bool,
        deleted_file: bool,
        start: int,
    ) -> None:
        ps, pd = self._prefixes(header)
        old: bytes | None
        new: bytes | None
        if old_name is not None and new_name is not None:
            old = None if old_name == b"/dev/null" else _strip(old_name, ps)
            new = None if new_name == b"/dev/null" else _strip(new_name, pd)
            if old is None and new is not None:
                new_file = True
            if new is None and old is not None:
                deleted_file = True
            if old is None and new is None:
                raise PatchError("both sides of the patch are /dev/null", start + 1)
            if old is not None and new is not None and header is not None and old != new:
                # names disagree with each other: trust the ---/+++ lines
                pass
        else:
            if header is None:
                raise PatchError("cannot determine file names from 'diff --git' line", start + 1)
            old = _strip(header[0], ps)
            new = _strip(header[1], pd)
        if new_file:
            fc.status = "added"
            fc.old_path = None
            fc.new_path = _dec(new if new is not None else old or b"")
        elif deleted_file:
            fc.status = "deleted"
            fc.old_path = _dec(old if old is not None else new or b"")
            fc.new_path = None
        else:
            fc.status = "modified"
            fc.old_path = _dec(old if old is not None else new or b"")
            fc.new_path = _dec(new if new is not None else old or b"")

    def _prefixes(self, header: tuple[bytes, bytes] | None) -> tuple[int, int]:
        if self.src_prefix is not None or self.dst_prefix is not None:
            return len(self.src_prefix or b""), len(self.dst_prefix or b"")
        if header is None:
            return 0, 0
        return _prefix_len(*header)

    # -- hunks and binary ------------------------------------------------

    def _binary(self, i: int, fc: FileChange) -> int:
        lines = self.lines
        if i >= len(lines):
            raise PatchError("truncated binary patch", i)
        fwd, i = parse_block(lines, i)
        rev = None
        if i < len(lines) and (lines[i].startswith((b"literal ", b"delta "))):
            rev, i = parse_block(lines, i)
        fc.binary = BinaryPatch(fwd, rev)
        return i

    def _hunks(self, i: int, fc: FileChange) -> int:
        lines = self.lines
        n = len(lines)
        while i < n and lines[i].startswith(b"@@ "):
            m = _HUNK_RE.match(lines[i])
            if not m:
                raise PatchError("malformed hunk header", i + 1)
            old_start = int(m.group(1))
            old_count = 1 if m.group(2) is None else int(m.group(2))
            new_start = int(m.group(3))
            new_count = 1 if m.group(4) is None else int(m.group(4))
            hunk = Hunk(old_start, old_count, new_start, new_count, _dec(m.group(5) or b""))
            hdr_line = i + 1
            i += 1
            old_left, new_left = old_count, new_count
            old_no, new_no = old_start, new_start
            while old_left > 0 or new_left > 0:
                if i >= n:
                    raise PatchError("hunk is truncated", n if n else 1)
                ln = lines[i]
                tag = ln[:1]
                if tag == b" " or ln == b"":
                    if old_left == 0 or new_left == 0:
                        raise PatchError("hunk has more lines than its header declares", i + 1)
                    hunk.lines.append(HunkLine("context", _dec(ln[1:]), old_no, new_no))
                    old_left -= 1
                    new_left -= 1
                    old_no += 1
                    new_no += 1
                elif tag == b"-":
                    if old_left == 0:
                        raise PatchError("hunk has more removed lines than its header declares", i + 1)
                    hunk.lines.append(HunkLine("remove", _dec(ln[1:]), old_no, None))
                    old_left -= 1
                    old_no += 1
                elif tag == b"+":
                    if new_left == 0:
                        raise PatchError("hunk has more added lines than its header declares", i + 1)
                    hunk.lines.append(HunkLine("add", _dec(ln[1:]), None, new_no))
                    new_left -= 1
                    new_no += 1
                elif tag == b"\\":
                    if not hunk.lines:
                        raise PatchError("'\\' marker before any hunk line", i + 1)
                    hunk.lines[-1].no_newline = True
                else:
                    raise PatchError(
                        f"unexpected line in hunk (declared at line {hdr_line})", i + 1
                    )
                i += 1
            while i < n and lines[i].startswith(b"\\"):
                if not hunk.lines:
                    raise PatchError("'\\' marker before any hunk line", i + 1)
                hunk.lines[-1].no_newline = True
                i += 1
            fc.hunks.append(hunk)
        return i

    # -- post processing -------------------------------------------------

    def _merge_type_changes(self) -> None:
        files = self.patch.files
        out: list[FileChange] = []
        for f in files:
            prev = out[-1] if out else None
            if (
                prev is not None
                and prev.status == "deleted"
                and f.status == "added"
                and prev.old_path == f.new_path
                and not prev.is_combined
                and not f.is_combined
            ):
                out[-1] = FileChange(
                    old_path=prev.old_path,
                    new_path=f.new_path,
                    status="type-changed",
                    old_mode=prev.old_mode,
                    new_mode=f.new_mode,
                    old_hash=prev.old_hash,
                    new_hash=f.new_hash,
                    is_binary=prev.is_binary or f.is_binary,
                    hunks=prev.hunks + f.hunks,
                    commit=prev.commit,
                    line=prev.line,
                    parts=(prev, f),
                )
            else:
                out.append(f)
        self.patch.files = out


def _strip_components(name: bytes, n: int) -> bytes:
    parts = name.split(b"/")
    if n <= 0:
        return name
    return b"/".join(parts[n:]) if len(parts) > n else parts[-1]


_ADDR_RE = re.compile(r"^(?P<name>.*?)\s*<(?P<addr>[^<>]*)>\s*$", re.S)


def _address(value: str) -> str:
    """``Name <addr>`` from a From header: encoded words decoded, quoting removed."""
    m = _ADDR_RE.match(value)
    if not m:
        return _decode_words(value).strip()
    name = m.group("name").strip()
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = re.sub(r"\\(.)", r"\1", name[1:-1])
    name = _decode_words(name)
    addr = m.group("addr").strip()
    return f"{name} <{addr}>" if name else addr


def _decode_words(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed encoded words fall back to the raw text
        return value


def parse(
    data: str | bytes,
    *,
    strip: int = 1,
    src_prefix: str | None = None,
    dst_prefix: str | None = None,
    merge_type_changes: bool = True,
) -> Patch:
    """Parse git-produced patch text.

    ``strip`` applies only to plain unified diffs without a ``diff --git``
    header. ``src_prefix`` / ``dst_prefix`` override prefix auto-detection for
    ``diff --git`` entries. With ``merge_type_changes`` a deletion immediately
    followed by a creation of the same path (how git prints a type change)
    becomes one ``type-changed`` entry.
    """
    raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else bytes(data)
    return _Parser(raw, strip, src_prefix, dst_prefix, merge_type_changes).run()
