#!/usr/bin/env python3
"""
Beekeeper MCP Server

Exposes Beekeeper's training management API as MCP tools for AI agents.

Configuration (environment variables):
  BEEKEEPER_HOST      Server URL, e.g. http://beekeeper.local:5000 or http://localhost:5000
  BEEKEEPER_API_KEY   API key (required only when auth is enabled)

Usage:
  beekeeper-mcp                        # if installed via pip
  python -m beekeeper_mcp              # if installed via pip
  python mcp_server.py                 # direct from repo

Claude Code / Claude Desktop config:
  {
    "mcpServers": {
      "beekeeper": {
        "command": "beekeeper-mcp",
        "env": {
          "BEEKEEPER_HOST": "http://<your-beekeeper-host>:5000",
          "BEEKEEPER_API_KEY": "your-api-key"
        }
      }
    }
  }
"""

import os
import time
from urllib.parse import quote

import requests
from fastmcp import FastMCP

BEEKEEPER_HOST = os.environ.get("BEEKEEPER_HOST", "http://localhost:5000").rstrip("/")
BEEKEEPER_API_KEY = os.environ.get("BEEKEEPER_API_KEY", "")

MCP_VERSION = "0.8.0"

mcp = FastMCP("Beekeeper")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if BEEKEEPER_API_KEY:
        h["Authorization"] = f"Bearer {BEEKEEPER_API_KEY}"
    return h


def _get(path: str) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    r = requests.post(url, json=body or {}, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def _patch(path: str, body: dict | None = None) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    r = requests.patch(url, json=body or {}, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _delete(path: str) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    r = requests.delete(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def _send_yaml(method: str, path: str, text: str) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    headers = {**_headers(), "Content-Type": "application/yaml"}
    r = requests.request(method, url, data=text.encode(), headers=headers, timeout=30)
    if r.status_code == 400:
        return r.json()
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------

@mcp.tool()
def get_version() -> dict:
    """
    Check MCP and server version compatibility.
    Call this at the start of any session to verify your MCP client is up to date.
    Returns mcp_version, server_version, min_mcp_version, and an 'outdated' flag.
    If outdated is true, reinstall: pip install -e /path/to/beekeeper
    """
    from packaging.version import Version
    result = _get("/version")
    server_data = result.get("data", {})
    min_required = server_data.get("min_mcp_version", "0.0.0")
    outdated = Version(MCP_VERSION) < Version(min_required)
    return {
        "mcp_version": MCP_VERSION,
        "server_version": server_data.get("server_version"),
        "min_mcp_version": min_required,
        "outdated": outdated,
        "warning": (
            f"MCP version {MCP_VERSION} is below the minimum required {min_required}. "
            "Reinstall: pip install -e /path/to/beekeeper"
        ) if outdated else None,
    }


# Projects
# ---------------------------------------------------------------------------

@mcp.tool()
def list_projects(folder: str | None = None) -> dict:
    """
    List all projects and their current setup/training status.

    folder: only list projects in this folder; folder="" lists unfiled projects.
    """
    if folder is None:
        return _get("/projects")
    return _get(f"/projects?folder={quote(folder)}")


@mcp.tool()
def list_folders() -> dict:
    """Folder names in use. Folders are flat labels on projects and pipelines."""
    return _get("/folders")


@mcp.tool()
def rename_folder(folder: str, new_name: str) -> dict:
    """Rename a folder by moving all its projects and pipelines to new_name."""
    return _post(f"/folders/{quote(folder)}/rename", {"new_name": new_name})


@mcp.tool()
def get_project(project_name: str) -> dict:
    """Get detailed info for a single project including config, status, and run history."""
    return _get(f"/projects/{project_name}")


@mcp.tool()
def create_project(
    project_name: str,
    git_url: str,
    branch: str = "main",
    python_version: str = "3.12",
    train_file: str = "train.py",
    env_type: str = "venv",
    output_paths: list[str] | None = None,
    folder: str | None = None,
    compute_target: str | None = None,
    setup_script: str | None = None,
) -> dict:
    """
    Create a new project. Setup (clone, env creation, pip install) runs automatically
    in the background. Poll get_project until setup_status == 'ready', or call
    retry_setup to block until done.

    compute_target: "local" (default) or "runpod". Only accepted when Runpod is enabled
    on the server (get_compute_status succeeds). A Runpod project's setup stops after the
    clone: no local env is built, since each pod builds its own at run time.

    setup_script: workspace-relative script run before each training start (and at setup
    for local projects), e.g. "setup.sh" to download a dataset.

    output_paths: workspace-relative directories the training script writes to that
    should survive workspace cleanup (e.g. ["saved_models", "exports"]).
    TensorBoard logs are protected automatically — do not include the TB log dir here.
    Defaults to [] (env vars still point at persistent storage; only symlinks differ).

    folder: dashboard grouping label (flat, no '/'); '' removes it from its folder.
    """
    body: dict = {
        "name": project_name,
        "git_url": git_url,
        "branch": branch,
        "python_version": python_version,
        "train_file": train_file,
        "env_type": env_type,
        "output_paths": output_paths or [],
    }
    if folder is not None:
        body["folder"] = folder
    if compute_target is not None:
        body["compute_target"] = compute_target
    if setup_script is not None:
        body["setup_script"] = setup_script
    return _post("/projects", body)


@mcp.tool()
def update_project(
    project_name: str,  # NOSONAR - S107: MCP tool parameters are its public interface
    branch: str | None = None,
    train_file: str | None = None,
    tensorboard_log_dir: str | None = None,
    requirements_file: str | None = None,
    setup_script: str | None = None,
    env_vars: dict | None = None,
    tb_logs_max_runs: int | None = None,
    run_history_max_runs: int | None = None,
    gpu_enabled: bool | None = None,
    gpu_memory_minimum: int | None = None,
    gpu_memory_preferred: int | None = None,
    data_dir_enabled: bool | None = None,
    data_dir_local: str | None = None,
    data_dir_remote: str | None = None,
    folder: str | None = None,
    compute_target: str | None = None,
    runpod_gpu_types: list[str] | None = None,
    runpod_image: str | None = None,
    data_dir_sync: bool | None = None,
    output_paths_add: list[str] | None = None,
) -> dict:
    """
    Update editable settings for an existing project. Only the fields you provide
    are changed — omitted fields are left as-is. Training must be stopped first.

    env_vars replaces the entire env_vars dict; pass the full desired set of variables.

    folder: dashboard grouping label (flat, no '/'); '' removes it from its folder.
    Folder-only changes are allowed while training.

    output_paths_add: workspace-relative folders to add to the project's output paths
    (existing ones are kept). Every run of the project, standalone or in a pipeline, then
    saves those folders in its run folder. This is the fix validate_pipeline offers for
    INHERIT_PATH_NOT_DECLARED; only make it when the user agrees.

    Static data directory (mounts a read-only system path into the workspace):
      data_dir_enabled — enable the symlink (default: false)
      data_dir_local   — workspace-relative symlink name the training script sees (default: "data")
      data_dir_remote  — absolute path on the server to symlink from; must already exist
                          and be a directory. Required when data_dir_enabled=True.
    Applied immediately if the workspace exists (symlink created/repaired on this call),
    otherwise applied on the next setup or training start.

    GPU memory management (opt-in per project):
      gpu_enabled         — enable GPU management for this project (default: false)
      gpu_memory_minimum  — MB; hard floor; run rejected if free VRAM is below this (0 = no check)
      gpu_memory_preferred — MB; full allocation including offloadable memory like replay buffers
                            (0 = no offload flag); if free VRAM < preferred but >= minimum,
                            GPU_OFFLOAD=1 is injected so the script can offload soft allocations.

    When gpu_enabled=True, these env vars are injected into every training run:
      CUDA_VISIBLE_DEVICES  — physical GPU index (transparent; script always sees cuda:0)
      GPU_DEVICE            — "cuda:0" — use directly with torch.device()
      GPU_MEMORY_FREE       — MB free at launch time
      GPU_MEMORY_MINIMUM    — MB, from project config
      GPU_MEMORY_PREFERRED  — MB, from project config
      GPU_OFFLOAD           — "0" or "1"; "1" means free VRAM < preferred, offload soft allocs

    Typical script pattern:
      buffer_device = "cpu" if os.environ.get("GPU_OFFLOAD") == "1" else "cuda"

    Runpod (only when the server has Runpod enabled; otherwise these are rejected):
      compute_target   — "local" or "runpod". Runpod runs rent a pod per run, sync results
                         back into the normal run folder and terminate the pod. Moving a
                         project back to "local" builds its local env (setup_status goes
                         to pending until it's ready).
      runpod_gpu_types — ordered GPU type ids to try (exact names from list_runpod_gpus);
                         [] means the pool's list, or the cheapest in-stock card with at
                         least gpu_memory_minimum MB (16 GB if unset).
      runpod_image     — container image override; "" means the pool default.
      data_dir_sync    — upload the data dir to the pod before training. Required when the
                         project has a data dir; the GPU bills while it uploads.
    Runpod costs real money. Check get_compute_status for caps and spend before starting
    Runpod runs, and tell the user what a run is likely to cost.
    """
    body = {}
    if branch is not None:
        body["branch"] = branch
    if train_file is not None:
        body["train_file"] = train_file
    if tensorboard_log_dir is not None:
        body["tensorboard_log_dir"] = tensorboard_log_dir
    if requirements_file is not None:
        body["requirements_file"] = requirements_file
    if setup_script is not None:
        body["setup_script"] = setup_script
    if env_vars is not None:
        body["env_vars"] = env_vars
    if tb_logs_max_runs is not None:
        body["tb_logs_max_runs"] = tb_logs_max_runs
    if run_history_max_runs is not None:
        body["run_history_max_runs"] = run_history_max_runs
    if gpu_enabled is not None:
        body["gpu_enabled"] = gpu_enabled
    if gpu_memory_minimum is not None:
        body["gpu_memory_minimum"] = gpu_memory_minimum
    if gpu_memory_preferred is not None:
        body["gpu_memory_preferred"] = gpu_memory_preferred
    if data_dir_enabled is not None:
        body["data_dir_enabled"] = data_dir_enabled
    if data_dir_local is not None:
        body["data_dir_local"] = data_dir_local
    if data_dir_remote is not None:
        body["data_dir_remote"] = data_dir_remote
    if folder is not None:
        body["folder"] = folder
    for key, value in (("compute_target", compute_target), ("runpod_gpu_types", runpod_gpu_types),
                       ("runpod_image", runpod_image), ("data_dir_sync", data_dir_sync)):
        if value is not None:
            body[key] = value
    if output_paths_add is not None:
        body["output_paths_add"] = output_paths_add
    return _patch(f"/projects/{project_name}", body)


@mcp.tool()
def delete_project(project_name: str) -> dict:
    """Delete a project and all its data (workspace, venv, logs). Irreversible."""
    return _delete(f"/projects/{project_name}")


@mcp.tool()
def retry_setup(project_name: str, wait: bool = True, timeout_seconds: int = 600) -> dict:
    """
    Retry a failed project setup. If wait=True (default), polls until setup completes
    or fails, returning the final status. If wait=False, returns immediately after
    triggering the retry.
    """
    result = _post(f"/projects/{project_name}/setup/retry")
    if not wait:
        return result

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(5)
        info = _get(f"/projects/{project_name}")
        status = info.get("data", {}).get("project", {}).get("setup_status", "unknown")
        if status == "ready":
            return {"success": True, "setup_status": "ready"}
        if status == "error":
            error = info.get("data", {}).get("project", {}).get("setup_error", "unknown")
            return {"success": False, "setup_status": "error", "error": error}

    return {"success": False, "setup_status": "timeout", "error": "Timed out waiting for setup"}


@mcp.tool()
def build_local_env(project_name: str, wait: bool = True, timeout_seconds: int = 900) -> dict:
    """
    Build a local environment (venv, setup script, pip install) for a project, keeping
    its workspace. Pipeline project tasks run on this server, so a Runpod project needs
    one before a pipeline can use it in a task step; its training stays on Runpod.
    This is the fix validate_pipeline offers for TASK_NEEDS_LOCAL_ENV; only run it when
    the user agrees. If wait=True (default), polls until setup is ready or fails.
    """
    result = _post(f"/projects/{project_name}/local-env")
    if not wait or not result.get("success"):
        return result

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(5)
        info = _get(f"/projects/{project_name}")
        project = info.get("data", {}).get("project", {})
        status = project.get("setup_status", "unknown")
        if status == "ready":
            return {"success": True, "setup_status": "ready"}
        if status == "error":
            return {"success": False, "setup_status": "error", "error": project.get("setup_error", "unknown")}

    return {"success": False, "setup_status": "timeout", "error": "Timed out waiting for the environment"}


@mcp.tool()
def get_project_instructions(project_name: str) -> str:
    """
    Fetch project-specific agent instructions as markdown. Contains full context
    about the project's purpose, metrics, and recommended workflows. Read this
    before analyzing or managing a project you haven't worked with before.
    """
    url = f"{BEEKEEPER_HOST}/api/v1/projects/{project_name}/agent/instructions"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@mcp.tool()
def start_training(project_name: str, branch: str | None = None,
                   env_vars: dict | None = None, queue: bool = False) -> dict:
    """
    Start training for a project. branch overrides the project's configured default.
    The pre-launch sequence (git sync, pip install) runs first — 30-120s.
    Returns run_id and run_ref. Use training_status() to confirm the run is active.

    run_id is the project's own run number (1, 2, 3, ... per project). When telling the
    user about a run, refer to it by its run_ref (e.g. "mnist-12").

    If GPU management is enabled (gpu_enabled=True on the project), a VRAM pre-flight
    check runs before launching. The run is rejected if free VRAM is below
    gpu_memory_minimum. On success, GPU_DEVICE, GPU_OFFLOAD, and GPU_MEMORY_* env vars
    are injected into the training process. See update_project() for full details.

    env_vars: per-run overrides merged over the project's env vars for this run only.
    queue=True: don't start now; add to the project's run queue. It starts automatically
    (within ~5s) when the project has a free slot. Returns queue_id and position.
    A start refused because the project is busy (error.details.code ALREADY_RUNNING /
    AT_CAPACITY / INSUFFICIENT_VRAM) can be retried with queue=True.

    Runpod projects (compute_target="runpod") start a rented pod, which costs money.
    Refusals that clear on their own and can be queued: SOFT_CAP (a spend cap was reached;
    waits for the next day/month) and NO_GPU_AVAILABLE. Refusals that need a settings
    change: NO_SPEND_CAP, RUNPOD_NOT_CONFIGURED, DATA_DIR_NOT_SYNCED, UPLOAD_TOO_LARGE,
    REMOTE_CONDA_UNSUPPORTED. While a Runpod run is active, training_status shows a
    "node" object with gpu_type, phase (provision/upload/prepare/train/finalize) and
    cost_per_hr.
    """
    body = {}
    if branch:
        body["branch"] = branch
    if env_vars:
        body["env_vars"] = env_vars
    if queue:
        body["queue"] = True
    try:
        return _post(f"/projects/{project_name}/training/start", body, timeout=180)
    except requests.exceptions.ReadTimeout:
        if queue:
            return {
                "success": True,
                "message": (
                    "The request timed out and may have been queued. "
                    f"Call list_queue({project_name!r}) to check before retrying."
                ),
            }
        return {
            "success": True,
            "message": "Pre-launch sequence is running. Check training_status in 30s.",
        }


@mcp.tool()
def list_queue(project_name: str) -> dict:
    """Runs queued for a project, in start order, with any wait_reason."""
    return _get(f"/projects/{project_name}/queue")


@mcp.tool()
def cancel_queued_run(project_name: str, queue_id: int) -> dict:
    """Cancel a queued run before it starts."""
    return _delete(f"/projects/{project_name}/queue/{queue_id}")


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------

@mcp.tool()
def list_pipelines(folder: str | None = None) -> dict:
    """List pipelines with their latest run. folder filters ("" = unfiled)."""
    return _get("/pipelines" if folder is None else f"/pipelines?folder={quote(folder)}")


@mcp.tool()
def get_pipeline(name: str) -> dict:
    """Pipeline definition (parsed) and its raw YAML."""
    return _get(f"/pipelines/{name}")


_FIX_NOTE = """
    Validation also checks the projects. An error that one call can fix has an error_code
    and a fix object naming that call, for example
      {"path": "steps[2].inherit", "error_code": "INHERIT_PATH_NOT_DECLARED",
       "fix": {"call": "update_project", "project": "my-model", "output_paths_add": ["checkpoints"]}}
      {"path": "steps[1].project", "error_code": "TASK_NEEDS_LOCAL_ENV",
       "fix": {"call": "build_local_env", "project": "my-model"}}
    Nothing is changed until you make that call, which is a separate tool call. Ask the
    user first: an added output path is saved by every later run of that project.
"""


def _with_fix_note(fn):
    fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n" + _FIX_NOTE
    return fn


@mcp.tool()
@_with_fix_note
def create_pipeline(definition_yaml: str) -> dict:
    """Create a pipeline from YAML. Returns validation errors on failure.

    Pipeline YAML:
      name: my-model-factory       # lowercase, digits, '_' '-'
      folder: chess                # optional
      steps:
        - name: fetch-data
          type: task
          project: my-model        # project task: runs in my-model's workspace on this server
          command: bash scripts/download.sh   # writes data/ (an output path of my-model)
        - name: pretrain           # unique step name
          type: train
          project: my-model        # an existing project; the run is queued on it
          script: pretrain.py      # optional; defaults to the project's train_file
          inherit: [data]          # optional; output paths copied from the step above
          branch: main             # optional
          env_vars: {LR: "1e-5"}   # optional, values must be quoted strings
        - name: check
          type: task
          project: my-model
          inherit: [checkpoints, data]
          command: test -s checkpoints/pretrained.pt
        - name: posttrain
          type: train
          project: my-model
          script: posttrain.py
          inherit: [checkpoints, data]
        - name: export
          type: task               # plain task (no project): shell command on the Beekeeper host
          inherit: [checkpoints]
          timeout_s: 600           # optional, default 3600
          command: cp checkpoints/posttrained.pt /models/my-model.pt

    inherit: copies the named output paths from the previous step's run folder into this
    step's fresh run folder, at the same relative paths, so the script opens
    checkpoints/pretrained.pt with no env vars or absolute paths. Only the step directly
    above is a source, and each path must be an output path of both projects, or a folder
    inside one (checkpoints/posttrain copies just that folder; a plain task has the paths
    it inherited). It works the same on this server and on Runpod;
    on Runpod the inherited files count toward the upload limit.

    Task commands run with bash -o pipefail, so a failure anywhere in a pipe fails the step.

    Project tasks (a task with project:) are runs of that project, kind "task": they go
    through its queue, get a run folder and output-path links, run with its env on PATH
    and no GPU, and always run on this server. A plain task with inherit runs in its own
    step folder; without inherit it runs in the pipeline run folder.

    Steps without inherit also get BEEKEEPER_PREV_STEP_DIR and BEEKEEPER_STEP_<NAME>_DIR
    (absolute folders of earlier steps). Steps with inherit don't. Every step gets
    BEEKEEPER_PIPELINE, BEEKEEPER_PIPELINE_RUN_ID and BEEKEEPER_PIPELINE_RUN_DIR.
    A step that exits non-zero fails the pipeline; later steps are skipped.
    """
    return _send_yaml("POST", "/pipelines", definition_yaml)


@mcp.tool()
@_with_fix_note
def validate_pipeline(definition_yaml: str) -> dict:
    """Validate pipeline YAML without saving it. Returns normalized YAML or validation errors.

    Field reference (script, inherit, project tasks): see create_pipeline.
    """
    return _send_yaml("POST", "/pipelines/validate", definition_yaml)


@mcp.tool()
@_with_fix_note
def update_pipeline(name: str, definition_yaml: str) -> dict:
    """Replace a pipeline's YAML. Running pipeline runs keep the definition they started with.

    Field reference (script, inherit, project tasks): see create_pipeline.
    """
    return _send_yaml("PUT", f"/pipelines/{name}", definition_yaml)


@mcp.tool()
def delete_pipeline(name: str) -> dict:
    """Delete a pipeline and its run history (training runs are kept). Refused while running."""
    return _delete(f"/pipelines/{name}")


@mcp.tool()
def start_pipeline(name: str, branches: dict[str, str] | None = None) -> dict:
    """Start or queue a pipeline run. Returns run_id, run_ref and status.

    branches optionally maps each train step name to the branch it should use. Omit a
    step (or give it an empty string) for Default: the YAML branch, then the project's
    branch. If this pipeline is already active, the new run waits in its FIFO queue.
    """
    if branches is None:
        return _post(f"/pipelines/{name}/runs")
    return _post(f"/pipelines/{name}/runs", {"branches": branches})


@mcp.tool()
def pipeline_status(name: str, run_id: int | None = None) -> dict:
    """Status of a pipeline run, with per-step status, training run refs, wait reasons
    and errors. Without run_id: the latest run that started (queued runs are skipped;
    list_pipeline_runs shows the queue)."""
    if run_id is None:
        return _get(f"/pipelines/{name}/runs/latest")
    return _get(f"/pipelines/{name}/runs/{run_id}")


@mcp.tool()
def list_pipeline_runs(name: str, limit: int = 20) -> dict:
    """List recent runs for a pipeline, newest first. limit is capped by the server at 200."""
    return _get(f"/pipelines/{name}/runs?limit={limit}")


@mcp.tool()
def cancel_pipeline(name: str, run_id: int) -> dict:
    """Cancel a running pipeline run and stop its current step.

    If the run finishes during cancellation, the returned status is its true terminal
    state ("succeeded" or "failed"), not "cancelled".
    """
    return _post(f"/pipelines/{name}/runs/{run_id}/cancel")


@mcp.tool()
def resume_pipeline(name: str, run_id: int, from_step: str | None = None) -> dict:
    """Resume or re-run the latest started pipeline run.

    Without from_step, a failed/cancelled run resumes at the step that stopped it. With
    from_step, that named step and every later step run again as new attempts in the same
    run, while earlier outputs are reused; this also works for succeeded runs. Only the
    latest started run of a pipeline can be resumed or re-run.
    """
    path = f"/pipelines/{name}/runs/{run_id}/resume"
    if from_step is None:
        return _post(path)
    return _post(path, {"from_step": from_step})


@mcp.tool()
def dismiss_pipeline_run(name: str, run_id: int) -> dict:
    """A failed run pauses its pipeline's queue. Dismiss it (without resuming) to let the
    next queued run start. Only the latest started run can be dismissed."""
    return _post(f"/pipelines/{name}/runs/{run_id}/dismiss")


@mcp.tool()
def keep_pipeline_run(name: str, run_id: int, keep: bool = True) -> dict:
    """Keep a pipeline run: the runs its steps made are never pruned from their projects'
    history. keep=False hands them back to normal pruning. A pipeline with
    `keep: on_success` in its YAML keeps each run that succeeds automatically."""
    return _post(f"/pipelines/{name}/runs/{run_id}/keep", {"keep": keep})


@mcp.tool()
def get_step_log(name: str, run_id: int, step_name: str, tail: int = 200) -> dict:
    """Log of a task step. For train steps use get_logs(project, run_id) with the step's run_id."""
    return _get(f"/pipelines/{name}/runs/{run_id}/steps/{step_name}/log?tail={tail}")


@mcp.tool()
def stop_training(project_name: str, run_id: int | None = None) -> dict:
    """
    Stop a training run. Provide run_id when multiple runs are active on the same project.
    If only one run is active and run_id is omitted, it stops automatically.

    run_id is the project's own run number (1, 2, 3, ... per project). When telling the
    user about a run, refer to it by its run_ref (e.g. "mnist-12").
    """
    body = {"run_id": run_id} if run_id is not None else {}
    return _post(f"/projects/{project_name}/training/stop", body)


@mcp.tool()
def training_status(project_name: str) -> dict:
    """
    Get all active training runs for a project.
    Returns a list of runs, each with run_id, run_ref, branch, status, elapsed seconds, pid, tb_port.
    Use run_id values when calling stop_training or get_logs for a specific run.

    run_id is the project's own run number (1, 2, 3, ... per project). When telling the
    user about a run, refer to it by its run_ref (e.g. "mnist-12").
    """
    return _get(f"/projects/{project_name}/training/status")


@mcp.tool()
def set_run_notable(project_name: str, run_id: int, notable: bool = True) -> dict:
    """Mark a run notable so history pruning never deletes it (its record, log and run
    folder), or clear the mark. To keep every run a pipeline run made, use
    keep_pipeline_run instead."""
    return _patch(f"/projects/{project_name}/runs/{run_id}", {"notable": notable})


# ---------------------------------------------------------------------------
# Logs & Analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def get_logs(project_name: str, run_id: int | None = None, tail: int = 100) -> str:
    """
    Fetch the last N lines of training logs for a run.
    Omit run_id for the project's latest run; pass it to read an earlier one.
    Use tail=500 for more context, tail=50 for a quick check.

    run_id is the project's own run number (1, 2, 3, ... per project). When telling the
    user about a run, refer to it by its run_ref (e.g. "mnist-12").
    """
    params = f"tail={tail}"
    if run_id is not None:
        params += f"&run_id={run_id}"
    result = _get(f"/projects/{project_name}/logs?{params}")
    return result.get("data", {}).get("content", "")


@mcp.tool()
def analyze_run(project_name: str, run_id: int | None = None) -> dict:
    """
    Synthesized analysis of a training run. Returns TensorBoard metrics (trends,
    peaks, convergence) plus a raw log tail. Interpret the log content yourself —
    Beekeeper does not parse or summarize log text. Use get_logs for a larger
    tail or download the full log via the run's log download endpoint if needed.

    When parallel runs are active and no run_id is given, returns an analysis
    for each active run keyed by run_id. Every result carries run_id and run_ref.

    run_id is the project's own run number (1, 2, 3, ... per project). When telling the
    user about a run, refer to it by its run_ref (e.g. "mnist-12").
    """
    if run_id is not None:
        run_meta = _get(f"/projects/{project_name}/runs/{run_id}")
        run_data = run_meta.get("data", {}).get("run", {})
        tb = _get(f"/projects/{project_name}/tensorboard/latest?run_id={run_id}")
        logs = _get(f"/projects/{project_name}/runs/{run_id}/logs?tail_lines=300")
        return {
            "run_id": run_id,
            "run_ref": run_data.get("run_ref"),
            "branch": run_data.get("branch"),
            "commit_sha": run_data.get("commit_sha"),
            "status": run_data.get("status"),
            "started_at": run_data.get("started_at"),
            "ended_at": run_data.get("ended_at"),
            "duration_seconds": run_data.get("duration_seconds"),
            "tensorboard": tb,
            "logs": logs,
        }

    status = _get(f"/projects/{project_name}/training/status")
    runs = status.get("data", status).get("runs", [])
    active_ids = [r["run_id"] for r in runs if r.get("status") in ("running", "starting")]

    if len(active_ids) > 1:
        results = {}
        for rid in active_ids:
            run_meta = _get(f"/projects/{project_name}/runs/{rid}")
            run_data = run_meta.get("data", {}).get("run", {})
            tb = _get(f"/projects/{project_name}/tensorboard/latest?run_id={rid}")
            logs = _get(f"/projects/{project_name}/runs/{rid}/logs?tail_lines=300")
            results[str(rid)] = {
                "run_id": rid,
                "run_ref": run_data.get("run_ref"),
                "branch": run_data.get("branch"),
                "commit_sha": run_data.get("commit_sha"),
                "status": run_data.get("status"),
                "tensorboard": tb,
                "logs": logs,
            }
        return {"parallel_runs": results}

    # Single active run — get branch from the active run list entry
    active_run = runs[0] if runs else {}
    tb = _get(f"/projects/{project_name}/tensorboard/latest")
    logs = _get(f"/projects/{project_name}/logs?tail=300")
    if active_run:
        run_id_out = active_run.get("run_id")
        run_ref_out = active_run.get("run_ref")
    else:
        # Idle: the tensorboard-latest response identifies the run it analyzed.
        tb_data = tb.get("data", {}) if isinstance(tb, dict) else {}
        run_id_out = tb_data.get("run_id")
        run_ref_out = tb_data.get("run_ref")
    return {
        "run_id": run_id_out,
        "run_ref": run_ref_out,
        "branch": active_run.get("branch"),
        "commit_sha": active_run.get("commit_sha"),
        "status": active_run.get("status"),
        "tensorboard": tb,
        "logs": logs,
    }


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@mcp.tool()
def list_branches(project_name: str) -> dict:
    """List all remote branches for a project and show which is currently active."""
    return _get(f"/projects/{project_name}/branches")


@mcp.tool()
def switch_branch(project_name: str, branch: str) -> dict:
    """
    Switch a project to a different branch. Training must be stopped first.
    The workspace is updated in-place (git fetch + reset).
    """
    return _post(f"/projects/{project_name}/branch", {"branch": branch})


@mcp.tool()
def rename_project(project_name: str, new_name: str) -> dict:
    """
    Rename a project. Updates the directory name and all run history records.

    Cannot rename while setup or training is active — stop training first.
    new_name must contain only letters, numbers, hyphens, and underscores.

    For conda projects, this destroys the old conda env and rebuilds it under
    the new name in the background (slow — can take several minutes). The
    response's setup_status will be "pending"; poll get_project until it's
    "ready" before starting training. venv projects rename instantly with no
    rebuild.
    """
    return _post(f"/projects/{project_name}/rename", {"new_name": new_name})


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> dict:
    """
    Get system stats: GPU utilization, VRAM, CPU, and RAM.

    Each entry in gpus[] includes compute_capability (e.g. "8.6"). gpu_platform
    gives host-wide info shared across all GPUs (one driver per host):
      platform          — "nvidia", "rocm", or "none"
      driver_version    — installed driver version
      max_cuda_version  — highest CUDA version the driver supports (nvidia only;
                           what `nvidia-smi`'s header shows — not any CUDA toolkit
                           version installed inside a project's venv)
      rocm_version      — best-effort, rocm-smi based (rocm only)
    Check this before picking a PyTorch/JAX build for a project's requirements.
    """
    return _get("/stats")


@mcp.tool()
def check_busy() -> dict:
    """
    Check if any training, project setup, or pipeline shell task is running.
    Returns the busy endpoint fields plus current capacity information.
    Prefer get_capacity() for new workflows — it shows available headroom, not just busy/free.
    """
    busy = _get("/busy").get("data", {})
    capacity = _get("/capacity")
    return {**busy, "capacity": capacity.get("data", {})}


@mcp.tool()
def get_capacity() -> dict:
    """
    System-wide training capacity and current resource utilization.

    Returns:
      - total_slots / running / available: aggregate slot counts across all projects
      - projects: per-project breakdown (running_runs, max_runs)
      - cpu: percent utilization, core count, current frequency (MHz)
      - memory: percent used, used_gb, total_gb (system RAM)
      - gpus: list of GPU dicts — index, name, gpu_util (%), mem_used/total/percent,
              temp (°C), fan (%), power/power_limit (W), compute_capability
              (e.g. "8.6"); empty list if no GPUs detected
      - gpu_platform: host-wide info shared across all GPUs — platform ("nvidia",
              "rocm", or "none"), driver_version, max_cuda_version (highest CUDA
              the driver supports, nvidia only), rocm_version (best-effort, rocm only)

    Use this before starting a new run — it shows headroom AND whether the machine
    is already under load. Prefer over check_busy() for all new agent workflows.
    Also check gpu_platform before picking a PyTorch/JAX build for requirements.txt.
    """
    return _get("/capacity")


# ---------------------------------------------------------------------------
# Compute providers (Runpod)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_compute_status() -> dict:
    """
    Runpod pool status, if the server has Runpod enabled (404 NOT_FOUND otherwise).

    Returns settings (cloud, GPU list, limits; never the API key), spend for each period
    (daily, monthly, total) with its soft and hard caps, burn_rate ($/hr of live pods),
    pods (one per active Runpod run: run_ref, gpu_type, phase, cost_per_hr, spent so far)
    and recent finished Runpod runs.

    Soft cap reached: no new pods start, running ones continue.
    Hard cap reached: Beekeeper stops every Runpod run and terminates its pod.
    """
    return _get("/compute/runpod")


@mcp.tool()
def list_runpod_gpus(cloud: str | None = None) -> dict:
    """
    Runpod GPU types with live availability (NONE/LOW/MEDIUM/HIGH), VRAM and $/hr for the
    pool's cloud, or cloud="COMMUNITY"/"SECURE". In-stock types come first, cheapest first.
    Use the exact id values for update_project(runpod_gpu_types=[...]).
    """
    return _get("/compute/runpod/gpus" + (f"?cloud={quote(cloud)}" if cloud else ""))


@mcp.tool()
def terminate_pod(pod_name: str) -> dict:
    """
    Stop the Runpod run on this pod and terminate it (pod_name from get_compute_status).
    Results get a short final sync first, so this is like stop_training but also works
    for a stray pod no run owns. Use stop_training for an ordinary stop.
    """
    return _post(f"/compute/runpod/pods/{quote(pod_name)}/terminate")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
