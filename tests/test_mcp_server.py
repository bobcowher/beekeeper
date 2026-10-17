"""MCP server: run_ref propagation in analyze_run, version pin."""
import pathlib
import tomllib

from beekeeper_mcp import server
from routes.api_v1 import MIN_MCP_VERSION


def _fake_get(responses):
    def _get(path):
        for prefix, resp in responses:
            if path.startswith(prefix):
                return resp
        raise AssertionError(f"unexpected path {path}")
    return _get


def _run(n):
    return {"data": {"run": {"run_id": n, "run_ref": f"p-{n}", "branch": "main",
                             "status": "running"}}}


def test_mcp_version_is_0_6_0_everywhere():
    pyproject = tomllib.loads(
        pathlib.Path(__file__).parents[1].joinpath("pyproject.toml").read_text()
    )
    assert server.MCP_VERSION == MIN_MCP_VERSION == pyproject["project"]["version"] == "0.6.0"


def test_analyze_run_explicit_run_id_has_run_ref(mocker):
    mocker.patch.object(server, "_get", side_effect=_fake_get([
        ("/projects/p/runs/7/logs", {}),
        ("/projects/p/runs/7", _run(7)),
        ("/projects/p/tensorboard/latest", {"data": {}}),
    ]))
    out = server.analyze_run("p", run_id=7)
    assert out["run_id"] == 7 and out["run_ref"] == "p-7"


def test_analyze_run_parallel_branch_has_run_ref(mocker):
    status = {"data": {"runs": [
        {"run_id": 1, "run_ref": "p-1", "status": "running"},
        {"run_id": 2, "run_ref": "p-2", "status": "running"},
    ]}}
    mocker.patch.object(server, "_get", side_effect=_fake_get([
        ("/projects/p/training/status", status),
        ("/projects/p/runs/1/logs", {}), ("/projects/p/runs/2/logs", {}),
        ("/projects/p/runs/1", _run(1)), ("/projects/p/runs/2", _run(2)),
        ("/projects/p/tensorboard/latest", {"data": {}}),
    ]))
    out = server.analyze_run("p")["parallel_runs"]
    assert out["1"]["run_ref"] == "p-1" and out["2"]["run_ref"] == "p-2"


def test_analyze_run_single_active_has_run_ref(mocker):
    status = {"data": {"runs": [{"run_id": 4, "run_ref": "p-4", "status": "running"}]}}
    mocker.patch.object(server, "_get", side_effect=_fake_get([
        ("/projects/p/training/status", status),
        ("/projects/p/tensorboard/latest", {"data": {"run_id": 4, "run_ref": "p-4"}}),
        ("/projects/p/logs", {}),
    ]))
    out = server.analyze_run("p")
    assert out["run_id"] == 4 and out["run_ref"] == "p-4"


def test_analyze_run_idle_takes_run_ref_from_tensorboard_latest(mocker):
    mocker.patch.object(server, "_get", side_effect=_fake_get([
        ("/projects/p/training/status", {"data": {"runs": []}}),
        ("/projects/p/tensorboard/latest", {"data": {"run_id": 9, "run_ref": "p-9"}}),
        ("/projects/p/logs", {}),
    ]))
    out = server.analyze_run("p")
    assert out["run_id"] == 9 and out["run_ref"] == "p-9"
