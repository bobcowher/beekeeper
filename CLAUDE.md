# Claude Code Instructions

## Git Workflow

When on the `develop` branch, go ahead and commit & push changes without asking.

## AI Agent API Usage

**When interacting with Beekeeper as an AI agent, ask for permission once to use all Beekeeper API endpoints.** After receiving permission, you can freely use:
- GET requests (project data, logs, status, metrics, branches, etc.)
- POST requests (start/stop training, switch branches, create projects, etc.)
- Any other Beekeeper API endpoints at `/api/v1/*`

Do not ask for permission repeatedly for each individual endpoint.

## Project Overview

Beekeeper is a Flask web app for managing ML training projects. It provides:
- Project setup (git clone, venv/conda, pip install)
- Training controls (start/stop)
- Log streaming and Tensorboard integration
- File browser

## Key Files

- `static/css/style.css` - All styling, VSCode-inspired dark theme with yellow accent
- `static/js/app.js` - Shared JS (collapsible sections, stats polling)
- `static/js/training.js` - Training controls, log streaming, TB management
- `templates/dashboard.html` - Main projects list
- `templates/project.html` - Individual project view
- `models/project.py` - Project dataclass with `setup_status` and `train_status` fields

## Status Colors

- **Green** (`--success: #4ec94e`) - Ready state
- **Blue** (`--running: #4a9eff`) - Running state
- **Yellow** (`--accent: #e8b931`) - Setup in progress (pending, cloning, installing_deps)
- **Red** (`--danger: #c94040`) - Error/crashed states
- **Gray** (`--text-secondary`) - Idle/stopped states

## Recent Work (March 25, 2026)

- **Log Analysis Endpoint** - `GET /api/v1/projects/<name>/logs/analysis`
  - Parses episode data from logs, returns trend analysis (improving/stable/declining)
  - Quartile breakdown, recent averages - works for active runs without tensorboard
  - Designed for AI agent consumption without needing screenshots

- **Branch Switching** - Dropdown in Project Info section
  - `GET /api/v1/projects/<name>/branches` - list remote branches
  - `POST /api/v1/projects/<name>/branch` - switch branches (checks for uncommitted changes)
  - Run history now shows branch and commit SHA

- **Admin Panel Fix** - Admin link shows when auth is disabled (so you can enable it)

- **Agent Instructions Improved** - Prominent opener telling agents to use API first

Previous work (March 2026):
- Agent Integration SDK (v1.0.5-beta)
- TensorBoard Metrics Analysis API (v1.0.4-beta)
- Running status (blue) vs Ready (green) distinction
- Collapsible sections, status badges, inline file viewer

## Dependency Management

**ALWAYS install dependencies via requirements.txt in the venv.**

```bash
# Install in venv (tests the file works)
./venv/bin/pip install -r requirements.txt

# Then restart the service to pick up changes
sudo systemctl restart beekeeper
```

**Never use `pip install <package>` directly.** This ensures requirements.txt is always tested and never breaks project setup.

## URLs

- **Local development:** http://localhost:5000
- **Remote lab:** http://192.168.1.57:5000 (production, runs live training jobs)
- **Documentation:** /home/robertcowher/webapps/teaandrobots/content/software/beekeeper (hosted at teaandrobots.com)

## Development Workflow

**Default: Always test locally after making changes**

1. Make code changes
2. Restart and verify service is running:
   ```bash
   sudo /usr/bin/systemctl restart beekeeper
   sudo /usr/bin/systemctl status beekeeper
   ```
3. Test the feature at http://localhost:5000
4. Commit & push (on develop branch, no need to ask)
5. If ready for production, ask to run deploy.sh

**Passwordless sudo commands** (configured in sudoers):
```bash
sudo /usr/bin/systemctl start beekeeper
sudo /usr/bin/systemctl stop beekeeper
sudo /usr/bin/systemctl restart beekeeper
sudo /usr/bin/systemctl status beekeeper
```

Note: Must use exact command format above - no extra flags like `--no-pager`.

## Deployment

**IMPORTANT:** Always use `./deploy.sh` - never manually SSH or restart services.

- **Ask for permission once per session** before running deploy.sh
- **Never run deploy.sh the first time without asking** - remote host runs live production jobs
- Deploys to remote host "lab" at 192.168.1.57:5000
- deploy.sh does a FULL rebuild (stops service, pulls from git, deletes venv, runs setup.sh, starts service)
- This ensures clean deployment but takes ~1-2 minutes due to venv rebuild

```bash
# What deploy.sh actually does:
ssh lab 'cd /home/bobcowher/beekeeper && sudo systemctl stop beekeeper && git pull && sudo rm -rf venv && ./setup.sh -y'
```

Note: setup.sh rebuilds the venv and reinstalls all dependencies, then starts the service.

## Release Process

When the user says **"Release the code to the public"**, follow these steps:

1. **Update CHANGELOG.md** - Add a new version section with release notes for all changes
2. **Commit changelog** - Commit to develop branch
3. **Merge to main** - `git checkout main && git merge develop`
4. **Tag the release** - `git tag -a X.Y.Z -m "Release X.Y.Z - description"`
5. **Push everything** - `git push && git push --tags`
6. **Check documentation** - Verify `/home/robertcowher/webapps/teaandrobots/content/software/beekeeper/index.md` is current (usually no changes needed)

Version numbering:
- Patch (X.Y.Z): Bug fixes, no new features
- Minor (X.Y.0): New features, backwards compatible
- Major (X.0.0): Breaking changes

After release, switch back to develop: `git checkout develop`
