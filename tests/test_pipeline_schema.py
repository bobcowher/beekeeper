import pytest

from services.pipeline_schema import (
    PipelineValidationError, parse_text, step_dir_var, to_yaml, valid_branch_name, validate,
)

GOOD = """
name: chess-factory
folder: chess
description: Pretrain to deploy.
steps:
  - name: pretrain
    type: train
    project: chess-pretrain
  - name: fine-tune
    type: train
    project: chess-ft
    branch: lora-v2
    env_vars:
      LR: "1e-5"
  - name: deploy
    type: task
    timeout_s: 600
    command: |
      cp "$BEEKEEPER_STEP_FINE_TUNE_DIR/model.gguf" /srv/models/
"""


def errors_for(text_or_dict, **kw):
    data = parse_text(text_or_dict) if isinstance(text_or_dict, str) else text_or_dict
    with pytest.raises(PipelineValidationError) as exc:
        validate(data, **kw)
    return {e["path"]: e["message"] for e in exc.value.errors}


def test_good_definition_normalizes():
    d = validate(parse_text(GOOD))
    assert d["name"] == "chess-factory" and d["folder"] == "chess"
    assert d["steps"][0] == {"name": "pretrain", "type": "train", "project": "chess-pretrain", "env_vars": {}}
    assert d["steps"][1]["branch"] == "lora-v2" and d["steps"][1]["env_vars"] == {"LR": "1e-5"}
    assert d["steps"][2]["timeout_s"] == 600 and d["steps"][2]["command"].startswith("cp ")


def test_task_timeout_defaults():
    d = validate({"name": "p", "steps": [{"name": "t", "type": "task", "command": "true"}]})
    assert d["steps"][0]["timeout_s"] == 3600


def test_json_text_is_accepted():
    d = validate(parse_text('{"name": "p", "steps": [{"name": "a", "type": "train", "project": "x"}]}'))
    assert d["steps"][0]["project"] == "x"


def test_step_dir_var():
    assert step_dir_var("fine-tune") == "BEEKEEPER_STEP_FINE_TUNE_DIR"


def test_round_trip_preserves_definition():
    d = validate(parse_text(GOOD))
    assert validate(parse_text(to_yaml(d))) == d
    assert "command: |" in to_yaml(d)


def test_yaml_bool_branch_rejected_with_quote_hint():
    errs = errors_for("name: p\nsteps:\n  - {name: a, type: train, project: x, branch: no}\n")
    assert "quote" in errs["steps[0].branch"]


@pytest.mark.parametrize("branch", ["main", "feat/x-1", "release-1.2"])
def test_valid_branch_names(branch):
    assert valid_branch_name(branch)


@pytest.mark.parametrize("branch", [
    "-x", "--upload-pack=evil", "a b", "a..b", "a~1", "x.lock", "/x", "x/", "", "a@{b",
])
def test_invalid_branch_names(branch):
    assert not valid_branch_name(branch)


def test_invalid_train_branch_rejected():
    errs = errors_for({
        "name": "p",
        "steps": [{"name": "a", "type": "train", "project": "x", "branch": "a..b"}],
    })
    assert errs["steps[0].branch"] == "is not a valid branch name"


@pytest.mark.parametrize("branch", ["", "   "])
def test_empty_train_branch_rejected(branch):
    errs = errors_for({
        "name": "p",
        "steps": [{"name": "a", "type": "train", "project": "x", "branch": branch}],
    })
    assert errs["steps[0].branch"] == "is not a valid branch name"


@pytest.mark.parametrize("text,key", [
    ("name: p\nname: q\nsteps: []\n", "'name'"),
    ("name: p\nsteps:\n  - name: a\n    type: task\n    command: x\n    command: y\n", "'command'"),
])
def test_duplicate_yaml_mapping_keys_rejected(text, key):
    with pytest.raises(PipelineValidationError) as exc:
        parse_text(text)
    assert "duplicate mapping key" in str(exc.value)
    assert key in str(exc.value)


@pytest.mark.parametrize("doc,path", [
    ({"name": "p\n", "steps": [{"name": "a", "type": "task", "command": "x"}]}, "name"),
    ({"name": "p", "steps": [{"name": "a\n", "type": "task", "command": "x"}]}, "steps[0].name"),
    ({"name": "p", "steps": [{"name": "a", "type": "train", "project": "x",
                                  "env_vars": {"BAD\n": "x"}}]}, "steps[0].env_vars.BAD\n"),
])
def test_pattern_fields_reject_trailing_newline(doc, path):
    assert path in errors_for(doc)


def test_yaml_float_env_rejected():
    errs = errors_for("name: p\nsteps:\n  - {name: a, type: train, project: x, env_vars: {LR: 0.001}}\n")
    assert "quote" in errs["steps[0].env_vars.LR"]


def test_numeric_name_rejected():
    assert "name" in errors_for("name: 3.10\nsteps:\n  - {name: a, type: task, command: 'true'}\n")


@pytest.mark.parametrize("doc,path", [
    ({"name": "p", "steps": [], "extra": 1}, "extra"),
    ({"name": "p", "steps": []}, "steps"),
    ({"name": "Bad Name", "steps": [{"name": "a", "type": "task", "command": "x"}]}, "name"),
    ({"name": "p", "folder": "a/b", "steps": [{"name": "a", "type": "task", "command": "x"}]}, "folder"),
    ({"name": "p", "steps": [{"name": "a", "type": "deploy"}]}, "steps[0].type"),
    ({"name": "p", "steps": [{"name": "a", "type": "train"}]}, "steps[0].project"),
    ({"name": "p", "steps": [{"name": "a", "type": "train", "project": "../outside"}]},
     "steps[0].project"),
    ({"name": "p", "steps": [{"name": "a", "type": "task", "command": "  "}]}, "steps[0].command"),
    ({"name": "p", "steps": [{"name": "a", "type": "task", "command": "x", "timeout_s": 0}]}, "steps[0].timeout_s"),
    ({"name": "p", "steps": [{"name": "a", "type": "task", "command": "x", "timeout_s": True}]}, "steps[0].timeout_s"),
    ({"name": "p", "steps": [{"name": "a", "type": "task", "command": "x", "project": "../y"}]}, "steps[0].project"),
    ({"name": "p", "steps": [{"name": "a", "type": "train", "project": "x",
                               "env_vars": {"BEEKEEPER_RUN_DIR": "x"}}]}, "steps[0].env_vars.BEEKEEPER_RUN_DIR"),
    ({"name": "p", "steps": [{"name": "A", "type": "task", "command": "x"}]}, "steps[0].name"),
])
def test_rejections(doc, path):
    assert path in errors_for(doc)


def test_duplicate_and_colliding_step_names():
    doc = {"name": "p", "steps": [
        {"name": "a-b", "type": "task", "command": "x"},
        {"name": "a_b", "type": "task", "command": "x"},
    ]}
    assert "steps[1].name" in errors_for(doc)


def test_too_many_steps():
    doc = {"name": "p", "steps": [{"name": f"s{i}", "type": "task", "command": "x"} for i in range(33)]}
    assert "steps" in errors_for(doc)


def test_expected_name_mismatch():
    assert "name" in errors_for({"name": "p", "steps": [{"name": "a", "type": "task", "command": "x"}]},
                                expected_name="q")


def test_invalid_yaml_and_non_mapping():
    with pytest.raises(PipelineValidationError):
        parse_text("name: [unclosed")
    with pytest.raises(PipelineValidationError):
        parse_text("- just a list")


def test_to_yaml_quotes_ambiguous_strings():
    d = validate({"name": "p", "steps": [{"name": "a", "type": "train", "project": "x", "branch": "no"}]})
    assert validate(parse_text(to_yaml(d)))["steps"][0]["branch"] == "no"


@pytest.mark.parametrize("text", ["no", "0", "[]"])
def test_falsy_non_string_description_rejected(text):
    errs = errors_for(f"name: p\ndescription: {text}\nsteps:\n  - {{name: a, type: task, command: x}}\n")
    assert "quote" in errs["description"]


# --- script, inherit and project tasks ------------------------------------------

def _steps(*steps):
    return {"name": "p", "steps": list(steps)}


TRAIN_A = {"name": "a", "type": "train", "project": "m"}


def test_train_step_script_and_inherit():
    d = validate(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "script": "posttrain.py",
                                  "inherit": ["checkpoints/", "data"]}))
    assert d["steps"][1]["script"] == "posttrain.py"
    assert d["steps"][1]["inherit"] == ["checkpoints", "data"]
    assert "script" not in d["steps"][0] and "inherit" not in d["steps"][0]


def test_inherit_string_becomes_list():
    d = validate(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "inherit": "checkpoints"}))
    assert d["steps"][1]["inherit"] == ["checkpoints"]


def test_empty_inherit_is_absent():
    d = validate(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "inherit": []}))
    assert "inherit" not in d["steps"][1]


def test_inherit_nested_path_normalized():
    d = validate(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "inherit": ["out//ckpt/"]}))
    assert d["steps"][1]["inherit"] == ["out/ckpt"]


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../../b", "", ".", 3])
def test_inherit_bad_entries(bad):
    errs = errors_for(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "inherit": [bad]}))
    assert "steps[1].inherit" in errs


def test_inherit_duplicates_after_normalizing():
    errs = errors_for(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m",
                                       "inherit": ["ckpt", "ckpt/"]}))
    assert errs["steps[1].inherit"] == "'ckpt' is listed twice"


def test_inherit_must_be_string_or_list():
    errs = errors_for(_steps(TRAIN_A, {"name": "b", "type": "train", "project": "m", "inherit": {"a": 1}}))
    assert errs["steps[1].inherit"] == "must be a path or a list of paths"


def test_inherit_on_first_step():
    errs = errors_for(_steps({"name": "a", "type": "train", "project": "m", "inherit": ["ckpt"]}))
    assert errs["steps[0].inherit"] == "the first step has nothing to inherit from"


@pytest.mark.parametrize("bad", ["../train.py", "/train.py", "", "a/../../t.py", "."])
def test_script_must_be_relative_inside_workspace(bad):
    errs = errors_for(_steps({"name": "a", "type": "train", "project": "m", "script": bad}))
    assert errs["steps[0].script"] == "must be a relative path inside the workspace"


def test_script_must_be_string():
    errs = errors_for(_steps({"name": "a", "type": "train", "project": "m", "script": 5}))
    assert "quote it" in errs["steps[0].script"]


def test_script_not_allowed_on_task():
    errs = errors_for(_steps({"name": "a", "type": "task", "command": "true", "script": "x.py"}))
    assert errs["steps[0].script"] == "unknown key for a task step"


def test_project_task():
    d = validate(_steps(TRAIN_A, {"name": "check", "type": "task", "project": "m", "command": "test -s c/x",
                                  "inherit": ["c"]}))
    assert d["steps"][1] == {"name": "check", "type": "task", "command": "test -s c/x",
                             "timeout_s": 3600, "project": "m", "inherit": ["c"]}


def test_project_task_bad_project_name():
    errs = errors_for(_steps({"name": "t", "type": "task", "project": "a b", "command": "true"}))
    assert "steps[0].project" in errs


def test_plain_task_previous_must_have_path():
    errs = errors_for(_steps(
        {"name": "a", "type": "task", "command": "true"},
        {"name": "b", "type": "train", "project": "m", "inherit": ["ckpt"]}))
    assert errs["steps[1].inherit"] == "'ckpt' isn't an output path of the previous step (a)"


def test_plain_task_passes_on_what_it_inherited():
    validate(_steps(TRAIN_A,
                    {"name": "check", "type": "task", "command": "true", "inherit": ["ckpt"]},
                    {"name": "c", "type": "train", "project": "m", "inherit": ["ckpt"]}))


def test_round_trip_with_new_fields():
    d = validate(_steps(TRAIN_A,
                        {"name": "b", "type": "train", "project": "m", "script": "post.py", "inherit": ["ckpt"]},
                        {"name": "c", "type": "task", "project": "m", "command": "true", "inherit": "ckpt"}))
    text = to_yaml(d)
    assert "script: post.py" in text
    assert validate(parse_text(text)) == d
