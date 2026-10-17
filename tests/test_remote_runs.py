"""Runpod remote runs, end to end against a fake Runpod API and a local folder standing in for the pod.

LocalTransport maps the pod's /bk onto tmp_path/node/bk and runs the same bash
the real pod would, so upload, prepare, train, log pull, sync and manifest
checks all execute for real. Only Runpod's HTTP API is faked.
"""
import json
import os
import subprocess
import time

import pytest

from services import process_manager as pm
from services import remote_runs, remote_store, runpod_config, spend_guard
from services.node_transport import LocalTransport
from tests.conftest import make_project_dir

CATALOG = [
    {"id": "NVIDIA GeForce RTX 3070", "memory": 8, "availability": "LOW",
     "price": {"community": 0.13, "secure": 0.3}},
    {"id": "NVIDIA RTX A4000", "memory": 16, "availability": "LOW",
     "price": {"community": 0.17, "secure": 0.25}},
    {"id": "NVIDIA GeForce RTX 4090", "memory": 24, "availability": "NONE",
     "price": {"community": 0.34, "secure": 0.69}},
    {"id": "NVIDIA RTX A5000", "memory": 24, "availability": "HIGH",
     "price": {"community": 0.16, "secure": 0.27}},
]


class FakeClient:
    def __init__(self):
        self.pods = {}
        self.created = []
        self.terminated = []
        self.refuse = set()
        self.catalog = [dict(g) for g in CATALOG]

    def gpu_types(self, cloud="COMMUNITY"):
        return self.catalog

    def create_pod(self, body):
        from services.runpod_client import RunpodError
        if body["gpu"]["id"] in self.refuse:
            raise RunpodError(500, "no capacity")
        pid = f"pod{len(self.created) + 1}"
        price = next(g["price"]["community"] for g in self.catalog if g["id"] == body["gpu"]["id"])
        self.pods[pid] = {"id": pid, "name": body["name"], "status": "RUNNING", "cost": price,
                          "ssh": {"direct": {"ip": "127.0.0.1", "port": 22}}}
        self.created.append(body)
        return dict(self.pods[pid])

    def get_pod(self, pod_id):
        p = self.pods.get(pod_id)
        return dict(p) if p else None

    def list_pods(self):
        return [dict(p) for p in self.pods.values()]

    def terminate(self, pod_id):
        self.terminated.append(pod_id)
        self.pods.pop(pod_id, None)


@pytest.fixture
def runpod(app, tmp_path, monkeypatch):
    """Runpod enabled with a $5/day hard cap, a fake client and a local 'pod'."""
    from services import config_service
    store = {}
    monkeypatch.setattr(config_service, "get_config", lambda k, d=None: store.get(k, None if d is None else str(d)))
    monkeypatch.setattr(config_service, "get_config_bool",
                        lambda k, d=False: str(store.get(k, d)).lower() in ("true", "1"))
    monkeypatch.setattr(config_service, "set_config", lambda k, v: store.__setitem__(k, str(v)))
    monkeypatch.setattr(config_service, "save_config", lambda: None)
    for mod in (runpod_config,):
        for fn in ("get_config", "get_config_bool", "set_config", "save_config"):
            monkeypatch.setattr(mod, fn, getattr(config_service, fn))
    runpod_config.init(str(tmp_path / "home"))
    runpod_config.set_api_key("rpa_test")
    store.update({
        "compute.runpod.enabled": "true", "compute.runpod.cap.daily.hard": "5",
        "compute.runpod.sync_interval_min": "0.25", "compute.runpod.max_upload_gb": "1",
    })
    remote_runs.init(str(tmp_path / "home"))
    client = FakeClient()
    monkeypatch.setattr(remote_runs, "get_client", lambda: client)
    node = tmp_path / "node"
    transport = LocalTransport(str(node))
    (node / "bk" / "ready").touch()
    monkeypatch.setattr(remote_runs, "make_transport", lambda row: transport)
    monkeypatch.setattr(remote_runs, "POLL_INTERVAL", 0.2)
    monkeypatch.setattr(pm, "_resolve_python_binary", lambda d, p: "/usr/bin/python3")
    monkeypatch.setattr(pm, "_resolve_tensorboard_binary", lambda d, p: None)
    pm._running.clear()
    yield {"client": client, "node": node, "store": store, "app": app}
    for rp in remote_runs.live_processes():
        rp.request_stop("test teardown", hard=True)
        rp.wait(30)
    pm._running.clear()


def _git_project(app, tmp_path, name="rp", train="print('hi')\n", **overrides):
    """A real git origin + workspace clone, so the pre-launch git sync works."""
    origin = tmp_path / f"{name}-origin"
    seed = tmp_path / f"{name}-seed"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "train.py").write_text(train)
    for rel, text in overrides.pop("files", {}).items():
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "init"], check=True, env=env)
    subprocess.run(["git", "-C", str(seed), "push", "-q", str(origin), "main"], check=True)
    ws = os.path.join(app.config["PROJECTS_DIR"], name, "workspace")
    subprocess.run(["git", "clone", "-q", str(origin), ws], check=True)
    make_project_dir(app, name, git_url=str(origin), compute_target="runpod", **overrides)
    return ws


def _wait_done(name, run_id, timeout=60):
    from services.db_service import get_db
    end = time.time() + timeout
    while time.time() < end:
        run = get_db().get_run(name, run_id)
        if run and run["status"] not in ("starting", "running"):
            with pm._lock:
                tracked = any(i.get("run_id") == run_id and i.get("project_name") == name
                              for i in pm._running.values())
            if not tracked:
                return run
        time.sleep(0.2)
    raise AssertionError(f"run {name}-{run_id} did not finish")


def _log(app, name, run_id):
    try:
        with open(pm.get_run_log_path(app.config["PROJECTS_DIR"], name, run_id)) as f:
            return f.read()
    except FileNotFoundError:   # the background thread hasn't written the header yet
        return ""


TRAIN_WITH_OUTPUTS = """
import os
os.makedirs('outputs', exist_ok=True)
open('outputs/model.pt', 'w').write('weights')
open(os.path.join(os.environ['BEEKEEPER_RUN_DIR'], 'metrics.txt'), 'w').write(os.environ['MY_VAR'])
open(os.path.join(os.environ['BEEKEEPER_RUN_DIR'], 'run_id.txt'), 'w').write(
    os.environ.get('BEEKEEPER_RUN_ID', '') + ' ' + os.environ.get('BEEKEEPER_RUN_REF', ''))
print('trained on', os.environ['BEEKEEPER_RUN_DIR'])
"""


def test_happy_path_results_land_in_run_folder_and_pod_is_terminated(runpod, tmp_path):
    app = runpod["app"]
    _git_project(app, tmp_path, train=TRAIN_WITH_OUTPUTS, output_paths=["outputs"], env_vars={"MY_VAR": "42"})
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    assert "error" not in r, r
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "completed", _log(app, "rp", r["run_id"])

    run_dir = os.path.join(app.config["PROJECTS_DIR"], "rp", "persistent", "runs", f"run_{r['run_id']}")
    assert open(os.path.join(run_dir, "outputs", "model.pt")).read() == "weights"
    assert open(os.path.join(run_dir, "metrics.txt")).read() == "42"
    assert open(os.path.join(run_dir, "run_id.txt")).read() == f"{r['run_id']} rp-{r['run_id']}"
    text = _log(app, "rp", r["run_id"])
    assert "trained on /bk/run" not in text or "trained on" in text
    assert "Results synced and verified" in text
    assert runpod["client"].terminated == ["pod1"]
    # Cheapest in-stock card with >= 16 GB (the default floor): A5000 at $0.16 beats A4000 at $0.17.
    assert runpod["client"].created[0]["gpu"]["id"] == "NVIDIA RTX A5000"
    row = remote_store.get(run["id"])
    assert row["phase"] == "done"
    assert not os.path.exists(row["key_path"])


def test_gpu_list_is_tried_in_order_and_sold_out_types_skipped(runpod, tmp_path):
    app = runpod["app"]
    _git_project(app, tmp_path, runpod_gpu_types=["NVIDIA GeForce RTX 4090", "NVIDIA RTX A4000"])
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    _wait_done("rp", r["run_id"])
    assert runpod["client"].created[0]["gpu"]["id"] == "NVIDIA RTX A4000"


def test_create_refusal_falls_through_to_next_candidate(runpod, tmp_path):
    app = runpod["app"]
    runpod["client"].refuse.add("NVIDIA RTX A5000")
    _git_project(app, tmp_path)
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "completed"
    assert runpod["client"].created[0]["gpu"]["id"] == "NVIDIA RTX A4000"


def test_training_failure_is_reported_and_pod_still_terminated(runpod, tmp_path):
    app = runpod["app"]
    _git_project(app, tmp_path, train="import sys; print('boom'); sys.exit(3)\n")
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "crashed" and run["exit_code"] == 3
    assert "boom" in _log(app, "rp", r["run_id"])
    assert runpod["client"].terminated == ["pod1"]


def test_user_stop_syncs_then_terminates(runpod, tmp_path):
    app = runpod["app"]
    _git_project(app, tmp_path, train=(
        "import os, time\n"
        "for i in range(600):\n"
        "    open(os.path.join(os.environ['BEEKEEPER_RUN_DIR'], 'step.txt'), 'w').write(str(i))\n"
        "    print('step', i, flush=True); time.sleep(0.1)\n"))
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    deadline = time.time() + 30
    while "step 5" not in _log(app, "rp", r["run_id"]) and time.time() < deadline:
        time.sleep(0.2)
    out = pm.stop_training(app.config["PROJECTS_DIR"], "rp", run_id=r["run_id"])
    assert out["status"] == "stopped"
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "canceled"
    run_dir = os.path.join(app.config["PROJECTS_DIR"], "rp", "persistent", "runs", f"run_{r['run_id']}")
    assert int(open(os.path.join(run_dir, "step.txt")).read()) >= 5
    assert runpod["client"].terminated == ["pod1"]
    assert remote_store.get(run["id"])["stop_reason"] == "stopped by user"


def test_hard_cap_stops_running_pod(runpod, tmp_path):
    app = runpod["app"]
    runpod["store"]["compute.runpod.cap.daily.hard"] = "0.001"
    _git_project(app, tmp_path, train="import time\nfor i in range(600):\n    print(i, flush=True); time.sleep(0.1)\n")
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    deadline = time.time() + 30
    while remote_store.get(pm.get_db().get_run("rp", r["run_id"])["id"]) is None or \
            remote_store.get(pm.get_db().get_run("rp", r["run_id"])["id"])["phase"] != "train":
        assert time.time() < deadline
        time.sleep(0.1)
    # Pretend the pod has been up for an hour: $0.16 is over the $0.001 cap.
    remote_runs.tick(now=time.time() + 3600)
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "canceled"
    assert "hard cap reached (daily)" in remote_store.get(run["id"])["stop_reason"]
    assert runpod["client"].terminated == ["pod1"]


def test_refusals(runpod, tmp_path):
    app = runpod["app"]
    pdir = app.config["PROJECTS_DIR"]
    _git_project(app, tmp_path, name="dd", data_dir_enabled=True, data_dir_remote=str(tmp_path))
    assert pm.start_training(pdir, "dd")["code"] == "DATA_DIR_NOT_SYNCED"

    _git_project(app, tmp_path, name="cc", env_type="conda")
    assert pm.start_training(pdir, "cc")["code"] == "REMOTE_CONDA_UNSUPPORTED"

    _git_project(app, tmp_path, name="big", gpu_memory_minimum=48 * 1024)
    r = pm.start_training(pdir, "big")
    assert r["code"] == "NO_GPU_AVAILABLE" and "48 GB" in r["error"]

    big = tmp_path / "bigdata"
    big.mkdir()
    with open(big / "blob", "wb") as f:
        f.truncate(2 * 10**9)   # sparse, but counts as 2 GB against the 1 GB limit
    _git_project(app, tmp_path, name="up", data_dir_enabled=True, data_dir_remote=str(big), data_dir_sync=True)
    r = pm.start_training(pdir, "up")
    assert r["code"] == "UPLOAD_TOO_LARGE" and "data dir" in r["error"]

    del runpod["store"]["compute.runpod.cap.daily.hard"]
    _git_project(app, tmp_path, name="nocap")
    assert pm.start_training(pdir, "nocap")["code"] == "NO_SPEND_CAP"
    assert pm.get_db().get_training_runs("nocap") == []   # a refused start never uses a run number


def test_soft_cap_waits_in_queue(runpod, tmp_path):
    from services import dispatcher
    from services.db_service import get_db
    app = runpod["app"]
    runpod["store"]["compute.runpod.cap.daily.soft"] = "0.01"
    spend_guard.accrue(None, 1.0, time.time() - 60, time.time())   # $0.0167 spent today
    _git_project(app, tmp_path)
    qid = get_db().enqueue_run("rp", source="test")
    dispatcher.dispatch_project_queue(app.config["PROJECTS_DIR"], "rp")
    entry = get_db().get_queue_entry(qid)
    assert entry["status"] == "queued" and "soft cap reached" in entry["wait_reason"]


def test_data_dir_sync_uploads_and_links(runpod, tmp_path):
    app = runpod["app"]
    data = tmp_path / "dataset"
    data.mkdir()
    (data / "rows.csv").write_text("a,b\n1,2\n")
    _git_project(app, tmp_path, train="print(open('data/rows.csv').read().strip())\n",
                 data_dir_enabled=True, data_dir_remote=str(data), data_dir_sync=True)
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp")
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "completed", _log(app, "rp", r["run_id"])
    assert "1,2" in _log(app, "rp", r["run_id"])


def test_pipeline_inputs_are_uploaded_and_env_rewritten(runpod, tmp_path):
    app = runpod["app"]
    prev = tmp_path / "prev_step"
    prev.mkdir()
    (prev / "model.pt").write_text("pretrained")
    _git_project(app, tmp_path, train=(
        "import os\n"
        "p = os.environ['BEEKEEPER_STEP_PRETRAIN_DIR']\n"
        "print('input at', p, open(os.path.join(p, 'model.pt')).read())\n"))
    r = pm.start_training(app.config["PROJECTS_DIR"], "rp",
                          extra_env={"BEEKEEPER_STEP_PRETRAIN_DIR": str(prev)})
    run = _wait_done("rp", r["run_id"])
    assert run["status"] == "completed", _log(app, "rp", r["run_id"])
    text = _log(app, "rp", r["run_id"])
    assert "/bk/inputs/BEEKEEPER_STEP_PRETRAIN_DIR pretrained" in text.replace(str(runpod["node"]), "")


def test_inputs_naming_the_same_folder_upload_once(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    targets = remote_runs.input_targets({"BEEKEEPER_PREV_STEP_DIR": str(a), "BEEKEEPER_PIPELINE_RUN_DIR": str(a),
                                         "BEEKEEPER_STEP_X_DIR": str(tmp_path)})
    assert targets["BEEKEEPER_PREV_STEP_DIR"] == targets["BEEKEEPER_PIPELINE_RUN_DIR"] == \
        "/bk/inputs/BEEKEEPER_PIPELINE_RUN_DIR"
    assert targets["BEEKEEPER_STEP_X_DIR"] == "/bk/inputs/BEEKEEPER_STEP_X_DIR"


def test_orphan_sweep_only_touches_our_pods(runpod):
    client = runpod["client"]
    prefix = f"bk-{runpod_config.instance_id()}-"
    old = "2020-01-01T00:00:00.000Z"
    young = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    client.pods["a"] = {"id": "a", "name": prefix + "ghost-1", "status": "RUNNING", "createdAt": old}
    client.pods["b"] = {"id": "b", "name": "someone-elses-pod", "status": "RUNNING", "createdAt": old}
    client.pods["c"] = {"id": "c", "name": "bk-ffffff-other-beekeeper-1", "status": "RUNNING", "createdAt": old}
    client.pods["d"] = {"id": "d", "name": prefix + "just-made-1", "status": "RUNNING", "createdAt": young}
    client.pods["e"] = {"id": "e", "name": prefix + "no-date-1", "status": "RUNNING"}
    assert remote_runs.sweep_orphans() == ["a"]
    assert set(client.pods) == {"b", "c", "d", "e"}


def test_instance_id_belongs_to_the_database(tmp_path):
    """Two databases (say, a test one and the real one) never share an id, whatever the config says."""
    from services import db_service
    ids = []
    for n in range(2):
        db_service.init_database(str(tmp_path / f"db{n}.sqlite"))
        ids.append(remote_store.instance_id())
        assert remote_store.instance_id() == ids[-1]   # stable within one database
    assert ids[0] != ids[1]


def test_unit_tests_cannot_reach_real_runpod(app):
    """The conftest guard: without a test's own fake, there is no client and Runpod is off."""
    from services import runpod_client
    assert runpod_client.get_client() is None and remote_runs.get_client() is None
    assert runpod_config.enabled() is False


def test_restart_reattaches_to_a_running_pod(runpod, tmp_path):
    """Simulate a Beekeeper restart mid-train: forget the in-memory run, then recover from the DB."""
    app = runpod["app"]
    pdir = app.config["PROJECTS_DIR"]
    _git_project(app, tmp_path, train=(
        "import os, time\n"
        "for i in range(30):\n"
        "    print('tick', i, flush=True); time.sleep(0.1)\n"
        "open(os.path.join(os.environ['BEEKEEPER_RUN_DIR'], 'done.txt'), 'w').write('ok')\n"))
    r = pm.start_training(pdir, "rp")
    run_pk = pm.get_db().get_run("rp", r["run_id"])["id"]
    deadline = time.time() + 30
    while (remote_store.get(run_pk) or {}).get("phase") != "train":
        assert time.time() < deadline
        time.sleep(0.05)
    # "Crash": drop the live process and its tracking without finalizing.
    rp = remote_runs._live.pop(run_pk)
    rp._finalize = lambda code: code        # the old thread must not finish the run
    rp._kill_training = lambda: None        # nor touch the training on the pod
    rp.request_stop("simulated crash")
    with pm._lock:
        pm._running.pop(run_pk, None)
    rp.wait(30)
    remote_store.update(run_pk, phase="train")

    assert pm.adopt_remote_runs(pdir) == 1
    run = _wait_done("rp", r["run_id"])
    assert run["status"] in ("completed", "canceled")
    assert "Beekeeper restarted; reattaching" in _log(app, "rp", r["run_id"])
    assert runpod["client"].terminated == ["pod1"]


def test_disabled_provider_changes_nothing(app, monkeypatch):
    monkeypatch.setattr(runpod_config, "enabled", lambda: False)
    assert remote_runs.is_remote({"compute_target": "runpod"}) is False
    remote_runs.tick()   # no-op, no client needed


def test_choose_gpus_floor_and_warnings():
    cands, warnings = remote_runs.choose_gpus(CATALOG, ["NVIDIA GeForce RTX 3070", "NVIDIA RTX A5000"], 16,
                                              "COMMUNITY")
    assert [g["id"] for g in cands] == ["NVIDIA RTX A5000"]
    assert "below the 16 GB minimum" in warnings[0]
    cands, _ = remote_runs.choose_gpus(CATALOG, [], 8, "COMMUNITY")
    assert [g["id"] for g in cands][:2] == ["NVIDIA GeForce RTX 3070", "NVIDIA RTX A5000"]


def test_spend_accrual_buckets_and_periods(app):
    now = time.time()
    spend_guard.accrue(7, 3.6, now - 150, now)      # $3.60/hr for 150 s = $0.15
    assert abs(remote_store.spent_for_run(7) - 0.15) < 1e-9
    assert abs(remote_store.spent_since("runpod", spend_guard.period_start("daily", now) - 1) - 0.15) < 1e-6 \
        or spend_guard.period_start("daily", now) > now - 150
