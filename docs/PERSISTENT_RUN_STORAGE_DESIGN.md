# Persistent Run Storage Design

## Summary

Beekeeper should treat workspaces as source checkouts and store generated run outputs in a durable project-local tree named `persistent`.

The canonical run artifact root should be:

```text
projects/<project>/persistent/runs/run_<run_id>/
```

Every run gets this directory before the training process starts. Beekeeper exports environment variables pointing at it, creates compatibility symlinks for common output paths, starts TensorBoard from the persistent run tree, and deletes the persistent run directory when the corresponding run record is deleted.

This should be always on. No new project-level toggle is needed.

## Goals

- Preserve TensorBoard events, checkpoints, model weights, and common output files outside transient parallel workspaces.
- Keep single-job projects simple: users do not need to enable a setting or learn a new model before running training.
- Make concurrent runs safe by default.
- Keep cleanup coherent: deleting a run deletes its stdout/stderr log, metric cache, and persistent artifacts together unless the run is protected/notable.
- Keep existing projects working through legacy path fallback.
- Avoid using `workspace/` as Beekeeper's durable artifact store.

## Non-Goals

- This is not a security boundary. Training code runs as the same user and can still intentionally delete files it can access.
- This does not capture every arbitrary output path. It protects user-configured paths and exposes environment variables for scripts that want an explicit contract.
- This does not add remote artifact storage.
- This does not add artifact browsing UI/API in the first implementation.
- This does not add a legacy migration tool in the first implementation.

## Current State

Beekeeper already captures stdout/stderr into `run_logs/`, so console logs are durable.

TensorBoard for parallel runs is partly protected today by a symlink from the transient workspace into the primary workspace. That means parallel TensorBoard data can survive deletion of `workspace-<run_id>/`.

The larger gap is generated non-TensorBoard artifacts from parallel runs: checkpoints, model files, exports, and generic outputs written into `workspace-<run_id>/` disappear when the transient workspace is removed.

Primary workspace risk is lower than initially framed because Beekeeper does `git fetch` and `git reset --hard`, not `git clean`. Untracked generated files usually survive in the primary workspace. Still, keeping durable run artifacts outside a git checkout is cleaner and makes single-run and parallel-run behavior consistent.

## Directory Layout

Project layout:

```text
projects/<project>/
  project.json
  workspace/
  workspace-<run_id>/          # transient, for parallel runs only
  run_logs/
  persistent/
    runs/
      run_<run_id>/
        events.out.tfevents.*  # TensorBoard event files live directly here
```

Canonical paths:

```text
persistent/runs/run_<run_id>
```

TensorBoard events should be written directly under `persistent/runs/run_<run_id>/`, not under a nested `tensorboard/` subdirectory. This keeps TensorBoard labels clean when the server watches `persistent/runs`.

## Environment Variables

Every training process should receive:

```text
BEEKEEPER_RUN_DIR=/abs/path/projects/<project>/persistent/runs/run_<run_id>
BEEKEEPER_TENSORBOARD_DIR=/abs/path/projects/<project>/persistent/runs/run_<run_id>
TENSORBOARD_LOG_DIR=/abs/path/projects/<project>/persistent/runs/run_<run_id>
```

`TENSORBOARD_LOG_DIR` is a generic compatibility alias. `BEEKEEPER_*` variables are the stable Beekeeper contract.

Beekeeper-owned values should win over project-defined `env_vars` for these reserved keys. If a project setting tries to define a reserved key, Beekeeper should use its own value and write a warning into the run log header.

Example warning:

```text
[beekeeper] Reserved env var BEEKEEPER_RUN_DIR was set in project settings.
[beekeeper] Beekeeper's run-specific value takes precedence.
```

## Workspace Symlink Compatibility

Many training scripts write to relative paths. Before launching training, Beekeeper should create symlinks from the active workspace into the persistent run directory.

The TensorBoard symlink is always known and should always be attempted:

```text
<workspace>/<tensorboard_log_dir> -> persistent/runs/run_<run_id>
```

Additional output paths should be user-configured through a project field:

```json
"output_paths": ["saved_models", "logs", "exports"]
```

`output_paths` defaults to `[]`. For each path in `output_paths`, Beekeeper creates:

```text
<workspace>/<path> -> persistent/runs/run_<run_id>/<path>
```

If `tensorboard_log_dir` is the default `runs`, this creates:

```text
workspace/runs -> persistent/runs/run_<run_id>
```

There should be no `persistent_output_paths_enabled` setting. Beekeeper should always attempt these symlinks.

Beekeeper should not hardcode non-TensorBoard paths such as `checkpoints`, `models`, or `outputs`. Those names are framework- and project-specific. Scripts that want durable storage without compatibility symlinks can write anywhere under `$BEEKEEPER_RUN_DIR`.

## Output Path Validation

`output_paths` entries must be safe workspace-relative directory paths.

Validation rules:

- no absolute paths
- no empty paths
- no `.` entry
- no `..` path segments
- no path segment may be empty
- no path may live under Beekeeper-owned roots such as `.git`, `persistent`, or `run_logs`
- no duplicate normalized paths
- no path may overlap another configured path as parent/child, such as `logs` and `logs/tb`
- no path may equal or overlap the configured `tensorboard_log_dir` as parent/child

Invalid paths should be rejected when project settings are saved or when a project is created through the API. Existing projects missing the field should default to an empty list.

## Conflict Handling

Beekeeper must avoid deleting user data in the primary workspace.

For each symlink target path:

1. If the path does not exist, create the symlink.
2. If the path is already a Beekeeper-managed symlink, replace it for the new run.
3. If the path exists as a regular file or directory in the primary workspace, leave it in place and log a warning.
4. If the path exists in a transient parallel workspace, replace it after clone/reset because that workspace is disposable.

Primary workspace warning example:

```text
[beekeeper] Output path 'saved_models' already exists and is not a Beekeeper symlink.
[beekeeper] Leaving it in place. Files written there are not protected by persistent storage.
[beekeeper] Use BEEKEEPER_RUN_DIR or remove/rename the existing path to enable protection.
```

Beekeeper should mark its symlinks so it can distinguish them from user-created symlinks. The simplest approach is to treat symlinks pointing under the current project's `persistent/runs/` tree as Beekeeper-managed.

## Database Changes

Add one column to `training_runs`:

```sql
ALTER TABLE training_runs ADD COLUMN persistent_dir TEXT;
```

For new runs:

```text
persistent_dir = persistent/runs/run_<run_id>
tensorboard_dir = persistent/runs/run_<run_id>
```

`tensorboard_dir` remains for API compatibility and metric parsing. For new runs it is project-relative and points directly at the run directory.

`output_paths` is a project setting, not a per-run DB field. Runtime artifact paths are derived from `persistent_dir` and the project's configured output paths.

## TensorBoard Behavior

Training-start TensorBoard and standalone on-demand TensorBoard must use the same launch-argument helper. Both must point at:

```text
projects/<project>/persistent/runs
```

This includes `start_tensorboard()`, which currently uses the workspace TensorBoard log directory. That function must change, otherwise clicking "Launch TensorBoard" while no run is active will miss new persistent runs.

Because event files live directly under each run directory, TensorBoard labels should be clean:

```text
run_123
run_124
```

Legacy logs under `workspace/runs` should remain discoverable for metrics and should be included in TensorBoard with `--logdir_spec` when that legacy directory exists and is non-empty:

```text
persistent:/path/to/persistent/runs,legacy:/path/to/workspace/runs
```

This keeps historical visual TensorBoard data visible in the iframe for existing projects while making `persistent/runs` the canonical location for new runs. New runs must use `persistent/runs`.

Implementation should use a shared helper, such as `_tb_launch_args(projects_dir, name, port)`, so training-start TensorBoard and standalone `start_tensorboard()` cannot drift. The helper should return either:

```text
tensorboard --logdir /path/to/persistent/runs --port <port> ...
```

or, when legacy `workspace/runs` exists and is non-empty:

```text
tensorboard --logdir_spec persistent:/path/to/persistent/runs,legacy:/path/to/workspace/runs --port <port> ...
```

## Path Resolution

Path resolution should prefer project-relative persistent paths and fall back to legacy workspace paths.

For a run's `tensorboard_dir`:

1. If `projects/<project>/<tensorboard_dir>` exists, use it.
2. Else if `projects/<project>/workspace/<tensorboard_dir>` exists, use it.
3. Else run existing TensorBoard auto-discovery against legacy locations such as `workspace/runs`, `workspace/logs`, and `workspace/tensorboard`.

This is important because new runs will store:

```text
persistent/runs/run_123
```

while old runs may store:

```text
runs/20260504-123456
runs/run_123
```

Code that blindly prepends `workspace/` to `tensorboard_dir` will break new persistent runs.

## Cleanup Semantics

Run cleanup should delete all Beekeeper-owned run artifacts together.

Deleting one run should delete:

- the DB row
- metric cache rows
- archived stdout/stderr log under `run_logs/`
- `persistent/runs/run_<run_id>/`

Retention cleanup should:

- sort runs by `started_at`
- keep the newest N non-notable runs
- always keep notable runs
- delete old DB rows, logs, metric cache rows, and persistent run directories

Clear-all-history should:

- delete all run records for the project
- delete associated `run_logs/` files
- delete `persistent/runs/`
- recreate an empty `persistent/runs/`

TensorBoard-only cleanup should be retired for new persistent runs:

- remove or guard the pre-launch `cleanup_old_tb_logs()` call so it only applies to legacy `workspace/runs` cleanup
- do not attempt to parse/prune `persistent/runs/run_<id>` directories with timestamp-based TensorBoard cleanup
- use run retention cleanup as the cleanup path for persistent run directories

The existing TensorBoard cleanup route/button can remain for compatibility, but for persistent runs it should route users toward run cleanup rather than deleting TensorBoard files independently.

## Existing Project Migration

Migration should be lazy and backward compatible.

On startup:

- add the `persistent_dir` DB column if missing
- add missing `output_paths` project setting with default `[]`
- do not move existing files

On new runs:

- create `persistent/runs/run_<run_id>/`
- write TensorBoard events and common outputs there through env vars and symlinks
- store `persistent_dir` and persistent `tensorboard_dir` in DB

For old runs:

- keep existing DB paths as-is
- rely on path resolution fallback and existing auto-discovery
- do not copy files automatically

No legacy migration tool is needed for the first implementation.

## API Impact

Existing run detail endpoints can add `persistent_dir` when available:

```json
{
  "id": 123,
  "status": "completed",
  "persistent_dir": "persistent/runs/run_123",
  "tensorboard_dir": "persistent/runs/run_123"
}
```

Artifact browsing endpoints are out of scope for this refactor. The layout should not block future endpoints such as:

```text
GET /api/v1/projects/<name>/runs/<run_id>/artifacts
GET /api/v1/projects/<name>/runs/<run_id>/artifacts/download?path=...
```

but they should not be part of the first implementation.

## UI Impact

Edit Project should add an "Output paths to protect" field. A comma-separated or one-path-per-line textarea is acceptable.

Suggested hint text:

```text
Workspace-relative directories your training script writes to, e.g. saved_models, exports.
TensorBoard logs are protected separately by the TensorBoard Log Dir setting.
```

Minimal UI changes:

- run history can show `persistent_dir` later, but does not need to in the first implementation
- TensorBoard launch should continue to work, now backed by `persistent/runs`
- cleanup labels should avoid implying that TensorBoard is the only run artifact
- project create/edit forms should validate `output_paths`

The first implementation should prioritize storage correctness over new UI surfaces.

## Implementation Plan

This should be one implementation phase.

1. Add `persistent_dir` migration to the database service.
2. Add `output_paths` to the project model and project/API create/edit flows, defaulting to `[]`.
3. Add helpers in `process_manager.py` to compute persistent paths from `project_name` and `run_id`.
4. Before training `Popen`, create:
   - `persistent/runs/run_<run_id>/`
5. Store `persistent_dir` and `tensorboard_dir` on the run record.
6. Create the TensorBoard workspace symlink and one workspace symlink for each validated `project.output_paths` entry using the conflict rules above.
7. Set reserved Beekeeper environment variables after project env vars with direct assignment, not `setdefault`, so Beekeeper values win.
8. Start training.
9. Start TensorBoard with the shared launch-argument helper so training-start TensorBoard and standalone TensorBoard both include conditional legacy `--logdir_spec`.
10. Remove existing workspace TensorBoard directory creation logic:
   - remove the parallel-run TensorBoard symlink block that redirects `workspace-<run_id>/runs` into primary `workspace/runs/run_<run_id>`
   - remove the single-run timestamp directory block that creates `workspace/runs/<timestamp>`
   - replace both with `persistent_dir`
11. Remove or guard the pre-launch `cleanup_old_tb_logs()` call so it is not used for persistent runs.
12. Update standalone `start_tensorboard()` to use the same shared launch-argument helper.
13. Update metric parsing path resolution to prefer project-relative paths, then workspace-relative legacy paths, then discovery.
14. Update run deletion, clear-history, and retention cleanup to delete `persistent/runs/run_<run_id>/`.
15. Add focused tests.

## Test Plan

Unit tests:

- persistent run directory is created before training `Popen`
- env vars point to persistent paths and override conflicting project env vars
- workspace TensorBoard symlink points at persistent run directory
- configured `output_paths` symlinks point at matching persistent subpaths
- invalid `output_paths` are rejected
- primary workspace conflicts are skipped and logged
- parallel workspace conflicts are replaced
- DB stores `persistent_dir` and project-relative `tensorboard_dir`
- training-start TensorBoard uses `persistent/runs`
- standalone `start_tensorboard()` uses `persistent/runs`
- standalone `start_tensorboard()` includes non-empty legacy `workspace/runs` via `--logdir_spec`
- metric parsing resolves project-relative persistent paths
- metric parsing still resolves legacy workspace paths
- run cleanup deletes persistent run directory
- notable run cleanup preserves persistent run directory
- pre-launch `cleanup_old_tb_logs()` is not called for persistent runs

Integration tests:

- single run writes TensorBoard events and appears in main TensorBoard
- two concurrent runs write TensorBoard events and both appear in main TensorBoard
- a run writes to a configured `output_paths` directory and files survive workspace cleanup
- stopped run leaves persistent artifacts
- crashed run leaves persistent artifacts
- run cleanup removes persistent artifacts for deleted runs

Manual smoke test:

1. Create a project with parallel runs enabled.
2. Configure `output_paths` with a project-specific artifact directory.
3. Start two runs that write TensorBoard events and files under that configured output path.
4. Confirm both appear under main TensorBoard as `run_<id>` labels.
5. Stop one run and let one complete.
6. Confirm both `persistent/runs/run_<id>/` directories remain.
7. Confirm configured output files remain under each persistent run directory.
8. Clear old run history.
9. Confirm matching persistent directories are removed.

## Risks

### Symlink Surprises

Replacing common output paths with symlinks can surprise users. Mitigation: skip existing primary workspace paths that are not Beekeeper-managed symlinks, and log the behavior clearly.

### Incomplete Artifact Capture

Scripts can write anywhere. Mitigation: protect user-configured paths via `output_paths` and expose `BEEKEEPER_RUN_DIR` for scripts that want an explicit contract.

### Disk Growth

Persistent storage makes artifacts survive. Mitigation: integrate persistent directories into run retention cleanup.

### Same-User Destructive Code

Training code can intentionally delete persistent paths. Mitigation: document this as safety improvement, not sandboxing.

### Legacy Visibility

Old TensorBoard logs should remain visible in standalone TensorBoard by conditionally including legacy roots with `--logdir_spec`. Metric path fallback remains the safety net for legacy runs whose visual TensorBoard data is not cleanly discoverable.

## Closed Decisions

- Standalone TensorBoard should include non-empty legacy `workspace/runs` via `--logdir_spec` in the first implementation.
- Non-TensorBoard output protection is user-configured through `output_paths`. No non-TensorBoard paths are hardcoded.
- TensorBoard-only cleanup is retired for new persistent runs. Run retention cleanup owns persistent run directory cleanup.
- Symlink conflict warnings in the run log are sufficient for v1. No extra UI/API warning surface is required now.

## Recommendation

Implement the simplified design in one phase:

- always create `persistent/runs/run_<run_id>/`
- always export Beekeeper persistent env vars
- always attempt the TensorBoard compatibility symlink, plus configured `output_paths` symlinks, with safe conflict handling
- store only one new DB field, `persistent_dir`
- store new `tensorboard_dir` values as project-relative persistent paths
- point all TensorBoard launch paths at `persistent/runs`
- delete persistent run directories with their run records

This keeps single-job usage simple and makes concurrent runs materially safer without adding a protection toggle. The only new project setting is the optional `output_paths` list for scripts that need additional workspace-relative artifact paths protected.

## Layout Tradeoff Commentary

The review recommends storing TensorBoard event files directly in `persistent/runs/run_<id>/` instead of `persistent/runs/run_<id>/tensorboard/`. I agree this is probably the right first implementation because it keeps TensorBoard labels clean when watching `persistent/runs`:

```text
run_123
run_124
```

The tradeoff is that a run directory now mixes TensorBoard event files with artifact subdirectories:

```text
persistent/runs/run_123/
  events.out.tfevents.*
  saved_models/
  exports/
```

That is less semantically tidy than a dedicated `tensorboard/` subdirectory, but TensorBoard ignores non-event files and the cleaner labels matter in the primary user-facing view. The alternative is to keep `tensorboard/` and launch TensorBoard with explicit `--logdir_spec` labels, but that adds more process-management code and makes dynamic discovery of newly created runs more complex.

My recommendation is to accept the slightly flatter layout now. If we later build richer artifact browsing, the UI can treat event files at the run root as Beekeeper-managed TensorBoard data and still present user-configured output paths as separate artifact groups.

---

## Design Complete (2026-05-04)

All review rounds closed. No open questions remain.

**Full change log across all review rounds:**

- Always-on persistent storage — no toggle
- 1 new DB column (`persistent_dir`) — derived paths not stored per-run
- No `metadata.json`
- Phases 4 and 5 dropped
- No configurable path mapping object — replaced with simple `output_paths` list
- `start_tensorboard()` updated to use `persistent/runs`
- Path resolution updated in `tensorboard_service.py`
- Single implementation phase
- TensorBoard label noise resolved via flat layout
- `cleanup_old_tb_logs()` retired for persistent runs
- Parallel-run TB symlink block explicitly removed
- Single-run timestamp-dir creation block explicitly removed
- Training-start and standalone TB unified via shared `_tb_launch_args` helper with conditional legacy `--logdir_spec`
- All four original open questions closed
- Hardcoded `checkpoints/`, `models/`, `outputs/` symlinks dropped — replaced with user-configured `output_paths`
- `BEEKEEPER_CHECKPOINT_DIR`, `BEEKEEPER_MODEL_DIR`, `BEEKEEPER_OUTPUT_DIR` env vars removed
- `Output Path Validation` section added with full safety rules
- Edit Project UI gets one new optional field: "Output paths to protect"

The design is ready to hand to an implementer.
