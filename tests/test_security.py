"""
Security tests for the file browser's path traversal protection.

Tests _safe_path() directly (unit level) rather than relying on HTTP URL
normalization, which may vary across Werkzeug versions.
"""
import os
from routes.files import _safe_path


def _src_dir(tmp_path, name="myproject"):
    """Return the expected src_dir path without creating it."""
    return os.path.realpath(
        os.path.join(str(tmp_path / "projects"), name, "src")
    )


# --- Happy path ---

def test_safe_path_root_returns_src_dir(tmp_path):
    """Empty subpath should return src_dir itself."""
    projects_dir = str(tmp_path / "projects")
    src_dir, target = _safe_path(projects_dir, "myproject", "")
    assert target == _src_dir(tmp_path)


def test_safe_path_valid_subpath(tmp_path):
    """A normal relative path inside src/ should be allowed."""
    projects_dir = str(tmp_path / "projects")
    src_dir, target = _safe_path(projects_dir, "myproject", "models/resnet.py")
    assert target is not None
    assert target.endswith(os.path.join("src", "models", "resnet.py"))


# --- Path traversal ---

def test_safe_path_blocks_dotdot_traversal(tmp_path):
    """Classic ../ traversal must be rejected."""
    projects_dir = str(tmp_path / "projects")
    src_dir, target = _safe_path(projects_dir, "myproject", "../../etc/passwd")
    assert target is None


def test_safe_path_blocks_deep_traversal(tmp_path):
    """Deeply nested ../ must also be rejected."""
    projects_dir = str(tmp_path / "projects")
    src_dir, target = _safe_path(projects_dir, "myproject", "a/b/../../../../etc/shadow")
    assert target is None


def test_safe_path_blocks_absolute_path(tmp_path):
    """
    os.path.join with an absolute subpath drops the prefix entirely.
    _safe_path must reject this.
    """
    projects_dir = str(tmp_path / "projects")
    src_dir, target = _safe_path(projects_dir, "myproject", "/etc/passwd")
    assert target is None


def test_safe_path_blocks_sibling_directory(tmp_path):
    """
    Regression: startswith("src") would falsely allow "src-evil" without
    the path-separator boundary check. E.g.:
      src_dir  = .../projects/myproject/src
      target   = .../projects/myproject/src-evil/payload
      'src-evil'.startswith('src') == True  ← the bug we're guarding against
    """
    projects_dir = str(tmp_path / "projects")
    # Navigate up one level then into a sibling directory with 'src' as prefix
    src_dir, target = _safe_path(projects_dir, "myproject", "../src-evil/payload")
    assert target is None


# --- Via HTTP ---

def test_http_traversal_blocked(client, ready_project, app):
    """Path traversal via the HTTP route must return 403."""
    # Create the src directory so routing reaches _safe_path
    src = os.path.join(app.config["PROJECTS_DIR"], "myproject", "src")
    os.makedirs(src)

    resp = client.get("/projects/myproject/files/../../etc/passwd")
    assert resp.status_code in (403, 404)
