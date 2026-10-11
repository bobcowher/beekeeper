import pytest

from services.pipeline_schema import (
    PipelineValidationError, parse_text, step_dir_var, to_yaml, validate,
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


@pytest.mark.parametrize("branch", ["", "   "])
def test_empty_train_branch_rejected(branch):
    errs = errors_for({
        "name": "p",
        "steps": [{"name": "a", "type": "train", "project": "x", "branch": branch}],
    })
    assert "empty" in errs["steps[0].branch"]


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
    ({"name": "p", "steps": [{"name": "a", "type": "task", "command": "x", "project": "y"}]}, "steps[0].project"),
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
