# services/pipeline_store.py
"""Pipeline definitions on disk: pipelines/<name>/pipeline.yaml."""
import os
import shutil
import tempfile

from services.pipeline_schema import NAME_RE, PipelineValidationError, parse_text, to_yaml, validate

PIPELINE_FILE = "pipeline.yaml"


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise FileNotFoundError(name)


def _path(pipelines_dir, name):
    _check_name(name)
    return os.path.join(pipelines_dir, name, PIPELINE_FILE)  # NOSONAR — name validated above


def pipeline_exists(pipelines_dir, name) -> bool:
    try:
        return os.path.isfile(_path(pipelines_dir, name))
    except FileNotFoundError:
        return False


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_pipeline_text(pipelines_dir, name) -> str:
    """The file as written, comments included, whether or not it is valid."""
    with open(_path(pipelines_dir, name)) as f:
        return f.read()


def load_pipeline(pipelines_dir, name) -> tuple[dict, str]:
    text = read_pipeline_text(pipelines_dir, name)
    return validate(parse_text(text), expected_name=name), text


def list_pipelines(pipelines_dir) -> list[dict]:
    out = []
    if not os.path.isdir(pipelines_dir):
        return out
    for name in sorted(os.listdir(pipelines_dir)):
        if not pipeline_exists(pipelines_dir, name):
            continue
        try:
            out.append(load_pipeline(pipelines_dir, name)[0])
        except (OSError, PipelineValidationError) as e:
            out.append({"name": name, "folder": "", "description": "", "steps": [], "error": str(e)})
    return out


def validate_with_projects(data, projects_dir=None, expected_name=None) -> dict:
    """Schema validation, then (with projects_dir) the checks that read project settings."""
    defn = validate(data, expected_name=expected_name)
    if projects_dir is not None:
        from services.pipeline_checks import check_steps
        errors = check_steps(projects_dir, defn["steps"])
        if errors:
            raise PipelineValidationError(errors)
    return defn


def save_pipeline_text(pipelines_dir, name, text, projects_dir=None) -> dict:
    defn = validate_with_projects(parse_text(text), projects_dir, expected_name=name)
    _atomic_write(_path(pipelines_dir, name), text)
    return defn


def save_pipeline(pipelines_dir, defn, expected_name=None, projects_dir=None) -> dict:
    defn = validate_with_projects(defn, projects_dir, expected_name=expected_name)
    _atomic_write(_path(pipelines_dir, defn["name"]), to_yaml(defn))
    return defn


def delete_pipeline_files(pipelines_dir, name):
    shutil.rmtree(os.path.dirname(_path(pipelines_dir, name)), ignore_errors=True)


def pipeline_run_dir(pipelines_dir, name, run_number) -> str:
    _check_name(name)
    return os.path.abspath(os.path.join(pipelines_dir, name, "runs", f"run_{run_number}"))


def rename_project_refs(pipelines_dir, old, new) -> list[str]:
    """Point every pipeline step that uses project `old` at `new`. Rewrites the YAML
    (comments in those files are lost). Returns the pipelines changed."""
    changed = []
    for defn in list_pipelines(pipelines_dir):
        if defn.get("error"):
            continue
        if any(s.get("project") == old for s in defn["steps"]):
            for s in defn["steps"]:
                if s.get("project") == old:   # train steps and project tasks
                    s["project"] = new
            save_pipeline(pipelines_dir, defn)
            changed.append(defn["name"])
    return changed
