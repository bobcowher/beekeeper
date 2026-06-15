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
import requests
from fastmcp import FastMCP

BEEKEEPER_HOST = os.environ.get("BEEKEEPER_HOST", "http://localhost:5000").rstrip("/")
BEEKEEPER_API_KEY = os.environ.get("BEEKEEPER_API_KEY", "")

MCP_VERSION = "0.1.3"

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
def list_projects() -> dict:
    """List all projects and their current setup/training status."""
    return _get("/projects")


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
) -> dict:
    """
    Create a new project. Setup (clone, env creation, pip install) runs automatically
    in the background. Poll get_project until setup_status == 'ready', or call
    retry_setup to block until done.

    output_paths: workspace-relative directories the training script writes to that
    should survive workspace cleanup (e.g. ["saved_models", "exports"]).
    TensorBoard logs are protected automatically — do not include the TB log dir here.
    Defaults to [] (env vars still point at persistent storage; only symlinks differ).
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
    return _post("/projects", body)


@mcp.tool()
def update_project(
    project_name: str,
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
) -> dict:
    """
    Update editable settings for an existing project. Only the fields you provide
    are changed — omitted fields are left as-is. Training must be stopped first.

    env_vars replaces the entire env_vars dict; pass the full desired set of variables.

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
def start_training(project_name: str, branch: str | None = None) -> dict:
    """
    Start training for a project. branch overrides the project's configured default.
    The pre-launch sequence (git sync, pip install) runs first — 30-120s.
    Returns run_id. Use training_status() to confirm the run is active.

    If GPU management is enabled (gpu_enabled=True on the project), a VRAM pre-flight
    check runs before launching. The run is rejected if free VRAM is below
    gpu_memory_minimum. On success, GPU_DEVICE, GPU_OFFLOAD, and GPU_MEMORY_* env vars
    are injected into the training process. See update_project() for full details.
    """
    body = {"branch": branch} if branch else {}
    try:
        return _post(f"/projects/{project_name}/training/start", body, timeout=180)
    except requests.exceptions.ReadTimeout:
        return {
            "success": True,
            "message": "Pre-launch sequence is running. Check training_status in 30s.",
        }


@mcp.tool()
def stop_training(project_name: str, run_id: int | None = None) -> dict:
    """
    Stop a training run. Provide run_id when multiple runs are active on the same project.
    If only one run is active and run_id is omitted, it stops automatically.
    """
    body = {"run_id": run_id} if run_id is not None else {}
    return _post(f"/projects/{project_name}/training/stop", body)


@mcp.tool()
def training_status(project_name: str) -> dict:
    """
    Get all active training runs for a project.
    Returns a list of runs, each with run_id, branch, status, elapsed seconds, pid, tb_port.
    Use run_id values when calling stop_training or get_logs for a specific run.
    """
    return _get(f"/projects/{project_name}/training/status")


# ---------------------------------------------------------------------------
# Logs & Analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def get_logs(project_name: str, run_id: int | None = None, tail: int = 100) -> str:
    """
    Fetch the last N lines of training logs for a specific run.
    Provide run_id when multiple runs are active — required to avoid ambiguity.
    Use tail=500 for more context, tail=50 for a quick check.
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
    for each active run keyed by run_id.
    """
    if run_id is not None:
        tb = _get(f"/projects/{project_name}/tensorboard/latest?run_id={run_id}")
        logs = _get(f"/projects/{project_name}/runs/{run_id}/logs?tail_lines=300")
        return {"run_id": run_id, "tensorboard": tb, "logs": logs}

    status = _get(f"/projects/{project_name}/training/status")
    runs = status.get("data", status).get("runs", [])
    active_ids = [r["run_id"] for r in runs if r.get("status") in ("running", "starting")]

    if len(active_ids) > 1:
        results = {}
        for rid in active_ids:
            tb = _get(f"/projects/{project_name}/tensorboard/latest?run_id={rid}")
            logs = _get(f"/projects/{project_name}/runs/{rid}/logs?tail_lines=300")
            results[str(rid)] = {"run_id": rid, "tensorboard": tb, "logs": logs}
        return {"parallel_runs": results}

    tb = _get(f"/projects/{project_name}/tensorboard/latest")
    logs = _get(f"/projects/{project_name}/logs?tail=300")
    return {"tensorboard": tb, "logs": logs}


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
    """
    return _post(f"/projects/{project_name}/rename", {"new_name": new_name})


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> dict:
    """Get system stats: GPU utilization, VRAM, CPU, and RAM."""
    return _get("/stats")


@mcp.tool()
def check_busy() -> dict:
    """
    Check if any training is running. Returns busy=True/False and running_projects list.
    Prefer get_capacity() for new workflows — it shows available headroom, not just busy/free.
    """
    capacity = _get("/capacity")
    data = capacity.get("data", {})
    running_projects = [
        p["name"] for p in data.get("projects", []) if p.get("running_runs", 0) > 0
    ]
    return {
        "busy": data.get("running", 0) > 0,
        "running_projects": running_projects,
    }


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
              temp (°C), fan (%), power/power_limit (W); empty list if no NVIDIA GPUs

    Use this before starting a new run — it shows headroom AND whether the machine
    is already under load. Prefer over check_busy() for all new agent workflows.
    """
    return _get("/capacity")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
