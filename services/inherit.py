"""Seeding a pipeline step's run folder with output paths from the step before it.

Each inherited path is copied with `cp -a --reflink=auto`, so a filesystem with
reflinks (btrfs, XFS) shares the blocks and ext4 gets a full copy. The manifest at
.beekeeper/inherited.json records what came from where (sizes, not hashes: R6).
"""
import json
import os
import shutil
import subprocess

MANIFEST = os.path.join(".beekeeper", "inherited.json")


class InheritError(Exception):
    pass


def _tree_size(path) -> tuple[int, int]:
    """(files, bytes) under path, not following symlinks."""
    if os.path.isfile(path) or os.path.islink(path):
        return 1, os.lstat(path).st_size
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
                files += 1
            except OSError:
                pass
    return files, total


def source_bytes(src_run_dir, paths) -> int:
    """Bytes a seeding would copy; missing paths count as 0 (the launch check reports them)."""
    return sum(_tree_size(os.path.join(src_run_dir, rel))[1]
               for rel in paths if os.path.lexists(os.path.join(src_run_dir, rel)))


def missing_paths(src_run_dir, paths) -> list[str]:
    return [rel for rel in paths if not os.path.lexists(os.path.join(src_run_dir or "", rel))]


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def seed_inherited(src_run_dir, dst_run_dir, paths, source_step=None, source_run_ref=None) -> list[dict]:
    """Copy each path from src_run_dir to the same relative path in dst_run_dir; write the manifest.

    Raises InheritError when a path is missing from the source (nothing is copied then) or
    a copy fails. A path already in dst_run_dir is replaced: on a clash the later copy wins.
    """
    label = source_run_ref or os.path.basename(os.path.normpath(src_run_dir))
    missing = missing_paths(src_run_dir, paths)
    if missing:
        raise InheritError(f"inherit: {missing[0]} not found in {label}'s run folder")
    entries = []
    for rel in paths:
        src, dst = os.path.join(src_run_dir, rel), os.path.join(dst_run_dir, rel)
        if os.path.lexists(dst):
            _remove(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        result = subprocess.run(["cp", "-a", "--reflink=auto", src, dst], capture_output=True, text=True)
        if result.returncode != 0:
            raise InheritError(f"inherit: could not copy {rel}: {result.stderr.strip()[-300:]}")
        files, size = _tree_size(dst)
        entries.append({"path": rel, "source_step": source_step, "source_run_ref": source_run_ref,
                        "files": files, "bytes": size})
    os.makedirs(os.path.join(dst_run_dir, ".beekeeper"), exist_ok=True)
    with open(os.path.join(dst_run_dir, MANIFEST), "w") as f:
        json.dump({"inherited": entries}, f, indent=2)
    return entries


def read_manifest(run_dir) -> list[dict]:
    try:
        with open(os.path.join(run_dir, MANIFEST)) as f:
            return json.load(f).get("inherited") or []
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def format_size(n: int) -> str:
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def log_lines(entries) -> list[str]:
    """The run log lines for a seeding: one per path, plus a warning for each empty one."""
    lines = []
    for e in entries:
        files = f"{e['files']} file{'' if e['files'] == 1 else 's'}"
        lines.append(f"[beekeeper] Inherited {e['path']}/ from {e['source_step']} ({e['source_run_ref']}): "
                     f"{files}, {format_size(e['bytes'])}")
        if e["files"] == 0:
            lines.append(f"[beekeeper] WARNING: inherited {e['path']}/ is empty")
    return lines
