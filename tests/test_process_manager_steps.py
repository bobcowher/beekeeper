"""process_manager for pipeline steps: script overrides, inherited paths and project tasks."""
import os
import shutil
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from services import process_manager as pm
from services.db_service import get_db
from tests.conftest import make_project_dir



def _ok():
    return MagicMock(returncode=0, stdout="", stderr="")


def _inline_thread(**kwargs):
    target, args = kwargs.get("target") or (lambda: None), kwargs.get("args", ())

    class _T:
        def start(self):
            target(*args)
    return _T()


def _run_only_cp(tracked=()):
    """subprocess.run stand-in: git and pip succeed, cp really copies, ls-files reports `tracked`."""
    def run(cmd, **kwargs):
        if cmd[0] == "cp":   # Popen is patched too, so copy in-process like cp -a would
            src, dst = cmd[-2], cmd[-1]
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            return _ok()
        if cmd[:2] == ["git", "ls-files"]:
            hit = [p for p in tracked if p == cmd[-1]]
            return MagicMock(returncode=0, stdout="\n".join(hit), stderr="")
        return _ok()
    return run


@pytest.fixture
def project(app):
    data = make_project_dir(app, "m", output_paths=["checkpoints", "data"])
    ws = os.path.join(app.config["PROJECTS_DIR"], "m", "workspace")
    os.makedirs(ws)
    for f in ("train.py", "pretrain.py", "posttrain.py"):
        with open(os.path.join(ws, f), "w") as fh:
            fh.write("print('x')\n")
    return data


@pytest.fixture
def source_run(tmp_path):
    src = tmp_path / "src_run"
    (src / "checkpoints").mkdir(parents=True)
    (src / "checkpoints" / "pretrained.pt").write_text("weights")
    (src / "data").mkdir()
    return {"paths": ["checkpoints"], "source_run_dir": str(src), "source_step": "pretrain",
            "source_run_ref": "m-1"}


def _launch(app, tracked=(), popen=None, **kwargs):
    with patch("services.process_manager._resolve_python_binary", return_value="/fake/venv/bin/python"), \
         patch("services.process_manager._resolve_tensorboard_binary", return_value=None), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", side_effect=_run_only_cp(tracked)), \
         patch("services.process_manager.subprocess.Popen",
               side_effect=popen or (lambda cmd, **kw: MagicMock(pid=9999))) as popen_mock, \
         patch("services.process_manager._monitor_process"):
        result = pm.start_training(app.config["PROJECTS_DIR"], "m", **kwargs)
    return result, popen_mock


def _log(app, run_id=1):
    return open(pm.get_run_log_path(app.config["PROJECTS_DIR"], "m", run_id=run_id)).read()


def test_script_overrides_train_file_and_is_stored(app, project):
    result, popen = _launch(app, script="posttrain.py")
    assert "error" not in result
    assert popen.call_args.args[0] == ["/fake/venv/bin/python", "-u", "posttrain.py"]
    row = get_db().get_run("m", 1)
    assert row["train_file"] == "posttrain.py" and row["kind"] == "train"
    assert "script  : posttrain.py" in _log(app)


def test_train_file_stored_without_script(app, project):
    _launch(app)
    assert get_db().get_run("m", 1)["train_file"] == "train.py"


def test_missing_script_aborts(app, project):
    result, popen = _launch(app, script="nope.py")
    popen.assert_not_called()
    assert "Training file not found: nope.py" in _log(app)


@pytest.mark.parametrize("bad", ["../x.py", "/abs.py"])
def test_bad_script_refused(app, project, bad):
    result, _ = _launch(app, script=bad)
    assert result["code"] == "INVALID_SCRIPT"


def test_inherited_paths_seeded_before_symlinks(app, project, source_run):
    run_dir = os.path.join(app.config["PROJECTS_DIR"], "m", "persistent", "runs", "run_1")
    ws = os.path.join(app.config["PROJECTS_DIR"], "m", "workspace")

    def popen(cmd, **kw):
        link = os.path.join(ws, "checkpoints")
        assert os.path.islink(link) and os.readlink(link) == os.path.join(run_dir, "checkpoints")
        assert open(os.path.join(link, "pretrained.pt")).read() == "weights"
        return MagicMock(pid=9999)

    result, popen_mock = _launch(app, popen=popen, inherit=source_run)
    assert "error" not in result
    popen_mock.assert_called_once()
    assert os.path.isfile(os.path.join(run_dir, ".beekeeper", "inherited.json"))
    assert "[beekeeper] Inherited checkpoints/ from pretrain (m-1): 1 file, 7 B" in _log(app)


def test_empty_inherited_path_warns(app, project, source_run):
    _launch(app, inherit=dict(source_run, paths=["data"]))
    assert "[beekeeper] WARNING: inherited data/ is empty" in _log(app)


def test_missing_inherited_source_refused_before_a_run_exists(app, project, source_run):
    result, _ = _launch(app, inherit=dict(source_run, paths=["models"]))
    assert result == {"error": "inherit: models not found in m-1's run folder", "code": "INHERIT_SOURCE_MISSING"}
    assert get_db().get_training_runs("m") == []


def test_inherited_path_clashing_with_tracked_file_aborts(app, project, source_run):
    ws = os.path.join(app.config["PROJECTS_DIR"], "m", "workspace")
    os.makedirs(os.path.join(ws, "checkpoints"))
    open(os.path.join(ws, "checkpoints", "README"), "w").close()
    result, popen = _launch(app, tracked=("checkpoints",), inherit=source_run)
    popen.assert_not_called()
    assert get_db().get_run("m", 1)["status"] == "crashed"
    assert "inherit: 'checkpoints' clashes with a tracked file or folder in the workspace" in _log(app)


def test_inherited_path_untracked_leftover_warning_becomes_error(app, project, source_run):
    ws = os.path.join(app.config["PROJECTS_DIR"], "m", "workspace")
    os.makedirs(os.path.join(ws, "checkpoints"))
    open(os.path.join(ws, "checkpoints", "stale.pt"), "w").close()
    _, popen = _launch(app, inherit=source_run)
    popen.assert_not_called()
    assert "inherit: Could not protect output path 'checkpoints'" in _log(app)
