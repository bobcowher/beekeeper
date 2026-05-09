import json
import os
import tempfile
from dataclasses import dataclass, field, asdict


@dataclass
class Project:
    name: str
    git_url: str
    branch: str = "main"
    python_version: str = "3.12"
    train_file: str = "train.py"
    tensorboard_log_dir: str = "runs"
    requirements_file: str = "requirements.txt"
    env_type: str = "venv"
    setup_script: str = ""
    data_dir_enabled: bool = False
    data_dir_local: str = "data"
    data_dir_remote: str = ""
    setup_status: str = "pending"
    setup_error: str = ""
    train_status: str = "idle"
    train_pid: int = 0
    env_vars: dict = field(default_factory=dict)
    pinned: bool = False
    last_run_at: float = 0.0
    created_at: float = 0.0
    tb_logs_max_runs: int = 10  # Keep only N most recent TensorBoard run directories (0 = unlimited)
    run_history_max_runs: int = 10  # Keep only N most recent run records in database (0 = unlimited)
    parallel_runs_enabled: bool = False
    max_parallel_runs: int = 2
    output_paths: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def save(self, projects_dir):
        project_dir = os.path.join(projects_dir, self.name)
        os.makedirs(project_dir, exist_ok=True)
        config_path = os.path.join(project_dir, "project.json")
        # Atomic write to avoid races with background threads
        fd, tmp_path = tempfile.mkstemp(dir=project_dir, suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp_path, config_path)
        except:
            os.unlink(tmp_path)
            raise

    @classmethod
    def load(cls, config_path):
        with open(config_path) as f:
            data = json.load(f)
        return cls(**data)
