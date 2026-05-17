# Beekeeper — Architecture

## What It Is

Beekeeper is a single-server Flask web app that manages long-running AI training processes. It clones a git repo, sets up a Python environment, launches the training script as a subprocess, streams its output via SSE, and embeds TensorBoard in an iframe. It exposes a REST API (`/api/v1/`) that an MCP server (`beekeeper_mcp/`) uses to give AI agents programmatic control over training runs.

---

## Directory Layout

```
app.py                      Flask app factory; sets BEEKEEPER_HOME; registers all blueprints
setup.sh                    Installs venv, creates systemd service, generates .secret_key

routes/
  dashboard.py              GET /  — project list
  project.py                Project CRUD, edit, delete
  training.py               Start/stop training, SSE log stream, log download, run history
  files.py                  File browser, zip download, inline file viewer
  stats.py                  GET /api/stats — GPU/CPU/memory polling
  api_v1.py                 Full REST API under /api/v1/ (used by MCP and external clients)
  auth.py                   Login, logout, register (first-user setup)
  admin.py                  User management, API key management, config UI

services/
  process_manager.py        Core: subprocess lifecycle, log file management, TensorBoard mgmt
  project_service.py        Git clone, env creation, pip install, retry logic
  tensorboard_service.py    TensorBoard metrics parsing, run directory pruning
  run_storage_service.py    Persistent run storage paths, log archival helpers
  auth_service.py           bcrypt hashing, session management, API key auth decorator
  db_service.py             SQLite access: users, sessions, API keys, training run history
  config_service.py         .properties file for runtime config (auth.enabled, etc.)
  stats_service.py          nvitop + psutil wrappers for system stats
  python_versions.py        Discovers available Python/conda versions (cached)
  agent_sdk_generator.py    Generates a downloadable Python SDK for a given project
  rate_limiter.py           Request rate limiting
  resource_tracker.py       GPU/CPU resource tracking across runs

models/
  project.py                Project dataclass — fields, save(), load(), to_dict()
  user.py                   User dataclass — fields, is_locked()

templates/                  Jinja2 HTML templates
static/
  css/style.css             Yellow/dark VSCode-inspired theme
  js/app.js                 System stats polling
  js/dashboard.js           Sort + pin logic
  js/training.js            Training controls, SSE log stream, collapsibles
  js/files.js               File browser UI, inline viewer modal
  js/history.js             Run history table, filtering, tagging

beekeeper_mcp/              Standalone pip package — MCP server exposing the /api/v1/ API as tools
  server.py                 All MCP tool definitions; reads BEEKEEPER_HOST + BEEKEEPER_API_KEY

tests/                      pytest suite (115 tests, no browser required except test_browser.py)
```

---

## Storage Layout

```
BEEKEEPER_HOME/             = the directory beekeeper is installed/checked out into
  projects/
    <name>/
      project.json          Project config and current status (source of truth for project state)
      workspace/            Git clone of the training repo (reset --hard before every run)
      venv/                 Python venv (or conda env stored elsewhere, referenced by name)
      train.log             Live log for the current/most recent non-parallel run
      train-<run_id>.log    Live log for a parallel run
      persistent/
        runs/
          run_<id>/         Survives workspace cleanup: TensorBoard logs, model outputs
  data/
    beekeeper.db            SQLite: users, sessions, api_keys, training_runs
  .secret_key               Flask session signing key (chmod 600, never committed)
  .config                   Runtime config (.properties format)
```

---

## Key Data Flows

### Project setup
1. `POST /api/v1/projects` → `api_v1.py` validates input, creates `Project`, calls `project_service.setup_project()`
2. `setup_project()` spawns a background thread running `_setup_project()`
3. Thread updates `project.json` at each stage: `cloning` → `creating_env` → `running_setup_script` → `installing` → `ready` (or `error`)
4. UI polls `GET /api/v1/projects/<name>` to watch `setup_status`

### Training start
1. `POST /api/v1/projects/<name>/training/start` → `start_training()` in `process_manager.py`
2. Pre-allocates a DB run record (status=`starting`), spawns background thread `_execute_training()`
3. Thread: git fetch + reset → data dir symlink → setup script → pip install → `Popen` the training script
4. A second thread `_monitor_process()` polls `process.poll()` until exit, then archives the log, finalises the DB record, and cleans up parallel workspaces
5. On clean exit, a third thread runs `tensorboard_service.parse_run_metrics()` to extract TB scalars into the DB

### Log streaming
1. `GET /<name>/logs/stream?tail=N` → SSE generator in `training.py`
2. `_tail_offset()` does a reverse seek to find the byte offset of the last N lines
3. Generator loops: read new bytes from offset → yield as `data:` SSE events → sleep 0.5s
4. Terminates when the run is no longer active and no new data arrives for 2+ cycles

### TensorBoard
- Starts automatically alongside training, targeting `persistent/runs/` (and `workspace/runs/` if legacy data exists)
- Each project gets one TB process; port allocated from 6006 upward
- TB process migrates to a standalone entry in `_tb_running` when training stops, so the UI stays live
- Idle reaper kills TB processes after 30 minutes of no UI access
- `tensorboard_service.py` also handles pruning old run directories, keeping at most `tb_logs_max_runs` entries

---

## Design Decisions

**JSON files for project config, not a database**
Each project's config and status lives in `projects/<name>/project.json`. This makes projects trivially inspectable, manually editable, and portable. It also means there's no migration story — adding a field just means adding a default in the dataclass.

**SQLite for auth and run history**
Auth (users, sessions, API keys) and run history are relational: you query across rows, join tables, need atomic updates. SQLite handles this well and adds no external dependency.

**Single gunicorn worker (`-w 1`)**
`process_manager.py` tracks running processes in a module-level dict (`_running`, `_tb_running`) protected by a `threading.Lock`. Multiple workers would each have their own copy of this dict, making state invisible across workers. Single worker + multiple threads is correct here.

**SSE for log streaming, not WebSocket**
SSE is unidirectional (server → client), which is all log streaming needs. It works through nginx without special config, degrades gracefully, and requires no JS library.

**Git remote is always authoritative**
Before every training run: `git fetch origin` then `git reset --hard origin/<branch>`. Never `git pull`. This prevents local divergence from ever accumulating.

**`validate_workspace_path` / `_safe_path` on all user-supplied paths**
Any path coming from project config (tensorboard log dir, train file, setup script) is validated with `validate_workspace_path()` before use. File browser requests go through `_safe_path()` which uses `os.path.realpath` and a path-separator boundary check to prevent traversal.

**`BEEKEEPER_HOME` = install directory**
`app.py` sets `BEEKEEPER_HOME = os.path.dirname(os.path.abspath(__file__))`. All paths — projects dir, DB, config file, secret key — derive from this. Keeps deployment simple: one directory, one service, no external config.

**MCP as a separate pip package**
`beekeeper_mcp/` is a standalone package that speaks only to the `/api/v1/` HTTP API. It has no direct import dependency on the Flask app. This means agents can run on a different machine from the Beekeeper server.

---

## Invariants

- **Project name = directory name.** `projects/<name>/` is the canonical project directory. Renaming a project is not supported.
- **`project.json` is the source of truth for project state.** `train_status`, `setup_status`, and all config live there. The in-memory `_running` dict is transient and not persisted.
- **All user-supplied workspace paths must pass `validate_workspace_path()`.** This includes tensorboard log dirs, train files, setup scripts, and output paths.
- **Do not add a second gunicorn worker** without first replacing the in-memory state in `process_manager.py` with a shared store (Redis, SQLite, etc.).
- **Do not use `git pull`.** Always `fetch` + `reset --hard origin/<branch>`.
- **Auth is optional.** All routes must remain accessible when auth is disabled. The `api_key_required` decorator is a no-op when auth is off.

---

## Testing

```bash
source venv/bin/activate
make test           # pytest, all non-browser tests
make coverage       # pytest with coverage report
make test-browser   # Playwright UI tests (requires playwright install)
```

Tests use a temp-directory fixture for project storage and mock the database and process manager where needed. Browser tests are optional and marked with `@pytest.mark.browser`.
