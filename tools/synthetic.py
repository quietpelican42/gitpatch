"""A small deterministic repository full of awkward file names and change types."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ENV = {
    **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR")},
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "TZ": "UTC",
    "LC_ALL": "C",
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}

NAMES = [
    "plain.txt",
    "sp ace.txt",
    "dir with space/inner file.txt",
    "ta\tb.txt",
    'qu"ote.txt',
    "back\\slash.txt",
    "new\nline.txt",
    "é.txt",
    "日本語/ファイル.txt",
    "-lead.txt",
    "b/b/deep.txt",
    "a b/c d.txt",
    "emoji-\U0001f600.txt",
]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, env=env or ENV, capture_output=True, stdin=subprocess.DEVNULL, timeout=60
    )


def build(repo: Path) -> str:
    """Create ``repo`` (a bare repository) and return the tip commit id."""
    work = repo.parent / (repo.name + ".work")
    for d in (work, repo):
        if d.exists():
            shutil.rmtree(d)
    if True:
        work.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(work)], check=True, env=ENV, timeout=60)
    n = [0]

    def commit(msg: str) -> None:
        n[0] += 1
        env = {**ENV, "GIT_AUTHOR_DATE": f"2020-01-{n[0]:02d}T00:00:00+0000", "GIT_COMMITTER_DATE": f"2020-01-{n[0]:02d}T00:00:00+0000"}
        _git(work, "add", "-A", env=env)
        _git(work, "commit", "-q", "--allow-empty", "-m", msg, env=env)

    def w(name: str, data: bytes) -> None:
        p = work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    body = b"".join(b"line %d\n" % i for i in range(1, 41))
    for name in NAMES:
        w(name, body)
    w("empty", b"")
    w("nonl", b"no newline at end")
    w("crlf.txt", b"crlf\r\nline two\r\n")
    w("run.sh", b"#!/bin/sh\necho hi\n")
    w("img.bin", bytes(range(256)) * 40)
    w("big.bin", bytes((i * 7) % 251 for i in range(20000)))
    os.symlink("plain.txt", work / "link")
    commit("add files")
    for name in NAMES:
        w(name, body.replace(b"line 20\n", b"LINE twenty\n"))
    os.chmod(work / "run.sh", 0o755)
    w("nonl", b"no newline at end, changed")
    w("crlf.txt", b"crlf\r\nline TWO\r\n")
    w("img.bin", bytes(range(256)) * 39 + b"tail")
    w("big.bin", bytes((i * 7) % 251 for i in range(20000)) + b"x" * 30)
    commit("edit everything")
    for i, name in enumerate(NAMES[:6]):
        os.rename(work / name, work / (name.replace(".txt", "") + "-renamed.txt"))
    os.rename(work / "img.bin", work / "img renamed.bin")
    os.remove(work / "link")
    w("link", b"now a regular file\n")
    os.symlink("nonl", work / "link2")
    w("empty", b"x\n")
    w("empty2", b"")
    w("copy of plain.txt", body.replace(b"line 20\n", b"LINE twenty\n"))
    commit("renames, type change, empties, copy")
    os.remove(work / "link")
    os.symlink("plain.txt", work / "link")
    os.chmod(work / "run.sh", 0o644)
    for name in NAMES[6:]:
        os.remove(work / name)
    os.remove(work / "big.bin")
    w("gone.bin", b"\x00\x01" * 100)
    w("tiny.bin", b"\x00")
    commit("delete, mode change, symlink back")
    w("gone.bin", b"")
    w("tiny.bin", b"\x00\x00\x00")
    os.remove(work / "empty2")
    commit("binary to empty")
    if True:
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(repo)], check=True, env=ENV, timeout=60)
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, env=ENV, timeout=60
    ).stdout.decode().strip()
