"""Pre-launch git sync against real git repos.

A run with a branch override must leave the workspace checked out on that branch,
so training code that asks git for the branch gets the run's branch.
"""
import subprocess

import pytest

from services.process_manager import _sync_workspace


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin with branches main and feature (different commits), plus a clone on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@t")
    _git(origin, "config", "user.name", "t")
    (origin / "train.py").write_text("print('main')\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "main")
    _git(origin, "checkout", "-q", "-b", "feature")
    (origin / "train.py").write_text("print('feature')\n")
    _git(origin, "commit", "-q", "-am", "feature")
    _git(origin, "checkout", "-q", "main")

    ws = tmp_path / "workspace"
    _git(tmp_path, "clone", "-q", str(origin), str(ws))
    return origin, ws


def test_override_branch_is_checked_out(repos):
    origin, ws = repos
    assert _sync_workspace(str(ws), "feature") is None
    assert _git(ws, "branch", "--show-current") == "feature"
    assert _git(ws, "rev-parse", "HEAD") == _git(origin, "rev-parse", "feature")
    # The default branch's local ref is left alone.
    assert _git(ws, "rev-parse", "main") == _git(origin, "rev-parse", "main")


def test_back_to_default_after_override(repos):
    _, ws = repos
    assert _sync_workspace(str(ws), "feature") is None
    assert _sync_workspace(str(ws), "main") is None
    assert _git(ws, "branch", "--show-current") == "main"
    assert (ws / "train.py").read_text() == "print('main')\n"


def test_remote_is_authoritative(repos):
    """Local edits, local commits and untracked files in the way don't survive or block the sync."""
    origin, ws = repos
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    (ws / "train.py").write_text("local commit\n")
    _git(ws, "commit", "-q", "-am", "local")
    (ws / "train.py").write_text("dirty\n")

    # Untracked file at a path that is tracked on feature.
    _git(origin, "checkout", "-q", "feature")
    (origin / "extra.py").write_text("tracked on feature\n")
    _git(origin, "add", "extra.py")
    _git(origin, "commit", "-q", "-m", "extra")
    _git(origin, "checkout", "-q", "main")
    (ws / "extra.py").write_text("untracked local\n")

    assert _sync_workspace(str(ws), "feature") is None
    assert _git(ws, "rev-parse", "HEAD") == _git(origin, "rev-parse", "feature")
    assert (ws / "train.py").read_text() == "print('feature')\n"
    assert (ws / "extra.py").read_text() == "tracked on feature\n"
    assert _git(ws, "status", "--porcelain") == ""

    assert _sync_workspace(str(ws), "main") is None
    assert _git(ws, "rev-parse", "HEAD") == _git(origin, "rev-parse", "main")


def test_missing_branch_reports_error(repos):
    _, ws = repos
    err = _sync_workspace(str(ws), "nope")
    assert err and "nope" in err
