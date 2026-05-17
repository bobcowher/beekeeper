# Agent Context — Beekeeper

This file is the starting point for AI agents working in this codebase.

**Read [`ARCHITECTURE.md`](ARCHITECTURE.md) first.** It covers the component map, storage layout, key data flows, design decisions, and invariants. Everything below assumes you've read it.

---

## Navigation Quick Reference

| I need to... | Look in... |
|---|---|
| Change how training starts or stops | `services/process_manager.py` — `start_training()`, `_execute_training()`, `stop_training()` |
| Change setup (clone, env, pip install) | `services/project_service.py` — `_setup_project()` |
| Change the REST API | `routes/api_v1.py` |
| Change MCP tools | `beekeeper_mcp/server.py` |
| Change the project data model | `models/project.py` — add field + default, tests will catch missing keys |
| Change auth logic | `services/auth_service.py`, `routes/auth.py` |
| Change user/session/run-history storage | `services/db_service.py` |
| Change log streaming | `routes/training.py` — `logs_stream()` and `_tail_offset()` |
| Change TensorBoard integration | `services/tensorboard_service.py`, `services/process_manager.py` — `start_tensorboard()` |
| Change file browser | `routes/files.py`, `static/js/files.js` |
| Change system stats | `services/stats_service.py`, `static/js/app.js` |
| Change the UI theme or layout | `static/css/style.css`, `templates/base.html` |
| Change dashboard sort/pin | `static/js/dashboard.js`, `routes/dashboard.py` |

---

## Before You Change Anything

1. **Run the tests first** to establish a baseline: `source venv/bin/activate && python -m pytest tests/ -q`
2. **Read `ARCHITECTURE.md` — especially the Invariants section.** Several decisions look wrong until you understand why they exist (single worker, no git pull, etc.).
3. **Check `models/project.py`** before adding any field that touches project config — new fields need a default value in the dataclass or existing projects break on load.

---

## Things That Will Break Silently If You Get Them Wrong

- **Adding a second gunicorn worker** — `_running` and `_tb_running` dicts in `process_manager.py` are in-memory. Two workers = two independent views of running processes = chaos.
- **Using `git pull` instead of fetch + reset** — `git pull` merges, which can create local commits and diverge from remote.
- **Skipping `validate_workspace_path()`** on any user-supplied path — path traversal vulnerability.
- **Adding a project field without a default** — `Project.from_dict()` loads from JSON; missing keys with no default cause a `KeyError` on existing projects.
- **Writing to `project.json` outside `Project.save()`** — the dataclass is the only safe writer; direct JSON writes risk partial writes and schema drift.

---

## Testing

```bash
python -m pytest tests/ -q                    # full suite (~5s)
python -m pytest tests/test_model.py -v       # project model only
python -m pytest tests/test_security.py -v    # path traversal guards
python -m pytest -m browser                   # Playwright UI tests (optional)
```

When you add a new `Project` field, update `test_to_dict_contains_all_fields` in `tests/test_model.py`.

When you add a new route, add a smoke test to `tests/test_smoke.py` (DOM ID contract) and an API shape test to `tests/test_api.py`.

---

## MCP Server

The MCP server (`beekeeper_mcp/`) is a separate pip package with no import dependency on the Flask app — it speaks only HTTP to `/api/v1/`. Changes to the API surface require a corresponding update to `beekeeper_mcp/server.py` and a version bump in both `beekeeper_mcp/server.py` (`MCP_VERSION`) and `pyproject.toml`.

`BEEKEEPER_HOST` defaults to `http://localhost:5000`. When documenting configuration, always use placeholder values — never real IPs or hostnames — to prevent agents from copying stale addresses into their configs.

---

## Updating This Document

Update `ARCHITECTURE.md` when:
- A new service is added or an existing one changes its responsibility
- A new design decision is made (and the rationale is worth preserving)
- An invariant changes

Update this file (`AGENT_CONTEXT.md`) when:
- The navigation table becomes wrong (route moves, service renamed)
- A new "silent failure" trap is discovered
- Testing conventions change
