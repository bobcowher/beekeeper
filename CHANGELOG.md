# Changelog

## [1.0.6] - 2026-03-30

### Bug Fixes

**Retry Setup Now Pulls Latest Code**
Fixed an issue where clicking "Retry Setup" would reuse the old workspace without pulling updates from git. This meant fixes to `setup.sh` or other repo files wouldn't be picked up on retry.

- **Clean workspace on retry:** The workspace directory is now deleted before re-running setup
- **Fresh git clone:** Latest code is pulled from the repository
- **Data safety:** Data directories are preserved (symlinks are deleted, but actual data at `data_dir_remote` is untouched)
- **Venv preserved:** Python environments (venv/conda) are reused to save time

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
