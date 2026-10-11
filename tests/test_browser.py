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
        page.goto(live_server + "/")
        label = page.locator(".folder-label", has_text="chess")
        assert label.is_visible()
        assert page.locator(".folder-label", has_text="Unfiled").is_visible()
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
        page.goto(live_server + "/")
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
        page.goto(f"{live_server}/projects/demo")
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
        page.goto(f"{live_server}/projects/demo")
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
        page.wait_for_url("**/pipelines/ui-pipe")
        page.locator("#step-chain .step-node").first.wait_for()
        page.fill("#pipeline-yaml", "name: ui-pipe\nsteps:\n  - {name: a, type: train, project: demo, branch: no}\n")
        page.click("#btn-yaml-save")
        page.locator("#yaml-errors li").first.wait_for()
        assert "quote" in page.locator("#yaml-errors").inner_text()
        page.fill("#pipeline-yaml", "name: ui-pipe\nsteps:\n  - {name: a, type: train, project: demo}\n  - {name: b, type: task, command: 'true'}\n")
        page.click("#btn-yaml-save")
        page.wait_for_function("document.getElementById('yaml-status').textContent === 'Saved'")
        page.click("#btn-pipeline-start")
        page.locator("#step-chain .step-pending, #step-chain .step-queued, #step-chain .step-failed, #step-chain .step-running").first.wait_for()
        page.wait_for_function("document.getElementById('pipeline-run-status').textContent === 'running'")
        assert not page.locator("#btn-pipeline-start").is_visible()
        assert page.locator("#btn-pipeline-cancel").is_visible()
        page.screenshot(path="/tmp/beekeeper-pipeline.png", full_page=True)
        page.goto(live_server + "/")
        assert page.locator('.pipeline-row[data-name="ui-pipe"]').is_visible()
        page.screenshot(path="/tmp/beekeeper-dashboard-pipelines.png", full_page=True)
        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        for path, shot in (("/pipelines/ui-pipe", "pipeline-390"), ("/", "dashboard-pipelines-390")):
            mobile.goto(live_server + path)
            mobile.wait_for_load_state("networkidle")
            mobile.screenshot(path=f"/tmp/beekeeper-{shot}.png", full_page=True)
            assert mobile.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
        assert errors == []
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
        log.wait_for(state="visible")
        scroll_top = log.evaluate("el => { el.scrollTop = 200; return el.scrollTop; }")
        assert scroll_top > 0

        page.wait_for_function(
            "document.getElementById('pipeline-run-error').textContent === 'refreshed'",
            timeout=7000,
        )
        assert log.text_content() == log_text
        assert log.evaluate("el => el.scrollTop") == scroll_top
        assert page.locator('[data-step-log="produce"]').get_attribute("aria-expanded") == "true"
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
        assert page.locator('#pipeline-runs tr[data-run="1"] td').last.inner_text() == ""
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
            page.goto(live_server + "/pipelines/layout-pipe")
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
        page.goto(live_server + "/pipelines/error-pipe")
        page.locator("#pipeline-yaml").wait_for()

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
            "run_id": 1, "run_ref": "xss-pipe#1", "status": "running", "current_step": 0,
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
        assert page.locator('label[for="pipeline-yaml"]').text_content().strip() == "Pipeline YAML"
        run_button = row.locator(".run-history-link")
        assert run_button.get_attribute("aria-label") == "Show pipeline run #1"
        run_button.focus()
        run_button.press("Enter")
        assert page.locator("#step-chain img").count() == 0
        assert payload in page.locator("#step-chain .step-name").inner_text()
        assert page.evaluate("window.__pipelineXss") is None

        page.locator("[data-step-log]").click()
        page.locator("#step-log").wait_for(state="visible")
        assert page.locator("#step-log").text_content() == log_payload
        assert page.locator("#step-log script").count() == 0
        assert page.evaluate("window.__pipelineLogXss") is None
        browser.close()
