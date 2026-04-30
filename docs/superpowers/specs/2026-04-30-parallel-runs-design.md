# Parallel Runs Design

**Date:** 2026-04-30  
**Branch:** `multi_branch`  
**Status:** Approved

## Problem

Beekeeper is built around a single run per project: one branch, one workspace, one status. When iterating on ML experiments, users want to run two branches simultaneously on the same project — e.g., `feature/ppo-clip` vs `feature/ppo-clip-v2` — without creating a second project.

## Goals

- Run up to N branches in parallel on a single project (per-project opt-in toggle)
- Branch is selected at run-start, not baked into project config
- Run ID is the universal handle — visible in the UI so users can reference it when talking to agents
- MCP surface exposes utilization capacity, not just busy/not-busy
- Backward compatible: single-run projects are unaffected
- Local testing only for this work; no deploys to production

## Non-Goals

- System-wide capacity enforcement (future work)
- Distributed multi-worker execution (future work, but this design is forward-compatible)
- Persistent worker slots (workspaces reused across runs — full clone per run is the chosen approach)

---

## Data Model

### Project model additions (`models/project.py`)

```python
parallel_runs_enabled: bool = False   # off by default; existing projects unaffected
max_parallel_runs: int = 2            # max simultaneous runs when enabled
```

`branch` stays as the project default branch for one-click starts. `train_status` stays as an aggregate status field for dashboard display (computed from active runs). No changes to the run DB schema — run IDs already exist.

---

## Process Manager (`services/process_manager.py`)

### `_running` dict re-keyed

**Before:** `{project_name: info_dict}`  
**After:** `{run_id: info_dict}`

Each `info_dict` gains a `project_name` field and a `workspace_dir` field. All existing fields (process, log_path, tb_port, started_at, run_id) are retained.

Helper added: `_get_runs_for_project(name) -> list[dict]` — filters `_running` by `project_name`. Used everywhere the old `_running.get(name)` pattern appeared.

### Workspace strategy

- Primary workspace stays at `workspace/` — used for the first run if not held by another active run
- Parallel (second+) runs clone fresh to `workspace-{run_id}/`
- On run completion or stop, `workspace-{run_id}/` is deleted if it is not the primary workspace
- Primary `workspace/` is never auto-deleted

This preserves backward compatibility for all existing projects and sets up cleanly for future multi-worker distribution (each worker owns its own workspace path).

### `start_training(projects_dir, name, branch=None)`

- `branch` defaults to `project.branch` if not provided
- Validates `parallel_runs_enabled` and `max_parallel_runs`; returns error if at capacity
- Selects workspace: uses `workspace/` if not held by an active run, otherwise clones fresh to `workspace-{run_id}/`
- Returns `{"run_id": N, "status": "starting"}`

### `stop_training(projects_dir, name, run_id)`

- Looks up `run_id` in `_running`; validates it belongs to `name`
- Sends SIGTERM → 5s wait → SIGKILL as before
- Cleans up `workspace-{run_id}/` after stop if it is not the primary workspace

### Log file naming

Each run writes to its own log file: `projects/{name}/train-{run_id}.log`. The primary workspace run continues to symlink `train.log → train-{run_id}.log` for backward compatibility with any tooling that reads `train.log` directly. Parallel workspace runs only have `train-{run_id}.log`.

The SSE stream endpoint resolves log path from `_running[run_id]["log_path"]` — no ambiguity regardless of workspace.

### `train_status` update logic

- Set to `"running"` when any run for the project starts
- Set to `"stopped"` or `"crashed"` only when the **last** active run for that project finishes
- While multiple runs are active, status stays `"running"` even if one finishes

### Tensorboard with parallel runs

Each run starts its own TB process pointing to its workspace's TB log dir (`workspace-{run_id}/runs/` for parallel runs, `workspace/runs/` for the primary). Each TB process gets its own port. The run row in the UI shows a TB button if a port is available for that run.

**TB log preservation on workspace deletion:** Before a parallel workspace (`workspace-{run_id}/`) is deleted, its TB run directory is moved into the primary workspace's TB base dir:

```
workspace-{run_id}/runs/{timestamp}/  →  workspace/runs/{timestamp}/
```

This means the `tensorboard_dir` value stored in the DB (`runs/{timestamp}`) remains valid — it is now relative to the primary `workspace/` which is never deleted. The primary TB process (watching `workspace/runs/`) picks up the moved data automatically.

Training logs are already safe: they are archived to `run_logs/run-{timestamp}-{id}.log` (project-level, outside any workspace) before the process record is finalized.

---

## API (`routes/api_v1.py`)

### Changed endpoints

| Endpoint | Change |
|---|---|
| `POST /api/v1/projects/{name}/training/start` | Body accepts `{"branch": "..."}` (optional). Returns `{"run_id": N, "status": "starting"}`. |
| `POST /api/v1/projects/{name}/training/stop` | Body accepts `{"run_id": N}`. If omitted and exactly one run active, stops it (backward compat). |
| `GET /api/v1/projects/{name}/training/status` | Returns `{"runs": [{run_id, branch, status, elapsed, pid, tb_port, resources}]}`. |
| `GET /api/v1/projects/{name}/logs/stream` | Accepts `?run_id=N&tail=500`. If omitted and exactly one run active, uses it. |
| `GET /api/v1/projects/{name}/logs` | Accepts `?run_id=N`. Same fallback as above. |

### New endpoint

```
GET /api/v1/capacity
```

Returns system-wide training utilization:

```json
{
  "total_slots": 4,
  "running": 1,
  "available": 3,
  "projects": [
    {"name": "ppo-agent", "running_runs": 1, "max_runs": 2},
    {"name": "sac-agent", "running_runs": 0, "max_runs": 2}
  ]
}
```

`total_slots` = sum of `max_parallel_runs` for projects with `parallel_runs_enabled=True`, plus 1 for each project without it enabled (they still support one run).

---

## MCP Server (`mcp_server.py`)

### Updated tools

**`start_training(name, branch=None)`**  
Branch is now selectable at call time. Defaults to project's configured branch. Returns `run_id` in response.

**`stop_training(name, run_id)`**  
`run_id` required when parallel runs are active. Backward-compatible: if only one run active and `run_id` omitted, stops it.

**`get_logs(name, run_id=None, tail=100)`**  
`run_id` selects which run's logs. If omitted and exactly one run is active, uses it. Agents must specify `run_id` when multiple runs are active.

**`training_status(name)`**  
Returns list of active runs:
```json
{
  "runs": [
    {"run_id": 42, "branch": "feature/ppo-clip", "status": "running", "elapsed": 754, "pid": 18432},
    {"run_id": 43, "branch": "feature/ppo-clip-v2", "status": "running", "elapsed": 242, "pid": 18901}
  ]
}
```

**`check_busy()`**  
Retained for backward compatibility. Delegates to capacity internally.

### New tool

**`get_capacity()`**  
```python
@mcp.tool()
def get_capacity() -> dict:
    """
    System-wide training capacity. Returns total slots, how many are running,
    and how many are available. Use this before starting a new run — it tells
    you not just busy/free but how much headroom exists. Prefer this over
    check_busy() for any new agent workflows.
    """
```

Returns the same shape as `GET /api/v1/capacity`.

---

## Frontend

### Project page — Training section

Run list replaces the single start/stop control:

- Each active run is a row: **status badge** · **branch name** (monospace) · **`#42`** (run ID, small monospace, secondary color) · **elapsed time** · **Stop button** · **▾ Logs toggle**
- Run ID `#42` is the key agent handoff point — small but readable, easy to copy and relay
- ▾ Logs expands an inline terminal panel streaming from `?run_id=42&tail=500` (SSE)
- "Start Run…" button opens a branch picker (remote branch dropdown) and starts a new run
- When `parallel_runs_enabled=False`: same UI, capped at 1 run — no visible change to existing single-run workflows

### Project Settings

- "Parallel Runs" checkbox toggle (maps to `parallel_runs_enabled`)
- When checked: "Max parallel runs" number field appears (maps to `max_parallel_runs`, default 2)

### Dashboard

`train_status` aggregate computation unchanged — "running" if any run active for that project.

---

## Backward Compatibility

| Scenario | Behavior |
|---|---|
| Existing project, `parallel_runs_enabled=False` | Single run only; all existing API shapes still work |
| `stop` with no `run_id`, one run active | Stops it silently (same as today) |
| `get_logs` with no `run_id`, one run active | Returns that run's logs (same as today) |
| `check_busy()` | Still works; delegates to capacity |
| `workspace/` directory | Never auto-deleted; unchanged for primary run |

---

## Testing

All changes tested locally (`make test` + manual verification at http://localhost:5000). No deploy to production until explicitly requested.
