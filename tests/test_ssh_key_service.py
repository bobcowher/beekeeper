import os

from services import ssh_key_service as sks


def test_ensure_instance_key_generates_keypair(tmp_path):
    home = str(tmp_path)
    assert not sks.has_instance_key(home)

    sks.ensure_instance_key(home)

    assert sks.has_instance_key(home)
    priv = sks.private_key_path(home)
    pub = sks.public_key_path(home)
    assert os.path.isfile(priv)
    assert os.path.isfile(pub)
    assert oct(os.stat(priv).st_mode & 0o777) == "0o600"
    assert sks.get_public_key(home).startswith("ssh-ed25519 ")


def test_ensure_instance_key_is_idempotent(tmp_path):
    home = str(tmp_path)
    sks.ensure_instance_key(home)
    first = sks.get_public_key(home)

    sks.ensure_instance_key(home)

    assert sks.get_public_key(home) == first


def test_get_public_key_returns_none_when_missing(tmp_path):
    assert sks.get_public_key(str(tmp_path)) is None


def test_regenerate_instance_key_replaces_keypair(tmp_path):
    home = str(tmp_path)
    sks.ensure_instance_key(home)
    first = sks.get_public_key(home)

    sks.regenerate_instance_key(home)

    second = sks.get_public_key(home)
    assert second is not None
    assert second != first


def test_is_ssh_auth_error_detects_known_markers():
    assert sks.is_ssh_auth_error("git@github.com: Permission denied (publickey).")
    assert sks.is_ssh_auth_error("Host key verification failed.")


def test_is_ssh_auth_error_ignores_unrelated_errors():
    assert not sks.is_ssh_auth_error("fatal: could not read Username for 'https://github.com'")
    assert not sks.is_ssh_auth_error("")
    assert not sks.is_ssh_auth_error(None)


def test_ensure_instance_key_swallows_missing_binary(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("ssh-keygen not found")

    monkeypatch.setattr(sks.subprocess, "run", _boom)

    sks.ensure_instance_key(str(tmp_path))  # must not raise

    assert not sks.has_instance_key(str(tmp_path))
