# Beekeeper

Beekeeper is a lightweight web app for managing AI training runs on a remote server. It handles cloning your repo, setting up the Python environment, streaming live logs, displaying TensorBoard, browsing output files, and optionally gating access behind user authentication and API keys.

---

## Setup

```bash
git clone https://github.com/bobcowher/beekeeper.git
cd beekeeper
bash setup.sh
```

> Tested on Ubuntu. Other Debian-based distros should work; other Linux distributions are untested.

---

## Authentication

Authentication is **optional and disabled by default**.

When enabled:
- Email/password login with bcrypt hashing
- Admin panel for user management
- API keys for agent/programmatic access
- Session-based web auth with lockout after 5 failed attempts

See [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) for setup and configuration.

---

## Known Limitations

- **Private repos** — Beekeeper clones over HTTPS with no credential support. Use public repos, or configure SSH agent forwarding on the server.
- **HTTPS** — Run Beekeeper behind a reverse proxy (nginx, Caddy, etc.) for TLS.
- **Single server** — One Beekeeper instance manages one server. Multi-server coordination is not supported.

---

## Troubleshooting

### Service won't start or keeps restarting
```bash
journalctl -u beekeeper -n 50
```
Common causes: port 5000 already in use, missing Python dependency, bad `BEEKEEPER_SECRET`.

### Port 5000 already in use
Find and kill the conflicting process, or edit the `--bind` line in the systemd service file (`/etc/systemd/system/beekeeper.service`) and re-run `sudo systemctl daemon-reload && sudo systemctl restart beekeeper`.

### Project setup is stuck or failed
A red error card appears on the project page. Hit **Retry Setup** — it skips steps that already completed (clone, env creation). Check `setup_error` in `projects/<name>/project.json` for the raw error.

### Training crashes immediately
Open the log — it's the first place the pre-launch sequence writes failure details. Most common causes: `pip install` failed (missing system dependency, bad requirements.txt), or the training file path is wrong.

### TensorBoard won't load
TensorBoard starts automatically on the first training run. If it doesn't appear:
- Check that `tensorboard` is in your project's `requirements.txt`
- The TB log dir setting must match what your training script writes to
- Ports 6006–6099 must be reachable from your browser — check firewall rules

### Locked out of your account
```bash
./admin.sh reset-password your@email.com
```

### MCP agent can't connect to Beekeeper
- Check `BEEKEEPER_HOST` is set correctly in your MCP config (format: `http://<host>:5000`)
- If auth is enabled, the agent needs `BEEKEEPER_API_KEY` set — generate one in the admin panel
- Verify Beekeeper is reachable: `curl http://<host>:5000/api/v1/version`

### Python version not available in the dropdown
Beekeeper discovers Python versions via conda or the system. Run `conda env list` or `python3 --version` on the server to confirm what's available. The dropdown caches on page load — refresh after installing a new version.

### Git clone times out
Large repos can exceed the 300s clone timeout. Consider a shallow clone in your repo, or pre-clone the workspace manually to `projects/<name>/workspace/` before hitting Retry Setup.

---

## Further Reading

- [Changelog](CHANGELOG.md)
- [Authentication](docs/AUTHENTICATION.md)
- [Site & documentation](https://www.teaandrobots.com/software/beekeeper/)
