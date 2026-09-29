"""Data model and the git-format summaries (``raw`` / ``numstat``)."""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Literal

from ._binary import BinaryPatch
from ._quote import quote
from .errors import ApplyError

Status = Literal["added", "deleted", "modified", "renamed", "copied", "type-changed"]

_STATUS_LETTER = {
    "added": "A",
    "deleted": "D",
    "modified": "M",
    "renamed": "R",
    "copied": "C",
    "type-changed": "T",
}
_ZERO_MODE = "000000"


def _b(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


@dataclass(slots=True)
class HunkLine:
    """One line of a hunk body.

    ``content`` excludes the leading marker and the trailing newline.
    ``no_newline`` is true when the line was followed by
    ``\\ No newline at end of file``.
    """

    kind: Literal["context", "add", "remove"]
    content: str
    old_lineno: int | None
    new_lineno: int | None
    no_newline: bool = False

    @property
    def raw(self) -> bytes:
        """The line as bytes, including its newline unless ``no_newline``."""
        body = self.content.encode("utf-8", "surrogateescape")
        return body if self.no_newline else body + b"\n"


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: list[HunkLine] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for ln in self.lines if ln.kind == "add")

    @property
    def deleted(self) -> int:
        return sum(1 for ln in self.lines if ln.kind == "remove")


@dataclass(slots=True)
class FileChange:
    """One file entry of a patch."""

    old_path: str | None
    new_path: str | None
    status: Status = "modified"
    similarity: int | None = None
    dissimilarity: int | None = None
    old_mode: int | None = None
    new_mode: int | None = None
    old_hash: str | None = None
    new_hash: str | None = None
    is_binary: bool = False
    binary: BinaryPatch | None = None
    hunks: list[Hunk] = field(default_factory=list)
    is_combined: bool = False
    commit: int | None = None
    line: int = 0
    end_line: int = 0
    parts: tuple[FileChange, ...] = ()

    @property
    def path(self) -> str:
        """``new_path``, or ``old_path`` for a deletion."""
        p = self.new_path if self.new_path is not None else self.old_path
        assert p is not None
        return p

    @property
    def added(self) -> int:
        return sum(h.added for h in self.hunks)

    @property
    def deleted(self) -> int:
        return sum(h.deleted for h in self.hunks)

    def apply(self, content: bytes | None) -> bytes | None:
        """Apply this entry to ``content`` (``None`` when the file does not exist yet).

        Hunks must fit at their stated line numbers: no offset search, no fuzz.
        Returns ``None`` for a deletion. Raises ``ApplyError`` on mismatch.
        """
        from ._apply import apply_file

        return apply_file(self, content)

    # -- summaries -------------------------------------------------------

    def _modes(self) -> tuple[str, str]:
        om = _ZERO_MODE if self.status == "added" else _fmt_mode(self.old_mode)
        nm = _ZERO_MODE if self.status == "deleted" else _fmt_mode(self.new_mode)
        return om, nm

    def _score(self) -> str:
        if self.status in ("renamed", "copied"):
            return "%03d" % (self.similarity if self.similarity is not None else 0)
        if self.dissimilarity is not None:
            return "%03d" % self.dissimilarity
        return ""


def _fmt_mode(mode: int | None) -> str:
    return _ZERO_MODE if mode is None else "%06o" % mode


@dataclass(slots=True)
class CommitInfo:
    """Commit metadata from a ``format-patch`` mail or ``git show`` / ``git log`` header."""

    kind: Literal["mbox", "show"]
    sha: str | None
    author: str | None
    date: str | None
    subject: str
    message: str
    line: int
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def date_parsed(self) -> _dt.datetime | None:
        if self.date is None:
            return None
        from email.utils import parsedate_to_datetime

        try:
            return parsedate_to_datetime(self.date)
        except (TypeError, ValueError):
            pass
        try:
            return _dt.datetime.strptime(self.date.strip(), "%a %b %d %H:%M:%S %Y %z")
        except ValueError:
            return None

    @property
    def title(self) -> str:
        """The subject without a leading ``[PATCH ...]`` tag (a bracket group containing PATCH)."""
        s = self.subject
        if s.startswith("["):
            end = s.find("]")
            if end > 0 and "PATCH" in s[:end].upper():
                return s[end + 1 :].lstrip()
        return s


@dataclass(slots=True)
class Patch:
    files: list[FileChange] = field(default_factory=list)
    commits: list[CommitInfo] = field(default_factory=list)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def _summarizable(self) -> list[FileChange]:
        return [f for f in self.files if not f.is_combined]

    def raw(self, *, z: bool = False) -> str:
        """Text equal to ``git diff --raw`` (or ``--raw -z``) for this patch.

        Blob ids and modes that the patch does not carry (a pure rename has no
        ``index`` line) print as zeros. Combined-diff entries are skipped.
        """
        out: list[str] = []
        for f in self._summarizable():
            om, nm = f._modes()
            oh = f.old_hash or "0000000"
            nh = f.new_hash or "0000000"
            width = max(len(oh), len(nh))
            oh = oh if f.old_hash else "0" * width
            nh = nh if f.new_hash else "0" * width
            head = f":{om} {nm} {oh} {nh} {_STATUS_LETTER[f.status]}{f._score()}"
            paths = [f.old_path, f.new_path] if f.status in ("renamed", "copied") else [f.path]
            if z:
                out.append(head + "\0" + "\0".join(str(p) for p in paths) + "\0")
            else:
                out.append(head + "\t" + "\t".join(quote(_b(str(p))) for p in paths) + "\n")
        return "".join(out)

    def numstat(self, *, z: bool = False) -> str:
        """Text equal to ``git diff --numstat`` (or ``--numstat -z``) for this patch.

        Binary files print ``-``. A pure rename of a binary file prints ``0 0``
        because the patch does not say the file is binary. Combined-diff
        entries are skipped.
        """
        out: list[str] = []
        for f in self._summarizable():
            counts = "-\t-" if f.is_binary else f"{f.added}\t{f.deleted}"
            if f.status in ("renamed", "copied"):
                assert f.old_path is not None and f.new_path is not None
                if z:
                    out.append(f"{counts}\t\0{f.old_path}\0{f.new_path}\0")
                else:
                    out.append(f"{counts}\t{_rename_label(f.old_path, f.new_path)}\n")
            elif z:
                out.append(f"{counts}\t{f.path}\0")
            else:
                out.append(f"{counts}\t{quote(_b(f.path))}\n")
        return "".join(out)


def _rename_label(old: str, new: str) -> str:
    """``dir/{a => b}/file`` style label used by non-``-z`` numstat and stat."""
    a = _b(old)
    b = _b(new)
    pfx = 0
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            break
        if a[i] == 0x2F:
            pfx = i + 1
    # suffix: longest common tail starting at a '/', not overlapping the prefix
    sfx = 0
    ia, ib = len(a), len(b)
    while ia > pfx and ib > pfx and a[ia - 1] == b[ib - 1]:
        ia -= 1
        ib -= 1
        if a[ia] == 0x2F:
            sfx = len(a) - ia
    # the tail must leave both middles non-overlapping with the prefix
    a_mid = a[pfx : len(a) - sfx]
    b_mid = b[pfx : len(b) - sfx]
    if pfx == 0 and sfx == 0:
        return f"{quote(a)} => {quote(b)}"
    prefix = a[:pfx].decode("utf-8", "surrogateescape")
    suffix = a[len(a) - sfx :].decode("utf-8", "surrogateescape") if sfx else ""
    mid = (
        a_mid.decode("utf-8", "surrogateescape")
        + " => "
        + b_mid.decode("utf-8", "surrogateescape")
    )
    return f"{prefix}{{{mid}}}{suffix}"


__all__ = [
    "ApplyError",
    "CommitInfo",
    "FileChange",
    "Hunk",
    "HunkLine",
    "Patch",
    "Status",
]
