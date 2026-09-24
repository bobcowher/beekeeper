"""Shared helpers for git subprocess calls that talk to a remote."""
import os

from services.ssh_key_service import has_instance_key, private_key_path

_BEEKEEPER_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git_env():
    """Env for subprocess.run() calls that clone/fetch/ls-remote over SSH.

    accept-new auto-trusts a host's key the first time we see it (so a fresh
    install doesn't fail on the first clone), but still hard-fails if a
    previously-trusted host's key ever changes — unlike disabling host key
    checking outright, this still catches a MITM'd or swapped remote.

    Also points git at Beekeeper's own managed instance key, if one has been
    generated, so SSH git URLs work without the beekeeper user needing a key
    of its own set up out of band.
    """
    ssh_command = "ssh -o StrictHostKeyChecking=accept-new"
    if has_instance_key(_BEEKEEPER_HOME):
        ssh_command += f" -i {private_key_path(_BEEKEEPER_HOME)} -o IdentitiesOnly=yes"
    return {
        **os.environ,
        "GIT_SSH_COMMAND": ssh_command,
    }
