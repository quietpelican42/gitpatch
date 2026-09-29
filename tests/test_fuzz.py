"""Mutated and random input must give a PatchError with a line number, or parse."""

from __future__ import annotations

import random
from pathlib import Path

import corpus
from gitpatch import PatchError, parse


def _seeds(root: Path) -> list[bytes]:
    repo = root / "synthetic.git"
    out = []
    for rev in (("HEAD~4", "HEAD~3"), ("HEAD~3", "HEAD~2"), ("HEAD~2", "HEAD~1"), ("HEAD~1", "HEAD")):
        out.append(corpus.git(repo, "diff", "-M", "-C", "--binary", *rev))
    out.append(corpus.git(repo, "format-patch", "--stdout", "-M", "--binary", "HEAD~3..HEAD"))
    out.append(corpus.git(repo, "show", "-M", "--binary", "HEAD~2"))
    return out


def _mutate(rng: random.Random, data: bytes) -> bytes:
    lines = data.split(b"\n")
    for _ in range(rng.randint(1, 4)):
        kind = rng.randrange(7)
        i = rng.randrange(len(lines))
        if kind == 0:
            del lines[i]
        elif kind == 1:
            lines.insert(i, rng.choice([b"@@ -1 +1 @@", b"+x", b"-y", b" z", b"\\ No newline at end of file", b"GIT binary patch", b"index a..b", b"rename from", b"diff --git a b", b"literal 5", b"", b'"\\']))
        elif kind == 2:
            lines = lines[: i + 1]
        elif kind == 3 and lines[i]:
            j = rng.randrange(len(lines[i]))
            lines[i] = lines[i][:j] + bytes([rng.randrange(256)]) + lines[i][j + 1 :]
        elif kind == 4 and lines[i]:
            lines[i] = lines[i][: rng.randrange(len(lines[i]))]
        elif kind == 5:
            lines[i] = lines[i].replace(b"1", b"9", 1)
        else:
            lines[i] = b"\xff\xfe" + lines[i]
    return b"\n".join(lines)


def test_mutated_patches_never_crash(synthetic_repo: Path) -> None:
    rng = random.Random(20260929)
    seeds = _seeds(synthetic_repo)
    parsed = errors = 0
    for _ in range(3000):
        data = _mutate(rng, rng.choice(seeds))
        try:
            parse(data)
            parsed += 1
        except PatchError as exc:
            errors += 1
            assert isinstance(exc.line, int) and exc.line >= 1
            assert f"line {exc.line}" in str(exc)
    assert parsed > 100 and errors > 100


def test_random_bytes_never_crash() -> None:
    rng = random.Random(7)
    for _ in range(2000):
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
        try:
            parse(data)
        except PatchError as exc:
            assert exc.line >= 1


def test_random_line_soup_never_crashes() -> None:
    rng = random.Random(11)
    frags = [b"diff --git a/x b/x", b"--- a/x", b"+++ b/x", b"@@ -1,2 +1,2 @@", b" ctx", b"-old", b"+new", b"GIT binary patch", b"literal 3", b"delta 2", b"Binary files a and b differ", b"old mode 100644", b"new mode 100755", b"index 1..2 100644", b"similarity index 90%", b"rename to y", b"copy from z", b"diff --cc f", b"@@@ -1 -1 +1 @@@", b"From " + b"a" * 40 + b" x", b"Subject: s", b'"unterminated']
    for _ in range(3000):
        data = b"\n".join(rng.choice(frags) for _ in range(rng.randrange(1, 14)))
        try:
            parse(data)
        except PatchError as exc:
            assert exc.line >= 1
