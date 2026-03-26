import secrets
import bcrypt
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

from flask import request, redirect, url_for, jsonify, g

from models.user import User
from services.config_service import get_config_int, is_auth_enabled
from services.db_service import get_db
from services.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


def create_session_for_user(user_id: int) -> str:
    """Create session for user and return session ID."""
    db = get_db()
    lifetime_days = get_config_int('session.lifetime_days', 7)
    expires_at = datetime.now() + timedelta(days=lifetime_days)
    return db.create_session(user_id, expires_at)


def get_current_user() -> Optional[User]:
    """Get current user from session cookie."""
    session_id = request.cookies.get('session_id')
    if not session_id:
        return None

    db = get_db()
    session_data = db.get_session(session_id)
    if not session_data:
        return None

    user_id, _ = session_data
    return db.get_user_by_id(user_id)


def login_required(f):
    """Decorator to require login for route."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_auth_enabled():
            return f(*args, **kwargs)  # Auth disabled, allow access

        user = get_current_user()
        if not user:
            return redirect(url_for('auth.login', next=request.url))

        g.user = user
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Decorator to require admin access for route."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_auth_enabled():
            return f(*args, **kwargs)  # Auth disabled, allow access

        user = get_current_user()
        if not user or not user.is_admin:
            return "Forbidden", 403

        g.user = user
        return f(*args, **kwargs)
    return decorated


def api_key_required(f):
    """Decorator to require API key for API routes."""
    @wraps(f)
    def decorated(*args, **kwargs):
        client_ip = request.remote_addr

        # Check rate limit
        rate_limiter = get_rate_limiter()
        max_requests = get_config_int('api.rate_limit_per_minute', 10)
        is_allowed, request_count = rate_limiter.is_allowed(client_ip, max_requests)

        if not is_allowed:
            logger.warning(f"API rate limit exceeded for IP {client_ip} ({request_count} requests/min)")
            return jsonify({
                "success": False,
                "error": {
                    "code": "RATE_LIMIT_EXCEEDED",
                    "message": f"Rate limit exceeded. Maximum {max_requests} requests per minute."
                }
            }), 429

        if not is_auth_enabled():
            return f(*args, **kwargs)  # Auth disabled, allow access

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            logger.warning(f"API request from {client_ip} missing Authorization header")
            return jsonify({
                "success": False,
                "error": {
                    "code": "UNAUTHORIZED",
                    "message": "Missing API key"
                }
            }), 401

        key = auth_header.split("Bearer ", 1)[1]
        db = get_db()
        user = db.validate_api_key(key)
        if not user:
            logger.warning(f"API request from {client_ip} with invalid API key")
            return jsonify({
                "success": False,
                "error": {
                    "code": "INVALID_KEY",
                    "message": "Invalid API key"
                }
            }), 401

        # Store user in request context
        g.user = user
        logger.info(f"API request from {client_ip} authenticated as {user.email}")
        return f(*args, **kwargs)
    return decorated


def generate_api_key() -> str:
    """Generate API key with format: bk_<32 random chars>."""
    return f"bk_{secrets.token_urlsafe(24)}"
