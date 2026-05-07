import os
import random
import subprocess
from flask import Flask

BEEKEEPER_HOME = os.path.dirname(os.path.abspath(__file__))
APP_VERSION = "1.0.8"


def _git_value(args, default="unknown"):
    """Return a short git value for deployment visibility."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=BEEKEEPER_HOME,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return default
    if result.returncode != 0:
        return default
    return result.stdout.strip() or default


def get_deploy_version():
    """Build the UI version label shown on deployed pages."""
    version = os.environ.get("BEEKEEPER_VERSION", APP_VERSION)
    branch = os.environ.get("BEEKEEPER_GIT_BRANCH") or _git_value(["rev-parse", "--abbrev-ref", "HEAD"])
    commit = os.environ.get("BEEKEEPER_GIT_SHA") or _git_value(["rev-parse", "--short", "HEAD"])
    return {
        "version": version,
        "branch": branch,
        "commit": commit,
        "label": f"v{version} {branch}@{commit}",
    }


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("BEEKEEPER_SECRET", "dev-secret-change-me")
    app.config["BEEKEEPER_HOME"] = BEEKEEPER_HOME
    app.config["PROJECTS_DIR"] = os.path.join(BEEKEEPER_HOME, "projects")
    app.config["DEPLOY_VERSION"] = get_deploy_version()

    os.makedirs(app.config["PROJECTS_DIR"], exist_ok=True)

    # Initialize config service
    from services.config_service import init_config
    init_config(BEEKEEPER_HOME)

    # Initialize database
    from services.db_service import init_database
    db_path = os.path.join(BEEKEEPER_HOME, 'data', 'beekeeper.db')
    init_database(db_path)

    # Register blueprints
    from routes.dashboard import dashboard_bp
    from routes.project import project_bp
    from routes.stats import stats_bp
    from routes.training import training_bp
    from routes.files import files_bp
    from routes.api_v1 import api_v1_bp
    from routes.auth import auth_bp
    from routes.admin import admin_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(stats_bp)
    app.register_blueprint(training_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(api_v1_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    # Register middleware
    from middleware.auth_middleware import register_middleware
    register_middleware(app)

    @app.context_processor
    def inject_deploy_version():
        return {"deploy_version": app.config["DEPLOY_VERSION"]}

    # Periodic cleanup of expired sessions
    @app.before_request
    def cleanup_sessions():
        # Run occasionally (1% of requests)
        if random.random() < 0.01:
            from services.db_service import get_db
            get_db().cleanup_expired_sessions()

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
