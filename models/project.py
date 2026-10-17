import json
import os
import re
import tempfile
from dataclasses import dataclass, field, asdict

# setup_status values where the background setup thread (project_service._setup_project)
# is still running — clone, env creation, setup script, or pip install in flight.
SETUP_ACTIVE_STATUSES = {"pending", "cloning", "creating_env", "running_setup_script", "installing_deps"}


FOLDER_RE = re.compile(r"^[A-Za-z0-9 _.-]{0,64}$")


def normalize_folder(value) -> str:
    """Trimmed folder name ('' = unfiled). Folders are flat: no '/'. Raises ValueError."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("folder must be a string")
    value = value.strip()
    if not FOLDER_RE.match(value):
        raise ValueError("folder may use letters, digits, spaces, '_', '.', '-' (max 64) and no '/'")
    return value


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
    gpu_enabled: bool = False
    gpu_memory_minimum: int = 0   # MB — hard floor; run rejected if free VRAM is below this
    gpu_memory_preferred: int = 0  # MB — full ask including offloadable allocations
    folder: str = ""  # flat grouping label; '' = unfiled
    # Runpod (ignored unless the provider is enabled under Admin → Compute Providers)
    compute_target: str = "local"  # "local" | "runpod"
    runpod_gpu_types: list = field(default_factory=list)  # ordered; empty = the pool's list
    runpod_image: str = ""  # empty = the pool's default image
    data_dir_sync: bool = False  # upload the data dir to the pod before training

    def to_dict(self):
        return asdict(self)

    def save(self, projects_dir):
        project_dir = os.path.join(projects_dir, self.name)
        os.makedirs(project_dir, exist_ok=True)
        config_path = os.path.join(project_dir, "project.json")  # NOSONAR
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
