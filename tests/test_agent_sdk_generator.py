from models.project import Project
from services.agent_sdk_generator import generate_sdk


def test_generated_sdk_run_methods_are_project_scoped():
    src = generate_sdk("demo", "http://localhost:5000", Project(name="demo", git_url="x"))
    compile(src, "sdk.py", "exec")
    assert 'f"/projects/{self.project_name}/runs/{run_id}/metrics"' in src
    assert 'f"/projects/{self.project_name}/runs/{run_id}")' in src
    assert 'f"/runs/' not in src
