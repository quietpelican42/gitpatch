from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))


@pytest.fixture(scope="session")
def synthetic_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git is not installed: install git to run the oracle tests")
    import synthetic

    root = tmp_path_factory.mktemp("synthetic")
    synthetic.build(root / "synthetic.git")
    return root


def git_out(repo: Path, *args: str) -> bytes:
    import corpus

    return corpus.git(repo, *args)


def run_git_version() -> str:
    return subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
