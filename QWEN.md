## Qwen Added Memories
- Beekeeper multi-server (static workers) implementation is complete and committed on the `multi-server` branch. Key architecture decisions:

1. **Database**: SQLite with migration-based schema evolution. Workers table added via `_run_migrations()` in db_service.py.

2. **Encryption**: Fernet symmetric encryption for worker auth credentials. Key stored at `data/worker.key` (0o600 perms), generated on first use. Crypto service is a global singleton initialized in app.py.

3. **SSH Worker Manager**: `services/ssh_worker.py` has two main classes:
   - `SSHWorkerConnection` — per-worker SSH connection via paramiko (connect, bootstrap, heartbeat, metrics, sync, training control)
   - `WorkerConnectionManager` — singleton that manages all connections and runs a 30s heartbeat thread

4. **Remote Agent**: A lightweight Python script is deployed to each worker via heredoc. It writes health status (CPU, memory, GPU) to `.bk-agent-status` JSON file every 10 seconds.

5. **Training Routing**: `routes/training.py` checks for assigned worker before start/stop/status/logs. If worker assigned → SSH route; if not → local (existing) route. Same UX for user either way.

6. **Workspace Sync**: Pull model (main host rsyncs from remote). Used for Tensorboard logs and file downloads.

7. **Config Gating**: `workers.enabled` in config.properties defaults to `false`. All worker UI/API behind this toggle.

8. **Project-Worker Assignment**: One-to-one (one project → one worker). Unassigned projects run locally.

9. **Dashboard**: `/api/stats` endpoint now includes `workers` array with remote GPU/CPU/RAM metrics.

Files created: models/worker.py, services/crypto_service.py, services/worker_service.py, services/ssh_worker.py, routes/worker_api.py, templates/admin_workers.html, tests/test_workers.py
- API rate limit feature completed: Default increased from 10 to 100 requests/minute (10x). Configurable via: (1) CLI: `./admin.sh set-config api.rate_limit_per_minute <value>`, (2) Web Admin UI: Settings form has "API Rate Limit (requests/minute)" field. Files modified: services/config_service.py (default value), services/auth_service.py (fallback default), admin.py (set-config command with validation), routes/admin.py (admin UI config dict and validation), templates/admin.html (form field), docs/AUTHENTICATION.md, AUTH_README.md. All changes committed.
