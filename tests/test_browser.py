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
import subprocess
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    # The live server shares the process-global limiter (600 req/min per IP by default) across the whole
    # module; reset it per test, as the app fixture does, so results don't depend on suite size.
    from services.rate_limiter import get_rate_limiter
    get_rate_limiter()._requests.clear()


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """Start Flask in a background thread with a real temp projects directory."""
    tmp = tmp_path_factory.mktemp("browser_projects")
    projects_dir = tmp / "projects"
    projects_dir.mkdir()
    remote_repo = tmp / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote_repo)],
        check=True,
        capture_output=True,
        text=True,
    )

    # Create a ready project on disk — no mocking needed, the route will call
    # get_training_status() which just checks in-memory state and returns idle.
    proj_dir = projects_dir / "demo"
    proj_dir.mkdir()
    (proj_dir / "project.json").write_text(json.dumps({
        "name": "demo",
        "git_url": str(remote_repo),
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
        "output_paths": [],
    }))

    proj2 = projects_dir / "chessbot"
    proj2.mkdir()
    (proj2 / "project.json").write_text(json.dumps({
        "name": "chessbot",
        "git_url": str(remote_repo),
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
        "output_paths": [],
        "folder": "chess",
    }))

    proj3 = projects_dir / "literal-folder"
    proj3.mkdir()
    literal_data = json.loads((proj2 / "project.json").read_text())
    literal_data.update({"name": "literal-folder", "folder": "__unfiled__"})
    (proj3 / "project.json").write_text(json.dumps(literal_data))

    (tmp / "pipelines").mkdir()
    from app import create_app
    flask_app = create_app({
        "TESTING": True,
        "PROJECTS_DIR": str(projects_dir),
        "PIPELINES_DIR": str(tmp / "pipelines"),
        "DB_PATH": str(tmp / "beekeeper-test.db"),
    })

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
        page.screenshot(path="/tmp/beekeeper-dashboard-desktop.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


@pytest.mark.browser
def test_projects_page_shows_project_link(live_server):
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(live_server + "/projects")
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
        assert "tbPort" in config
        assert config["host"] in ("localhost", "127.0.0.1")
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

        expect(page.locator("#training-section")).to_be_attached()
        expect(page.locator("#run-list")).to_be_attached()
        expect(page.locator("#branch-picker")).to_be_attached()
        expect(page.locator("#btn-confirm-start")).to_be_visible()
        expect(page.locator("#btn-start-run")).to_be_attached()
        browser.close()


@pytest.mark.browser
def test_collapsible_history_section_toggles(live_server):
    """Clicking a collapsible section header should expand and collapse the body."""
    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"{live_server}/projects/demo")

        logs_body = page.locator("#history-body")
        # Starts hidden when idle
        expect(logs_body).to_be_hidden()

        page.locator("#history-section .collapsible-header").click()
        expect(logs_body).to_be_visible()

        # Click again — should collapse
        page.locator("#history-section .collapsible-header").click()
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


@pytest.mark.browser
def test_dashboard_folder_groups_collapse_and_persist(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(live_server + "/projects")
        label = page.locator(".folder-label", has_text="chess")
        assert label.is_visible()
        assert page.locator('.folder-label[data-folder-key="unfiled:"]').is_visible()
        row = page.locator('.project-row[data-name="chessbot"]')
        assert row.is_visible()
        page.screenshot(path="/tmp/beekeeper-folders-open.png", full_page=True)
        label.click()
        assert not row.is_visible()
        page.reload()
        assert not page.locator('.project-row[data-name="chessbot"]').is_visible()
        page.screenshot(path="/tmp/beekeeper-folders.png", full_page=True)
        browser.close()


@pytest.mark.browser
def test_literal_unfiled_folder_has_independent_collapse_state(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(live_server + "/projects")
        literal = page.locator('.folder-label[data-folder-key="folder:__unfiled__"]')
        unfiled = page.locator('.folder-label[data-folder-key="unfiled:"]')
        assert literal.is_visible() and unfiled.is_visible()
        literal.click()
        assert not page.locator('.project-row[data-name="literal-folder"]').is_visible()
        assert page.locator('.project-row[data-name="demo"]').is_visible()
        browser.close()


@pytest.mark.browser
def test_queue_add_and_cancel(live_server):
    """Add to Queue creates a queued row; Cancel removes it (dispatcher is off under TESTING)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route(
            "**/api/v1/projects/demo/training/can-start",
            lambda route: route.fulfill(
                content_type="application/json",
                body=json.dumps({"success": True, "data": {
                    "can_start": False, "code": "AT_CAPACITY",
                    "message": "At capacity (1 parallel run)",
                }}),
            ),
        )
        page.goto(f"{live_server}/projects/demo")
        page.locator("#btn-confirm-queue").wait_for(state="visible")
        page.click("#btn-confirm-queue")
        row = page.locator("#queue-list .queue-row")
        row.first.wait_for(timeout=5000)
        assert row.count() == 1
        page.screenshot(path="/tmp/beekeeper-queue.png", full_page=True)
        row.first.locator("button").click()
        page.wait_for_function("document.querySelectorAll('#queue-list .queue-row').length === 0")
        assert not page.locator("#queue-section").is_visible()
        browser.close()


@pytest.mark.browser
def test_queue_polling_pauses_hidden_and_cancel_handles_network_error(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route(
            "**/api/v1/projects/demo/training/can-start",
            lambda route: route.fulfill(
                content_type="application/json",
                body=json.dumps({"success": True, "data": {
                    "can_start": False, "code": "ALREADY_RUNNING",
                    "message": "Training is already running",
                }}),
            ),
        )
        page.goto(f"{live_server}/projects/demo")
        page.locator("#btn-confirm-queue").wait_for(state="visible")
        page.click("#btn-confirm-queue")
        row = page.locator("#queue-list .queue-row")
        row.first.wait_for(timeout=5000)

        requests = {"queue": 0}

        def count_queue(route):
            requests["queue"] += 1
            route.continue_()

        page.route("**/api/v1/projects/demo/queue", count_queue)
        page.evaluate("""
            () => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                document.dispatchEvent(new Event('visibilitychange'));
            }
        """)
        initial = requests["queue"]
        page.wait_for_timeout(5500)
        assert requests["queue"] == initial

        messages = []
        page.on("dialog", lambda dialog: (messages.append(dialog.message), dialog.accept()))
        page.route("**/api/v1/projects/demo/queue/*", lambda route: route.abort())
        row.first.locator("button").click()
        page.wait_for_timeout(250)
        assert messages and "Network request failed" in messages[0]
        assert row.count() == 1
        browser.close()


@pytest.mark.browser
def test_pipelines_intro_shows_until_first_pipeline(live_server):
    # Runs before any test in this module creates a pipeline (the server is module-scoped).
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, tag in ((1280, "desktop"), (390, "390")):
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.goto(live_server + "/pipelines")
            assert page.locator("#pipeline-intro").is_visible()
            page.screenshot(path=f"/tmp/beekeeper-pipelines-intro-{tag}.png", full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        browser.close()


@pytest.mark.browser
def test_pipeline_page_yaml_errors_and_run(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(live_server + "/pipelines/new")
        page.fill("#name", "ui-pipe")
        page.click("button[type=submit]")
        page.wait_for_url("**/pipelines/ui-pipe/edit")
        page.locator(".step-editor").first.wait_for()
        page.click("#tab-yaml")
        page.fill("#pipeline-yaml", "name: ui-pipe\nsteps:\n  - {name: a, type: train, project: demo, branch: no}\n")
        page.click("#btn-yaml-save")
        page.locator("#yaml-errors li").first.wait_for()
        assert "quote" in page.locator("#yaml-errors").inner_text()
        page.fill("#pipeline-yaml", "name: ui-pipe\nsteps:\n  - {name: a, type: train, project: demo}\n  - {name: b, type: task, command: 'true'}\n")
        page.click("#btn-yaml-save")
        page.wait_for_url("**/pipelines/ui-pipe")
        page.locator("#step-chain .step-node").first.wait_for()
        assert not page.locator("#step-panel").is_visible()   # closed until a step is picked
        page.click("#btn-pipeline-start")
        page.click("#btn-run-submit")   # train steps open the branch form first
        page.locator("#step-chain .step-pending, #step-chain .step-queued, #step-chain .step-failed, #step-chain .step-running").first.wait_for()
        page.wait_for_function("document.getElementById('pipeline-run-status').textContent === 'running'")
        assert page.locator("#btn-pipeline-start").inner_text() == "Queue Run"
        assert page.locator("#btn-pipeline-cancel").is_visible()
        page.screenshot(path="/tmp/beekeeper-pipeline.png", full_page=True)
        page.goto(live_server + "/pipelines")
        assert page.locator('.pipeline-row[data-name="ui-pipe"]').is_visible()
        page.screenshot(path="/tmp/beekeeper-dashboard-pipelines.png", full_page=True)
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        for path, shot in (
            ("/pipelines/ui-pipe", "pipeline-390"),
            ("/", "dashboard-390"),
            ("/projects", "projects-390"),
            ("/pipelines", "pipelines-390"),
        ):
            mobile.goto(live_server + path)
            mobile.wait_for_load_state("networkidle")
            mobile.screenshot(path=f"/tmp/beekeeper-{shot}.png", full_page=True)
            assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
        mobile.goto(live_server + "/")
        mobile.locator("#sidebar-toggle").click()
        mobile.wait_for_timeout(300)
        assert "open" in (mobile.locator("#sidebar").get_attribute("class") or "")
        assert mobile.locator("#sidebar").bounding_box()["x"] == 0
        assert mobile.locator('.sidebar-nav a[href="/projects"]').is_visible()
        assert mobile.locator('.sidebar-nav a[href="/pipelines"]').is_visible()
        mobile.screenshot(path="/tmp/beekeeper-sidebar-390.png", full_page=True)
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_start_and_queue_buttons_follow_server_preflight(live_server):
    from playwright.sync_api import sync_playwright

    state = {"can_start": True, "code": None, "message": "Ready to start"}

    def serve_preflight(route):
        route.fulfill(
            content_type="application/json",
            body=json.dumps({"success": True, "data": state}),
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.route("**/api/v1/projects/demo/training/can-start", serve_preflight)
        page.goto(f"{live_server}/projects/demo")
        page.locator("#btn-confirm-start").wait_for(state="visible")
        assert not page.locator("#btn-confirm-queue").is_visible()

        state.update({
            "can_start": False, "code": "AT_CAPACITY",
            "message": "At capacity (2 parallel runs)",
        })
        page.reload()
        page.locator("#btn-confirm-queue").wait_for(state="visible")
        assert not page.locator("#btn-confirm-start").is_visible()
        assert page.locator("#start-reason").inner_text() == "At capacity (2 parallel runs)"
        page.screenshot(path="/tmp/beekeeper-queue-only.png", full_page=True)
        browser.close()


@pytest.mark.browser
def test_pipeline_step_log_survives_periodic_refresh(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: log-pipe\nsteps:\n  - {name: produce, type: task, command: 'true'}\n"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok

        requests = {"runs": 0}
        run = {
            "run_id": 1,
            "started": True,
            "status": "running",
            "current_step": 0,
            "error": "",
            "started_at": "2026-10-10T12:00:00",
            "ended_at": None,
            "steps": [{
                "index": 0,
                "name": "produce",
                "type": "task",
                "status": "running",
                "attempt": 1,
                "started_at": "2026-10-10T12:00:00",
                "ended_at": None,
                "has_log": True,
            }],
        }

        def serve_runs(route):
            requests["runs"] += 1
            payload = dict(run)
            if requests["runs"] > 1:
                payload["error"] = "refreshed"
            route.fulfill(json={"success": True, "data": {"runs": [payload]}})

        page.route("**/api/v1/pipelines/log-pipe/runs?limit=50", serve_runs)
        log_text = "".join(f"line {i}\n" for i in range(1000))
        page.route(
            "**/api/v1/pipelines/log-pipe/runs/1/steps/produce/log?tail=500",
            lambda route: route.fulfill(json={"success": True, "data": {"log": log_text}}),
        )

        page.goto(live_server + "/pipelines/log-pipe")
        page.click('[data-step-log="produce"]')
        log = page.locator("#step-log")
        page.wait_for_function("document.getElementById('step-log').textContent.startsWith('line 0')")
        scroll_top = log.evaluate("el => { el.scrollTop = 200; return el.scrollTop; }")
        assert scroll_top > 0

        page.wait_for_function(
            "document.getElementById('pipeline-run-error').textContent === 'refreshed'",
            timeout=7000,
        )
        assert log.text_content() == log_text
        assert log.evaluate("el => el.scrollTop") == scroll_top
        assert page.locator('[data-step-log="produce"]').get_attribute("aria-pressed") == "true"
        browser.close()


@pytest.mark.browser
def test_pipeline_history_labels_running_and_stopped_steps(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: status-pipe\nsteps:\n  - {name: first, type: task, command: 'true'}\n  - {name: second, type: task, command: 'true'}\n"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok

        def run(run_id, status, current_step):
            return {
                "run_id": run_id,
                "started": True,
                "status": status,
                "current_step": current_step,
                "error": "",
                "started_at": "2026-10-10T12:00:00",
                "ended_at": None,
                "steps": [
                    {"name": "first", "type": "task", "status": "succeeded", "attempt": 1},
                    {"name": "second", "type": "task", "status": "pending", "attempt": 0},
                ],
            }

        runs = [run(4, "running", 0), run(3, "failed", 1), run(2, "cancelled", 0), run(1, "succeeded", 2)]
        page.route(
            "**/api/v1/pipelines/status-pipe/runs?limit=50",
            lambda route: route.fulfill(json={"success": True, "data": {"runs": runs}}),
        )
        page.goto(live_server + "/pipelines/status-pipe")
        page.locator('#pipeline-runs tr[data-run="4"]').wait_for()

        assert page.locator("#pipeline-runs thead").inner_text().strip().endswith("PROGRESS")
        assert page.locator('#pipeline-runs tr[data-run="4"] td').last.inner_text() == "Running: first"
        assert page.locator('#pipeline-runs tr[data-run="3"] td').last.inner_text() == "Stopped at second"
        assert page.locator('#pipeline-runs tr[data-run="2"] td').last.inner_text() == "Stopped at first"
        assert page.locator('#pipeline-runs tr[data-run="1"] td').last.inner_text() == "2/2 steps"
        browser.close()


@pytest.mark.browser
def test_pipeline_save_does_not_overlap_deploy_badge(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: layout-pipe\nsteps:\n  - {name: first, type: task, command: 'true'}\n"

    def overlaps(first, second):
        return not (
            first["x"] + first["width"] <= second["x"] or
            second["x"] + second["width"] <= first["x"] or
            first["y"] + first["height"] <= second["y"] or
            second["y"] + second["height"] <= first["y"]
        )

    with sync_playwright() as p:
        browser = p.chromium.launch()
        setup = browser.new_page()
        response = setup.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok
        setup.close()

        for width, height, label in ((390, 844, "390"), (1280, 900, "desktop")):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(live_server + "/pipelines/layout-pipe/edit")
            page.locator("#btn-yaml-save").wait_for()
            if width == 390:
                page.evaluate("""
                    () => {
                        const save = document.getElementById('btn-yaml-save').getBoundingClientRect();
                        const badge = document.querySelector('.deploy-version').getBoundingClientRect();
                        const targetTop = window.innerHeight - badge.height - 8;
                        window.scrollBy(0, save.top - targetTop);
                    }
                """)
            page.screenshot(path=f"/tmp/beekeeper-pipeline-layout-{label}.png", full_page=True)
            save_box = page.locator("#btn-yaml-save").bounding_box()
            badge_box = page.locator(".deploy-version").bounding_box()
            assert save_box and badge_box
            assert not overlaps(save_box, badge_box), label
            page.close()
        browser.close()


@pytest.mark.browser
def test_pipeline_chain_arrow_uses_accent_colour(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: arrow-pipe\nsteps:\n  - {name: first, type: task, command: 'true'}\n  - {name: second, type: task, command: 'true'}\n"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok
        page.goto(live_server + "/pipelines/arrow-pipe")
        page.locator("#step-chain .step-node").nth(1).wait_for()

        colours = page.evaluate("""
            () => {
                const root = getComputedStyle(document.documentElement);
                const probe = document.createElement('span');
                probe.style.color = root.getPropertyValue('--accent');
                document.body.appendChild(probe);
                const accent = getComputedStyle(probe).color;
                probe.remove();
                const arrow = getComputedStyle(document.querySelector('.step-node'), '::after').color;
                return {accent, arrow};
            }
        """)
        assert colours["arrow"] == colours["accent"]
        browser.close()


@pytest.mark.browser
def test_pipeline_network_error_is_reported_without_pageerror(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: error-pipe\nsteps:\n  - {name: one, type: task, command: 'true'}\n"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok
        page.goto(live_server + "/pipelines/error-pipe/edit")
        page.locator(".step-editor").first.wait_for()

        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.route("**/api/v1/pipelines/validate", lambda route: route.abort())
        page.click("#btn-yaml-validate")

        alert = page.locator("#pipeline-page-error")
        alert.wait_for(state="visible")
        assert "Could not validate pipeline" in alert.inner_text()
        assert not page.locator("#btn-yaml-validate").is_disabled()
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_timeout(500)
        assert alert.is_visible(), "background refresh must not erase an action error"
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_polling_pauses_while_page_is_hidden(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: poll-pipe\nsteps:\n  - {name: one, type: task, command: 'true'}\n"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok

        requests = {"runs": 0}

        def count_runs(route):
            requests["runs"] += 1
            route.continue_()

        page.route("**/api/v1/pipelines/poll-pipe/runs?limit=50", count_runs)
        page.goto(live_server + "/pipelines/poll-pipe")
        page.locator("#step-chain .step-node").wait_for()
        initial = requests["runs"]

        page.evaluate("""
            () => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                document.dispatchEvent(new Event('visibilitychange'));
            }
        """)
        page.wait_for_timeout(5500)
        assert requests["runs"] == initial

        page.evaluate("""
            () => {
                Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                document.dispatchEvent(new Event('visibilitychange'));
            }
        """)
        page.wait_for_timeout(1000)
        assert requests["runs"] > initial
        browser.close()


@pytest.mark.browser
def test_pipeline_dynamic_content_is_text_and_controls_are_accessible(live_server):
    from playwright.sync_api import sync_playwright

    yaml = "name: xss-pipe\nsteps:\n  - {name: safe, type: task, command: 'true'}\n"
    payload = '</div><img src=x onerror="window.__pipelineXss=1">'
    log_payload = '<script>window.__pipelineLogXss=1</script>'
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        response = page.request.fetch(
            live_server + "/api/v1/pipelines",
            method="POST",
            headers={"Content-Type": "application/yaml"},
            data=yaml,
        )
        assert response.ok

        definition = {
            "name": "xss-pipe", "description": payload,
            "steps": [{"name": payload, "type": "task", "command": "true"}],
        }
        run = {
            "run_id": 1, "run_ref": "xss-pipe#1", "status": "running", "current_step": 0, "started": True,
            "error": payload, "started_at": "2026-10-10T12:00:00", "ended_at": None,
            "steps": [{
                "name": payload, "type": "task", "status": "running", "attempt": 1,
                "started_at": "2026-10-10T12:00:00", "ended_at": None, "has_log": True,
            }],
        }
        page.route(
            live_server + "/api/v1/pipelines/xss-pipe",
            lambda route: route.fulfill(json={
                "success": True, "data": {"pipeline": definition, "yaml": yaml},
            }),
        )
        page.route(
            "**/api/v1/pipelines/xss-pipe/runs?limit=50",
            lambda route: route.fulfill(json={"success": True, "data": {"runs": [run]}}),
        )
        page.route(
            "**/api/v1/pipelines/xss-pipe/runs/1/steps/**/log?tail=500",
            lambda route: route.fulfill(json={"success": True, "data": {"log": log_payload}}),
        )

        page.goto(live_server + "/pipelines/xss-pipe")
        row = page.locator('#pipeline-runs tr[data-run="1"]')
        row.wait_for()
        assert page.locator("#info-description").text_content() == payload
        run_button = row.locator(".run-history-link")
        assert run_button.get_attribute("aria-label") == "Show pipeline run #1"
        run_button.focus()
        run_button.press("Enter")
        assert page.locator("#step-chain img").count() == 0
        assert payload in page.locator("#step-chain .step-name").inner_text()
        assert page.evaluate("window.__pipelineXss") is None

        page.locator("[data-step-log]").click()
        page.wait_for_function("document.getElementById('step-log').textContent !== 'Loading log…'")
        assert page.locator("#step-log").text_content() == log_payload
        assert page.locator("#step-log script").count() == 0
        assert page.evaluate("window.__pipelineLogXss") is None
        browser.close()


def _create_pipeline(page, live_server, yaml):
    response = page.request.fetch(
        live_server + "/api/v1/pipelines", method="POST",
        headers={"Content-Type": "application/yaml"}, data=yaml,
    )
    assert response.ok


@pytest.mark.browser
def test_pipeline_never_run_shows_steps_and_panel_on_click(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server,
                         "name: fresh-pipe\nsteps:\n  - {name: fetch, type: task, command: './download.sh'}\n"
                         "  - {name: train, type: train, project: demo, branch: dev}\n")
        page.goto(live_server + "/pipelines/fresh-pipe")
        page.locator("#step-chain .step-node").nth(1).wait_for()
        assert page.locator("#pipeline-run-status").inner_text().lower() == "never run"
        assert page.locator("#pipeline-runs-empty").is_visible()
        assert page.locator("#info-steps").inner_text() == "2 (1 train, 1 task)"
        assert not page.locator("#step-panel").is_visible()          # closed until a step is picked
        page.click('#step-chain .step-node:nth-child(1) .step-card')
        assert "./download.sh" in page.locator("#step-panel").inner_text()
        page.click('#step-chain .step-node:nth-child(2) .step-card')
        assert "demo on branch dev" in page.locator("#step-panel-body").inner_text()
        page.click('#step-chain .step-node:nth-child(2) .step-card')  # clicking it again closes it
        assert not page.locator("#step-panel").is_visible()
        page.click("#btn-pipeline-edit")
        page.wait_for_url("**/pipelines/fresh-pipe/edit")
        assert "./download.sh" in page.locator("#pipeline-yaml").input_value()
        assert not page.locator("#btn-pipeline-delete").is_visible()  # tucked into the Danger Zone
        page.screenshot(path="/tmp/beekeeper-pipeline-never-run.png", full_page=True)
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_run_view_opens_steps_on_click_and_switches_runs(live_server):
    from playwright.sync_api import sync_playwright

    def step(name, status, has_log=True):
        return {"index": 0, "name": name, "type": "task", "status": status, "attempt": 1,
                "started_at": "2026-10-10T12:00:00", "ended_at": "2026-10-10T12:00:05",
                "has_log": has_log, "error": "exit code 1" if status == "failed" else None}

    runs = [
        {"run_id": 2, "run_ref": "fail-pipe#2", "status": "failed", "current_step": 1, "started": True,
         "error": "step 'second': exit code 1", "started_at": "2026-10-10T12:00:00",
         "ended_at": "2026-10-10T12:00:10", "steps": [step("first", "succeeded"), step("second", "failed")]},
        {"run_id": 1, "run_ref": "fail-pipe#1", "status": "succeeded", "current_step": 1, "started": True,
         "error": None, "started_at": "2026-10-09T12:00:00",
         "ended_at": "2026-10-09T12:00:10", "steps": [step("first", "succeeded"), step("second", "succeeded")]},
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server,
                         "name: fail-pipe\nsteps:\n  - {name: first, type: task, command: 'true'}\n"
                         "  - {name: second, type: task, command: 'false'}\n")
        page.route("**/api/v1/pipelines/fail-pipe/runs?limit=50",
                   lambda route: route.fulfill(json={"success": True, "data": {"runs": runs}}))
        page.route("**/api/v1/pipelines/fail-pipe/runs/*/steps/*/log?tail=500",
                   lambda route: route.fulfill(json={"success": True, "data": {
                       "log": route.request.url.split("/runs/")[1].split("/log")[0]}}))

        page.goto(live_server + "/pipelines/fail-pipe")
        log = page.locator("#step-log")
        page.wait_for_function("document.getElementById('run-title').textContent === 'Run #2'")
        assert page.locator("#btn-pipeline-resume").is_visible()
        assert "exit code 1" in page.locator('#step-chain .step-node.step-failed').inner_text()
        assert not page.locator("#step-panel").is_visible()          # no log until a step is picked
        page.click('[data-step-log="second"]')
        page.wait_for_function("document.getElementById('step-log').textContent === '2/steps/second'")
        assert page.locator("#step-chain .step-node.selected").inner_text().startswith("second")
        page.screenshot(path="/tmp/beekeeper-pipeline-failed.png", full_page=True)

        page.click('#pipeline-runs tr[data-run="1"]')
        page.wait_for_function("document.getElementById('run-title').textContent === 'Run #1 (older run)'")
        assert page.locator('#pipeline-runs tr[data-run="1"]').get_attribute("class") == "selected"
        assert not page.locator("#btn-pipeline-resume").is_visible()
        assert not page.locator("#step-panel").is_visible()          # selection belonged to run #2

        page.click('[data-step-log="first"]')
        page.wait_for_function("document.getElementById('step-log').textContent === '1/steps/first'")
        page.click("#btn-step-close")
        assert not log.is_visible()
        page.click("#btn-show-latest")
        page.wait_for_function("document.getElementById('run-title').textContent === 'Run #2'")

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.route("**/api/v1/pipelines/fail-pipe/runs?limit=50",
                     lambda route: route.fulfill(json={"success": True, "data": {"runs": runs}}))
        mobile.goto(live_server + "/pipelines/fail-pipe")
        mobile.click('[data-step-log="second"]')
        # Logs aren't mocked here, so the fetch 404s: the error belongs in the panel, not the page header.
        mobile.wait_for_function("document.getElementById('step-log').textContent.startsWith('Could not load log')")
        assert not mobile.locator("#pipeline-page-error").is_visible()
        assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        mobile.screenshot(path="/tmp/beekeeper-pipeline-failed-390.png", full_page=True)
        assert errors == []
        browser.close()


def _pipeline_yaml(page, live_server, name):
    return page.request.get(f"{live_server}/api/v1/pipelines/{name}").json()["data"]["yaml"]


@pytest.mark.browser
def test_pipeline_editor_builds_steps_graphically(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(live_server + "/pipelines/new")
        page.fill("#name", "graph-pipe")
        page.click("button[type=submit]")
        page.wait_for_url("**/pipelines/graph-pipe/edit")
        first = page.locator(".step-editor").first
        first.wait_for()
        assert page.locator("#tab-graphical").get_attribute("aria-selected") == "true"
        assert page.locator(".step-editor-name").first.input_value() == "prepare"

        page.fill("#g-description", "Built in the graphical editor")
        page.click("#btn-add-step")
        second = page.locator(".step-editor").nth(1)
        second.locator(".segmented-btn", has_text="Train").click()
        second = page.locator(".step-editor").nth(1)
        second.locator(".step-editor-name").fill("fit")
        second.locator("select").select_option("demo")
        second.locator("text=+ Add Variable").click()
        second.locator('input[aria-label="Variable name"]').fill("EPOCHS")
        second.locator('input[aria-label="Variable value"]').fill("3")
        assert page.locator("#yaml-dirty").is_visible()
        page.screenshot(path="/tmp/beekeeper-pipeline-editor.png", full_page=True)

        page.click("#btn-yaml-save")
        page.wait_for_url("**/pipelines/graph-pipe")
        saved = _pipeline_yaml(page, live_server, "graph-pipe")
        assert "description: Built in the graphical editor" in saved
        assert "- name: fit\n  type: train\n  project: demo\n  env_vars:\n    EPOCHS: '3'" in saved
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_editor_round_trips_between_tabs(live_server):
    from playwright.sync_api import sync_playwright

    yaml = ("# keep me\nname: trip-pipe\nsteps:\n  - name: one\n    type: task\n    command: echo one\n"
            "  - name: two\n    type: task\n    command: echo two\n")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server, yaml)
        page.goto(live_server + "/pipelines/trip-pipe/edit")
        page.locator(".step-editor").nth(1).wait_for()
        # Several steps start collapsed to a one-line summary; clicking one opens its form.
        assert page.locator(".step-editor-summary").nth(1).inner_text() == "echo two"
        assert not page.locator(".step-editor-body").nth(1).is_visible()
        page.locator(".step-editor-toggle").nth(1).click()
        assert page.locator(".step-editor-body").nth(1).is_visible()
        page.locator(".step-editor textarea").nth(1).fill("echo three")
        assert page.locator(".step-editor-summary").nth(1).inner_text() == "echo three"
        page.click("#tab-yaml")
        page.fill("#pipeline-yaml", yaml)        # back to the file as written
        page.click("#tab-graphical")
        page.locator(".step-editor").nth(1).wait_for()

        # Looking without editing leaves the YAML text, comments included, untouched.
        page.click("#tab-yaml")
        page.locator("#pipeline-yaml").wait_for()
        assert page.locator("#pipeline-yaml").input_value() == yaml
        assert page.locator("#field-reference").is_visible()

        # YAML edits show up graphically.
        page.fill("#pipeline-yaml", yaml.replace("echo two", "echo TWO"))
        page.click("#tab-graphical")
        page.locator(".step-editor").nth(1).wait_for()
        assert page.locator(".step-editor textarea").nth(1).input_value() == "echo TWO"
        assert not page.locator("#field-reference").is_visible()

        # The first graphical edit warns about the comment, and reordering/deleting reach the YAML.
        page.click('button[aria-label="Move step 2 up"]')
        notice = page.locator("#editor-notice")
        assert notice.is_visible() and "1 comment" in notice.inner_text()
        assert page.locator(".step-editor-name").first.input_value() == "two"
        assert page.locator('button[aria-label="Move step 1 up"]').is_disabled()
        page.click('button[aria-label="Delete step 2"]')
        assert page.locator(".step-editor").count() == 1
        page.click("#tab-yaml")
        page.wait_for_function("!document.getElementById('pipeline-yaml').value.includes('# keep me')")
        text = page.locator("#pipeline-yaml").input_value()
        assert "name: two" in text and "echo TWO" in text and "name: one" not in text
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_editor_blocks_graphical_for_yaml_it_cannot_show(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        _create_pipeline(page, live_server, "name: block-pipe\nsteps:\n  - {name: a, type: task, command: 'true'}\n")
        page.goto(live_server + "/pipelines/block-pipe/edit")
        page.locator(".step-editor").first.wait_for()
        page.click("#tab-yaml")

        page.fill("#pipeline-yaml", "name: block-pipe\nsteps: [\n")
        page.click("#tab-graphical")
        page.locator("#yaml-errors li").first.wait_for()
        assert "Invalid YAML" in page.locator("#yaml-errors").inner_text()
        assert page.locator("#pipeline-yaml").is_visible()
        assert page.locator("#tab-yaml").get_attribute("aria-selected") == "true"

        page.fill("#pipeline-yaml", "name: block-pipe\nsteps:\n  - {name: a, type: task, command: 'true', retries: 2}\n")
        page.click("#tab-graphical")
        page.wait_for_function("document.getElementById('editor-notice').textContent.includes('retries')")
        assert page.locator("#pipeline-yaml").is_visible()

        # Incomplete-but-representable YAML is fine: the gaps show as field errors.
        page.fill("#pipeline-yaml", "name: block-pipe\nsteps:\n  - {name: a, type: train}\n")
        page.click("#tab-graphical")
        page.locator(".step-editor select").wait_for()
        page.click("#btn-yaml-save")
        error = page.locator(".step-editor .field-error").first
        error.wait_for()
        assert "required" in error.inner_text()
        assert page.locator(".step-editor select").get_attribute("class") == "invalid"
        browser.close()


@pytest.mark.browser
def test_pipeline_editor_fits_phone_width(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _create_pipeline(page, live_server,
                         "name: phone-pipe\nsteps:\n  - {name: fetch, type: task, command: './download.sh'}\n"
                         "  - name: fit\n    type: train\n    project: demo\n    env_vars: {EPOCHS: '3'}\n")
        page.goto(live_server + "/pipelines/phone-pipe/edit")
        page.locator(".step-editor").nth(1).wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path="/tmp/beekeeper-pipeline-editor-390.png", full_page=True)
        browser.close()


@pytest.mark.browser
def test_pipeline_editor_drag_reorders_steps(live_server):
    from playwright.sync_api import sync_playwright

    yaml = ("name: drag-pipe\nsteps:\n"
            + "".join(f"  - {{name: s{i}, type: task, command: 'echo {i}'}}\n" for i in range(1, 5)))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server, yaml)
        page.goto(live_server + "/pipelines/drag-pipe/edit")
        page.locator(".step-editor").nth(3).wait_for()

        def names():
            return [t.strip() for t in page.locator(".step-editor-title").all_inner_texts()]

        # Drag s1 down past s3.
        handle = page.locator(".step-drag-handle").first.bounding_box()
        target = page.locator(".step-editor").nth(2).bounding_box()
        page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
        page.mouse.down()
        page.mouse.move(handle["x"] + 5, target["y"] + target["height"] * 0.75, steps=12)
        assert page.locator(".step-editor.dragging").count() == 1
        page.screenshot(path="/tmp/beekeeper-pipeline-editor-dragging.png")
        page.mouse.up()
        assert names() == ["s2", "s3", "s1", "s4"]
        assert page.locator(".step-editor.dragging").count() == 0
        assert page.locator(".step-insert").count() == 3          # insert buttons are back
        assert page.locator("#yaml-dirty").is_visible()
        assert page.locator(".step-editor-num").first.inner_text() == "1"

        # Drag s4 up to the top.
        handle = page.locator(".step-drag-handle").nth(3).bounding_box()
        top = page.locator(".step-editor").first.bounding_box()
        page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
        page.mouse.down()
        page.mouse.move(handle["x"] + 300, top["y"] + 2, steps=12)   # released away from the handle
        page.mouse.up()
        assert page.locator(".step-editor.dragging").count() == 0
        assert names() == ["s4", "s2", "s3", "s1"]

        page.click("#tab-yaml")
        page.wait_for_function("document.getElementById('pipeline-yaml').value.indexOf('s4') < "
                               "document.getElementById('pipeline-yaml').value.indexOf('s2')")
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_run_form_overrides_branch_and_queues(live_server):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server,
                         "name: branch-pipe\nsteps:\n  - {name: train, type: train, project: demo}\n"
                         "  - {name: export, type: task, command: 'true'}\n")
        page.route("**/api/v1/projects/demo/branches", lambda route: route.fulfill(
            json={"success": True, "data": {"branches": ["feat-x", "main"], "current": "main"}}))
        page.goto(live_server + "/pipelines/branch-pipe")
        page.click("#btn-pipeline-start")
        select = page.locator("#run-branch-train")
        page.wait_for_function("document.getElementById('run-branch-train').options.length === 2")
        assert select.locator("option").first.inner_text() == "Default (main)"
        assert not page.locator("#btn-pipeline-start").is_visible()
        select.select_option("feat-x")
        page.screenshot(path="/tmp/beekeeper-pipeline-run-form.png", full_page=True)
        page.click("#btn-run-submit")
        page.wait_for_function("document.getElementById('pipeline-run-status').textContent === 'running'")
        assert page.locator("#run-form").is_hidden()
        assert "train → feat-x" in page.locator("#pipeline-runs tbody").inner_text()
        assert "@ feat-x" in page.locator("#step-chain").inner_text()

        page.click("#btn-pipeline-start")
        assert page.locator("#btn-run-submit").inner_text() == "Add to Queue"
        page.click("#btn-run-submit")
        page.locator("#queued-list li").first.wait_for()
        assert "default" in page.locator("#queued-list").inner_text()
        assert page.locator("#pipeline-run-status").text_content() == "running"   # still shows the started run
        page.screenshot(path="/tmp/beekeeper-pipeline-queued.png", full_page=True)
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.route("**/api/v1/projects/demo/branches", lambda route: route.fulfill(
            json={"success": True, "data": {"branches": ["feat-x", "main"], "current": "main"}}))
        mobile.goto(live_server + "/pipelines/branch-pipe")
        mobile.click("#btn-pipeline-start")
        mobile.wait_for_function("document.getElementById('run-branch-train').options.length === 2")
        assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
        mobile.screenshot(path="/tmp/beekeeper-pipeline-run-form-390.png", full_page=True)
        page.click("#queued-list li button:has-text('Remove')")
        page.wait_for_function("document.getElementById('queued-runs').hidden")
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_failed_run_pauses_queue_until_skipped(live_server):
    from playwright.sync_api import sync_playwright

    def run(n, status, dismissed=False):
        return {"run_id": n, "run_ref": f"paused-pipe#{n}", "pipeline": "paused-pipe", "status": status,
                "current_step": 0, "error": "step 'a': boom" if status == "failed" else None,
                "overrides": {}, "dismissed": dismissed, "started": status != "queued",
                "started_at": "2026-10-12T10:00:00", "ended_at": None,
                "steps": [{"index": 0, "name": "a", "type": "task", "status":
                           "failed" if status == "failed" else "pending", "attempt": 1, "error": None,
                           "exit_code": None, "started_at": None, "ended_at": None, "run_dir": None,
                           "has_log": False}]}

    state = {"dismissed": False}

    def serve_runs(route):
        runs = [run(2, "queued"), run(1, "failed", state["dismissed"])]
        route.fulfill(json={"success": True, "data": {"runs": runs}})

    def dismiss(route):
        state["dismissed"] = True
        route.fulfill(json={"success": True, "data": {"run_id": 1, "status": "failed"}})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server, "name: paused-pipe\nsteps:\n  - {name: a, type: task, command: 'false'}\n")
        page.route("**/api/v1/pipelines/paused-pipe/runs?limit=50", serve_runs)
        page.route("**/api/v1/pipelines/paused-pipe/runs/1/dismiss", dismiss)
        page.goto(live_server + "/pipelines/paused-pipe")
        page.locator("#queue-paused").wait_for()
        assert "#1 failed" in page.locator("#queue-paused").inner_text()
        assert page.locator("#btn-pipeline-resume").is_visible()
        page.screenshot(path="/tmp/beekeeper-pipeline-paused.png", full_page=True)
        page.click("#btn-queue-skip")
        page.wait_for_selector("#queue-paused", state="hidden")
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_pipeline_step_panel_reruns_from_step(live_server):
    import json as _json
    from playwright.sync_api import sync_playwright

    def step(i, name, status):
        return {"index": i, "name": name, "type": "task", "status": status, "attempt": 1, "error": None,
                "exit_code": 0, "started_at": None, "ended_at": None, "run_dir": None, "has_log": False}

    def run(n, status, current, statuses):
        return {"run_id": n, "run_ref": f"rerun-pipe#{n}", "pipeline": "rerun-pipe", "status": status, "started": True,
                "current_step": current, "error": None, "overrides": {}, "dismissed": False,
                "started_at": "2026-10-12T10:00:00", "ended_at": "2026-10-12T10:05:00",
                "steps": [step(i, nm, st) for i, (nm, st) in enumerate(zip(["a", "b", "c"], statuses))]}

    removed = dict(run(3, "cancelled", 0, ["pending", "pending", "pending"]), started=False)
    runs = [removed, run(2, "failed", 1, ["succeeded", "failed", "skipped"]),
            run(1, "succeeded", 2, ["succeeded", "succeeded", "succeeded"])]
    posted = []

    def resume(route):
        posted.append(_json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"success": True, "data": {"run_id": 2, "run_ref": "rerun-pipe#2", "status": "running"}})

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())
        _create_pipeline(page, live_server, "name: rerun-pipe\nsteps:\n"
                         "  - {name: a, type: task, command: 'true'}\n  - {name: b, type: task, command: 'true'}\n"
                         "  - {name: c, type: task, command: 'true'}\n")
        page.route("**/api/v1/pipelines/rerun-pipe/runs?limit=50",
                   lambda route: route.fulfill(json={"success": True, "data": {"runs": runs}}))
        page.route("**/api/v1/pipelines/rerun-pipe/runs/2/resume", resume)
        page.goto(live_server + "/pipelines/rerun-pipe")
        page.locator("#step-chain .step-node").nth(2).wait_for()

        page.click('#step-chain .step-node:nth-child(3) .step-card')      # c never ran
        assert page.locator("#btn-step-rerun").is_hidden()
        page.click('#step-chain .step-node:nth-child(1) .step-card')      # a ran
        assert page.locator("#btn-step-rerun").is_visible()
        page.screenshot(path="/tmp/beekeeper-pipeline-rerun.png", full_page=True)
        page.click("#btn-step-rerun")
        page.wait_for_function("document.getElementById('step-panel').hidden")
        assert posted == [{"from_step": "a"}]

        page.click('#pipeline-runs tbody tr[data-run="1"]')               # an older run cannot re-run
        page.click('#step-chain .step-node:nth-child(1) .step-card')
        assert page.locator("#btn-step-rerun").is_hidden()
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_project_queue_list_shows_pipeline_source_and_settings(live_server):
    from playwright.sync_api import sync_playwright

    reason = "Training is already running. To run multiple runs simultaneously, enable Parallel Runs in Edit Project."
    queue = [
        {"queue_id": 7, "position": 1, "branch": "arch-modern-wsd", "source": "pipeline:tune-wsd#1",
         "env_vars": {"BEEKEEPER_PIPELINE": "tune-wsd", "BEEKEEPER_PIPELINE_RUN_DIR": "/x/run_1",
                      "LR_SCHEDULE": "wsd", "TUNING_NAME": "wsd_seed2"},
         "wait_reason": reason, "created_at": "2026-10-12T22:37:44"},
        {"queue_id": 8, "position": 2, "branch": "arch-modern-accum", "source": "manual",
         "env_vars": {}, "wait_reason": None, "created_at": "2026-10-12T22:37:45"},
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in (1280, 390):
            page = browser.new_page(viewport={"width": width, "height": 900})
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.route("**/api/v1/projects/demo/queue",
                       lambda route: route.fulfill(json={"success": True, "data": {"queue": queue}}))
            page.goto(f"{live_server}/projects/demo")
            page.locator("#queue-list .queue-row").nth(1).wait_for()
            first = page.locator("#queue-list .queue-row").first
            assert first.locator(".queue-pos").inner_text() == "#1"
            assert first.locator("a.queue-source").get_attribute("href") == "/pipelines/tune-wsd"
            chips = first.locator(".env-chip").all_inner_texts()
            assert chips == ["LR_SCHEDULE=wsd", "TUNING_NAME=wsd_seed2"]
            assert "added here" in page.locator("#queue-list .queue-row").nth(1).inner_text()
            assert "Next starts when free" in page.locator("#queue-wait").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth") <= width
            box = first.locator("button").bounding_box()
            assert box["x"] + box["width"] <= width
            page.locator("#queue-section").scroll_into_view_if_needed()
            page.screenshot(path=f"/tmp/beekeeper-project-queue-{width}.png", full_page=True)
            assert errors == []
            page.close()
        browser.close()


@pytest.mark.browser
def test_pipeline_waiting_on_a_project_queue_reads_as_waiting(live_server):
    from playwright.sync_api import sync_playwright

    run = {"run_id": 1, "run_ref": "wait-pipe#1", "pipeline": "wait-pipe", "status": "running",
           "waiting": True, "started": True, "current_step": 0, "error": None, "overrides": {},
           "dismissed": False, "started_at": "2026-10-12T22:37:44", "ended_at": None,
           "steps": [{"index": 0, "name": "wsd", "type": "train", "status": "queued", "attempt": 1,
                      "error": None, "exit_code": None, "started_at": None, "ended_at": None, "run_dir": None,
                      "project": "demo", "branch": "arch-modern-wsd-a-rather-long-branch-name",
                      "queue_position": 2, "wait_reason": "Training is already running."}]}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        _create_pipeline(page, live_server, "name: wait-pipe\nsteps:\n  - {name: wsd, type: train, project: demo}\n")
        page.route("**/api/v1/pipelines/wait-pipe/runs?limit=50",
                   lambda route: route.fulfill(json={"success": True, "data": {"runs": [run]}}))
        page.goto(live_server + "/pipelines/wait-pipe")
        page.wait_for_function("document.getElementById('pipeline-run-status').textContent === 'waiting'")
        assert "wsd waiting in demo's queue (position 2)" in page.locator("#run-summary").inner_text()
        assert "Waiting: wsd" in page.locator("#pipeline-runs tbody").inner_text()
        assert "arch-modern-wsd-a-rather-long-branch-name" in page.locator("#step-chain").inner_text()
        page.screenshot(path="/tmp/beekeeper-pipeline-waiting.png", full_page=True)
        assert errors == []
        browser.close()


@pytest.mark.browser
def test_dashboard_queued_card_names_the_pipeline_and_its_settings(live_server):
    from playwright.sync_api import sync_playwright

    activity = {"running": [], "recent": [], "queued": [
        {"project": "demo", "branch": "arch-modern-wsd", "position": 1, "url": "/projects/demo",
         "pipeline_ref": "tune-wsd#1", "pipeline_url": "/pipelines/tune-wsd", "env": {"TUNING_NAME": "wsd_seed2"}},
        {"project": "demo", "branch": "", "position": 2, "url": "/projects/demo",
         "pipeline_ref": None, "pipeline_url": None, "env": {}},
    ]}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.route("**/dashboard/activity", lambda route: route.fulfill(json=activity))
        page.goto(live_server + "/")
        page.wait_for_function("document.querySelectorAll('#queued-activity .activity-row').length === 2")
        first, second = page.locator("#queued-activity .activity-row").all()
        assert first.get_attribute("href") == "/pipelines/tune-wsd"
        assert "activity-pipeline" in first.get_attribute("class")
        assert "demo · arch-modern-wsd · position 1 · TUNING_NAME=wsd_seed2" in first.inner_text()
        assert second.get_attribute("href") == "/projects/demo"
        page.locator("#queued-activity-card").screenshot(path="/tmp/beekeeper-dashboard-queued.png")
        assert errors == []
        browser.close()
