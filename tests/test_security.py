"""
Security tests for the file browser's path traversal protection.

Tests _safe_path() directly (unit level) rather than relying on HTTP URL
normalization, which may vary across Werkzeug versions.
"""
import os
from routes.files import _safe_path


def _workspace_dir(tmp_path, name="myproject"):
    """Return the expected workspace_dir path without creating it."""
    return os.path.realpath(
        os.path.join(str(tmp_path / "projects"), name, "workspace")
    )


# --- Happy path ---

def test_safe_path_root_returns_src_dir(tmp_path):
    """Empty subpath should return workspace_dir itself."""
    projects_dir = str(tmp_path / "projects")
    os.makedirs(os.path.join(projects_dir, "myproject", "workspace"))
    workspace_dir, target = _safe_path(projects_dir, "myproject", "")
    assert target == _workspace_dir(tmp_path)


def test_safe_path_valid_subpath(tmp_path):
    """A normal relative path inside workspace/ should be allowed."""
    projects_dir = str(tmp_path / "projects")
    os.makedirs(os.path.join(projects_dir, "myproject", "workspace"))
    workspace_dir, target = _safe_path(projects_dir, "myproject", "models/resnet.py")
    assert target is not None
    assert target.endswith(os.path.join("workspace", "models", "resnet.py"))


# --- Path traversal ---

def test_safe_path_blocks_dotdot_traversal(tmp_path):
    """Classic ../ traversal must be rejected."""
    projects_dir = str(tmp_path / "projects")
    workspace_dir, target = _safe_path(projects_dir, "myproject", "../../etc/passwd")
    assert target is None


def test_safe_path_blocks_deep_traversal(tmp_path):
    """Deeply nested ../ must also be rejected."""
    projects_dir = str(tmp_path / "projects")
    workspace_dir, target = _safe_path(projects_dir, "myproject", "a/b/../../../../etc/shadow")
    assert target is None


def test_safe_path_blocks_absolute_path(tmp_path):
    """
    os.path.join with an absolute subpath drops the prefix entirely.
    _safe_path must reject this.
    """
    projects_dir = str(tmp_path / "projects")
    workspace_dir, target = _safe_path(projects_dir, "myproject", "/etc/passwd")
    assert target is None


def test_safe_path_blocks_sibling_directory(tmp_path):
    """
    Regression: startswith("workspace") would falsely allow "workspace-evil" without
    the path-separator boundary check. E.g.:
      workspace_dir  = .../projects/myproject/workspace
      target         = .../projects/myproject/workspace-evil/payload
      'workspace-evil'.startswith('workspace') == True  ← the bug we're guarding against
    """
    projects_dir = str(tmp_path / "projects")
    # Navigate up one level then into a sibling directory with 'workspace' as prefix
    workspace_dir, target = _safe_path(projects_dir, "myproject", "../workspace-evil/payload")
    assert target is None


# --- output_path symlinks into persistent/ ---

def test_safe_path_allows_symlink_into_persistent(tmp_path):
    """Workspace symlinks created by output_paths resolve into persistent/ — must be allowed."""
    projects_dir = str(tmp_path / "projects")
    persistent_target = os.path.join(projects_dir, "myproject", "persistent", "runs", "run_1", "checkpoints")
    os.makedirs(persistent_target)
    workspace = os.path.join(projects_dir, "myproject", "workspace")
    os.makedirs(workspace)
    symlink = os.path.join(workspace, "checkpoints")
    os.symlink(persistent_target, symlink)

    _, target = _safe_path(projects_dir, "myproject", "checkpoints")
    assert target is not None
    assert "persistent" in target


def test_safe_path_blocks_symlink_outside_project(tmp_path):
    """A symlink escaping to outside the project directory must still be blocked."""
    projects_dir = str(tmp_path / "projects")
    workspace = os.path.join(projects_dir, "myproject", "workspace")
    os.makedirs(workspace)
    outside = str(tmp_path / "outside")
    os.makedirs(outside)
    symlink = os.path.join(workspace, "escape")
    os.symlink(outside, symlink)

    _, target = _safe_path(projects_dir, "myproject", "escape")
    assert target is None


# --- Via HTTP ---

def test_http_traversal_blocked(client, ready_project, app):
    """Path traversal via the HTTP route must return 403."""
    # Create the workspace directory so routing reaches _safe_path
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "workspace")
    os.makedirs(src)

    resp = client.get("/projects/myproject/files/../../etc/passwd")
    assert resp.status_code in (403, 404)
