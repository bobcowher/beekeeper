"""MCP tools are thin HTTP wrappers; verify the request each one sends."""
import pytest

from beekeeper_mcp import server


@pytest.fixture
def calls(monkeypatch):
    log = []
    for verb in ("_get", "_post", "_patch", "_delete"):
        def fake(path, body=None, timeout=30, _verb=verb):
            log.append((_verb, path, body))
            return {"success": True, "data": {}}
        monkeypatch.setattr(server, verb, fake)
    return log


def test_list_projects_folder_filter(calls):
    server.list_projects(folder="chess")
    assert calls[-1] == ("_get", "/projects?folder=chess", None)


def test_list_projects_no_filter(calls):
    server.list_projects()
    assert calls[-1] == ("_get", "/projects", None)


def test_list_projects_unfiled_filter(calls):
    server.list_projects(folder="")
    assert calls[-1] == ("_get", "/projects?folder=", None)


def test_list_folders(calls):
    server.list_folders()
    assert calls[-1] == ("_get", "/folders", None)


def test_rename_folder(calls):
    server.rename_folder("chess", "Chess")
    assert calls[-1] == ("_post", "/folders/chess/rename", {"new_name": "Chess"})


def test_create_project_folder(calls):
    server.create_project("p1", "https://example.com/r.git", folder="chess")
    verb, path, body = calls[-1]
    assert (verb, path) == ("_post", "/projects") and body["folder"] == "chess"


def test_create_project_without_folder_omits_key(calls):
    server.create_project("p1", "https://example.com/r.git")
    assert "folder" not in calls[-1][2]


def test_update_project_folder(calls):
    server.update_project("p1", folder="chess")
    verb, path, body = calls[-1]
    assert (verb, path) == ("_patch", "/projects/p1") and body == {"folder": "chess"}


def test_update_project_empty_folder_unfiles(calls):
    server.update_project("p1", folder="")
    assert calls[-1][2] == {"folder": ""}


def test_update_project_without_folder_omits_key(calls):
    server.update_project("p1", branch="dev")
    assert "folder" not in calls[-1][2]


def test_start_training_queue(calls):
    server.start_training("p", branch="dev", env_vars={"LR": "1"}, queue=True)
    assert calls[-1] == ("_post", "/projects/p/training/start",
                         {"branch": "dev", "env_vars": {"LR": "1"}, "queue": True})


def test_start_training_plain_body_unchanged(calls):
    server.start_training("p")
    assert calls[-1] == ("_post", "/projects/p/training/start", {})


def test_list_and_cancel_queue(calls):
    server.list_queue("p")
    assert calls[-1] == ("_get", "/projects/p/queue", None)
    server.cancel_queued_run("p", 7)
    assert calls[-1] == ("_delete", "/projects/p/queue/7", None)
