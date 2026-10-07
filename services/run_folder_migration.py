"""One-time rename of persistent run folders from run_<pk> to run_<run_id>."""
import datetime
import glob
import json
import logging
import os

import psutil

from services.db_service import get_db
from services.run_storage_service import MARKER, read_run_marker as _marker

log = logging.getLogger(__name__)

RUN_FOLDER_MIGRATION = "run_folders_per_project_numbers"
LEGACY_LOG_MIGRATION = "run_logs_unarchived_pk_named"


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


def archive_pk_named_log(projects_dir: str, run: dict) -> str:
    """Move projects/<name>/train-<pk>.log into run_logs/ under the run's number.

    Uses the archive naming run-<ts>-<run_number:04d>.log (ts = the log's mtime) and
    returns the project-relative path; the caller records it as log_file_path. Raises
    OSError (FileExistsError if that archive name is taken).
    """
    project_dir = os.path.join(projects_dir, run["project_name"])
    src = os.path.join(project_dir, f"train-{run['id']}.log")
    ts = datetime.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y%m%d-%H%M%S")
    rel = f"run_logs/run-{ts}-{run['run_number']:04d}.log"
    dest = os.path.join(project_dir, rel)
    if os.path.exists(dest):
        raise FileExistsError(f"{dest} exists")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.rename(src, dest)
    return rel


def _archive_legacy_live_logs(projects_dir: str, db) -> list[str]:
    """Archive live logs still named train-<pk>.log (unarchived runs from before run numbers).

    Code now reads train-<run_number>.log, so such a log would be shown for the wrong run
    and truncated by a later run whose number equals the pk. Returns conflict strings.
    """
    with db._get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project_name, run_number, log_file_path FROM training_runs"
        )]
    numbers = {(r["project_name"], r["run_number"]) for r in rows}
    conflicts = []
    for r in rows:
        if r["log_file_path"] or r["run_number"] == r["id"]:
            continue
        project_dir = os.path.join(projects_dir, r["project_name"])
        src = os.path.join(project_dir, f"train-{r['id']}.log")
        if not os.path.isfile(src):
            continue
        if (r["project_name"], r["id"]) in numbers:
            # A run numbered <pk> exists in this project: it truncated and owns this file.
            log.warning("Leaving %s: it belongs to %s run #%s", src, r["project_name"], r["id"])
            continue
        try:
            rel = archive_pk_named_log(projects_dir, r)
        except OSError as e:
            conflicts.append(f"{r['project_name']}: could not archive {src}: {e}")
            continue
        db.update_training_run(r["id"], log_file_path=rel)
    return conflicts


def _repoint_workspace_links(projects_dir: str, migrated: list[dict]) -> list[str]:
    """Re-point workspace redirect symlinks (absolute run_<pk> targets) at run_<number>.

    All new targets are computed from the links as they stand, in one lookup against the
    old-path -> new-path mapping, so a path reused by another run is never chained.
    Returns conflict strings for links that could not be re-pointed.
    """
    by_project: dict[str, dict[str, str]] = {}
    for r in migrated:
        base = os.path.join(projects_dir, r["project_name"], "persistent", "runs")
        old = os.path.normpath(os.path.join(base, f"run_{r['id']}"))
        if r.get("resumed") and os.path.exists(old):
            continue  # run_<pk> now belongs to a newer run; links pointing at it are its own
        by_project.setdefault(r["project_name"], {})[old] = os.path.join(base, f"run_{r['run_number']}")

    conflicts = []
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
                try:
                    if not os.path.islink(link):
                        continue
                    target = os.path.normpath(os.readlink(link))
                except OSError as e:
                    conflicts.append(f"{name}: could not read link {link}: {e}")
                    continue
                for old, new in mapping.items():
                    if target == old or target.startswith(old + os.sep):
                        planned.append((link, new + target[len(old):]))
                        break
        for link, new_target in planned:
            try:
                os.unlink(link)
                os.symlink(new_target, link)
            except OSError as e:
                log.exception("Could not re-point workspace link %s -> %s", link, new_target)
                conflicts.append(f"{name}: could not re-point {link}: {e}")
    return conflicts


def migrate_run_folders(projects_dir: str) -> str:
    """Rename persistent/runs/run_<pk> to run_<run_number> for every run, after archiving
    live logs still named train-<pk>.log.

    Resumable: safe to kill at any point and rerun. Every row's stored persistent_dir always
    names the folder that actually holds its data (run_<pk>, .migrating_<pk>, or
    run_<number>). Per-row filesystem errors are recorded as conflicts and the remaining
    rows still migrate. Returns "done", "already_applied", "deferred" (live training
    process), or "conflicts" (left unapplied; retried at the next startup).
    """
    db = get_db()
    logs_done = db.migration_applied(LEGACY_LOG_MIGRATION)
    folders_done = db.migration_applied(RUN_FOLDER_MIGRATION)
    if logs_done and folders_done:
        return "already_applied"

    # Both steps move files a live training process may be writing: defer them all.
    live = _live_run_dirs(projects_dir)
    if live:
        log.warning("Deferring run folder migration: %d training process(es) live: %s", len(live), live)
        return "deferred"

    conflicts = []
    if not logs_done:
        log_conflicts = _archive_legacy_live_logs(projects_dir, db)
        if not log_conflicts:
            db.mark_migration_applied(LEGACY_LOG_MIGRATION)
        conflicts += log_conflicts
    if not folders_done:
        conflicts += _migrate_folders(projects_dir, db)

    if conflicts:
        for c in conflicts:
            log.error("Run folder migration conflict: %s", c)
        return "conflicts"
    return "done"


def _migrate_folders(projects_dir: str, db) -> list[str]:
    with db._get_connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, project_name, run_number, persistent_dir, tensorboard_dir FROM training_runs"
        )]
    todo = []
    for r in rows:
        if r["persistent_dir"] == f"persistent/runs/.migrating_{r['id']}":
            r["resumed"] = True    # staged by an earlier attempt that hit a conflict
            todo.append(r)
        elif r["persistent_dir"] == f"persistent/runs/run_{r['id']}" and r["run_number"] != r["id"]:
            r["resumed"] = False
            todo.append(r)
    conflicts = []

    def paths(r):
        base = os.path.join(projects_dir, r["project_name"], "persistent", "runs")
        return (os.path.join(base, f"run_{r['id']}"),
                os.path.join(base, f".migrating_{r['id']}"),
                os.path.join(base, f"run_{r['run_number']}"))

    def set_dir(r, new_rel):
        if r["persistent_dir"] == new_rel:
            return
        fields = {"persistent_dir": new_rel}
        if r["tensorboard_dir"] == r["persistent_dir"]:
            fields["tensorboard_dir"] = new_rel
        db.update_training_run(r["id"], **fields)
        r["persistent_dir"] = new_rel
        if "tensorboard_dir" in fields:
            r["tensorboard_dir"] = new_rel

    # Pass 1: tag each pk-named folder with its owner and move it out of the way.
    # A folder carrying another run's marker belongs to that run (already placed by an
    # earlier attempt): never touch it; this run's own folder is then treated as missing.
    # Rows resumed from .migrating_<pk> are already staged: run_<pk> is not theirs.
    pass1_conflicts = set()
    for r in todo:
        if r["resumed"]:
            continue
        src, staged, _ = paths(r)
        if not os.path.isdir(src) or _marker(src) not in (None, r["id"]):
            continue
        if os.path.exists(staged):
            conflicts.append(f"{r['project_name']}: both {src} and {staged} exist")
            pass1_conflicts.add(r["id"])
            continue
        try:
            with open(os.path.join(src, MARKER), "w") as f:
                f.write(str(r["id"]))
            os.rename(src, staged)
        except Exception as e:
            # Folder (and the row's pointer to it) stays at run_<pk>; retried next startup.
            log.exception("Could not stage %s", src)
            conflicts.append(f"{r['project_name']}: could not stage {src}: {e}")
            pass1_conflicts.add(r["id"])

    # Pass 2: place each staged folder at run_<number> and record it.
    migrated = []

    def place(r):
        _, staged, dest = paths(r)
        new_rel = f"persistent/runs/run_{r['run_number']}"
        staged_rel = f"persistent/runs/.migrating_{r['id']}"
        if os.path.isdir(staged):
            if os.path.exists(dest):
                conflicts.append(f"{r['project_name']}: {dest} exists; {staged} left in place")
                set_dir(r, staged_rel)
                return
            try:
                os.rename(staged, dest)
            except OSError as e:
                conflicts.append(f"{r['project_name']}: could not place {staged} at {dest}: {e}")
                set_dir(r, staged_rel)
                return
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

    for r in todo:
        if r["id"] in pass1_conflicts:
            continue  # conflict already recorded; row still points at its run_<pk> folder
        try:
            place(r)
        except Exception as e:
            # E.g. a DB error after a rename: the next startup re-derives this row's state
            # (an own-marker folder at run_<number> is adopted); new runs refuse to reuse
            # a folder another row still stores.
            log.exception("Could not migrate run folder for run_pk %s", r["id"])
            conflicts.append(f"{r['project_name']}: run_pk {r['id']}: {e}")

    conflicts += _repoint_workspace_links(projects_dir, migrated)

    if not conflicts:
        db.mark_migration_applied(RUN_FOLDER_MIGRATION)
        log.info("Run folder migration complete: %d run(s) updated", len(todo))
    return conflicts
