#!/usr/bin/env python3
"""
Beekeeper MCP Server

Exposes Beekeeper's training management API as MCP tools for AI agents.

Configuration (environment variables):
  BEEKEEPER_HOST      Server URL, e.g. http://192.168.1.57:5000
  BEEKEEPER_API_KEY   API key (required only when auth is enabled)

Usage:
  python mcp_server.py

Add to Claude Desktop / Claude Code config:
  {
    "mcpServers": {
      "beekeeper": {
        "command": "python",
        "args": ["/path/to/beekeeper/mcp_server.py"],
        "env": {
          "BEEKEEPER_HOST": "http://192.168.1.57:5000",
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


def _delete(path: str) -> dict:
    url = f"{BEEKEEPER_HOST}/api/v1{path}"
    r = requests.delete(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@mcp.tool()
def list_projects() -> dict:
    """List all projects and their current setup/training status."""
    return _get("/projects")


@mcp.tool()
def get_project(name: str) -> dict:
    """Get detailed info for a single project including config, status, and run history."""
    return _get(f"/projects/{name}")


@mcp.tool()
def create_project(
    name: str,
    git_url: str,
    branch: str = "main",
    python_version: str = "3.12",
    train_file: str = "train.py",
    env_type: str = "venv",
) -> dict:
    """
    Create a new project. Setup (clone, env creation, pip install) runs automatically
    in the background. Poll get_project until setup_status == 'ready', or call
    wait_for_setup to block until done.
    """
    return _post("/projects", {
        "name": name,
        "git_url": git_url,
        "branch": branch,
        "python_version": python_version,
        "train_file": train_file,
        "env_type": env_type,
    })


@mcp.tool()
def delete_project(name: str) -> dict:
    """Delete a project and all its data (workspace, venv, logs). Irreversible."""
    return _delete(f"/projects/{name}")


@mcp.tool()
def retry_setup(name: str, wait: bool = True, timeout_seconds: int = 600) -> dict:
    """
    Retry a failed project setup. If wait=True (default), polls until setup completes
    or fails, returning the final status. If wait=False, returns immediately after
    triggering the retry.
    """
    result = _post(f"/projects/{name}/setup/retry")
    if not wait:
        return result

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(5)
        info = _get(f"/projects/{name}")
        status = info.get("data", {}).get("project", {}).get("setup_status", "unknown")
        if status == "ready":
            return {"success": True, "setup_status": "ready"}
        if status == "error":
            error = info.get("data", {}).get("project", {}).get("setup_error", "unknown")
            return {"success": False, "setup_status": "error", "error": error}

    return {"success": False, "setup_status": "timeout", "error": "Timed out waiting for setup"}


@mcp.tool()
def get_project_instructions(name: str) -> str:
    """
    Fetch project-specific agent instructions as markdown. Contains full context
    about the project's purpose, metrics, and recommended workflows. Read this
    before analyzing or managing a project you haven't worked with before.
    """
    url = f"{BEEKEEPER_HOST}/api/v1/projects/{name}/agent/instructions"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@mcp.tool()
def start_training(name: str) -> dict:
    """
    Start training for a project. The pre-launch sequence (git pull, pip install)
    runs first — this can take 30-120s. Check training_status afterward to confirm
    the run is active.
    """
    try:
        return _post(f"/projects/{name}/training/start", timeout=180)
    except requests.exceptions.ReadTimeout:
        return {
            "success": True,
            "message": "Pre-launch sequence is running. Check training_status in 30s.",
        }


@mcp.tool()
def stop_training(name: str) -> dict:
    """Stop training for a project (SIGTERM, then SIGKILL after 5s if needed)."""
    return _post(f"/projects/{name}/training/stop")


@mcp.tool()
def training_status(name: str) -> dict:
    """Get the current training status for a project."""
    return _get(f"/projects/{name}/training/status")


# ---------------------------------------------------------------------------
# Logs & Analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def get_logs(name: str, tail: int = 100) -> str:
    """
    Fetch the last N lines of training logs. Returns raw log text.
    Use tail=500 for more context, tail=50 for a quick check.
    """
    result = _get(f"/projects/{name}/logs?tail={tail}")
    return result.get("data", {}).get("content", "")


@mcp.tool()
def analyze_run(name: str) -> dict:
    """
    Synthesized analysis of the current or most recent training run.
    Combines TensorBoard metrics (trends, peaks, convergence) with log-based
    episode statistics (reward trends, quartile progression). Use this as the
    primary tool for assessing training progress.
    """
    tb = _get(f"/projects/{name}/tensorboard/latest")
    logs = _get(f"/projects/{name}/logs/analysis")
    return {"tensorboard": tb, "log_analysis": logs}


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

@mcp.tool()
def list_branches(name: str) -> dict:
    """List all remote branches for a project and show which is currently active."""
    return _get(f"/projects/{name}/branches")


@mcp.tool()
def switch_branch(name: str, branch: str) -> dict:
    """
    Switch a project to a different branch. Training must be stopped first.
    The workspace is updated in-place (git fetch + reset).
    """
    return _post(f"/projects/{name}/branch", {"branch": branch})


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
    Check if any project is currently training. Returns busy=True/False and
    a list of running projects. Always call this before starting a new training
    run — the GPU can only handle one job at a time.
    """
    return _get("/busy")


if __name__ == "__main__":
    mcp.run()
