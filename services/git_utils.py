"""Shared helpers for git subprocess calls that talk to a remote."""
import os


def git_env():
    """Env for subprocess.run() calls that clone/fetch/ls-remote over SSH.

    accept-new auto-trusts a host's key the first time we see it (so a fresh
    install doesn't fail on the first clone), but still hard-fails if a
    previously-trusted host's key ever changes — unlike disabling host key
    checking outright, this still catches a MITM'd or swapped remote.
    """
    return {
        **os.environ,
        "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=accept-new",
    }
