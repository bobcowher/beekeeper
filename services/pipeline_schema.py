"""Pipeline definition schema: YAML in, strictly-typed dict out.

Validation runs on the parsed value, so YAML's implicit typing (`no` -> False,
`0.001` -> float) is caught here and reported with a "quote it" hint.
"""
import re
import unicodedata

import yaml

from models.project import normalize_folder

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STEP_NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_STEPS = 32
DEFAULT_TASK_TIMEOUT = 3600
MAX_TASK_TIMEOUT = 86400
TOP_KEYS = {"name", "folder", "description", "steps"}
TRAIN_KEYS = {"name", "type", "project", "branch", "env_vars"}
TASK_KEYS = {"name", "type", "command", "timeout_s"}


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
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item)


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


def _validate_step(step, path, err):
    if not isinstance(step, dict):
        err(path, "must be a mapping")
        return None
    name = _str_field(step, "name", path, err, required=True)
    if name is not None and not STEP_NAME_RE.fullmatch(name):
        err(f"{path}.name", "use lowercase letters, digits, '_' or '-' (max 32)")
        name = None
    stype = step.get("type")
    if stype not in ("train", "task"):
        err(f"{path}.type", "must be 'train' or 'task'")
        return None
    allowed = TRAIN_KEYS if stype == "train" else TASK_KEYS
    for key in sorted(set(step) - allowed, key=str):
        err(f"{path}.{key}", f"unknown key for a {stype} step")

    if stype == "train":
        project = _str_field(step, "project", path, err, required=True)
        if project is not None and not PROJECT_NAME_RE.fullmatch(project):
            err(f"{path}.project", "must name a project using only letters, digits, '_' or '-'")
            project = None
        branch = _str_field(step, "branch", path, err)
        branch_ok = branch is None or valid_branch_name(branch)
        if not branch_ok:
            err(f"{path}.branch", "is not a valid branch name")
        env = _env_field(step.get("env_vars", {}), f"{path}.env_vars", err)
        if name is None or project is None or env is None or not branch_ok:
            return None
        out = {"name": name, "type": "train", "project": project, "env_vars": env}
        if branch is not None:
            out["branch"] = branch
        return out

    command = _str_field(step, "command", path, err, required=True)
    if command is not None and not command.strip():
        err(f"{path}.command", "must not be empty")
        command = None
    timeout = step.get("timeout_s", DEFAULT_TASK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TASK_TIMEOUT:
        err(f"{path}.timeout_s", f"must be an integer from 1 to {MAX_TASK_TIMEOUT}")
        timeout = None
    if name is None or command is None or timeout is None:
        return None
    return {"name": name, "type": "task", "command": command, "timeout_s": timeout}


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

    steps = data.get("steps")
    out_steps = []
    if not isinstance(steps, list) or not steps:
        err("steps", "must be a non-empty list")
    elif len(steps) > MAX_STEPS:
        err("steps", f"at most {MAX_STEPS} steps")
    else:
        seen: dict[str, str] = {}
        for i, step in enumerate(steps):
            out = _validate_step(step, f"steps[{i}]", err)
            if out is None:
                continue
            var = step_dir_var(out["name"])
            if var in seen:
                err(f"steps[{i}].name", f"duplicates or collides with step '{seen[var]}' (both use {var})")
                continue
            seen[var] = out["name"]
            out_steps.append(out)

    if errors:
        raise PipelineValidationError(errors)
    return {"name": name, "folder": folder, "description": description, "steps": out_steps}


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)


def to_yaml(defn: dict) -> str:
    out = {"name": defn["name"]}
    if defn.get("folder"):
        out["folder"] = defn["folder"]
    if defn.get("description"):
        out["description"] = defn["description"]
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
