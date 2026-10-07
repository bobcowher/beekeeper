"""One-time rename of persistent run folders from run_<pk> to run_<run_id>."""
import logging
import os

import psutil

from services.db_service import get_db

log = logging.getLogger(__name__)

RUN_FOLDER_MIGRATION = "run_folders_per_project_numbers"
MARKER = ".beekeeper_run_pk"


def _live_run_dirs(projects_dir: str) -> list[str]:
    """BEEKEEPER_RUN_DIR values of live processes that point under projects_dir."""
    root = os.path.realpath(projects_dir) + os.sep
    found = []
    for proc in psutil.process_iter(["environ"]):
        try:
            env = proc.info.get("environ") or {}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        run_dir = env.get("BEEKEEPER_RUN_DIR")
        if run_dir and os.path.realpath(run_dir).startswith(root):
            found.append(run_dir)
    return found


def _marker(path: str) -> int | None:
    try:
        with open(os.path.join(path, MARKER)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def migrate_run_folders(projects_dir: str) -> str:
    """Rename persistent/runs/run_<pk> to run_<run_number> for every run.

    Resumable: safe to kill at any point and rerun. Returns "done",
    "already_applied", "deferred" (live training process), or "conflicts".
    """
    db = get_db()
    if db.migration_applied(RUN_FOLDER_MIGRATION):
        return "already_applied"

    live = _live_run_dirs(projects_dir)
    if live:
        log.warning("Deferring run folder migration: %d training process(es) live: %s", len(live), live)
        return "deferred"

    with db._get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project_name, run_number, persistent_dir, tensorboard_dir FROM training_runs"
        )]
    todo = [
        r for r in rows
        if r["persistent_dir"] == f"persistent/runs/run_{r['id']}" and r["run_number"] != r["id"]
    ]
    conflicts = []

    def paths(r):
        base = os.path.join(projects_dir, r["project_name"], "persistent", "runs")
        return (os.path.join(base, f"run_{r['id']}"),
                os.path.join(base, f".migrating_{r['id']}"),
                os.path.join(base, f"run_{r['run_number']}"))

    def set_dir(r, new_rel):
        fields = {"persistent_dir": new_rel}
        if r["tensorboard_dir"] == r["persistent_dir"]:
            fields["tensorboard_dir"] = new_rel
        db.update_training_run(r["id"], **fields)

    # Pass 1: tag each pk-named folder with its owner and move it out of the way.
    for r in todo:
        src, staged, _ = paths(r)
        if os.path.isdir(src) and os.path.exists(staged):
            conflicts.append(f"{r['project_name']}: both {src} and {staged} exist")
            continue
        if os.path.isdir(src):
            with open(os.path.join(src, MARKER), "w") as f:
                f.write(str(r["id"]))
            os.rename(src, staged)

    # Pass 2: place each staged folder at run_<number> and record it.
    for r in todo:
        src, staged, dest = paths(r)
        new_rel = f"persistent/runs/run_{r['run_number']}"
        if os.path.isdir(src) and os.path.exists(staged):
            continue  # conflict recorded in pass 1
        if os.path.isdir(staged):
            if os.path.exists(dest):
                conflicts.append(f"{r['project_name']}: {dest} exists; {staged} left in place")
                continue
            os.rename(staged, dest)
            set_dir(r, new_rel)
        elif os.path.isdir(dest):
            if _marker(dest) == r["id"]:
                set_dir(r, new_rel)        # moved by an interrupted earlier attempt
            else:
                set_dir(r, None)           # foreign folder; this run's own folder is gone
        else:
            set_dir(r, new_rel)            # folder already gone (pruned/never created)

    if conflicts:
        for c in conflicts:
            log.error("Run folder migration conflict: %s", c)
        return "conflicts"

    db.mark_migration_applied(RUN_FOLDER_MIGRATION)
    log.info("Run folder migration complete: %d run(s) updated", len(todo))
    return "done"
