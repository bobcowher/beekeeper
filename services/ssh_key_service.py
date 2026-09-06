"""Instance-wide SSH keypair, used to authenticate git clone/fetch over SSH."""
import logging
import os
import subprocess

log = logging.getLogger(__name__)

KEY_FILENAME = "id_ed25519"


def _ssh_dir(beekeeper_home):
    return os.path.join(beekeeper_home, ".ssh")


def private_key_path(beekeeper_home):
    return os.path.join(_ssh_dir(beekeeper_home), KEY_FILENAME)


def public_key_path(beekeeper_home):
    return private_key_path(beekeeper_home) + ".pub"


def has_instance_key(beekeeper_home):
    return os.path.isfile(private_key_path(beekeeper_home))


def get_public_key(beekeeper_home):
    """Return the public key text, or None if no key has been generated."""
    pub_path = public_key_path(beekeeper_home)
    if not os.path.isfile(pub_path):
        return None
    with open(pub_path) as f:
        return f.read().strip()


def _generate(beekeeper_home):
    ssh_dir = _ssh_dir(beekeeper_home)
    os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
    key_path = private_key_path(beekeeper_home)
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-C", "beekeeper"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    os.chmod(key_path, 0o600)


def ensure_instance_key(beekeeper_home):
    """Generate the instance SSH keypair on first startup if it doesn't exist yet.

    Swallows failures (e.g. ssh-keygen missing) so a host without it can still
    start and use HTTPS git URLs; only SSH clones would be unavailable.
    """
    if has_instance_key(beekeeper_home):
        return
    try:
        _generate(beekeeper_home)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.error("Failed to generate instance SSH key: %s", e)


_SSH_AUTH_ERROR_MARKERS = (
    "Permission denied (publickey)",
    "Host key verification failed",
)


def is_ssh_auth_error(error_text):
    """True if a git error looks like a missing/rejected SSH key, not e.g. a bad branch name."""
    if not error_text:
        return False
    return any(marker in error_text for marker in _SSH_AUTH_ERROR_MARKERS)


def regenerate_instance_key(beekeeper_home):
    """Discard the current keypair and generate a fresh one."""
    for path in (private_key_path(beekeeper_home), public_key_path(beekeeper_home)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _generate(beekeeper_home)
