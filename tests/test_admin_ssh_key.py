from services.ssh_key_service import ensure_instance_key, get_public_key
from conftest import make_project_dir


def test_admin_page_shows_public_key(client, app, tmp_path):
    ssh_home = tmp_path / "ssh_home"
    ssh_home.mkdir()
    app.config["BEEKEEPER_HOME"] = str(ssh_home)
    ensure_instance_key(str(ssh_home))

    resp = client.get("/admin/")

    assert resp.status_code == 200
    key = get_public_key(str(ssh_home))
    assert key is not None
    assert key.encode() in resp.data


def test_regenerate_ssh_key_replaces_it(client, app, tmp_path):
    ssh_home = tmp_path / "ssh_home"
    ssh_home.mkdir()
    app.config["BEEKEEPER_HOME"] = str(ssh_home)
    ensure_instance_key(str(ssh_home))
    before = get_public_key(str(ssh_home))

    resp = client.post("/admin/ssh-key/regenerate", follow_redirects=True)

    assert resp.status_code == 200
    after = get_public_key(str(ssh_home))
    assert after is not None
    assert after != before


def test_project_page_links_to_admin_on_ssh_auth_error(client, app):
    make_project_dir(
        app, name="sshfail",
        setup_status="error",
        setup_error="git@github.com: Permission denied (publickey).",
    )

    resp = client.get("/projects/sshfail")

    assert resp.status_code == 200
    assert b"Admin" in resp.data
    assert b"ssh-key" in resp.data


def test_project_page_omits_ssh_hint_for_other_errors(client, app):
    make_project_dir(
        app, name="otherfail",
        setup_status="error",
        setup_error="requirements.txt: No such file or directory",
    )

    resp = client.get("/projects/otherfail")

    assert resp.status_code == 200
    assert b"ssh-key" not in resp.data
