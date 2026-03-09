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

## Recent Work (March 2026)

- Added "Running" status with blue color to distinguish from "Ready" (green)
- Dashboard now shows training status for ready projects (running/crashed) or "Ready" if idle
- Fixed status badge alignment with fixed-width columns
- Made Danger Zone collapsible (starts closed)
- Collapsible JS moved to app.js for shared use across all pages
- Fixed HTML entity rendering for collapse arrows (use Unicode directly)

## Deployment

Local `deploy.sh` (gitignored) deploys to remote host "lab":
```
ssh -t lab 'cd /home/bobcowher/beekeeper && sudo systemctl stop beekeeper && git pull && sudo systemctl start beekeeper'
```
