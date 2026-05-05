import pytest

from services.project_service import validate_output_paths


def test_validate_output_paths_normalizes_form_text():
    assert validate_output_paths("saved_models\nexports/checkpoints", "runs") == [
        "saved_models",
        "exports/checkpoints",
    ]


@pytest.mark.parametrize(
    "paths",
    [
        ["/tmp/models"],
        ["../models"],
        ["models/../exports"],
        ["models//exports"],
        ["."],
        ["persistent/models"],
        ["run_logs/archive"],
        [".git/hooks"],
        ["models", "models/checkpoints"],
        ["runs/scalars"],
    ],
)
def test_validate_output_paths_rejects_unsafe_paths(paths):
    with pytest.raises(ValueError):
        validate_output_paths(paths, "runs")
