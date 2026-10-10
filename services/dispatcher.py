"""Single background loop: drain per-project run queues, then advance pipelines.

tick() is idempotent and reads all state from SQLite and project.json, so a
restart simply resumes. Tests call tick() directly; the thread never starts
under TESTING.
"""
import json
import logging
import os
import threading
import time

from models.project import SETUP_ACTIVE_STATUSES
from services import process_manager
from services.db_service import get_db

log = logging.getLogger(__name__)

DISPATCH_INTERVAL = 5          # seconds between ticks
PURGE_INTERVAL = 3600          # seconds between queue-history purges
QUEUE_RETENTION_DAYS = 30
TRANSIENT_CODES = {"ALREADY_RUNNING", "AT_CAPACITY", "INSUFFICIENT_VRAM"}

_thread = None
_last_purge = 0.0


def _setup_active(projects_dir, name) -> bool:
    try:
        with open(os.path.join(projects_dir, name, "project.json")) as f:  # NOSONAR
            return json.load(f).get("setup_status") in SETUP_ACTIVE_STATUSES
    except (OSError, ValueError):
        return False


def _is_transient(code, projects_dir, name) -> bool:
    if code in TRANSIENT_CODES:
        return True
    return code == "SETUP_INCOMPLETE" and _setup_active(projects_dir, name)


def dispatch_project_queue(projects_dir, name) -> None:
    """Try to start the oldest queued run for one project."""
    db = get_db()
    queued = db.list_queue(name)
    if not queued:
        return
    entry = queued[0]
    result = process_manager.start_training(
        projects_dir, name, branch=entry["branch"], extra_env=entry["env_vars"] or None)
    if "error" not in result:
        run = db.get_run(name, result["run_id"])
        db.mark_queue_started(entry["id"], run["id"])
        log.info("Queue: started %s from queue entry %s", result.get("run_ref"), entry["id"])
    elif _is_transient(result.get("code"), projects_dir, name):
        if entry.get("wait_reason") != result["error"]:
            db.set_queue_wait_reason(entry["id"], result["error"])
    else:
        db.mark_queue_failed(entry["id"], result["error"])
        log.warning("Queue: entry %s for %s failed: %s", entry["id"], name, result["error"])


def _maybe_purge():
    global _last_purge
    now = time.time()
    if now - _last_purge >= PURGE_INTERVAL:
        _last_purge = now
        get_db().purge_old_queue_entries(days=QUEUE_RETENTION_DAYS)


def tick(projects_dir, pipelines_dir=None) -> None:
    for name in get_db().projects_with_queued_runs():
        try:
            dispatch_project_queue(projects_dir, name)
        except Exception:
            log.exception("Queue dispatch failed for %s", name)
    if pipelines_dir:
        from services import pipeline_engine
        try:
            pipeline_engine.tick(projects_dir, pipelines_dir)
        except Exception:
            log.exception("Pipeline pass failed")
    try:
        _maybe_purge()
    except Exception:
        log.exception("Queue purge failed")


def start_dispatcher(app) -> None:
    global _thread
    if _thread is not None:
        return
    projects_dir = app.config["PROJECTS_DIR"]
    pipelines_dir = app.config["PIPELINES_DIR"]

    def loop():
        while True:
            try:
                tick(projects_dir, pipelines_dir)
            except Exception:
                log.exception("Dispatcher tick failed")
            time.sleep(DISPATCH_INTERVAL)

    _thread = threading.Thread(target=loop, name="beekeeper-dispatcher", daemon=True)
    _thread.start()
