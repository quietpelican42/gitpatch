"""Compare with the installed git on a generated repository (skips without git)."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

import corpus
from gitpatch import parse


def test_synthetic_repo_matches_git(synthetic_repo: Path) -> None:
    res = corpus.Result()
    rng = random.Random(1)
    tip = corpus.git(synthetic_repo / "synthetic.git", "rev-parse", "HEAD").decode().strip()
    cases = corpus.make_cases(synthetic_repo, "synthetic", tip)
    assert len(cases) >= 50
    for case in cases:
        corpus.run_case(synthetic_repo, case, res, rng)
    assert res.bad == []
    assert res.patches >= 50
    assert res.exact > 400
    assert res.binary_checked > 50 and res.binary_ok == res.binary_checked
    assert res.apply_checked > 200 and res.apply_ok == res.apply_checked
    for kind in ("renamed", "copied", "type-changed", "added", "deleted", "binary", "mode-change", "non-ascii"):
        assert res.seen[kind] > 0, kind


def test_odd_names_round_trip(synthetic_repo: Path) -> None:
    repo = synthetic_repo / "synthetic.git"
    text = corpus.git(repo, "diff", "-M", "--binary", "HEAD~4", "HEAD~3")
    paths = {f.new_path for f in parse(text).files}
    for name in ("sp ace.txt", "ta\tb.txt", 'qu"ote.txt', "back\\slash.txt", "new\nline.txt", "é.txt", "-lead.txt", "a b/c d.txt"):
        assert name in paths


def test_text_summaries_equal_git_output(synthetic_repo: Path) -> None:
    repo = synthetic_repo / "synthetic.git"
    for rev in (("HEAD~4", "HEAD~3"), ("HEAD~3", "HEAD~2")):
        text = corpus.git(repo, "diff", "-M", "-C", "--binary", *rev)
        p = parse(text)
        # numstat (text form, quoted names, brace rename labels) is byte-identical to git's, apart from pure binary renames
        mine = p.numstat().encode("utf-8", "surrogateescape").split(b"\n")
        want = corpus.git(repo, "diff", "-M", "-C", "--numstat", *rev).split(b"\n")
        assert len(mine) == len(want)
        for a, b in zip(mine, want):
            # exception: a pure rename of a binary file (git reads the blobs, the patch says nothing)
            if a != b and b.startswith(b"-\t-\t") and a.startswith(b"0\t0\t") and b" => " in a:
                b = b"0\t0\t" + b[4:]
            assert a == b


def test_git_available_version() -> None:
    assert os.environ.get("GIT_CONFIG_GLOBAL", "") != "x"
    assert corpus.ENV["TZ"] == "UTC"


@pytest.mark.skipif(not os.environ.get("GITPATCH_CORPUS"), reason="set GITPATCH_CORPUS to a directory prepared with `python tools/corpus.py fetch DIR` to run the real-repository corpus")
def test_real_repositories() -> None:
    root = Path(os.environ["GITPATCH_CORPUS"])
    res = corpus.check(root)
    assert res.bad == []
    assert res.patches >= 300
