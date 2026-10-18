import json
import os
import stat

import pytest

from services import inherit


def _write(path, text="x", mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


@pytest.fixture
def dirs(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    _write(str(src / "checkpoints" / "pretrained.pt"), "weights")
    _write(str(src / "checkpoints" / "sub" / "run.sh"), "#!/bin/sh\n", mode=0o755)
    _write(str(src / "data" / "train.csv"), "a,b\n1,2\n")
    _write(str(src / "other" / "not-inherited.txt"))
    dst.mkdir()
    return str(src), str(dst)


def test_copies_paths_at_same_relative_paths_with_modes(dirs):
    src, dst = dirs
    entries = inherit.seed_inherited(src, dst, ["checkpoints", "data"], "pretrain", "my-model-3")
    assert open(os.path.join(dst, "checkpoints", "pretrained.pt")).read() == "weights"
    assert open(os.path.join(dst, "data", "train.csv")).read() == "a,b\n1,2\n"
    mode = stat.S_IMODE(os.stat(os.path.join(dst, "checkpoints", "sub", "run.sh")).st_mode)
    assert mode == 0o755
    assert not os.path.exists(os.path.join(dst, "other"))
    assert entries == [
        {"path": "checkpoints", "source_step": "pretrain", "source_run_ref": "my-model-3",
         "files": 2, "bytes": len("weights") + len("#!/bin/sh\n")},
        {"path": "data", "source_step": "pretrain", "source_run_ref": "my-model-3",
         "files": 1, "bytes": len("a,b\n1,2\n")},
    ]


def test_writes_manifest(dirs):
    src, dst = dirs
    entries = inherit.seed_inherited(src, dst, ["data"], "pretrain", "my-model-3")
    with open(os.path.join(dst, ".beekeeper", "inherited.json")) as f:
        assert json.load(f) == {"inherited": entries}
    assert inherit.read_manifest(dst) == entries
    assert inherit.read_manifest(os.path.join(dst, "nowhere")) == []


def test_nested_path_creates_parents(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    _write(str(src / "out" / "ckpt" / "m.pt"))
    inherit.seed_inherited(str(src), str(dst), ["out/ckpt"], "a", "p-1")
    assert os.path.isfile(dst / "out" / "ckpt" / "m.pt")


def test_later_copy_wins_on_a_clash(dirs):
    src, dst = dirs
    _write(os.path.join(dst, "checkpoints", "pretrained.pt"), "stale")
    _write(os.path.join(dst, "checkpoints", "leftover.pt"), "stale")
    inherit.seed_inherited(src, dst, ["checkpoints"], "pretrain", "my-model-3")
    assert open(os.path.join(dst, "checkpoints", "pretrained.pt")).read() == "weights"
    assert not os.path.exists(os.path.join(dst, "checkpoints", "leftover.pt"))


def test_overlapping_paths_later_one_wins(dirs):
    src, dst = dirs
    inherit.seed_inherited(src, dst, ["checkpoints", "checkpoints/sub"], "pretrain", "my-model-3")
    assert os.path.isfile(os.path.join(dst, "checkpoints", "sub", "run.sh"))
    assert not os.path.exists(os.path.join(dst, "checkpoints", "sub", "sub"))


def test_missing_source_raises(dirs):
    src, dst = dirs
    with pytest.raises(inherit.InheritError) as exc:
        inherit.seed_inherited(src, dst, ["checkpoints", "models"], "pretrain", "my-model-3")
    assert str(exc.value) == "inherit: models not found in my-model-3's run folder"


def test_empty_source_is_allowed(tmp_path):
    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "checkpoints").mkdir(parents=True)
    entries = inherit.seed_inherited(str(src), str(dst), ["checkpoints"], "a", "p-1")
    assert entries[0]["files"] == 0
    assert os.path.isdir(dst / "checkpoints")
    assert inherit.log_lines(entries) == [
        "[beekeeper] Inherited checkpoints/ from a (p-1): 0 files, 0 B",
        "[beekeeper] WARNING: inherited checkpoints/ is empty",
    ]


def test_log_line_format():
    entry = {"path": "checkpoints", "source_step": "pretrain", "source_run_ref": "my-model-3",
             "files": 412, "bytes": 2_100_000_000}
    assert inherit.log_lines([entry]) == [
        "[beekeeper] Inherited checkpoints/ from pretrain (my-model-3): 412 files, 2.1 GB"]
    one = dict(entry, files=1, bytes=1500)
    assert inherit.log_lines([one]) == ["[beekeeper] Inherited checkpoints/ from pretrain (my-model-3): 1 file, 1.5 KB"]


def test_source_bytes(dirs):
    src, _ = dirs
    assert inherit.source_bytes(src, ["data", "missing"]) == len("a,b\n1,2\n")
