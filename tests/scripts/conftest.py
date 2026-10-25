"""Fixtures for the import scripts: fictional upstream checkouts, built as local git repos."""

import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

GIT = shutil.which("git")

MakeRepo = Callable[[str, Mapping[str, str | Mapping[str, Any]]], Path]


def _git(repo: Path, *args: str) -> None:
    assert GIT is not None
    subprocess.run(  # noqa: S603 - fixed arguments, local temporary repository
        [GIT, "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.org", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def make_repo(tmp_path: Path) -> MakeRepo:
    """Create (or update) a committed git repository holding the given files."""
    if GIT is None:
        pytest.skip("git is not installed")

    def make(name: str, files: Mapping[str, str | Mapping[str, Any]]) -> Path:
        repo = tmp_path / "upstream" / name
        if not repo.exists():
            repo.mkdir(parents=True)
            _git(repo, "init", "-q")
        for relative_path, content in files.items():
            path = repo / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            text = content if isinstance(content, str) else yaml.safe_dump(dict(content))
            path.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "fixture")
        return repo

    return make


@pytest.fixture
def brokers_dir(tmp_path: Path) -> Path:
    """An empty Gomme broker directory."""
    path = tmp_path / "brokers"
    path.mkdir()
    return path
