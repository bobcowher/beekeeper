from flask import request, redirect, url_for, g

from services.auth_service import get_current_user
from services.config_service import is_auth_enabled
from services.db_service import get_db


def register_middleware(app):
    """Register authentication middleware."""

    @app.before_request
    def check_auth():
        """Check authentication before each request."""
        # Skip auth check for certain paths
        exempt_paths = ['/auth/', '/static/']
        if any(request.path.startswith(path) for path in exempt_paths):
            return None

        # If auth is disabled, allow all access
        if not is_auth_enabled():
            g.user = None
            return None

        # If no users exist, redirect to registration
        db = get_db()
        if db.get_user_count() == 0:
            if request.path != '/auth/register':
                return redirect(url_for('auth.register'))
            return None

        # Check session
        user = get_current_user()
        if not user:
            # Allow access to login/register
            if request.path in ['/auth/login', '/auth/register']:
                return None
            return redirect(url_for('auth.login', next=request.url))

        # Store user in g for templates
        g.user = user
        return None

    @app.context_processor
    def inject_user():
        """Inject user into all templates."""
        return dict(user=g.get('user', None))
