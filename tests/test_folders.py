import json
import os

import pytest

from models.project import Project, normalize_folder
from tests.conftest import make_project_dir


def test_project_folder_defaults_to_empty(tmp_path):
    p = Project(name="a", git_url="x")
    assert p.folder == ""


def test_legacy_project_json_without_folder_loads(app):
    make_project_dir(app, "legacy")
    path = os.path.join(app.config["PROJECTS_DIR"], "legacy", "project.json")
    assert Project.load(path).folder == ""


@pytest.mark.parametrize("raw,expected", [
    ("  chess ", "chess"), ("", ""), (None, ""), ("RL v2.1_x-y", "RL v2.1_x-y"),
])
def test_normalize_folder_accepts(raw, expected):
    assert normalize_folder(raw) == expected


@pytest.mark.parametrize("raw", ["a/b", "x" * 65, "café", 3, True])
def test_normalize_folder_rejects(raw):
    with pytest.raises(ValueError):
        normalize_folder(raw)


def test_list_projects_includes_folder_and_filters(client, app):
    make_project_dir(app, "p1", folder="chess")
    make_project_dir(app, "p2", folder="")
    make_project_dir(app, "p3", folder="chess")
    all_ = client.get("/api/v1/projects").get_json()["data"]["projects"]
    assert {p["name"]: p["folder"] for p in all_} == {"p1": "chess", "p2": "", "p3": "chess"}
    chess = client.get("/api/v1/projects?folder=chess").get_json()["data"]["projects"]
    assert [p["name"] for p in chess] == ["p1", "p3"]
    unfiled = client.get("/api/v1/projects?folder=").get_json()["data"]["projects"]
    assert [p["name"] for p in unfiled] == ["p2"]


def test_patch_folder(client, app):
    make_project_dir(app, "p1")
    r = client.patch("/api/v1/projects/p1", json={"folder": " chess "})
    assert r.status_code == 200
    path = os.path.join(app.config["PROJECTS_DIR"], "p1", "project.json")
    assert json.load(open(path))["folder"] == "chess"


def test_patch_invalid_folder_is_400(client, app):
    make_project_dir(app, "p1")
    r = client.patch("/api/v1/projects/p1", json={"folder": "a/b"})
    assert r.status_code == 400
    assert r.get_json()["error"]["code"] == "INVALID_FOLDER"


def test_patch_folder_allowed_while_running(client, app, monkeypatch):
    make_project_dir(app, "p1")
    from routes import api_v1
    monkeypatch.setattr(api_v1, "get_training_status", lambda name: {"status": "running"})
    assert client.patch("/api/v1/projects/p1", json={"folder": "chess"}).status_code == 200
    assert client.patch("/api/v1/projects/p1", json={"folder": "x", "branch": "dev"}).status_code == 409
    empty = client.patch("/api/v1/projects/p1", json={})
    assert empty.status_code == 400
    assert empty.get_json()["error"] == {"code": "NO_FIELDS", "message": "No fields to update"}


def test_list_and_rename_folder(client, app):
    make_project_dir(app, "p1", folder="chess")
    make_project_dir(app, "p2", folder="chess")
    make_project_dir(app, "p3", folder="go")
    assert client.get("/api/v1/folders").get_json()["data"]["folders"] == ["chess", "go"]
    r = client.post("/api/v1/folders/chess/rename", json={"new_name": "Chess Engines"})
    assert r.get_json()["data"]["renamed"] == 2
    assert client.get("/api/v1/folders").get_json()["data"]["folders"] == ["Chess Engines", "go"]


def test_rename_folder_validates_all_projects_before_saving(app):
    from services.folder_service import rename_folder

    make_project_dir(app, "a-good", folder="chess")
    make_project_dir(app, "z-corrupt", folder="chess")
    corrupt_path = os.path.join(app.config["PROJECTS_DIR"], "z-corrupt", "project.json")
    with open(corrupt_path, "w") as f:
        f.write("{not json")

    with pytest.raises(json.JSONDecodeError):
        rename_folder(app.config["PROJECTS_DIR"], "chess", "renamed")

    good_path = os.path.join(app.config["PROJECTS_DIR"], "a-good", "project.json")
    assert json.load(open(good_path))["folder"] == "chess"


def test_rename_folder_invalid_target_is_400(client, app):
    make_project_dir(app, "p1", folder="chess")
    r = client.post("/api/v1/folders/chess/rename", json={"new_name": "a/b"})
    assert r.status_code == 400


def test_dashboard_rows_carry_folder(client, app):
    make_project_dir(app, "p1", folder="chess")
    html = client.get("/").get_data(as_text=True)
    assert 'data-folder="chess"' in html
    assert 'data-kind="project"' in html


def test_edit_form_saves_folder(client, app):
    make_project_dir(app, "p1")
    client.post("/projects/p1/edit", data={
        "branch": "main", "train_file": "train.py", "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt", "folder": " chess ",
    })
    path = os.path.join(app.config["PROJECTS_DIR"], "p1", "project.json")
    assert json.load(open(path))["folder"] == "chess"


def test_edit_form_rejects_bad_folder(client, app):
    make_project_dir(app, "p1", folder="ok")
    client.post("/projects/p1/edit", data={
        "branch": "main", "train_file": "train.py", "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt", "folder": "a/b",
    })
    path = os.path.join(app.config["PROJECTS_DIR"], "p1", "project.json")
    assert json.load(open(path))["folder"] == "ok"
