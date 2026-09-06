from services.ssh_key_service import ensure_instance_key, get_public_key


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
