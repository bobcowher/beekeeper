"""Public run identity: per-project run numbers and human-readable refs."""


def run_ref(project_name: str, run_id: int) -> str:
    return f"{project_name}-{run_id}"


def public_run(row: dict) -> dict:
    """Serialize a training_runs row for API/UI consumers. Never exposes the primary key."""
    out = dict(row)
    out.pop("id", None)
    out["run_id"] = out.pop("run_number")
    out["run_ref"] = run_ref(out["project_name"], out["run_id"])
    return out
