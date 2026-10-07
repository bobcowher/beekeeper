"""One-time rename of persistent run folders from run_<pk> to run_<run_id>."""
import glob
import json
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


def _repoint_workspace_links(projects_dir: str, migrated: list[dict]) -> None:
    """Re-point workspace redirect symlinks (absolute run_<pk> targets) at run_<number>.

    All new targets are computed from the links as they stand, in one lookup against the
    old-path -> new-path mapping, so a path reused by another run is never chained.
    """
    by_project: dict[str, dict[str, str]] = {}
    for r in migrated:
        base = os.path.join(projects_dir, r["project_name"], "persistent", "runs")
        by_project.setdefault(r["project_name"], {})[
            os.path.normpath(os.path.join(base, f"run_{r['id']}"))
        ] = os.path.join(base, f"run_{r['run_number']}")

    for name, mapping in by_project.items():
        project_dir = os.path.join(projects_dir, name)
        try:
            with open(os.path.join(project_dir, "project.json")) as f:
                cfg = json.load(f)
            rels = [cfg.get("tensorboard_log_dir") or "runs"] + list(cfg.get("output_paths") or [])
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        workspaces = [project_dir + os.sep + "workspace"] + sorted(glob.glob(project_dir + os.sep + "workspace-*"))
        planned = []
        for ws in workspaces:
            for rel in rels:
                link = os.path.join(ws, str(rel))
                if not os.path.islink(link):
                    continue
                target = os.path.normpath(os.readlink(link))
                for old, new in mapping.items():
                    if target == old or target.startswith(old + os.sep):
                        planned.append((link, new + target[len(old):]))
                        break
        for link, new_target in planned:
            try:
                os.unlink(link)
                os.symlink(new_target, link)
            except OSError:
                log.exception("Could not re-point workspace link %s -> %s", link, new_target)


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
    # A folder carrying another run's marker belongs to that run (already placed by an
    # earlier attempt): never touch it; this run's own folder is then treated as missing.
    pass1_conflicts = set()
    for r in todo:
        src, staged, _ = paths(r)
        if not os.path.isdir(src) or _marker(src) not in (None, r["id"]):
            continue
        if os.path.exists(staged):
            conflicts.append(f"{r['project_name']}: both {src} and {staged} exist")
            pass1_conflicts.add(r["id"])
            continue
        with open(os.path.join(src, MARKER), "w") as f:
            f.write(str(r["id"]))
        os.rename(src, staged)

    # Pass 2: place each staged folder at run_<number> and record it.
    migrated = []
    for r in todo:
        if r["id"] in pass1_conflicts:
            continue  # conflict already recorded; row left untouched for the next startup
        _, staged, dest = paths(r)
        new_rel = f"persistent/runs/run_{r['run_number']}"
        if os.path.isdir(staged):
            if os.path.exists(dest):
                conflicts.append(f"{r['project_name']}: {dest} exists; {staged} left in place")
                continue
            os.rename(staged, dest)
            set_dir(r, new_rel)
            migrated.append(r)
        elif os.path.isdir(dest):
            if _marker(dest) == r["id"]:
                set_dir(r, new_rel)        # moved by an interrupted earlier attempt
                migrated.append(r)
            else:
                set_dir(r, None)           # foreign folder; this run's own folder is gone
        else:
            set_dir(r, new_rel)            # folder already gone (pruned/never created)
            migrated.append(r)

    _repoint_workspace_links(projects_dir, migrated)

    if conflicts:
        for c in conflicts:
            log.error("Run folder migration conflict: %s", c)
        return "conflicts"

    db.mark_migration_applied(RUN_FOLDER_MIGRATION)
    log.info("Run folder migration complete: %d run(s) updated", len(todo))
    return "done"
