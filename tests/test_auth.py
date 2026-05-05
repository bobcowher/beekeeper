"""
Tests for routes/auth.py — specifically the redirect validation logic
added to the login handler (lines 76-79 in auth.py).

Strategy: auth is disabled by default in config, so we mock
is_auth_enabled() + get_db() to exercise the login route directly
without a real database or active auth middleware.
"""
from unittest.mock import MagicMock


def _make_user(name="Alice", email="alice@example.com"):
    user = MagicMock()
    user.id = 1
    user.name = name
    user.email = email
    user.is_locked.return_value = False
    user.failed_login_attempts = 0
    return user


def _mock_db(mocker, user, password_ok=True):
    """Patch get_db to return a mock with a ready user and correct password."""
    db = MagicMock()
    db.get_user_by_email.return_value = user
    db.get_user_by_id.return_value = user
    db.get_password_hash.return_value = "hashed"
    db.get_user_count.return_value = 1
    mocker.patch("routes.auth.get_db", return_value=db)
    mocker.patch("middleware.auth_middleware.get_db", return_value=db)
    mocker.patch("services.auth_service.get_db", return_value=db)
    mocker.patch(
        "routes.auth.verify_password",
        return_value=password_ok,
    )
    mocker.patch(
        "routes.auth.create_session_for_user",
        return_value="test-session-id",
    )
    return db


def _enable_auth(mocker):
    """Make is_auth_enabled() return True everywhere it's imported."""
    mocker.patch("routes.auth.get_current_user", return_value=None)
    mocker.patch("middleware.auth_middleware.is_auth_enabled", return_value=False)


def _post_login(client, next_param=None, email="alice@example.com", password="secret"):
    """POST to /auth/login, optionally with ?next=..."""
    url = "/auth/login"
    if next_param is not None:
        url = f"/auth/login?next={next_param}"
    return client.post(
        url,
        data={"email": email, "password": password},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Redirect validation tests
# ---------------------------------------------------------------------------

def test_login_with_valid_next_redirects_to_next(client, mocker):
    """Valid relative ?next=/projects/foo should redirect there after login."""
    user = _make_user()
    _mock_db(mocker, user)
    _enable_auth(mocker)

    resp = _post_login(client, next_param="/projects/foo")

    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    assert location.endswith("/projects/foo"), f"Expected /projects/foo in Location, got {location!r}"


def test_login_with_no_next_redirects_to_dashboard(client, mocker):
    """Missing ?next should redirect to dashboard root."""
    user = _make_user()
    _mock_db(mocker, user)
    _enable_auth(mocker)

    resp = _post_login(client)

    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    assert location.endswith("/"), f"Expected dashboard (/) in Location, got {location!r}"


def test_login_with_double_slash_next_redirects_to_dashboard(client, mocker):
    """?next=//evil.com (protocol-relative open redirect) must go to dashboard."""
    user = _make_user()
    _mock_db(mocker, user)
    _enable_auth(mocker)

    resp = _post_login(client, next_param="//evil.com")

    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    # Must NOT redirect to the evil domain
    assert "evil.com" not in location, f"Open redirect not blocked! Location: {location!r}"
    assert location.endswith("/"), f"Expected dashboard (/) in Location, got {location!r}"


def test_login_with_absolute_url_next_redirects_to_dashboard(client, mocker):
    """?next=http://evil.com (absolute URL) must go to dashboard, not evil host."""
    user = _make_user()
    _mock_db(mocker, user)
    _enable_auth(mocker)

    resp = _post_login(client, next_param="http://evil.com/steal")

    assert resp.status_code == 302
    location = resp.headers.get("Location", "")
    assert "evil.com" not in location, f"Open redirect not blocked! Location: {location!r}"
    assert location.endswith("/"), f"Expected dashboard (/) in Location, got {location!r}"


# ---------------------------------------------------------------------------
# Basic login page tests
# ---------------------------------------------------------------------------

def test_login_get_returns_200(client):
    """GET /auth/login renders the login page."""
    resp = client.get("/auth/login")
    assert resp.status_code == 200


def test_login_missing_fields_redirects(client, mocker):
    """POST with empty email/password should redirect back to login."""
    mocker.patch("middleware.auth_middleware.is_auth_enabled", return_value=False)
    resp = client.post("/auth/login", data={"email": "", "password": ""}, follow_redirects=False)
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers.get("Location", "")
