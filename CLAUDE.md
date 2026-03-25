# Claude Code Instructions

## Git Workflow

When on the `develop` branch, go ahead and commit & push changes without asking.

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

## Recent Work (March 23, 2026)

- **Agent Integration SDK** - Downloadable single-file Python SDK for AI agent automation
  - Endpoint: `GET /api/v1/projects/<name>/agent/sdk`
  - Pre-configured BeekeeperClient class with all API methods
  - Download button in project API section
  - See: CHANGELOG.md v1.0.5-beta, softwarespec.md

Previous work (March 2026):
- TensorBoard Metrics Analysis API with intelligent analysis (v1.0.4-beta)
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
- deploy.sh handles everything: stops service, pulls from git, starts service
- Deploys to remote host "lab" at 192.168.1.57:5000

```bash
# What deploy.sh does:
ssh -t lab 'cd /home/bobcowher/beekeeper && sudo systemctl stop beekeeper && git pull && sudo systemctl start beekeeper'
```
