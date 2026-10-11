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


@pytest.fixture
def yaml_calls(monkeypatch, calls):
    def fake(method, path, text):
        calls.append(("_send_yaml", method, path, text))
        return {"success": True, "data": {}}
    monkeypatch.setattr(server, "_send_yaml", fake)
    return calls


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


def test_start_training_queue_timeout_reports_uncertain_queue_state(monkeypatch):
    def timeout(*args, **kwargs):
        raise server.requests.exceptions.ReadTimeout()

    monkeypatch.setattr(server, "_post", timeout)
    result = server.start_training("p", queue=True)
    assert "may have been queued" in result["message"]
    assert "list_queue('p')" in result["message"]


def test_list_and_cancel_queue(calls):
    server.list_queue("p")
    assert calls[-1] == ("_get", "/projects/p/queue", None)
    server.cancel_queued_run("p", 7)
    assert calls[-1] == ("_delete", "/projects/p/queue/7", None)


def test_create_and_update_pipeline_send_yaml(yaml_calls):
    server.create_pipeline("name: p\nsteps: []\n")
    assert yaml_calls[-1] == ("_send_yaml", "POST", "/pipelines", "name: p\nsteps: []\n")
    server.update_pipeline("p", "x")
    assert yaml_calls[-1] == ("_send_yaml", "PUT", "/pipelines/p", "x")


def test_validate_pipeline_sends_yaml(yaml_calls):
    server.validate_pipeline("name: p\nsteps: []\n")
    assert yaml_calls[-1] == (
        "_send_yaml", "POST", "/pipelines/validate", "name: p\nsteps: []\n"
    )


def test_pipeline_definition_tools(calls):
    server.get_pipeline("p")
    assert calls[-1] == ("_get", "/pipelines/p", None)
    server.delete_pipeline("p")
    assert calls[-1] == ("_delete", "/pipelines/p", None)


def test_pipeline_run_tools(calls):
    server.start_pipeline("p")
    assert calls[-1] == ("_post", "/pipelines/p/runs", None)
    server.pipeline_status("p")
    assert calls[-1] == ("_get", "/pipelines/p/runs?limit=1", None)
    server.pipeline_status("p", run_id=3)
    assert calls[-1] == ("_get", "/pipelines/p/runs/3", None)
    server.list_pipeline_runs("p", limit=7)
    assert calls[-1] == ("_get", "/pipelines/p/runs?limit=7", None)
    server.cancel_pipeline("p", 3)
    assert calls[-1] == ("_post", "/pipelines/p/runs/3/cancel", None)
    server.resume_pipeline("p", 3)
    assert calls[-1] == ("_post", "/pipelines/p/runs/3/resume", None)
    server.get_step_log("p", 3, "deploy", tail=50)
    assert calls[-1] == ("_get", "/pipelines/p/runs/3/steps/deploy/log?tail=50", None)
    server.list_pipelines(folder="chess")
    assert calls[-1] == ("_get", "/pipelines?folder=chess", None)


def test_list_pipelines_filters(calls):
    server.list_pipelines()
    assert calls[-1] == ("_get", "/pipelines", None)
    server.list_pipelines(folder="")
    assert calls[-1] == ("_get", "/pipelines?folder=", None)


def test_check_busy_uses_busy_endpoint_and_keeps_capacity(monkeypatch):
    responses = {
        "/busy": {"data": {
            "busy": True,
            "running_projects": [],
            "running_tasks": ["factory#2:deploy"],
            "setting_up_projects": [],
        }},
        "/capacity": {"data": {"running": 0, "available": 2}},
    }
    monkeypatch.setattr(server, "_get", lambda path: responses[path])

    result = server.check_busy()

    assert result["busy"] is True
    assert result["running_tasks"] == ["factory#2:deploy"]
    assert result["capacity"] == {"running": 0, "available": 2}
