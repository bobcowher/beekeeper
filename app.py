import os
from flask import Flask

BEEKEEPER_HOME = os.path.dirname(os.path.abspath(__file__))


def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("BEEKEEPER_SECRET", "dev-secret-change-me")
    app.config["BEEKEEPER_HOME"] = BEEKEEPER_HOME
    app.config["PROJECTS_DIR"] = os.path.join(BEEKEEPER_HOME, "projects")

    os.makedirs(app.config["PROJECTS_DIR"], exist_ok=True)

    from routes.dashboard import dashboard_bp
    from routes.project import project_bp
    from routes.stats import stats_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(project_bp)
    app.register_blueprint(stats_bp)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, host="0.0.0.0", port=5000)
