import os
import subprocess
from pathlib import Path

import pytest


def git(repo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class Repo(os.PathLike):
    """A repo path that also carries .shas (c1, c2, c3)."""

    def __init__(self, path, shas):
        self.path, self.shas = Path(path), shas

    def __fspath__(self):
        return str(self.path)

    __str__ = __fspath__


@pytest.fixture
def repo(tmp_path):
    """Tiny throwaway git repo with 3 commits; `marker.txt` holds the commit number, `broken` file in c3."""
    r = tmp_path / "target"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    shas = []
    for i in (1, 2, 3):
        (r / "marker.txt").write_text(str(i))
        if i == 3:
            (r / "BROKEN").write_text("x")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", f"c{i}")
        shas.append(git(r, "rev-parse", "HEAD"))
    return Repo(r, shas)
