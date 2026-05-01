# Parallel Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow a project to run multiple branches in parallel, with run ID as the universal handle across UI, API, and MCP.

**Architecture:** Re-key `_running` from `{project_name}` to `{run_id}`. Pre-create the DB run record in `start_training()` so `run_id` is available immediately for slot reservation and the API response. Each parallel run gets a full fresh clone at `workspace-{run_id}/`; TB logs are migrated to the primary workspace before the clone is deleted. The frontend replaces the single start/stop control with a run list; each row shows the run ID for agent handoff.

**Tech Stack:** Python/Flask, SQLite (db_service), Jinja2 templates, vanilla JS (training.js), SSE log streaming. Gemini CLI used as headless code reviewer after major task groups.

**Spec:** `docs/superpowers/specs/2026-04-30-parallel-runs-design.md`

**Branch:** `multi_branch` — test locally only, no deploys.

---

## File Map

| File | Change |
|---|---|
| `models/project.py` | Add `parallel_runs_enabled`, `max_parallel_runs` |
| `services/db_service.py` | Extend `update_training_run` allowed fields |
| `services/process_manager.py` | Re-key `_running`, new helpers, updated start/stop/status |
| `routes/api_v1.py` | Update training endpoints, add `/capacity` |
| `routes/training.py` | `logs/stream` accepts `?run_id=N` |
| `mcp_server.py` | Update tools, add `get_capacity()` |
| `templates/edit_project.html` | Add parallel runs toggle + max field |
| `templates/project.html` | Replace Controls section with run list |
| `static/js/training.js` | Rewrite for run list model |
| `static/css/style.css` | Add run row + inline log panel styles |
| `tests/test_model.py` | New field tests |
| `tests/test_process_manager.py` | Update for new signatures |
| `tests/test_api.py` | Update for new response shapes, add capacity test |

---

## Task 1: Project model — parallel run fields

**Files:**
- Modify: `models/project.py`
- Modify: `tests/test_model.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_model.py`:

```python
def test_parallel_runs_defaults(tmp_path):
    """New fields default to off / 2."""
    from models.project import Project
    p = Project(name="x", git_url="https://example.com/repo.git")
    assert p.parallel_runs_enabled is False
    assert p.max_parallel_runs == 2

def test_parallel_runs_persisted(tmp_path):
    """parallel_runs_enabled and max_parallel_runs round-trip through JSON."""
    from models.project import Project
    p = Project(name="x", git_url="https://example.com/repo.git",
                parallel_runs_enabled=True, max_parallel_runs=3)
    p.save(str(tmp_path))
    loaded = Project.load(str(tmp_path / "x" / "project.json"))
    assert loaded.parallel_runs_enabled is True
    assert loaded.max_parallel_runs == 3

def test_old_project_json_loads_without_parallel_fields(tmp_path):
    """Existing project.json files without the new fields load with defaults."""
    import json, os
    proj_dir = tmp_path / "old"
    proj_dir.mkdir()
    data = {"name": "old", "git_url": "https://example.com/repo.git",
            "branch": "main", "python_version": "3.11", "train_file": "train.py",
            "tensorboard_log_dir": "runs", "requirements_file": "requirements.txt",
            "env_type": "venv", "setup_script": "", "data_dir_enabled": False,
            "data_dir_local": "data", "data_dir_remote": "", "setup_status": "pending",
            "setup_error": "", "train_status": "idle", "train_pid": 0, "env_vars": {},
            "pinned": False, "last_run_at": 0.0, "tb_logs_max_runs": 10,
            "run_history_max_runs": 10}
    (proj_dir / "project.json").write_text(json.dumps(data))
    from models.project import Project
    p = Project.load(str(proj_dir / "project.json"))
    assert p.parallel_runs_enabled is False
    assert p.max_parallel_runs == 2
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd /home/robertcowher/pythonprojects/beekeeper && make test 2>&1 | tail -20
```
Expected: FAIL — `parallel_runs_enabled` and `max_parallel_runs` not defined.

- [ ] **Step 3: Add fields to Project dataclass**

In `models/project.py`, add after `run_history_max_runs`:

```python
    parallel_runs_enabled: bool = False
    max_parallel_runs: int = 2
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
make test 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add models/project.py tests/test_model.py
git commit -m "feat: add parallel_runs_enabled and max_parallel_runs to Project model"
```

---

## Task 2: DB service — extend update_training_run

**Files:**
- Modify: `services/db_service.py`

`update_training_run` currently only allows `ended_at, duration_seconds, status, exit_code, log_file_path, tensorboard_dir`. We need to also allow metadata fields so `_execute_training` can update the pre-created record after git sync.

- [ ] **Step 1: Extend allowed_fields**

In `services/db_service.py`, find `update_training_run` and change the `allowed_fields` list:

```python
    def update_training_run(self, run_id: int, **fields):
        """Update run fields (status, timing, metadata, etc.)."""
        allowed_fields = [
            'ended_at', 'duration_seconds', 'status', 'exit_code',
            'log_file_path', 'tensorboard_dir',
            'commit_sha', 'commit_message', 'python_version', 'gpu_info', 'hostname',
        ]
```

- [ ] **Step 2: Run tests to confirm nothing broken**

```bash
make test 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add services/db_service.py
git commit -m "feat: extend update_training_run to allow metadata field updates"
```

---

## Task 3: Process manager — re-key _running and add helpers

**Files:**
- Modify: `services/process_manager.py`
- Modify: `tests/test_process_manager.py`

This task re-keys `_running` from `{project_name: info}` to `{run_id: info}` and adds `_get_runs_for_project`, `get_runs_for_project`, and updates `get_training_status` to remain backward-compatible. Does NOT change `start_training` or `stop_training` signatures yet — that comes in Tasks 4 and 6.

- [ ] **Step 1: Add new test for get_runs_for_project**

Add to `tests/test_process_manager.py`:

```python
def test_get_runs_for_project_empty(tmp_path):
    """Returns empty list when no runs active."""
    from services import process_manager
    # Ensure clean state
    process_manager._running.clear()
    from services.process_manager import get_runs_for_project
    assert get_runs_for_project("nonexistent") == []

def test_get_runs_for_project_returns_active(tmp_path):
    """Returns one entry per active run for the project."""
    from services import process_manager
    process_manager._running.clear()
    mock_proc = MagicMock()
    mock_proc.pid = 1234
    process_manager._running[42] = {
        "process": mock_proc,
        "starting": False,
        "project_name": "myproject",
        "run_id": 42,
        "branch": "main",
        "workspace_dir": "/fake/workspace",
        "log_path": "/fake/train.log",
        "tb_port": None,
        "started_at": 0.0,
    }
    from services.process_manager import get_runs_for_project
    with patch("services.process_manager.get_process_resources", return_value=None):
        runs = get_runs_for_project("myproject")
    assert len(runs) == 1
    assert runs[0]["run_id"] == 42
    assert runs[0]["branch"] == "main"
    assert runs[0]["status"] == "running"
    process_manager._running.clear()
```

- [ ] **Step 2: Run to confirm failure**

```bash
make test 2>&1 | grep -E "FAILED|ERROR|passed|failed" | tail -10
```
Expected: FAIL — `get_runs_for_project` not defined.

- [ ] **Step 3: Re-key _running and add helpers**

In `services/process_manager.py`, change the `_running` declaration comment and add helpers. Replace the existing `_running = {}` line and the `get_training_status` function:

```python
_running = {}  # {run_id: info_dict} — keyed by DB run ID (int)
```

Add these functions after `_tb_idle_reaper`:

```python
def _get_runs_for_project(name: str) -> list:
    """Internal: return all active info dicts for a project (holds _lock NOT required — callers handle locking)."""
    return [info for info in _running.values() if info.get("project_name") == name]


def get_runs_for_project(name: str) -> list:
    """Return active run summary dicts for a project. Safe for API/MCP consumers."""
    now = time.time()
    result = []
    with _lock:
        for run_id, info in _running.items():
            if info.get("project_name") != name:
                continue
            if info.get("starting"):
                result.append({
                    "run_id": run_id,
                    "branch": info.get("branch", ""),
                    "status": "starting",
                    "pid": None,
                    "elapsed": None,
                    "tb_port": None,
                    "resources": None,
                })
            else:
                proc = info.get("process")
                pid = proc.pid if proc else None
                from services.resource_tracker import get_process_resources
                resources = get_process_resources(pid) if pid else None
                result.append({
                    "run_id": run_id,
                    "branch": info.get("branch", ""),
                    "status": "running",
                    "pid": pid,
                    "elapsed": now - info.get("started_at", now),
                    "tb_port": info.get("tb_port"),
                    "resources": resources,
                })
    return result
```

Replace `get_training_status` entirely:

```python
def get_training_status(name: str) -> dict:
    """Backward-compat: return status for the first active run, or idle."""
    runs = get_runs_for_project(name)
    if runs:
        r = runs[0]
        return {
            "status": r["status"],
            "pid": r["pid"],
            "run_id": r["run_id"],
            "started_at": None,
            "tb_port": r["tb_port"],
            "elapsed": r["elapsed"],
            "resources": r["resources"],
        }
    # Check standalone TB
    with _lock:
        tb_info = _tb_running.get(name)
        if tb_info:
            tb_info["last_access"] = time.time()
            tb_port = tb_info.get("tb_port")
        else:
            tb_port = None
    return {
        "status": "idle",
        "pid": None,
        "started_at": None,
        "tb_port": tb_port,
        "elapsed": None,
        "resources": None,
    }
```

Also add a log-path helper used by the SSE endpoint:

```python
def get_run_log_path(projects_dir: str, name: str, run_id: int = None) -> str:
    """Return the active log file path for a run. Falls back to train.log."""
    with _lock:
        if run_id is not None:
            info = _running.get(run_id)
            if info and info.get("log_path"):
                return info["log_path"]
        else:
            active = [
                info for info in _running.values()
                if info.get("project_name") == name and not info.get("starting")
            ]
            if len(active) == 1 and active[0].get("log_path"):
                return active[0]["log_path"]
    return os.path.join(projects_dir, name, "train.log")
```

- [ ] **Step 4: Update _monitor_process signature**

Change `def _monitor_process(projects_dir, name):` to:

```python
def _monitor_process(projects_dir, name, run_id):
    """Background thread that waits for the training process to exit."""
    while True:
        with _lock:
            info = _running.get(run_id)
            if not info:
                return
            proc = info["process"]

        ret = proc.poll()
        if ret is not None:
            log_path = None
            started_at = None
            with _lock:
                info = _running.get(run_id)
                if info:
                    log_path = info.get("log_path")
                    started_at = info.get("started_at")
                    tb = info.get("tb_process")
                    tb_port = info.get("tb_port")
                    if tb and tb.poll() is None and tb_port:
                        _tb_running[name] = {
                            "tb_process": tb,
                            "tb_port": tb_port,
                            "last_access": time.time(),
                        }
                        log.info("Migrated TB for %s run %d to standalone (port %d)", name, run_id, tb_port)
                    del _running[run_id]

            if log_path and started_at:
                _append_run_footer(log_path, ret, started_at)

            archived_log_path = None
            if log_path and os.path.isfile(log_path):
                archived_log_path = _archive_run_log(projects_dir, name, run_id, log_path)

            if started_at:
                _finalize_run_record(run_id, ret, started_at, archived_log_path)

            if ret == 0:
                import threading as _threading
                from services import tensorboard_service
                _threading.Thread(
                    target=tensorboard_service.parse_run_metrics,
                    args=(projects_dir, name, run_id),
                    daemon=True,
                ).start()

            _prune_old_runs(projects_dir, name, keep_last=20)

            # Only update train_status when the last run for this project finishes
            with _lock:
                remaining = _get_runs_for_project(name)
            if not remaining:
                status = "stopped" if ret == 0 else "crashed"
                _update_project_json(projects_dir, name, train_status=status, train_pid=0)
                log.info("All runs for %s finished. Status: %s", name, status)
            return

        time.sleep(1)
```

- [ ] **Step 5: Update start_training's thread call to pass run_id**

In the existing `start_training`, the monitor thread launch currently passes just `(projects_dir, name)`. Since Tasks 4-5 will rewrite `start_training` entirely, just add a note marker here and move on.

- [ ] **Step 6: Run tests**

```bash
make test 2>&1 | tail -20
```

Some existing tests that call `start_training` may fail because `_monitor_process` signature changed and the thread call inside `start_training` hasn't been updated yet. That's acceptable — Tasks 4-5 fix those. The new `get_runs_for_project` tests should pass.

- [ ] **Step 7: Commit what passes**

```bash
git add services/process_manager.py tests/test_process_manager.py
git commit -m "refactor: re-key _running by run_id, add get_runs_for_project and get_run_log_path helpers"
```

---

## Task 4: Process manager — rewrite start_training

**Files:**
- Modify: `services/process_manager.py`
- Modify: `tests/test_process_manager.py`

`start_training` gains a `branch` param, pre-creates the DB run record, checks parallel capacity, and selects the workspace atomically.

- [ ] **Step 1: Update test helper and existing tests for new signature**

In `tests/test_process_manager.py`, update `_make_project` to include the new fields:

```python
def _make_project(tmp_path, **overrides):
    """Create a minimal ready project on disk."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(exist_ok=True)
    proj_dir = projects_dir / "myproject"
    proj_dir.mkdir()
    src_dir = proj_dir / "workspace"
    src_dir.mkdir()
    (src_dir / "train.py").write_text("print('training')")
    (src_dir / "requirements.txt").write_text("numpy\n")

    data = {
        "name": "myproject",
        "git_url": "https://github.com/user/repo.git",
        "branch": "main",
        "python_version": "3.11",
        "train_file": "train.py",
        "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt",
        "env_type": "venv",
        "setup_status": "ready",
        "setup_error": "",
        "train_status": "idle",
        "train_pid": 0,
        "env_vars": {},
        "parallel_runs_enabled": False,
        "max_parallel_runs": 2,
    }
    data.update(overrides)
    (proj_dir / "project.json").write_text(json.dumps(data))
    return str(projects_dir)
```

Add a mock for `get_db` that all test patches need. Update ALL existing `start_training` tests to add a `get_db` mock — since `start_training` now pre-creates a DB run record, every test must mock it. Add this to each existing test's `with patch(...)` block:

```python
patch("services.process_manager.get_db", return_value=MagicMock(
    create_training_run=MagicMock(return_value=99),
    delete_training_run=MagicMock(),
    get_training_runs=MagicMock(return_value=[]),
)),
```

Also add `patch("services.process_manager._running", {})` to isolate state between tests, or call `process_manager._running.clear()` in each test setup.

Add new tests for parallel capacity:

```python
def test_start_training_rejects_second_run_when_parallel_disabled(tmp_path):
    """Second start_training call is rejected when parallel_runs_enabled=False."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=1),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        result = process_manager.start_training(projects_dir, "myproject")

    assert "error" in result
    process_manager._running.clear()


def test_start_training_allows_second_run_when_parallel_enabled(tmp_path):
    """Second start_training is allowed when parallel_runs_enabled=True."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path, parallel_runs_enabled=True, max_parallel_runs=2)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=2),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    # Simulate run 1 already active on primary workspace
    process_manager._running[1] = {
        "process": MagicMock(), "starting": False,
        "project_name": "myproject", "run_id": 1,
        "branch": "main",
        "workspace_dir": os.path.join(projects_dir, "myproject", "workspace"),
    }

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject", branch="feature/x")

    assert "error" not in result
    assert result.get("run_id") == 2
    process_manager._running.clear()


def test_start_training_returns_run_id(tmp_path):
    """start_training returns run_id in the response."""
    from services import process_manager
    process_manager._running.clear()
    projects_dir = _make_project(tmp_path)
    mock_db = MagicMock(create_training_run=MagicMock(return_value=42),
                        delete_training_run=MagicMock(),
                        get_training_runs=MagicMock(return_value=[]))

    with patch("services.process_manager._resolve_python_binary", return_value="/fake/python"), \
         patch("services.process_manager._update_project_json"), \
         patch("services.process_manager.threading.Thread", side_effect=_inline_thread), \
         patch("services.process_manager.subprocess.run", return_value=_ok_run()), \
         patch("services.process_manager.subprocess.Popen") as mock_popen, \
         patch("services.process_manager._monitor_process"), \
         patch("services.process_manager.get_db", return_value=mock_db):
        mock_popen.return_value = MagicMock(pid=9999)
        result = process_manager.start_training(projects_dir, "myproject")

    assert result.get("run_id") == 42
    process_manager._running.clear()
```

- [ ] **Step 2: Run tests to confirm new tests fail**

```bash
make test 2>&1 | grep -E "FAILED|passed|failed" | tail -10
```

- [ ] **Step 3: Rewrite start_training**

Replace the entire `start_training` function in `services/process_manager.py`:

```python
def start_training(projects_dir, name, branch=None):
    """Validate, reserve a training slot, and launch the pre-launch sequence in background."""
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return {"error": "Project not found"}

    with open(config_path) as f:
        project = json.load(f)

    if project.get("setup_status") != "ready":
        return {"error": "Project setup is not complete"}

    python_bin = _resolve_python_binary(projects_dir, project)
    if not python_bin:
        if project.get("env_type") == "conda":
            hint = f"conda env beekeeper-{name}"
        else:
            hint = os.path.join(projects_dir, name, "venv", "bin")
        return {"error": f"Could not find Python binary (checked {hint})"}

    if branch is None:
        branch = project.get("branch", "main")

    # Pre-create DB record so run_id is available immediately for slot reservation
    from services.db_service import get_db
    run_id = get_db().create_training_run(
        project_name=name,
        metadata={
            "started_at": datetime.datetime.now(),
            "status": "starting",
            "branch": branch,
        }
    )

    primary_ws = os.path.join(projects_dir, name, "workspace")

    with _lock:
        active_runs = _get_runs_for_project(name)
        parallel_enabled = project.get("parallel_runs_enabled", False)
        max_runs = project.get("max_parallel_runs", 2)

        if not parallel_enabled and len(active_runs) > 0:
            get_db().delete_training_run(run_id)
            return {"error": "Training is already running"}

        if parallel_enabled and len(active_runs) >= max_runs:
            get_db().delete_training_run(run_id)
            return {"error": f"At capacity ({max_runs} parallel runs)"}

        primary_in_use = any(info.get("workspace_dir") == primary_ws for info in active_runs)
        workspace_dir = (
            primary_ws if not primary_in_use
            else os.path.join(projects_dir, name, f"workspace-{run_id}")
        )

        _running[run_id] = {
            "process": None,
            "starting": True,
            "project_name": name,
            "run_id": run_id,
            "branch": branch,
            "workspace_dir": workspace_dir,
        }

    _update_project_json(projects_dir, name, train_status="starting")

    thread = threading.Thread(
        target=_execute_training,
        args=(projects_dir, name, project, python_bin, run_id, branch, workspace_dir),
        daemon=True,
    )
    thread.start()

    return {"run_id": run_id, "status": "starting"}
```

- [ ] **Step 4: Run tests**

```bash
make test 2>&1 | tail -20
```
Expected: new tests pass; existing pre-launch tests need `_execute_training` updated in Task 5.

- [ ] **Step 5: Commit**

```bash
git add services/process_manager.py tests/test_process_manager.py
git commit -m "feat: start_training accepts branch param, pre-creates run record, checks parallel capacity"
```

---

## Task 5: Process manager — rewrite _execute_training

**Files:**
- Modify: `services/process_manager.py`

`_execute_training` gains `run_id, branch, workspace_dir` params. For parallel workspaces it clones the repo first. Uses the pre-created run_id (no longer calls `create_training_run`). Updates DB with full metadata after git sync.

- [ ] **Step 1: Rewrite _execute_training signature and pre-launch**

Replace the entire `_execute_training` function:

```python
def _execute_training(projects_dir, name, project, python_bin, run_id, branch, workspace_dir):
    """Run the full pre-launch sequence and start the training subprocess (runs in background thread)."""
    is_parallel = workspace_dir != os.path.join(projects_dir, name, "workspace")
    log_path = (
        os.path.join(projects_dir, name, f"train-{run_id}.log")
        if is_parallel
        else os.path.join(projects_dir, name, "train.log")
    )

    def _abort(msg):
        log.error("Pre-launch failed for %s run %d: %s", name, run_id, msg)
        try:
            with open(log_path, "w") as lf:
                lf.write(f"[beekeeper] Pre-launch failed: {msg}\n")
        except Exception:
            pass
        from services.db_service import get_db
        get_db().update_training_run(run_id, status="crashed")
        with _lock:
            remaining = _get_runs_for_project(name)
            _running.pop(run_id, None)
            remaining_after = _get_runs_for_project(name)
        if not remaining_after:
            _update_project_json(projects_dir, name, train_status="stopped")
        if is_parallel and os.path.isdir(workspace_dir):
            import shutil
            try:
                shutil.rmtree(workspace_dir)
            except Exception:
                pass

    # For parallel runs: clone fresh workspace
    if is_parallel:
        git_url = project.get("git_url", "")
        try:
            result = subprocess.run(
                ["git", "clone", git_url, workspace_dir],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return _abort(f"Git clone failed: {result.stderr.strip()[-500:]}")
        except subprocess.TimeoutExpired:
            return _abort("Git clone timed out (300s)")
        except Exception as e:
            return _abort(f"Git clone failed: {e}")

    # Sync to remote — remote is always authoritative
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=workspace_dir,
            capture_output=True, text=True, timeout=60,
        )
        if fetch.returncode != 0:
            return _abort(f"Git fetch failed: {fetch.stderr.strip()[-500:]}")
        reset = subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=workspace_dir,
            capture_output=True, text=True, timeout=30,
        )
        if reset.returncode != 0:
            return _abort(f"Git reset failed: {reset.stderr.strip()[-500:]}")
    except subprocess.TimeoutExpired:
        return _abort("Git sync timed out (60s)")
    except Exception as e:
        return _abort(f"Git sync failed: {e}")

    run_meta = _collect_run_metadata(workspace_dir, python_bin, branch)

    # Update the pre-created DB record with full metadata
    from services.db_service import get_db
    get_db().update_training_run(
        run_id,
        status="running",
        commit_sha=run_meta["commit_sha"],
        commit_message=run_meta["commit_msg"],
        python_version=run_meta["python_version"],
        gpu_info=json.dumps(run_meta["gpu_info"]),
        hostname=run_meta["hostname"],
    )

    # Ensure data dir symlink (before setup script so it can use it)
    if project.get("data_dir_enabled") and project.get("data_dir_remote"):
        data_dir_remote = project["data_dir_remote"]
        data_dir_local = project.get("data_dir_local", "data")
        local_path = os.path.join(workspace_dir, data_dir_local)
        if os.path.islink(local_path):
            if os.readlink(local_path) != data_dir_remote:
                os.unlink(local_path)
                os.symlink(data_dir_remote, local_path)
        elif os.path.exists(local_path):
            return _abort(
                f"'{data_dir_local}' already exists in the repository and is not a symlink. "
                f"Remove it from the repo or disable the data directory in project settings."
            )
        elif os.path.isdir(data_dir_remote):
            os.symlink(data_dir_remote, local_path)
        else:
            return _abort(f"Data directory '{data_dir_remote}' does not exist on this server.")

    # Run setup script if configured
    setup_script = project.get("setup_script", "")
    if setup_script:
        script_path = os.path.join(workspace_dir, setup_script)
        if os.path.isfile(script_path):
            try:
                if project.get("env_type") == "conda":
                    from services.python_versions import _find_conda_bin
                    conda_bin = _find_conda_bin()
                    env_name = f"beekeeper-{name}"
                    cmd = [conda_bin, "run", "-n", env_name, "bash", script_path]
                    env = None
                else:
                    venv_path = os.path.join(projects_dir, name, "venv")
                    cmd = ["bash", script_path]
                    env = os.environ.copy()
                    env["VIRTUAL_ENV"] = venv_path
                    env["PATH"] = f"{venv_path}/bin:{env.get('PATH', '')}"

                result = subprocess.run(
                    cmd, cwd=workspace_dir, env=env,
                    capture_output=True, text=True, timeout=300,
                )
                if result.returncode != 0:
                    return _abort(f"Setup script failed: {result.stderr.strip()[-500:]}")
            except subprocess.TimeoutExpired:
                return _abort("Setup script timed out (300s)")
            except Exception as e:
                return _abort(f"Setup script failed: {e}")

    # Install/update dependencies
    req_file = project.get("requirements_file", "requirements.txt")
    req_path = os.path.join(workspace_dir, req_file)
    if os.path.isfile(req_path):
        try:
            result = subprocess.run(
                [python_bin, "-m", "pip", "install", "-r", req_path, "--quiet"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                return _abort(f"Pip install failed: {result.stderr.strip()[-500:]}")
        except subprocess.TimeoutExpired:
            return _abort("Pip install timed out (300s)")
        except Exception as e:
            return _abort(f"Pip install failed: {e}")

    train_file = project.get("train_file", "train.py")
    train_path = os.path.join(workspace_dir, train_file)
    if not os.path.isfile(train_path):
        return _abort(f"Training file not found: {train_file}")

    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    _write_run_header(log_fd, run_meta, project)

    proc_env = os.environ.copy()
    proc_env.update(project.get("env_vars") or {})

    try:
        proc = subprocess.Popen(
            [python_bin, "-u", train_file],
            cwd=workspace_dir,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env=proc_env,
            start_new_session=True,
        )
    except Exception as e:
        os.close(log_fd)
        return _abort(f"Failed to start training: {e}")

    os.close(log_fd)

    # Kill any standalone TB for this project before starting a new one
    with _lock:
        old_tb = _tb_running.pop(name, None)
    if old_tb:
        _kill_tb_process(old_tb["tb_process"])

    # Start tensorboard for this run's workspace
    tb_process = None
    tb_port = None
    tb_run_dir_rel = None
    tb_bin = _resolve_tensorboard_binary(projects_dir, project)
    if tb_bin:
        tb_port = _find_free_port()
        if tb_port:
            tb_logdir_base = os.path.join(workspace_dir, project.get("tensorboard_log_dir", "runs"))

            tb_logs_max_runs = project.get("tb_logs_max_runs", 20)
            if tb_logs_max_runs > 0 and not is_parallel:
                # Only auto-cleanup TB for primary workspace (parallel workspaces are transient)
                from services.tensorboard_service import cleanup_old_tb_logs
                protected_tb_dirs = set()
                try:
                    for r in get_db().get_training_runs(name, limit=1000):
                        if r.get("notable") and r.get("tensorboard_dir"):
                            protected_tb_dirs.add(os.path.basename(r["tensorboard_dir"]))
                except Exception:
                    pass
                cleanup_result = cleanup_old_tb_logs(tb_logdir_base, tb_logs_max_runs,
                                                     protected_dirs=protected_tb_dirs)
                if cleanup_result["deleted"]:
                    log.info(f"Auto-cleanup TB: {cleanup_result['message']}")

            run_timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            tb_run_dir = os.path.join(tb_logdir_base, run_timestamp)
            os.makedirs(tb_run_dir, exist_ok=True)
            tb_run_dir_rel = f"{project.get('tensorboard_log_dir', 'runs')}/{run_timestamp}"

            run_history_max_runs = project.get("run_history_max_runs", 10)
            if run_history_max_runs > 0 and not is_parallel:
                runs = get_db().get_training_runs(name, limit=1000)
                runs.sort(key=lambda r: r["started_at"], reverse=True)
                deleted_count = 0
                for r in runs[run_history_max_runs:]:
                    if not r.get("notable", 0):
                        get_db().delete_training_run(r["id"])
                        deleted_count += 1
                if deleted_count > 0:
                    log.info(f"Auto-cleanup: deleted {deleted_count} old run(s)")

            try:
                tb_process = subprocess.Popen(
                    [tb_bin, "--logdir", tb_logdir_base, "--port", str(tb_port), "--bind_all"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                get_db().update_training_run(run_id, tensorboard_dir=tb_run_dir_rel)
            except Exception as e:
                log.warning("Failed to start tensorboard for %s run %d: %s", name, run_id, e)
                tb_port = None

    with _lock:
        _running[run_id] = {
            "process": proc,
            "log_path": log_path,
            "tb_process": tb_process,
            "tb_port": tb_port,
            "started_at": time.time(),
            "run_id": run_id,
            "project_name": name,
            "branch": branch,
            "workspace_dir": workspace_dir,
        }

    _update_project_json(projects_dir, name,
                         train_status="running", train_pid=proc.pid,
                         last_run_at=time.time())

    thread = threading.Thread(
        target=_monitor_process, args=(projects_dir, name, run_id), daemon=True
    )
    thread.start()
```

- [ ] **Step 2: Run tests**

```bash
make test 2>&1 | tail -20
```
Expected: all existing pre-launch tests pass (they mock subprocess, threading, and now get_db).

- [ ] **Step 3: Commit**

```bash
git add services/process_manager.py
git commit -m "feat: _execute_training accepts run_id/branch/workspace, handles parallel clone and log naming"
```

---

## Task 6: Process manager — rewrite stop_training and add TB migration

**Files:**
- Modify: `services/process_manager.py`

- [ ] **Step 1: Add TB migration helper**

Add this function to `services/process_manager.py` before `stop_training`:

```python
def _move_tb_logs_to_primary(projects_dir: str, name: str, workspace_dir: str, run_id: int):
    """Move TB run dir from parallel workspace to primary workspace before deletion."""
    import shutil
    from services.db_service import get_db

    run = get_db().get_training_run(run_id)
    if not run or not run.get("tensorboard_dir"):
        return

    tb_rel = run["tensorboard_dir"]  # e.g. "runs/20260430-123456"
    src = os.path.join(workspace_dir, tb_rel)
    primary_ws = os.path.join(projects_dir, name, "workspace")
    dst = os.path.join(primary_ws, tb_rel)

    if not os.path.isdir(src):
        return

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        shutil.move(src, dst)
        log.info("Moved TB logs %s → %s", src, dst)
    except Exception as e:
        log.warning("Failed to move TB logs %s → %s: %s", src, dst, e)
```

- [ ] **Step 2: Rewrite stop_training**

Replace the entire `stop_training` function:

```python
def stop_training(projects_dir, name, run_id=None):
    """Stop a training run. run_id selects which run; omit if only one is active."""
    primary_ws = os.path.join(projects_dir, name, "workspace")

    with _lock:
        if run_id is not None:
            info = _running.get(run_id)
            if not info or info.get("project_name") != name:
                return {"error": f"Run {run_id} not found for project {name}"}
        else:
            project_runs = [rid for rid, info in _running.items()
                            if info.get("project_name") == name]
            if not project_runs:
                try:
                    config_path = os.path.join(projects_dir, name, "project.json")
                    project = Project.load(config_path)
                    if project.train_status == "running":
                        project.train_status = "stopped"
                        project.save(projects_dir)
                        return {"stopped": True}
                except Exception:
                    pass
                return {"error": "Training is not running"}
            if len(project_runs) > 1:
                return {"error": "Multiple runs active — specify run_id"}
            run_id = project_runs[0]
            info = _running.get(run_id)

        proc = info["process"]
        workspace_dir = info.get("workspace_dir", primary_ws)

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=3)
        except (ProcessLookupError, OSError):
            pass

    exit_code = proc.returncode if proc.returncode is not None else -15

    with _lock:
        info = _running.pop(run_id, None)
        if info:
            log_path = info.get("log_path")
            started_at = info.get("started_at")

            if log_path and started_at:
                _append_run_footer(log_path, exit_code, started_at)

            archived_log_path = None
            if log_path and os.path.isfile(log_path):
                archived_log_path = _archive_run_log(projects_dir, name, run_id, log_path)

            if started_at:
                _finalize_run_record(run_id, exit_code, started_at, archived_log_path)

            tb = info.get("tb_process")
            tb_port = info.get("tb_port")
            if tb and tb.poll() is None and tb_port:
                _tb_running[name] = {
                    "tb_process": tb,
                    "tb_port": tb_port,
                    "last_access": time.time(),
                }
                log.info("Migrated TB for %s run %d to standalone", name, run_id)

    is_parallel = workspace_dir != primary_ws
    if is_parallel:
        _move_tb_logs_to_primary(projects_dir, name, workspace_dir, run_id)
        import shutil
        try:
            shutil.rmtree(workspace_dir)
            log.info("Deleted parallel workspace %s", workspace_dir)
        except Exception as e:
            log.warning("Failed to delete parallel workspace %s: %s", workspace_dir, e)

    with _lock:
        remaining = _get_runs_for_project(name)
    if not remaining:
        _update_project_json(projects_dir, name, train_status="stopped", train_pid=0)

    return {"status": "stopped", "run_id": run_id}
```

- [ ] **Step 3: Run tests**

```bash
make test 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add services/process_manager.py
git commit -m "feat: stop_training accepts run_id, migrates TB logs, cleans up parallel workspace"
```

---

## Task 6b: Gemini review — process_manager.py

- [ ] **Step 1: Run Gemini review**

```bash
cat /home/robertcowher/pythonprojects/beekeeper/services/process_manager.py | \
  gemini -p "Review this Python process manager for a Flask ML training app. Focus on: (1) thread safety — are all accesses to _running and _tb_running properly locked? (2) correctness of the parallel workspace selection logic — can two threads race to claim the primary workspace? (3) TB log migration — will the move happen before workspace deletion? (4) train_status aggregate logic — could it get stuck in 'running' if a run crashes? Return a prioritized list of issues only."
```

- [ ] **Step 2: Address any critical issues found before continuing**

---

## Task 7: API — update training endpoints

**Files:**
- Modify: `routes/api_v1.py`
- Modify: `routes/training.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add tests for new API shapes**

Add to `tests/test_api.py`:

```python
def test_training_start_returns_run_id(client, ready_project):
    """POST /training/start returns run_id."""
    from unittest.mock import patch
    with patch("routes.api_v1.start_training", return_value={"run_id": 7, "status": "starting"}):
        r = client.post("/api/v1/projects/myproject/training/start",
                        json={}, headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    assert data["data"]["run_id"] == 7


def test_training_start_passes_branch(client, ready_project):
    """POST /training/start forwards branch param."""
    from unittest.mock import patch, call
    with patch("routes.api_v1.start_training", return_value={"run_id": 8, "status": "starting"}) as mock_start:
        client.post("/api/v1/projects/myproject/training/start",
                    json={"branch": "feature/test"},
                    headers={"Authorization": "Bearer test"})
    _, kwargs = mock_start.call_args
    assert kwargs.get("branch") == "feature/test"


def test_training_status_returns_runs_list(client, ready_project):
    """GET /training/status returns runs list."""
    from unittest.mock import patch
    mock_runs = [{"run_id": 3, "branch": "main", "status": "running", "elapsed": 60, "pid": 123, "tb_port": None, "resources": None}]
    with patch("routes.api_v1.get_runs_for_project", return_value=mock_runs):
        r = client.get("/api/v1/projects/myproject/training/status",
                       headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert "runs" in data["data"]
    assert data["data"]["runs"][0]["run_id"] == 3
```

- [ ] **Step 2: Run to confirm failure**

```bash
make test 2>&1 | grep -E "FAILED|passed|failed" | tail -10
```

- [ ] **Step 3: Update api_v1.py training/start endpoint**

Find `@api_v1_bp.route("/projects/<name>/training/start", methods=["POST"])` and update the handler:

```python
@api_v1_bp.route("/projects/<name>/training/start", methods=["POST"])
@api_key_required
def training_start(name):
    """Start training for a project."""
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    body = request.get_json() or {}
    branch = body.get("branch")  # None → use project default
    result = start_training(projects_dir, name, branch=branch)
    if "error" in result:
        return api_response(error_code="START_ERROR", error_message=result["error"], status_code=400)
    return api_response(data=result)
```

- [ ] **Step 4: Update api_v1.py training/stop endpoint**

Find the stop endpoint and update:

```python
@api_v1_bp.route("/projects/<name>/training/stop", methods=["POST"])
@api_key_required
def training_stop(name):
    """Stop training. Provide run_id in body when multiple runs are active."""
    load_project(name)
    projects_dir = current_app.config["PROJECTS_DIR"]
    body = request.get_json() or {}
    run_id = body.get("run_id")
    result = stop_training(projects_dir, name, run_id=run_id)
    if "error" in result:
        return api_response(error_code="STOP_ERROR", error_message=result["error"], status_code=400)
    return api_response(data=result)
```

- [ ] **Step 5: Update training/status endpoint to return runs list**

Find the status endpoint and add the import + update:

```python
@api_v1_bp.route("/projects/<name>/training/status")
@api_key_required
def training_status(name):
    """Get active training runs for a project."""
    load_project(name)
    from services.process_manager import get_runs_for_project
    runs = get_runs_for_project(name)
    return api_response(data={"runs": runs})
```

Also add `get_runs_for_project` to the import at the top of `api_v1.py`:

```python
from services.process_manager import start_training, stop_training, get_training_status, get_runs_for_project, get_run_log_path
```

- [ ] **Step 6: Update logs/stream in routes/training.py to accept run_id**

Find `logs_stream` in `routes/training.py`. Import `get_run_log_path` and `get_runs_for_project` and update:

```python
from services.process_manager import (
    start_training, stop_training, get_training_status,
    start_tensorboard, stop_tensorboard, get_run_log_path, get_runs_for_project,
)
```

Replace the entire `logs_stream` route:

```python
@training_bp.route("/<name>/logs/stream")
def logs_stream(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    tail = request.args.get("tail", type=int)
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)

    def generate():
        if tail and os.path.isfile(log_path):
            offset = _tail_offset(log_path, tail)
        else:
            offset = 0

        retries_without_data = 0
        max_idle = 300

        while True:
            try:
                if os.path.isfile(log_path):
                    size = os.path.getsize(log_path)
                    if size < offset:
                        offset = 0
                    if size > offset:
                        with open(log_path, "r") as f:
                            f.seek(offset)
                            chunk = f.read()
                            offset = f.tell()
                        if chunk:
                            for line in chunk.splitlines(True):
                                yield f"data: {line.rstrip()}\n\n"
                            retries_without_data = 0
                            continue

                retries_without_data += 1
                # Use public API to check if this specific run is still active
                runs = get_runs_for_project(name)
                if run_id is not None:
                    run_active = any(r["run_id"] == run_id for r in runs)
                else:
                    run_active = len(runs) > 0
                if not run_active and retries_without_data > 2:
                    yield "data: \n\nevent: done\ndata: finished\n\n"
                    return
                if retries_without_data > max_idle:
                    return

            except Exception:
                pass

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

- [ ] **Step 7: Update api_v1.py get_logs to use get_run_log_path for active runs**

In `api_v1.py`, find the `get_logs` route. The current fallback for active parallel runs uses the wrong path. Replace the log path resolution block:

```python
    run_id = request.args.get("run_id", type=int)

    if run_id is not None:
        from services.db_service import get_db
        from services.process_manager import get_run_log_path
        run = get_db().get_training_run(run_id)
        if not run or run['project_name'] != name:
            return api_response(error_code="NOT_FOUND",
                                error_message=f"Run {run_id} not found", status_code=404)
        # Prefer archived log; fall back to live log path (handles active parallel runs)
        if run.get('log_file_path'):
            log_path = os.path.join(projects_dir, name, run['log_file_path'])
        else:
            log_path = get_run_log_path(projects_dir, name, run_id=run_id)
    else:
        log_path = get_run_log_path(projects_dir, name)
```

- [ ] **Step 8: Update api_v1.py stream_logs to accept run_id**

In `api_v1.py`, find the `stream_logs` route (at `/projects/<name>/logs/stream`). Add `run_id` support using `get_run_log_path`:

```python
@api_v1_bp.route("/projects/<name>/logs/stream")
@api_key_required
def stream_logs(name):
    """SSE stream of log content."""
    load_project(name)
    from services.process_manager import get_run_log_path, get_runs_for_project
    projects_dir = current_app.config["PROJECTS_DIR"]
    tail = request.args.get("tail", type=int)
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)
    # ... rest of generate() identical to routes/training.py logs_stream ...
```

Copy the `generate()` body from `routes/training.py`'s updated `logs_stream` (Step 6 above) into this route, replacing the existing body.

- [ ] **Step 9: Update logs/download in routes/training.py to accept run_id**

Replace the `logs_download` route:

```python
@training_bp.route("/<name>/logs/download")
def logs_download(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    run_id = request.args.get("run_id", type=int)
    log_path = get_run_log_path(projects_dir, name, run_id=run_id)
    if not os.path.isfile(log_path):
        return jsonify({"error": "No log file found"}), 404
    filename = f"{name}-run-{run_id}.log" if run_id else f"{name}-train.log"
    return send_file(log_path, as_attachment=True, download_name=filename)
```

- [ ] **Step 7: Run tests**

```bash
make test 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add routes/api_v1.py routes/training.py tests/test_api.py
git commit -m "feat: API training endpoints accept branch/run_id, status returns runs list, logs/stream accepts run_id"
```

---

## Task 8: API — /capacity endpoint

**Files:**
- Modify: `routes/api_v1.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write test**

Add to `tests/test_api.py`:

```python
def test_capacity_endpoint_shape(client):
    """GET /capacity returns total_slots, running, available, projects."""
    r = client.get("/api/v1/capacity", headers={"Authorization": "Bearer test"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["success"] is True
    body = data["data"]
    assert "total_slots" in body
    assert "running" in body
    assert "available" in body
    assert "projects" in body
    assert body["available"] == body["total_slots"] - body["running"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
make test 2>&1 | grep "capacity" | tail -5
```

- [ ] **Step 3: Add /capacity route to api_v1.py**

Add after the `/busy` route:

```python
@api_v1_bp.route("/capacity")
@api_key_required
def get_capacity():
    """System-wide training capacity: total slots, running, available."""
    from services.process_manager import get_runs_for_project
    projects_dir = current_app.config["PROJECTS_DIR"]
    total_slots = 0
    total_running = 0
    project_list = []

    if os.path.isdir(projects_dir):
        for proj_name in sorted(os.listdir(projects_dir)):
            config_path = os.path.join(projects_dir, proj_name, "project.json")
            if not os.path.isfile(config_path):
                continue
            try:
                project = Project.load(config_path)
            except Exception:
                continue
            max_runs = project.max_parallel_runs if project.parallel_runs_enabled else 1
            running_count = len(get_runs_for_project(proj_name))
            total_slots += max_runs
            total_running += running_count
            project_list.append({
                "name": proj_name,
                "running_runs": running_count,
                "max_runs": max_runs,
            })

    return api_response(data={
        "total_slots": total_slots,
        "running": total_running,
        "available": total_slots - total_running,
        "projects": project_list,
    })
```

- [ ] **Step 4: Run tests**

```bash
make test 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add routes/api_v1.py tests/test_api.py
git commit -m "feat: add /api/v1/capacity endpoint for system-wide utilization"
```

---

## Task 9: MCP server — update tools and add get_capacity

**Files:**
- Modify: `mcp_server.py`

No tests for MCP (it's a thin wrapper over the API). Verify manually after task 11.

- [ ] **Step 1: Update start_training tool**

Replace the `start_training` MCP tool:

```python
@mcp.tool()
def start_training(name: str, branch: str = None) -> dict:
    """
    Start training for a project. branch overrides the project's configured default.
    The pre-launch sequence (git sync, pip install) runs first — 30-120s.
    Returns run_id. Use training_status() to confirm the run is active.
    """
    body = {"branch": branch} if branch else {}
    try:
        return _post(f"/projects/{name}/training/start", body, timeout=180)
    except requests.exceptions.ReadTimeout:
        return {
            "success": True,
            "message": "Pre-launch sequence is running. Check training_status in 30s.",
        }
```

- [ ] **Step 2: Update stop_training tool**

```python
@mcp.tool()
def stop_training(name: str, run_id: int = None) -> dict:
    """
    Stop a training run. Provide run_id when multiple runs are active on the same project.
    If only one run is active and run_id is omitted, it stops automatically.
    """
    body = {"run_id": run_id} if run_id is not None else {}
    return _post(f"/projects/{name}/training/stop", body)
```

- [ ] **Step 3: Update training_status tool**

```python
@mcp.tool()
def training_status(name: str) -> dict:
    """
    Get all active training runs for a project.
    Returns a list of runs, each with run_id, branch, status, elapsed seconds, pid, tb_port.
    Use run_id values when calling stop_training or get_logs for a specific run.
    """
    return _get(f"/projects/{name}/training/status")
```

- [ ] **Step 4: Update get_logs tool**

```python
@mcp.tool()
def get_logs(name: str, run_id: int = None, tail: int = 100) -> str:
    """
    Fetch the last N lines of training logs for a specific run.
    Provide run_id when multiple runs are active — required to avoid ambiguity.
    Use tail=500 for more context, tail=50 for a quick check.
    """
    params = f"tail={tail}"
    if run_id is not None:
        params += f"&run_id={run_id}"
    result = _get(f"/projects/{name}/logs?{params}")
    return result.get("data", {}).get("content", "")
```

- [ ] **Step 5: Update check_busy to delegate to capacity**

```python
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
```

- [ ] **Step 6: Add get_capacity tool**

```python
@mcp.tool()
def get_capacity() -> dict:
    """
    System-wide training capacity. Returns total_slots, running, available, and per-project breakdown.
    Use this before starting a new run — it tells you not just busy/free but how much headroom exists.
    Prefer this over check_busy() for any new agent workflows.
    """
    return _get("/capacity")
```

- [ ] **Step 7: Commit**

```bash
git add mcp_server.py
git commit -m "feat: MCP tools updated for parallel runs — branch/run_id params, get_capacity tool"
```

---

## Task 9b: Gemini review — API and MCP

- [ ] **Step 1: Run Gemini review**

```bash
cat /home/robertcowher/pythonprojects/beekeeper/routes/api_v1.py \
    /home/robertcowher/pythonprojects/beekeeper/mcp_server.py | \
  gemini -p "Review these files for a Flask ML training management app. Focus on: (1) are backward-compat fallbacks (run_id omitted, branch omitted) correct? (2) does the /capacity endpoint correctly aggregate slots for both parallel-enabled and non-parallel projects? (3) any MCP tool docstrings that would mislead an AI agent? Return a prioritized issue list only."
```

- [ ] **Step 2: Address any critical issues before continuing**

---

## Task 10: Project settings — parallel runs UI

**Files:**
- Modify: `templates/edit_project.html`
- Modify: `routes/project.py`

- [ ] **Step 1: Add fields to edit_project.html**

In `templates/edit_project.html`, find the closing `</form>` tag and add before it (before the submit button section):

```html
    <h2>Parallel Runs</h2>

    <div class="form-group">
        <label class="checkbox-label">
            <input type="checkbox" name="parallel_runs_enabled" id="parallel-runs-toggle"
                   value="1" {{ 'checked' if project.parallel_runs_enabled }}>
            Enable parallel runs
            <span class="tooltip-icon" title="Allow multiple branches to run simultaneously on this project.">?</span>
        </label>
    </div>

    <div class="form-group" id="max-parallel-runs-group"
         style="{{ '' if project.parallel_runs_enabled else 'display:none' }}">
        <label for="max_parallel_runs">
            Max parallel runs
            <span class="tooltip-icon" title="Maximum number of branches that can run simultaneously. Minimum 2.">?</span>
        </label>
        <input type="number" id="max_parallel_runs" name="max_parallel_runs"
               value="{{ project.max_parallel_runs }}" min="2" max="8">
    </div>

    <script>
    document.getElementById('parallel-runs-toggle').addEventListener('change', function() {
        document.getElementById('max-parallel-runs-group').style.display = this.checked ? '' : 'none';
    });
    </script>
```

- [ ] **Step 2: Update project update route to handle new fields**

In `routes/project.py`, find the `update` route (handles `POST /projects/<name>/edit`). Add handling for the new fields inside the update logic alongside the other fields:

```python
    project.parallel_runs_enabled = bool(request.form.get("parallel_runs_enabled"))
    try:
        project.max_parallel_runs = max(2, int(request.form.get("max_parallel_runs", 2)))
    except (ValueError, TypeError):
        project.max_parallel_runs = 2
```

- [ ] **Step 3: Restart and verify locally**

```bash
sudo /usr/bin/systemctl restart beekeeper && sudo /usr/bin/systemctl status beekeeper
```

Open http://localhost:5000, go to a project's edit page, verify the Parallel Runs section appears, toggle it, and verify the max field shows/hides correctly.

- [ ] **Step 4: Commit**

```bash
git add templates/edit_project.html routes/project.py
git commit -m "feat: add parallel runs toggle and max_parallel_runs to project settings UI"
```

---

## Task 11: Training section — run list UI

**Files:**
- Modify: `templates/project.html`
- Modify: `static/css/style.css`
- Modify: `static/js/training.js`

This is the largest frontend change. The Controls section and the Logs section are replaced by a dynamic run list where each row has inline expandable logs.

- [ ] **Step 1: Add CSS for run list**

In `static/css/style.css`, add at the end:

```css
/* Parallel run list */
.run-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
}

.run-row {
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
}

.run-row.run-active {
    border-color: var(--running);
}

.run-row-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.75rem;
    flex-wrap: wrap;
}

.run-id {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    color: var(--text-secondary);
    background: var(--bg-primary);
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid var(--border);
}

.run-branch {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    color: var(--accent);
}

.run-elapsed {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin-left: auto;
}

.run-log-panel {
    border-top: 1px solid var(--border);
    background: var(--bg-primary);
}

.run-log-controls {
    display: flex;
    gap: 0.5rem;
    padding: 0.4rem 0.75rem;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
}

.run-log-terminal {
    padding: 0.5rem 0.75rem;
    max-height: 320px;
    overflow-y: auto;
    font-family: var(--font-mono);
    font-size: 0.8rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-all;
}

.run-list-actions {
    margin-top: 0.5rem;
    display: flex;
    gap: 0.5rem;
    align-items: center;
}

/* Branch picker modal */
.branch-picker {
    display: none;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem;
    margin-top: 0.5rem;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
}

.branch-picker.open {
    display: flex;
}
```

- [ ] **Step 2: Replace the Controls and Logs sections in project.html**

Replace everything from `{% if project.get('setup_status') == 'ready' %}` (line ~71) through the closing of the Logs section (line ~109) with:

```html
{% if project.get('setup_status') == 'ready' %}
<section class="card" id="training-section">
    <div class="card-header">
        <h2>Training</h2>
    </div>
    <div id="run-list" class="run-list" data-name="{{ project.name }}">
        <!-- Populated by training.js -->
    </div>
    <div class="run-list-actions">
        <button class="btn btn-success btn-sm" id="btn-start-run">+ Start Run…</button>
    </div>
    <div class="branch-picker" id="branch-picker">
        <label for="run-branch-select" style="font-size:0.85rem;">Branch:</label>
        <select id="run-branch-select" class="form-control" style="width:auto;min-width:200px;">
            <option value="{{ project.branch }}">{{ project.branch }} (default)</option>
        </select>
        <button class="btn btn-success btn-sm" id="btn-confirm-start">Start</button>
        <button class="btn btn-secondary btn-sm" id="btn-cancel-start">Cancel</button>
    </div>
</section>
{% else %}
<section class="card">
    <h2>Training</h2>
    <p class="muted">Project setup must complete before training can start.</p>
</section>
{% endif %}
```

Pass `project.parallel_runs_enabled` to the template context in the project route (see below), and expose it as a data attribute:

```html
<div id="run-list" class="run-list"
     data-name="{{ project.name }}"
     data-parallel="{{ 'true' if project.parallel_runs_enabled else 'false' }}"
     data-default-branch="{{ project.branch }}">
```

- [ ] **Step 3: Update project route to pass training info as runs list**

In `routes/project.py`, find the `detail` view. Update it to pass `runs` (list) instead of the single `training` status dict:

```python
from services.process_manager import get_runs_for_project, get_training_status

# Replace:
#   training = get_training_status(project.name)
# With:
    runs = get_runs_for_project(project.name)
    training = get_training_status(project.name)  # keep for backward-compat with status badge
```

Pass `runs=runs` to the template render call.

- [ ] **Step 4: Rewrite training.js**

Replace `static/js/training.js` entirely:

```javascript
/**
 * training.js — parallel run list for the project page.
 * Manages multiple active training runs: start, stop, inline SSE logs.
 */

const runListEl = document.getElementById('run-list');
if (!runListEl) { throw new Error('run-list element not found'); }

const PROJECT_NAME = runListEl.dataset.name;
const PARALLEL_ENABLED = runListEl.dataset.parallel === 'true';
const DEFAULT_BRANCH = runListEl.dataset.defaultBranch || 'main';

// Map of run_id -> { branch, status, sseSource, logEl, elapsedEl, startedAt }
const activeRuns = new Map();

// --- Branch picker ---
const startBtn = document.getElementById('btn-start-run');
const branchPicker = document.getElementById('branch-picker');
const branchSelect = document.getElementById('run-branch-select');
const confirmStartBtn = document.getElementById('btn-confirm-start');
const cancelStartBtn = document.getElementById('btn-cancel-start');

if (startBtn) {
    startBtn.addEventListener('click', async () => {
        branchPicker.classList.add('open');
        startBtn.disabled = true;
        await loadBranches();
    });
}
if (cancelStartBtn) {
    cancelStartBtn.addEventListener('click', () => {
        branchPicker.classList.remove('open');
        startBtn.disabled = false;
    });
}
if (confirmStartBtn) {
    confirmStartBtn.addEventListener('click', () => startRun(branchSelect.value));
}

async function loadBranches() {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/branches`);
        const data = await r.json();
        const branches = data?.data?.branches || [];
        branchSelect.innerHTML = '';
        branches.forEach(b => {
            const opt = document.createElement('option');
            opt.value = b;
            opt.textContent = b === DEFAULT_BRANCH ? `${b} (default)` : b;
            if (b === DEFAULT_BRANCH) opt.selected = true;
            branchSelect.appendChild(opt);
        });
        if (!branches.includes(DEFAULT_BRANCH)) {
            const opt = document.createElement('option');
            opt.value = DEFAULT_BRANCH;
            opt.textContent = `${DEFAULT_BRANCH} (default)`;
            opt.selected = true;
            branchSelect.insertBefore(opt, branchSelect.firstChild);
        }
    } catch (e) {
        console.warn('Could not load branches:', e);
    }
}

async function startRun(branch) {
    branchPicker.classList.remove('open');
    startBtn.disabled = false;
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/start`, {
            method: 'POST',
            body: JSON.stringify({ branch }),
        });
        const data = await r.json();
        if (!data.success) {
            alert(`Could not start run: ${data.error?.message || 'unknown error'}`);
            return;
        }
        // Poll until the run appears in status
        setTimeout(refreshRuns, 2000);
    } catch (e) {
        alert(`Could not start run: ${e}`);
    }
}

// --- Stop a run ---
async function stopRun(runId) {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/stop`, {
            method: 'POST',
            body: JSON.stringify({ run_id: runId }),
        });
        const data = await r.json();
        if (!data.success) {
            alert(`Could not stop run: ${data.error?.message || 'unknown error'}`);
        }
        setTimeout(refreshRuns, 1500);
    } catch (e) {
        alert(`Could not stop run: ${e}`);
    }
}

// --- Run row rendering ---
function renderRunRow(run) {
    const { run_id, branch, status, pid, elapsed, tb_port } = run;
    const div = document.createElement('div');
    div.className = `run-row ${status === 'running' || status === 'starting' ? 'run-active' : ''}`;
    div.id = `run-row-${run_id}`;
    div.innerHTML = `
        <div class="run-row-header">
            <span class="status-badge status-${status === 'starting' ? 'running' : status}">${status}</span>
            <span class="run-branch">${branch}</span>
            <span class="run-id">#${run_id}</span>
            ${pid ? `<span class="muted" style="font-size:0.75rem;">PID ${pid}</span>` : ''}
            <span class="run-elapsed" id="elapsed-${run_id}">
                ${elapsed ? formatElapsed(elapsed) : ''}
            </span>
            ${tb_port ? `<a href="http://${location.hostname}:${tb_port}" target="_blank" class="btn btn-secondary btn-sm">Tensorboard</a>` : ''}
            <button class="btn btn-secondary btn-sm" id="btn-logs-${run_id}">▶ Logs</button>
            ${status !== 'starting' ? `<button class="btn btn-danger btn-sm" id="btn-stop-${run_id}">■ Stop</button>` : ''}
        </div>
        <div class="run-log-panel" id="log-panel-${run_id}" style="display:none">
            <div class="run-log-controls">
                <button class="btn btn-secondary btn-sm" id="btn-clear-log-${run_id}">Clear</button>
                <a href="/projects/${PROJECT_NAME}/logs/download?run_id=${run_id}" class="btn btn-secondary btn-sm">Download</a>
            </div>
            <pre class="run-log-terminal" id="log-terminal-${run_id}"></pre>
        </div>
    `;

    const logsBtn = div.querySelector(`#btn-logs-${run_id}`);
    const logPanel = div.querySelector(`#log-panel-${run_id}`);
    const logTerminal = div.querySelector(`#log-terminal-${run_id}`);
    const stopBtn = div.querySelector(`#btn-stop-${run_id}`);
    const clearBtn = div.querySelector(`#btn-clear-log-${run_id}`);

    logsBtn.addEventListener('click', () => toggleLogs(run_id, logPanel, logTerminal, logsBtn, status));
    if (stopBtn) stopBtn.addEventListener('click', () => stopRun(run_id));
    if (clearBtn) clearBtn.addEventListener('click', () => { logTerminal.textContent = ''; });

    // Auto-expand logs for active runs
    if (status === 'running') {
        logPanel.style.display = '';
        logsBtn.textContent = '▼ Logs';
        startLogStream(run_id, logTerminal);
    }

    return div;
}

function toggleLogs(runId, logPanel, logTerminal, logsBtn, status) {
    const isOpen = logPanel.style.display !== 'none';
    if (isOpen) {
        logPanel.style.display = 'none';
        logsBtn.textContent = '▶ Logs';
        stopLogStream(runId);
    } else {
        logPanel.style.display = '';
        logsBtn.textContent = '▼ Logs';
        if (status === 'running' || status === 'starting') {
            startLogStream(runId, logTerminal);
        } else {
            // Load historical log
            loadHistoricalLog(runId, logTerminal);
        }
    }
}

// --- SSE log streaming ---
function startLogStream(runId, terminalEl) {
    if (activeRuns.has(runId) && activeRuns.get(runId).sseSource) return;
    const src = new EventSource(
        `/projects/${PROJECT_NAME}/logs/stream?run_id=${runId}&tail=500`
    );
    src.onmessage = (e) => {
        if (e.data) {
            terminalEl.textContent += e.data + '\n';
            terminalEl.scrollTop = terminalEl.scrollHeight;
        }
    };
    src.addEventListener('done', () => {
        src.close();
        if (activeRuns.has(runId)) {
            activeRuns.get(runId).sseSource = null;
        }
        setTimeout(refreshRuns, 1000);
    });
    src.onerror = () => src.close();
    if (activeRuns.has(runId)) {
        activeRuns.get(runId).sseSource = src;
    }
}

function stopLogStream(runId) {
    const state = activeRuns.get(runId);
    if (state?.sseSource) {
        state.sseSource.close();
        state.sseSource = null;
    }
}

async function loadHistoricalLog(runId, terminalEl) {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/logs?run_id=${runId}&tail=500`);
        const data = await r.json();
        terminalEl.textContent = data?.data?.content || '(no log)';
        terminalEl.scrollTop = terminalEl.scrollHeight;
    } catch (e) {
        terminalEl.textContent = `(error loading log: ${e})`;
    }
}

// --- Refresh runs from API ---
async function refreshRuns() {
    try {
        const r = await apiFetch(`/api/v1/projects/${PROJECT_NAME}/training/status`);
        const data = await r.json();
        const runs = data?.data?.runs || [];
        reconcileRunList(runs);
        updateStartButton(runs);
    } catch (e) {
        console.warn('Could not refresh runs:', e);
    }
}

function reconcileRunList(runs) {
    const currentIds = new Set(runs.map(r => r.run_id));

    // Remove rows for runs that finished
    for (const [runId] of activeRuns) {
        if (!currentIds.has(runId)) {
            stopLogStream(runId);
            activeRuns.delete(runId);
            const row = document.getElementById(`run-row-${runId}`);
            if (row) row.remove();
        }
    }

    // Add or update rows for current runs
    for (const run of runs) {
        if (!activeRuns.has(run.run_id)) {
            activeRuns.set(run.run_id, { branch: run.branch, startedAt: Date.now() - (run.elapsed || 0) * 1000 });
            const row = renderRunRow(run);
            runListEl.appendChild(row);
        } else {
            // Update elapsed
            const elapsedEl = document.getElementById(`elapsed-${run.run_id}`);
            if (elapsedEl && run.elapsed) {
                elapsedEl.textContent = formatElapsed(run.elapsed);
            }
        }
    }

    // Show idle state if no runs
    if (runs.length === 0 && runListEl.children.length === 0) {
        const idle = document.createElement('p');
        idle.className = 'muted';
        idle.id = 'run-idle-msg';
        idle.textContent = 'No active runs.';
        runListEl.appendChild(idle);
    } else {
        const idleMsg = document.getElementById('run-idle-msg');
        if (idleMsg && runs.length > 0) idleMsg.remove();
    }
}

function updateStartButton(runs) {
    if (!startBtn) return;
    if (!PARALLEL_ENABLED && runs.length > 0) {
        startBtn.disabled = true;
        startBtn.title = 'Enable parallel runs in project settings to run multiple branches';
    } else {
        startBtn.disabled = false;
        startBtn.title = '';
    }
}

// --- Elapsed timer ---
function formatElapsed(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
}

setInterval(async () => {
    for (const [runId, state] of activeRuns) {
        const elapsedEl = document.getElementById(`elapsed-${runId}`);
        if (elapsedEl && state.startedAt) {
            elapsedEl.textContent = formatElapsed((Date.now() - state.startedAt) / 1000);
        }
    }
}, 5000);

// --- API helper ---
function apiFetch(url, opts = {}) {
    return fetch(url, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
    });
}

// --- Init ---
refreshRuns();
setInterval(refreshRuns, 10000);
```

- [ ] **Step 5: Restart and verify locally**

```bash
sudo /usr/bin/systemctl restart beekeeper && sudo /usr/bin/systemctl status beekeeper
```

Open http://localhost:5000. On a project page:
- Verify the Training section shows "No active runs." and a "+ Start Run…" button
- Click "+ Start Run…", verify branch picker appears with the project's default branch
- Start a run, verify the run row appears with status badge, branch, `#ID`, elapsed, Stop button, ▾ Logs
- Expand logs, verify SSE stream works
- Stop the run, verify the row disappears

- [ ] **Step 6: Run full test suite**

```bash
make test 2>&1 | tail -20
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add templates/project.html routes/project.py static/js/training.js static/css/style.css
git commit -m "feat: replace single training control with parallel run list, inline logs, branch picker"
```

---

## Task 11b: Gemini review — frontend

- [ ] **Step 1: Run Gemini review**

```bash
cat /home/robertcowher/pythonprojects/beekeeper/static/js/training.js | \
  gemini -p "Review this JavaScript for a Flask training management dashboard. Focus on: (1) SSE stream lifecycle — are streams cleaned up on row removal and page unload? (2) race conditions — can reconcileRunList produce duplicate rows? (3) the 'Start Run' button disabled state — is it correctly managed across parallel/non-parallel modes? (4) any obvious UX issues with the inline log panel. Return a prioritized issue list only."
```

- [ ] **Step 2: Address any critical issues found**

---

## Task 12: Final test run and smoke test

- [ ] **Step 1: Run full test suite**

```bash
make test 2>&1 | tail -30
```
Expected: all 55+ tests pass. If any fail, fix them before continuing.

- [ ] **Step 2: Smoke test parallel run (single-run project)**

Start a project that has `parallel_runs_enabled=False`. Start one run. Verify:
- Run row appears with run ID visible
- Second "Start Run…" click is disabled/errors

- [ ] **Step 3: Smoke test parallel runs (parallel-enabled project)**

Enable parallel runs on a project (via Edit). Start run on branch A. Immediately start run on branch B. Verify:
- Both run rows appear simultaneously
- Each has its own run ID
- Logs are independent per row
- Stopping one does not affect the other
- After both stop, `train_status` returns to idle

- [ ] **Step 4: Smoke test /capacity**

```bash
curl -s http://localhost:5000/api/v1/capacity | python3 -m json.tool
```
Verify `total_slots`, `running`, `available`, and `projects` are all present and correct.

- [ ] **Step 5: Smoke test MCP get_capacity**

```bash
curl -s http://localhost:5000/api/v1/capacity
```

- [ ] **Step 6: Final commit**

```bash
git add -A
git status  # confirm nothing unexpected
git commit -m "feat: parallel runs complete — run ID visible in UI, capacity API, MCP tools updated"
```
