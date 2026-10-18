"""Project-dependent pipeline checks: output paths on both sides of an inherit, local envs for tasks."""
import pytest

from services import pipeline_checks
from services.pipeline_schema import validate
from tests.conftest import make_project_dir


def _steps(*steps):
    return validate({"name": "p", "steps": list(steps)})["steps"]


def check(app, steps):
    return pipeline_checks.check_steps(app.config["PROJECTS_DIR"], steps)


def test_declared_on_both_sides_passes(app):
    make_project_dir(app, "m", output_paths=["checkpoints", "data"])
    steps = _steps({"name": "pre", "type": "train", "project": "m", "script": "pretrain.py"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["checkpoints"]})
    assert check(app, steps) == []


def test_subfolder_of_an_output_path_passes(app):
    make_project_dir(app, "m", output_paths=["checkpoints"])
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["checkpoints/pretrain"]})
    assert check(app, steps) == []


def test_output_path_prefix_is_not_a_parent(app):
    make_project_dir(app, "m", output_paths=["checkpoints"])
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["checkpoints2/x"]})
    assert [e["message"] for e in check(app, steps)] == [
        "'checkpoints2/x' isn't an output path of the previous step (pre)",
        "m doesn't list 'checkpoints2/x' as an output path"]


def test_subfolder_of_the_data_dir_link_cannot_be_inherited(app):
    make_project_dir(app, "m", output_paths=["checkpoints", "data"], data_dir_enabled=True)
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["data/champion"]})
    assert check(app, steps) == [{"path": "steps[1].inherit", "message": "'data/champion' is inside m's data dir link"}]


def test_previous_project_lacks_path(app):
    make_project_dir(app, "a", output_paths=[])
    make_project_dir(app, "b", output_paths=["checkpoints"])
    steps = _steps({"name": "pre", "type": "train", "project": "a"},
                   {"name": "post", "type": "train", "project": "b", "inherit": ["checkpoints"]})
    assert check(app, steps) == [{
        "path": "steps[1].inherit",
        "message": "'checkpoints' isn't an output path of the previous step (pre)",
    }]


def test_receiving_project_lacks_path_offers_fix(app):
    make_project_dir(app, "a", output_paths=["checkpoints"])
    make_project_dir(app, "my-model", output_paths=[])
    steps = _steps({"name": "pre", "type": "train", "project": "a"},
                   {"name": "post", "type": "train", "project": "my-model", "inherit": ["checkpoints"]})
    assert check(app, steps) == [{
        "path": "steps[1].inherit",
        "message": "my-model doesn't list 'checkpoints' as an output path",
        "error_code": "INHERIT_PATH_NOT_DECLARED",
        "fix": {"call": "update_project", "project": "my-model", "output_paths_add": ["checkpoints"]},
    }]


def test_project_task_counts_as_having_project_paths(app):
    make_project_dir(app, "m", output_paths=["checkpoints"])
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "check", "type": "task", "project": "m", "command": "true",
                    "inherit": ["checkpoints"]},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["checkpoints"]})
    assert check(app, steps) == []


def test_plain_task_receiver_always_has_path(app):
    make_project_dir(app, "m", output_paths=["checkpoints"])
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "export", "type": "task", "command": "true", "inherit": ["checkpoints"]})
    assert check(app, steps) == []


def test_data_dir_link_cannot_be_inherited(app):
    make_project_dir(app, "m", output_paths=["data"], data_dir_enabled=True, data_dir_local="data",
                     data_dir_remote="/srv/data")
    steps = _steps({"name": "pre", "type": "train", "project": "m"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["data"]})
    assert check(app, steps) == [{"path": "steps[1].inherit", "message": "'data' is m's data dir link"}]


def test_tensorboard_dir_cannot_be_inherited(app):
    make_project_dir(app, "a", output_paths=["runs"])
    make_project_dir(app, "m", tensorboard_log_dir="runs")
    steps = _steps({"name": "pre", "type": "train", "project": "a"},
                   {"name": "post", "type": "train", "project": "m", "inherit": ["runs/"]})
    assert check(app, steps) == [{"path": "steps[1].inherit", "message": "'runs' is m's TensorBoard dir"}]


def test_missing_projects_are_skipped(app):
    steps = _steps({"name": "pre", "type": "train", "project": "ghost"},
                   {"name": "post", "type": "train", "project": "ghost2", "inherit": ["checkpoints"]})
    assert check(app, steps) == []


def test_runpod_project_task_without_local_env_offers_fix(app, monkeypatch):
    from services import remote_runs
    make_project_dir(app, "my-model", compute_target="runpod")
    monkeypatch.setattr(remote_runs, "is_remote", lambda p: p.get("compute_target") == "runpod")
    monkeypatch.setattr(pipeline_checks, "_has_local_env", lambda d, p: False)
    steps = _steps({"name": "t", "type": "task", "project": "my-model", "command": "true"})
    assert check(app, steps) == [{
        "path": "steps[0].project",
        "message": "my-model runs on Runpod and has no local environment for tasks",
        "error_code": "TASK_NEEDS_LOCAL_ENV",
        "fix": {"call": "build_local_env", "project": "my-model"},
    }]


def test_runpod_project_task_with_local_env_passes(app, monkeypatch):
    from services import remote_runs
    make_project_dir(app, "my-model", compute_target="runpod")
    monkeypatch.setattr(remote_runs, "is_remote", lambda p: True)
    monkeypatch.setattr(pipeline_checks, "_has_local_env", lambda d, p: True)
    steps = _steps({"name": "t", "type": "task", "project": "my-model", "command": "true"})
    assert check(app, steps) == []


def test_runpod_train_step_needs_no_local_env(app, monkeypatch):
    from services import remote_runs
    make_project_dir(app, "my-model", compute_target="runpod")
    monkeypatch.setattr(remote_runs, "is_remote", lambda p: True)
    monkeypatch.setattr(pipeline_checks, "_has_local_env", lambda d, p: False)
    assert check(app, _steps({"name": "t", "type": "train", "project": "my-model"})) == []


def test_indices_limit_which_steps_are_checked(app):
    make_project_dir(app, "a", output_paths=[])
    make_project_dir(app, "b", output_paths=[])
    steps = _steps({"name": "s0", "type": "train", "project": "a"},
                   {"name": "s1", "type": "train", "project": "b", "inherit": ["x"]},
                   {"name": "s2", "type": "train", "project": "a", "inherit": ["y"]})
    paths = {e["path"] for e in pipeline_checks.check_steps(app.config["PROJECTS_DIR"], steps, indices=[2])}
    assert paths == {"steps[2].inherit"}


def test_save_with_projects_dir_runs_checks(app):
    from services import pipeline_store
    from services.pipeline_schema import PipelineValidationError
    make_project_dir(app, "a", output_paths=["checkpoints"])
    make_project_dir(app, "b", output_paths=[])
    defn = {"name": "p", "steps": [{"name": "s0", "type": "train", "project": "a"},
                                   {"name": "s1", "type": "train", "project": "b", "inherit": ["checkpoints"]}]}
    with pytest.raises(PipelineValidationError) as exc:
        pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], defn, projects_dir=app.config["PROJECTS_DIR"])
    assert exc.value.errors[0]["error_code"] == "INHERIT_PATH_NOT_DECLARED"
    # Without projects_dir (e.g. a project rename rewriting refs) only the schema applies.
    pipeline_store.save_pipeline(app.config["PIPELINES_DIR"], defn)
