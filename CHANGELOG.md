# Changelog

## [Unreleased]

---

## [1.0.8] - 2026-05-16

### New Features

- **Secret key auto-generation** — Beekeeper now generates and persists a secure random key in `.secret_key` on first start. No user configuration needed; `BEEKEEPER_SECRET` env var still works as an override.
- `GET /api/v1/version` — returns `server_version` and `min_mcp_version`; no auth required
- MCP `get_version()` tool — compares installed MCP version against server's minimum, returns `outdated` flag with reinstall message if behind
- MCP `update_project` tool — update project settings (branch, train file, TB dir, env vars, parallel runs) from an agent
- MCP `get_capacity` tool — returns training slot capacity: total, running, and available headroom

### Bug Fixes

- Branch switching now blocked during `starting` state — previously a switch could corrupt a run's workspace during the pre-launch git pull
- Improved 400 error when starting a run while one is active and parallel runs are disabled — message now explains how to enable parallel runs
- Fixed potential None-dereference crash in login path when a user record exists but has no stored password hash

### Maintenance

- Added `ARCHITECTURE.md` — component map, storage layout, data flows, design decisions, and invariants for maintainers and agents
- Added `AGENT_CONTEXT.md` — navigation table, silent-failure traps, and testing conventions for AI agents working in the codebase
- Rewrote `README.md` — removed original spec; added troubleshooting section

---

## [1.0.7] - 2026-04-30

### New Features

**Parallel Runs**
- Run multiple training branches simultaneously on the same project
- Enable per-project via Edit → Parallel Runs toggle and Max Parallel Runs setting
- Each parallel run gets its own workspace clone, log file, and Tensorboard instance
- Parallel workspaces are cleaned up automatically when the run completes
- `POST /api/v1/projects/<name>/training/start` accepts an optional `branch` parameter
- `GET /api/v1/projects/<name>/training/status` returns a `runs` array, one entry per active run
- `POST /api/v1/projects/<name>/training/stop` accepts an optional `run_id` parameter

**Run List UI**
- Training section replaced with a live run list — each active run is its own row
- Each row shows: status badge, branch, run ID, elapsed time, Tensorboard link, inline logs, stop button
- Branch picker visible by default (no extra click required); picks up available branches on load
- `+ Start Run…` button appears below the run list when parallel runs are enabled and a run is active
- Inline log terminal per run — capped at 2,000 lines to prevent memory bloat on long runs
- Run rows re-render automatically on status transitions (e.g. `starting` → `running`)

**Run Annotations**
- Star any run in Run History to mark it as notable (⭐ button on each row)
- Starred runs are exempt from automatic pruning — they'll never be deleted by Cleanup Old Runs
- Add tags to runs (comma-separated, e.g. `baseline,lr=0.01`) — searchable via filter bar
- Add freeform notes to any run — saved automatically on blur
- Filter run history by starred status or tag text

**Run Compare / Diff**
- Select two runs in Run History and click **Compare selected**
- Modal shows both runs side by side: branch, status, commit, duration, tags, notes
- Includes a `git diff` between the two commits — see exactly what code changed between runs

**Run ID in History Table**
- Run History table now has a `#` column showing the run ID
- Matches the ID shown in the active run list and log file headers

**MCP Server**
- Replaced CLI binary with a Python MCP server (`mcp_server.py`) using `fastmcp`
- Install via pip: `pip install beekeeper-mcp` (version 0.1.0)
- Or run directly from the repo: `python mcp_server.py`
- MCP tools: `list_projects`, `get_project`, `create_project`, `retry_setup`, `delete_project`, `get_project_instructions`, `start_training`, `stop_training`, `training_status`, `get_logs`, `analyze_run`, `list_branches`, `switch_branch`, `get_stats`, `check_busy`
- Configured via `BEEKEEPER_HOST` and `BEEKEEPER_API_KEY` env vars
- Claude Code registration: `claude mcp add beekeeper -s user -e BEEKEEPER_HOST=http://server:5000 -- beekeeper-mcp`

**MCP documentation page** (`/api/v1/mcp`)
- Setup guide with config snippet, tool reference, and example workflows

**Agent instructions updated to MCP**
- Global and project-specific agent instruction endpoints now describe MCP tools
- `get_project_instructions(name)` replaces old CLI-based instructions

**Branch Switching**
- Active Branch dropdown in the Project Info card — switch branches without editing the project
- Beekeeper runs `git fetch` + `git reset --hard origin/<branch>` for a clean switch
- API: `GET /api/v1/projects/<name>/branches` (list), `POST /api/v1/projects/<name>/branch` (switch)

**EMA-Smoothed Metric Analysis**
- `recent_trend` field now computed on EMA-smoothed values (was: raw values)
- New `late_slope_pct` field: slope of the last 20% of training, normalized as % of total metric range
- `smoothed_points` (detail=medium) returns ~100 EMA-smoothed curve points for plotting
- `smoothed_final_value`: EMA value at end of training (more stable than raw final)
- Peak detection uses EMA-smoothed signal — single noisy episodes no longer mask the true peak

**TensorBoard Log Retention Management**
- `tb_logs_max_runs` project setting (default: 10) — auto-prune old TB run dirs on training start
- Manual cleanup UI in TensorBoard section with configurable keep count
- API endpoint: `POST /api/v1/projects/{name}/tensorboard/cleanup`

**Other**
- `GET /api/v1/busy` — check if any training is running (useful before deploy/restart)
- Clone project: UI button and `POST /api/v1/projects/<name>/clone`
- `POST /api/v1/projects` — create projects via API
- Comprehensive API docs at `/api/v1/docs`
- File browser: last-modified timestamps, sortable columns, copy-curl menu item
- Project-level resource tracking (CPU, RAM, GPU) in training controls section

### Breaking Changes

- CLI binary and `beekeeper-cli` repo dependency removed
- `/api/v1/cli/version` endpoint removed
- `/api/v1/cli` page removed (replaced by `/api/v1/mcp`)

### Bug Fixes

- Fixed stale `train_status: running` in project JSON after server restart
- Fixed path traversal check in file browser (`startswith` → `realpath` + separator boundary)
- Fixed file browser modification times displaying as NaN
- Fixed `setup.sh` running in base Python env instead of project conda/venv env
- Fixed Retry Setup not pulling latest code from git before re-running setup
- Fixed TB log cleanup off-by-one (new empty dir consumed one keep-count slot)
- Fixed inline log terminal XSS: branch/status now HTML-escaped before `innerHTML`

---

## [1.0.6] - 2026-03-30

### Bug Fixes

**Retry Setup Now Pulls Latest Code**
Fixed an issue where clicking "Retry Setup" would reuse the old workspace without pulling updates from git. This meant fixes to `setup.sh` or other repo files wouldn't be picked up on retry.

- **Clean workspace on retry:** The workspace directory is now deleted before re-running setup
- **Fresh git clone:** Latest code is pulled from the repository
- **Data safety:** Data directories are preserved (symlinks are deleted, but actual data at `data_dir_remote` is untouched)
- **Venv preserved:** Python environments (venv/conda) are reused to save time

**Setup Script Now Runs in Activated Environment**
Fixed an issue where `setup.sh` was running in the base Python environment instead of the project's conda/venv environment. This caused pip install commands in setup scripts to install packages to the wrong location.

- **Conda projects:** Uses `conda run -n env_name bash setup.sh`
- **Venv projects:** Sets `VIRTUAL_ENV` and `PATH` environment variables
- **Applies to both:** Initial project setup and pre-training execution
- **Result:** Python/pip commands in setup.sh now correctly resolve to the project environment

---

## [1.0.5] - 2026-03-23

### New Features

**Agent Instructions**
AI agents can now access comprehensive, project-specific API documentation directly from the project page.

- **Copy/paste ready:** Agent Instructions section in project API tab
- **Project-specific:** All examples pre-filled with actual project name, URL, and details
- **Complete API coverage:** All endpoints with request/response formats and common workflows
- **Agent-optimized:** Written for AI agents to read and understand, not traditional SDK documentation
- **No file management:** Just copy instructions and provide to your AI agent

**Use Case:** Open project page, copy Agent Instructions, paste into Claude or save to beekeeper.md—agent can immediately control training.

---

## [1.0.4] - 2026-03-22

### New Features

**TensorBoard Metrics Analysis API**
AI agents and automation tools can now query training metrics via REST API with intelligent analysis instead of raw time series data.

- **Multi-level detail:** Summary (default), sampled points (medium), or full data (high)
- **Automatic analysis:** Trend detection (improving/stable/unstable), convergence analysis, anomaly detection
- **Smart sampling:** ~100 key points including first, last, min, max, inflection points
- **Background processing:** Metrics are parsed and cached automatically when training completes
- **Works with any metrics:** Auto-detects all scalar metrics from PyTorch SummaryWriter, TensorFlow, or any TFEvents producer

**New API Endpoints:**
- `GET /api/v1/projects/<name>/tensorboard/latest` - Latest completed run metrics
- `GET /api/v1/projects/<name>/runs/<run_id>/metrics` - Specific run metrics

**Dependencies Added:**
- `tbparse==0.0.8` - Lightweight TensorBoard parser (no TensorFlow dependency)
- `numpy` - Statistical analysis

**See Also:** Updated `API_IMPLEMENTATION.md` with full endpoint documentation and response format examples.

---

## [1.0.2] - 2026-03-14

### New Features

**Inline File Viewer**
Files in the workspace can now be previewed directly in the browser without downloading. Click any viewable filename or the **view** button in the Files section to open a modal viewer.

- **Images** (png, jpg, jpeg, gif, webp, svg, bmp, ico) — rendered inline and **auto-refreshed every 2 seconds**, so debug images update live as training writes them. Uses a preload-swap approach to avoid flicker.
- **Text files** (py, log, json, yaml, md, sh, csv, toml, js, ts, html, xml, and more) — displayed in a monospace viewer. Files over 1 MB fall back to download.

Non-viewable file types continue to download as before.

**Dashboard Sort and Pin**
The project list now has sort controls and per-project pinning.

- **Sort** — toggle between **Last Run** (default, most recently started training floats up) and **A–Z**. Preference is saved per browser via `localStorage`. Projects that have never been run sort to the bottom of Last Run order.
- **Pin** — click the 📌 icon on any project to pin it. Pinned projects always appear at the top of the list above the sort section, regardless of sort order. Pinning state persists in `project.json`.

---

## [1.0.1] - 2026-03-09

### New Features

**Run Log Banners**
Each training run now opens and closes with a structured banner in `train.log`. The header shows timestamp, hostname, git commit SHA + message, branch, Python version, training script, and GPU model/VRAM. The footer shows elapsed time and exit status (COMPLETED or CRASHED).

**Setup Script Support**
Projects can specify an optional shell script (e.g. `setup.sh`) to run before pip install — both during initial project setup and before each training run. Useful for downloading datasets, linking shared weights, or any system-level prep.

**Data Directory (Symlink)**
Projects can map a persistent volume or NAS share into the workspace via a symlink. Configure a local path (e.g. `data`) and a system path (e.g. `/mnt/nas/datasets`) — Beekeeper creates and verifies the symlink at setup and before each run.

**Auto pip install Before Each Run**
Dependencies are installed/updated from the requirements file before every training run, ensuring newly added packages are always present without a manual re-setup.

**Retry Setup**
When project setup fails, a **Retry Setup** button appears. It skips the git clone if `workspace/` already exists and skips environment creation if the venv/conda env is already there, resuming from the failed step.

**Python Version Caching**
The New Project page no longer runs a slow `conda search python` on every load. Available Python versions are cached after the first discovery.

**Running Status (Blue)**
Training status now distinguishes "Running" (blue) from "Ready" (green). The dashboard shows running/crashed status for active projects.

**Collapsible Danger Zone**
The Delete Project section starts collapsed to reduce accidental clicks.

**Mobile Responsive Layout**
The sidebar collapses behind a hamburger menu at ≤768px. Forms and training controls stack vertically on small screens.

**Automated Test Suite**
55 tests covering API endpoints, page rendering, log tail algorithm, path traversal security, project model save/load, and the training pre-launch sequence. Run with `make test`.

### Bug Fixes

- Fixed collapsible sections not working on page load (wrapped in `DOMContentLoaded`)
- Fixed collapse arrow display (HTML entities → Unicode)
- Fixed status badge alignment on dashboard (fixed-width columns)
- Fixed path traversal check in file browser (`startswith` → `realpath` + separator boundary)
- Fixed dashboard status display and alignment

### Internal

- Cloned repo directory renamed from `src/` to `workspace/`
- Collapsible JS moved from `training.js` to `app.js` (shared across all pages)
