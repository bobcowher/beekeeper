# Beekeeper test runner
# ROS sets PYTHONPATH in this environment which would pull in ROS pytest plugins.
# Clearing it ensures only the beekeeper venv is visible to pytest.

.PHONY: test test-browser test-all

test:
	PYTHONPATH="" venv/bin/pytest tests/ -m "not browser" -v

test-browser:
	PYTHONPATH="" venv/bin/pytest tests/ -m "browser" -v

test-all:
	PYTHONPATH="" venv/bin/pytest tests/ -v

coverage:
	PYTHONPATH="" venv/bin/pytest tests/ -m "not browser" --cov=. --cov-report=term-missing
