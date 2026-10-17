"""Admin, Nodes page and API surface for the Runpod provider: hidden when off, working when on."""
import pytest

from services import runpod_config
from tests.conftest import make_project_dir


@pytest.fixture
def config_store(monkeypatch, tmp_path):
    store = {}
    get = lambda k, d=None: store.get(k, None if d is None else str(d))  # noqa: E731
    for name, fn in [("get_config", get),
                     ("get_config_bool", lambda k, d=False: str(store.get(k, d)).lower() in ("true", "1")),
                     ("set_config", lambda k, v: store.__setitem__(k, str(v))),
                     ("save_config", lambda: None)]:
        monkeypatch.setattr(runpod_config, name, fn)
    runpod_config.init(str(tmp_path / "home"))
    return store


def test_everything_hidden_when_disabled(client, app, config_store):
    make_project_dir(app, "p")
    assert client.get("/nodes").status_code == 404
    assert client.get("/nodes/status").status_code == 404
    assert b'href="/nodes"' not in client.get("/").data
    assert b"remote-compute-strip" not in client.get("/").data
    assert b"Where to Run" not in client.get("/projects/p/edit").data
    project = client.get("/api/v1/projects/p").get_json()["data"]["project"]
    assert "compute_target" not in project
    r = client.patch("/api/v1/projects/p", json={"compute_target": "runpod"})
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "RUNPOD_DISABLED"
    assert client.get("/api/v1/compute/runpod").status_code == 404
    # The admin section is always there, so it can be turned on.
    assert b"Compute Providers" in client.get("/admin/").data


def test_admin_save_enables_and_stores_key_out_of_config(client, config_store, tmp_path):
    r = client.post("/admin/compute/runpod", data={
        "enabled": "true", "api_key": "rpa_secret", "cap_daily_soft": "8", "cap_daily_hard": "10",
        "cap_monthly_soft": "", "cap_monthly_hard": "", "cap_total_soft": "", "cap_total_hard": "",
        "cloud_type": "community", "default_image": "", "gpu_types": "NVIDIA RTX A5000\nNVIDIA GeForce RTX 4090",
        "max_concurrent_pods": "2", "max_runtime_hours": "12", "max_upload_gb": "20",
        "sync_interval_min": "10", "hold_for_sync_hours": "2", "container_disk_gb": "50",
    })
    assert r.status_code == 302
    s = runpod_config.load()
    assert s.enabled and s.has_key and s.caps["daily"] == {"soft": 8.0, "hard": 10.0}
    assert s.gpu_types == ["NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090"]
    assert s.max_runtime_hours == 12.0 and s.cloud_type == "COMMUNITY"
    assert "rpa_secret" not in str(config_store)
    key_file = tmp_path / "home" / "secrets" / "runpod_api_key"
    assert key_file.read_text() == "rpa_secret" and oct(key_file.stat().st_mode)[-3:] == "600"
    # A blank key field keeps the saved key.
    client.post("/admin/compute/runpod", data={"enabled": "true", "api_key": "", "cap_daily_hard": "10",
                                               "max_concurrent_pods": "2", "max_runtime_hours": "12",
                                               "max_upload_gb": "20", "sync_interval_min": "10",
                                               "hold_for_sync_hours": "2", "container_disk_gb": "50"})
    assert runpod_config.get_api_key() == "rpa_secret"


def test_admin_rejects_soft_above_hard(client, config_store):
    client.post("/admin/compute/runpod", data={
        "enabled": "true", "cap_daily_soft": "20", "cap_daily_hard": "10", "max_concurrent_pods": "2",
        "max_runtime_hours": "12", "max_upload_gb": "20", "sync_interval_min": "10",
        "hold_for_sync_hours": "2", "container_disk_gb": "50"})
    assert runpod_config.load().caps["daily"]["hard"] is None   # nothing saved


def test_enabled_surfaces(client, app, config_store):
    config_store.update({"compute.runpod.enabled": "true", "compute.runpod.cap.daily.hard": "10"})
    make_project_dir(app, "p")
    assert b'href="/nodes"' in client.get("/").data
    assert b"remote-compute-strip" in client.get("/").data
    assert client.get("/nodes").status_code == 200
    status = client.get("/nodes/status").get_json()
    assert status["spend"]["daily"] == {"spent": 0.0, "soft": None, "hard": 10.0}
    assert status["pods"] == []

    r = client.patch("/api/v1/projects/p", json={"compute_target": "runpod", "runpod_gpu_types": ["NVIDIA RTX A5000"],
                                                 "data_dir_sync": True})
    assert r.status_code == 200, r.get_json()
    project = r.get_json()["data"]["project"]
    assert project["compute_target"] == "runpod" and project["runpod_gpu_types"] == ["NVIDIA RTX A5000"]
    assert client.patch("/api/v1/projects/p", json={"compute_target": "mars"}).status_code == 400

    page = client.get("/projects/p").data
    assert b"runs on runpod" in page
    edit = client.get("/projects/p/edit").data
    assert b"Where to Run" in edit

    api = client.get("/api/v1/compute/runpod").get_json()["data"]
    assert "api_key" not in str(api["settings"]).lower().replace("api_key_set", "")


def test_edit_form_saves_compute_fields(client, app, config_store):
    config_store.update({"compute.runpod.enabled": "true"})
    make_project_dir(app, "p")
    r = client.post("/projects/p/edit", data={
        "branch": "main", "train_file": "train.py", "compute_target": "runpod",
        "runpod_gpu_types": "NVIDIA GeForce RTX 4090\n\nNVIDIA RTX A6000", "runpod_image": " my/image:1 ",
        "data_dir_sync": "1"})
    assert r.status_code == 302
    import json, os
    with open(os.path.join(app.config["PROJECTS_DIR"], "p", "project.json")) as f:
        saved = json.load(f)
    assert saved["compute_target"] == "runpod"
    assert saved["runpod_gpu_types"] == ["NVIDIA GeForce RTX 4090", "NVIDIA RTX A6000"]
    assert saved["runpod_image"] == "my/image:1" and saved["data_dir_sync"] is True
