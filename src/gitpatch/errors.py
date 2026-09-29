"""Exceptions."""

from __future__ import annotations


class PatchError(ValueError):
    """The input is not a well-formed patch.

    ``line`` is the 1-based line number where the problem was detected.
    """

    def __init__(self, message: str, line: int) -> None:
        super().__init__(f"line {line}: {message}")
        self.message = message
        self.line = line


class ApplyError(ValueError):
    """A hunk or binary payload does not fit the content it is applied to."""
