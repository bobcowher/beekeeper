"""Pipeline definition schema: YAML in, strictly-typed dict out.

Validation runs on the parsed value, so YAML's implicit typing (`no` -> False,
`0.001` -> float) is caught here and reported with a "quote it" hint.
"""
import posixpath
import re
import unicodedata

import yaml

from models.project import normalize_folder

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STEP_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)
MAX_STEPS = 32
DEFAULT_TASK_TIMEOUT = 3600
MAX_TASK_TIMEOUT = 86400
TOP_KEYS = {"name", "folder", "description", "keep", "steps"}
KEEP_VALUES = ("on_success",)
TRAIN_KEYS = {"name", "type", "project", "branch", "env_vars", "script", "inherit"}
TASK_KEYS = {"name", "type", "command", "timeout_s", "project", "inherit"}


def valid_branch_name(s) -> bool:
    if not isinstance(s, str) or not s or len(s) > 255:
        return False
    if s[0] in "-/" or s.endswith("/") or s.endswith(".lock"):
        return False
    if ".." in s or "@{" in s:
        return False
    if any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in s):
        return False
    return not any(ch in "~^:?*[\\" for ch in s)


class PipelineValidationError(ValueError):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("; ".join(f"{e['path'] or '(root)'}: {e['message']}" for e in errors))


def step_dir_var(step_name: str) -> str:
    return f"BEEKEEPER_STEP_{step_name.upper().replace('-', '_')}_DIR"


def parse_text(text: str) -> dict:
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        _reject_duplicate_keys(node)
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PipelineValidationError([{"path": "", "message": f"Invalid YAML: {e}"}])
    if not isinstance(data, dict):
        raise PipelineValidationError([{"path": "", "message": "A pipeline must be a mapping"}])
    return data


def _reject_duplicate_keys(node):
    """Reject duplicate YAML mapping keys before safe_load's last-value-wins construction."""
    if isinstance(node, yaml.MappingNode):
        _reject_duplicate_mapping_keys(node)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item)


def _reject_duplicate_mapping_keys(node):
    seen = set()
    for key_node, value_node in node.value:
        key = (key_node.tag, key_node.value) if isinstance(key_node, yaml.ScalarNode) else str(key_node)
        if key in seen:
            label = repr(key_node.value) if isinstance(key_node, yaml.ScalarNode) else "mapping key"
            raise PipelineValidationError([{
                "path": "",
                "message": f"Invalid YAML: duplicate mapping key {label} at line {key_node.start_mark.line + 1}",
            }])
        seen.add(key)
        _reject_duplicate_keys(key_node)
        _reject_duplicate_keys(value_node)


def _not_string(value) -> str:
    return f"must be a string, got {type(value).__name__} {value!r}; quote it in YAML"


def _str_field(step, key, path, err, required=False):
    if key not in step:
        if required:
            err(f"{path}.{key}", "required")
        return None
    value = step[key]
    if not isinstance(value, str):
        err(f"{path}.{key}", _not_string(value))
        return None
    return value


def _env_field(env, path, err):
    if not isinstance(env, dict):
        err(path, 'must be a mapping of NAME: "value"')
        return None
    ok = True
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key):
            err(f"{path}.{key}", "invalid environment variable name")
            ok = False
        elif key.startswith("BEEKEEPER_"):
            err(f"{path}.{key}", "BEEKEEPER_* variables are reserved")
            ok = False
        elif not isinstance(value, str):
            err(f"{path}.{key}", _not_string(value))
            ok = False
    return dict(env) if ok else None


def normalize_rel_path(value) -> str | None:
    """A workspace-relative path with no '..' and no leading '/', normalized; None if it isn't one."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.startswith("/") or ".." in value.split("/"):
        return None
    value = posixpath.normpath(value)
    return None if value in (".", "") else value


def path_within(rel, paths) -> bool:
    """True when rel is one of paths or a folder inside one, e.g. checkpoints/posttrain in checkpoints."""
    return any(rel == p or rel.startswith(p + "/") for p in paths)


def _script_field(step, path, err):
    script = _str_field(step, "script", path, err)
    if script is None:
        return None
    normalized = normalize_rel_path(script)
    if normalized is None:
        err(f"{path}.script", "must be a relative path inside the workspace")
        return False
    return normalized


def _inherit_field(step, path, err):
    """The step's inherit list, normalized. [] when absent, None when invalid."""
    value = step.get("inherit")
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        err(f"{path}.inherit", "must be a path or a list of paths")
        return None
    out = []
    for item in value:
        normalized = normalize_rel_path(item)
        if normalized is None:
            err(f"{path}.inherit", f"{item!r} must be a relative path inside the workspace, with no '..'")
            return None
        if normalized in out:
            err(f"{path}.inherit", f"'{normalized}' is listed twice")
            return None
        out.append(normalized)
    for item in out:
        outer = next((p for p in out if p != item and path_within(item, [p])), None)
        if outer:
            err(f"{path}.inherit", f"'{item}' is inside '{outer}', which is already inherited")
            return None
    return out


def _project_field(step, path, err, required):
    project = _str_field(step, "project", path, err, required=required)
    if project is not None and not PROJECT_NAME_RE.fullmatch(project):
        err(f"{path}.project", "must name a project using only letters, digits, '_' or '-'")
        return False
    return project


def _validated_step_name(step, path, err):
    name = _str_field(step, "name", path, err, required=True)
    if name is not None and not STEP_NAME_RE.fullmatch(name):
        err(f"{path}.name", "use lowercase letters, digits, '_' or '-' (max 32)")
        return None
    return name


def _validate_train_step(step, path, err, name):
    project = _project_field(step, path, err, required=True) or None
    script = _script_field(step, path, err)
    inherit = _inherit_field(step, path, err)
    branch = _str_field(step, "branch", path, err)
    branch_ok = branch is None or valid_branch_name(branch)
    if not branch_ok:
        err(f"{path}.branch", "is not a valid branch name")
    env = _env_field(step.get("env_vars", {}), f"{path}.env_vars", err)
    if name is None or project is None or env is None or not branch_ok or script is False or inherit is None:
        return None
    result = {"name": name, "type": "train", "project": project, "env_vars": env}
    if branch is not None:
        result["branch"] = branch
    if script is not None:
        result["script"] = script
    if inherit:
        result["inherit"] = inherit
    return result


def _validate_task_step(step, path, err, name):
    command = _str_field(step, "command", path, err, required=True)
    if command is not None and not command.strip():
        err(f"{path}.command", "must not be empty")
        command = None
    timeout = step.get("timeout_s", DEFAULT_TASK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TASK_TIMEOUT:
        err(f"{path}.timeout_s", f"must be an integer from 1 to {MAX_TASK_TIMEOUT}")
        timeout = None
    project = _project_field(step, path, err, required=False)
    inherit = _inherit_field(step, path, err)
    if name is None or command is None or timeout is None or project is False or inherit is None:
        return None
    result = {"name": name, "type": "task"}
    if project is not None:
        result["project"] = project   # before the command, so the YAML reads "where, then what"
    result.update(command=command, timeout_s=timeout)
    if inherit:
        result["inherit"] = inherit
    return result


def _validate_step(step, path, err):
    if not isinstance(step, dict):
        err(path, "must be a mapping")
        return None
    name = _validated_step_name(step, path, err)
    stype = step.get("type")
    if stype not in ("train", "task"):
        err(f"{path}.type", "must be 'train' or 'task'")
        return None
    allowed = TRAIN_KEYS if stype == "train" else TASK_KEYS
    for key in sorted(set(step) - allowed, key=str):
        err(f"{path}.{key}", f"unknown key for a {stype} step")

    if stype == "train":
        return _validate_train_step(step, path, err, name)
    return _validate_task_step(step, path, err, name)


def _validate_steps(steps, err):
    if not isinstance(steps, list) or not steps:
        err("steps", "must be a non-empty list")
        return []
    if len(steps) > MAX_STEPS:
        err("steps", f"at most {MAX_STEPS} steps")
        return []
    seen: dict[str, str] = {}
    output = []
    for index, step in enumerate(steps):
        validated = _validate_step(step, f"steps[{index}]", err)
        if validated is None:
            continue
        variable = step_dir_var(validated["name"])
        if variable in seen:
            err(f"steps[{index}].name", f"duplicates or collides with step '{seen[variable]}' (both use {variable})")
            continue
        seen[variable] = validated["name"]
        output.append(validated)
    _check_inherit_order(steps, output, err)
    return output


def _check_inherit_order(raw_steps, steps, err):
    """The project-independent half of the "has path P" rule (services/pipeline_checks.py has the rest).

    Runs only when every step validated, so indexes line up with the YAML.
    """
    if len(steps) != len(raw_steps):
        return
    for index, step in enumerate(steps):
        inherit = step.get("inherit") or []
        if not inherit:
            continue
        if index == 0:
            err("steps[0].inherit", "the first step has nothing to inherit from")
            continue
        prev = steps[index - 1]
        if prev["type"] != "task" or prev.get("project"):
            continue   # a project's output paths: checked against the project
        for rel in inherit:
            if not path_within(rel, prev.get("inherit") or []):
                err(f"steps[{index}].inherit", f"'{rel}' isn't an output path of the previous step ({prev['name']})")


def validate(data, expected_name: str | None = None) -> dict:
    errors: list[dict] = []

    def err(path, message):
        errors.append({"path": path, "message": message})

    if not isinstance(data, dict):
        raise PipelineValidationError([{"path": "", "message": "A pipeline must be a mapping"}])
    for key in sorted(set(data) - TOP_KEYS, key=str):
        err(str(key), "unknown key")

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        err("name", "required; lowercase letters, digits, '_' or '-' (max 64), starting with a letter or digit")
    elif expected_name is not None and name != expected_name:
        err("name", f"must match the pipeline name '{expected_name}'")

    folder = ""
    try:
        folder = normalize_folder(data.get("folder", ""))
    except ValueError as e:
        err("folder", str(e))

    description = data.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        err("description", _not_string(description))

    keep = data.get("keep")
    if keep is not None and keep not in KEEP_VALUES:
        err("keep", "must be on_success, or left out")

    out_steps = _validate_steps(data.get("steps"), err)

    if errors:
        raise PipelineValidationError(errors)
    defn = {"name": name, "folder": folder, "description": description, "steps": out_steps}
    if keep:
        defn["keep"] = keep
    return defn


class _Dumper(yaml.SafeDumper):
    pass


# Plain scalars a YAML 1.2 parser reads as a number, bool or null. PyYAML follows YAML 1.1 and
# leaves some of these unquoted (5e6, 1e-3, 0o17), so another tool would read "5e6" as a float.
_YAML12_NON_STRING = re.compile(
    r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?|0o[0-7]+|0x[0-9a-fA-F]+"
    r"|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)|true|True|TRUE|false|False|FALSE|null|Null|NULL|~")


def _represent_str(dumper, value):
    if "\n" in value:
        style = "|"
    elif _YAML12_NON_STRING.fullmatch(value):
        style = "'"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def to_yaml(defn: dict) -> str:
    out = {"name": defn["name"]}
    if defn.get("folder"):
        out["folder"] = defn["folder"]
    if defn.get("description"):
        out["description"] = defn["description"]
    if defn.get("keep"):
        out["keep"] = defn["keep"]
    steps = []
    for step in defn["steps"]:
        s = dict(step)
        if s.get("type") == "train" and not s.get("env_vars"):
            s.pop("env_vars", None)
        if s.get("type") == "task" and s.get("timeout_s") == DEFAULT_TASK_TIMEOUT:
            s.pop("timeout_s")
        steps.append(s)
    out["steps"] = steps
    return yaml.dump(out, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True)


def draft_yaml(data: dict) -> str:
    """YAML for a definition that may not validate yet, so an editor can switch views mid-edit."""
    try:
        return to_yaml(validate(data))
    except PipelineValidationError:
        return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True)
