import json
import os
import tempfile
from dataclasses import dataclass, asdict


@dataclass
class Project:
    name: str
    git_url: str
    branch: str = "main"
    python_version: str = "3.12"
    train_file: str = "train.py"
    tensorboard_log_dir: str = "runs"
    requirements_file: str = "requirements.txt"
    setup_status: str = "pending"
    setup_error: str = ""

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
