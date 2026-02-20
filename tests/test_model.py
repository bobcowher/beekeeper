"""
Unit tests for the Project dataclass and its JSON persistence.
"""
from models.project import Project


def test_project_defaults():
    p = Project(name="test", git_url="https://github.com/x/y.git")
    assert p.branch == "main"
    assert p.python_version == "3.12"
    assert p.train_file == "train.py"
    assert p.setup_status == "pending"
    assert p.train_status == "idle"
    assert p.train_pid == 0
    assert p.env_vars == {}
    assert p.setup_error == ""


def test_save_creates_file(tmp_path):
    p = Project(name="myproj", git_url="https://github.com/x/y.git")
    p.save(str(tmp_path))
    assert (tmp_path / "myproj" / "project.json").exists()


def test_save_and_load_roundtrip(tmp_path):
    p = Project(
        name="myproj",
        git_url="https://github.com/x/y.git",
        branch="dev",
        python_version="3.11",
        setup_status="ready",
        env_vars={"BATCH_SIZE": "32", "LR": "0.001"},
    )
    p.save(str(tmp_path))
    loaded = Project.load(str(tmp_path / "myproj" / "project.json"))

    assert loaded.name == "myproj"
    assert loaded.branch == "dev"
    assert loaded.python_version == "3.11"
    assert loaded.setup_status == "ready"
    assert loaded.env_vars == {"BATCH_SIZE": "32", "LR": "0.001"}


def test_save_is_atomic_no_temp_files_left(tmp_path):
    """save() writes to a tmp file then os.replace — no partial files left behind."""
    p = Project(name="atomic", git_url="https://github.com/x/y.git")
    p.save(str(tmp_path))

    project_dir = tmp_path / "atomic"
    all_files = list(project_dir.iterdir())
    assert len(all_files) == 1
    assert all_files[0].name == "project.json"


def test_env_vars_empty_dict_roundtrip(tmp_path):
    p = Project(name="empty", git_url="https://github.com/x/y.git")
    p.save(str(tmp_path))
    loaded = Project.load(str(tmp_path / "empty" / "project.json"))
    assert loaded.env_vars == {}


def test_to_dict_contains_all_fields(tmp_path):
    p = Project(name="myproj", git_url="https://github.com/x/y.git")
    d = p.to_dict()
    expected_keys = {
        "name", "git_url", "branch", "python_version", "train_file",
        "tensorboard_log_dir", "requirements_file", "env_type",
        "setup_status", "setup_error", "train_status", "train_pid", "env_vars",
    }
    assert expected_keys == set(d.keys())


def test_save_overwrites_existing(tmp_path):
    p = Project(name="myproj", git_url="https://github.com/x/y.git", branch="main")
    p.save(str(tmp_path))

    p2 = Project(name="myproj", git_url="https://github.com/x/y.git", branch="dev")
    p2.save(str(tmp_path))

    loaded = Project.load(str(tmp_path / "myproj" / "project.json"))
    assert loaded.branch == "dev"
