"""
Browser smoke tests using Playwright.

These run a real Chromium browser against a live Flask server.
They catch what the Flask test client cannot: JavaScript errors,
broken DOM bindings, and failed dynamic rendering.

Requirements:
    pip install playwright
    playwright install chromium

Run only browser tests:    pytest -m browser
Skip browser tests:        pytest -m "not browser"
"""
import json
import threading
import time

import pytest


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start Flask in a background thread with a real temp projects directory."""
    tmp = tmp_path_factory.mktemp("browser_projects")
    projects_dir = tmp / "projects"
    projects_dir.mkdir()

    # Create a ready project on disk — no mocking needed, the route will call
    # get_training_status() which just checks in-memory state and returns idle.
    proj_dir = projects_dir / "demo"
    proj_dir.mkdir()
    (proj_dir / "project.json").write_text(json.dumps({
        "name": "demo",
        "git_url": "https://github.com/user/repo.git",
        "branch": "main",
        "python_version": "3.11",
        "train_file": "train.py",
        "tensorboard_log_dir": "runs",
        "requirements_file": "requirements.txt",
        "env_type": "venv",
        "setup_status": "ready",
        "setup_error": "",
        "train_status": "idle",
        "train_pid": 0,
        "env_vars": {},
    }))

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["PROJECTS_DIR"] = str(projects_dir)

    server_thread = threading.Thread(
        target=lambda: flask_app.run(port=5998, use_reloader=False, debug=False),
        daemon=True,
    )
    server_thread.start()
    time.sleep(1)  # give the server a moment to bind

    yield "http://localhost:5998"


@pytest.mark.browser
def test_dashboard_loads_no_console_errors(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: errors.append(str(err)))

        page.goto(live_server)
        page.wait_for_load_state("networkidle")

        assert page.title() != ""
        assert errors == [], f"JavaScript errors on dashboard: {errors}"
        browser.close()


@pytest.mark.browser
def test_dashboard_shows_project_link(live_server):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(live_server)
        expect(page.locator("text=demo")).to_be_visible()
        browser.close()


@pytest.mark.browser
def test_project_detail_js_config_injected(live_server):
    """
    window.TRAINING_CONFIG must be defined on the project detail page.
    If it's missing, training.js exits immediately and all controls are dead.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{live_server}/projects/demo")
        page.wait_for_load_state("networkidle")

        config = page.evaluate("() => window.TRAINING_CONFIG")
        assert config is not None, "window.TRAINING_CONFIG was not injected"
        assert config["name"] == "demo"
        assert config["status"] == "idle"
        browser.close()


@pytest.mark.browser
def test_project_detail_key_elements_present(live_server):
    """
    The DOM IDs that training.js binds event listeners to must exist.
    If these are renamed in the template without updating training.js,
    training controls silently stop working.
    """
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{live_server}/projects/demo")

        expect(page.locator("#btn-start")).to_be_visible()
        expect(page.locator("#log-terminal")).to_be_attached()
        expect(page.locator("#training-controls")).to_be_attached()
        browser.close()


@pytest.mark.browser
def test_collapsible_logs_section_toggles(live_server):
    """Clicking the Logs header should expand the logs body."""
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{live_server}/projects/demo")

        logs_body = page.locator("#logs-body")
        # Starts hidden when idle
        expect(logs_body).to_be_hidden()

        page.locator("#logs-section .collapsible-header").click()
        expect(logs_body).to_be_visible()

        # Click again — should collapse
        page.locator("#logs-section .collapsible-header").click()
        expect(logs_body).to_be_hidden()
        browser.close()


@pytest.mark.browser
def test_project_detail_no_console_errors(live_server):
    """No JavaScript console errors on the project detail page."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda err: errors.append(str(err)))

        page.goto(f"{live_server}/projects/demo")
        page.wait_for_load_state("networkidle")

        assert errors == [], f"JavaScript errors on project detail: {errors}"
        browser.close()
