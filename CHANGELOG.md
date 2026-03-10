# Changelog

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
