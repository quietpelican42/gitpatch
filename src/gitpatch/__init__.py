"""Parser for git-produced patches (``git diff``, ``git format-patch``, ``git show``)."""

from ._binary import BinaryData, BinaryPatch, apply_delta
from ._parser import parse
from .errors import ApplyError, PatchError
from .model import CommitInfo, FileChange, Hunk, HunkLine, Patch, Status

__all__ = [
    "ApplyError",
    "BinaryData",
    "BinaryPatch",
    "CommitInfo",
    "FileChange",
    "Hunk",
    "HunkLine",
    "Patch",
    "PatchError",
    "Status",
    "apply_delta",
    "parse",
]
