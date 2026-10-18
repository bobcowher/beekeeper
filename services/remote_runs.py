"""Training runs on a Runpod pod.

process_manager hands a remote project's run here at the point where it would
start a local subprocess. RemoteProcess stands in for that Popen object: poll()
returns None until the pod has trained, the results are synced home and the
pod is terminated. Everything above process_manager (queue, pipelines, UI)
sees an ordinary run.

Phases (persisted in remote_runs.phase so a restart knows where it stopped):
    provision -> upload -> prepare -> train -> finalize -> done
"""
import base64
import logging
import math
import os
import shlex
import subprocess
import threading
import time

from services import remote_store, runpod_config, spend_guard
from services.node_transport import SshTransport
from services.runpod_client import RunpodError, get_client
from services.run_refs import run_ref

log = logging.getLogger(__name__)

PROVIDER = "runpod"
DEFAULT_MIN_VRAM_GB = 16
PROVISION_TIMEOUT = 20 * 60
POLL_INTERVAL = 3
HARD_STOP_SYNC_TIMEOUT = 120
ORPHAN_SWEEP_INTERVAL = 300
ORPHAN_MIN_AGE = 600       # never sweep a pod younger than this, whatever its name
_SIZE_CACHE_TTL = 600
_BILLING_PHASES = {"provision", "upload", "prepare", "train", "finalize"}
_PATH_ENV_KEYS = ("BEEKEEPER_PREV_STEP_DIR", "BEEKEEPER_PIPELINE_RUN_DIR")

_live = {}                 # run_pk -> RemoteProcess
_live_lock = threading.Lock()
_accrual_lock = threading.Lock()
_size_cache = {}
_last_sweep = 0.0
_home = None


def init(beekeeper_home: str):
    global _home
    _home = beekeeper_home
    runpod_config.init(beekeeper_home)


def is_remote(project: dict) -> bool:
    return project.get("compute_target") == "runpod" and runpod_config.enabled()


def make_transport(row: dict):
    """Tests monkeypatch this to return a LocalTransport."""
    return SshTransport(row["ssh_host"], row["ssh_port"], row["key_path"])


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

def _dir_size(path: str, skip_top: tuple = ()) -> int:
    key = (path, skip_top)
    hit = _size_cache.get(key)
    if hit and time.time() - hit[0] < _SIZE_CACHE_TTL:
        return hit[1]
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__")
                   and not (root == path and d in skip_top)]
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    _size_cache[key] = (time.time(), total)
    return total


# A plain task that inherits works in steps/<task>/ under the pipeline run folder. Later steps
# get those files through inherit or PREV_STEP_DIR, so the shared folder goes up without them.
_PIPELINE_DIR_SKIP = ("steps",)


def _input_skip(inputs: dict, path: str) -> tuple:
    pipe = inputs.get("BEEKEEPER_PIPELINE_RUN_DIR")
    return _PIPELINE_DIR_SKIP if pipe and os.path.realpath(path) == os.path.realpath(pipe) else ()


def _input_dirs(extra_env: dict | None) -> dict:
    """{env key: local dir} for the pipeline hand-off variables that point at existing folders."""
    out = {}
    for key, value in (extra_env or {}).items():
        is_path_key = key in _PATH_ENV_KEYS or (key.startswith("BEEKEEPER_STEP_") and key.endswith("_DIR"))
        if is_path_key and isinstance(value, str) and os.path.isdir(value):
            out[key] = value
    return out


def _fmt_gb(n_bytes: float) -> str:
    return f"{n_bytes / 1e9:.1f} GB"


def min_vram_gb(project: dict) -> int:
    mb = int(project.get("gpu_memory_minimum") or 0)
    return math.ceil(mb / 1024) if mb > 0 else DEFAULT_MIN_VRAM_GB


def choose_gpus(catalog: list[dict], wanted: list[str], floor_gb: int, cloud: str) -> tuple[list[dict], list[str]]:
    """(candidates in try order, warnings). Listed types are tried in order; otherwise cheapest first."""
    key = cloud.lower()
    priced = [g for g in catalog if (g.get("price") or {}).get(key)]
    in_stock = [g for g in priced if g.get("availability") not in (None, "NONE")]
    warnings = []
    if wanted:
        by_id = {g["id"]: g for g in priced}
        out = []
        for w in wanted:
            g = by_id.get(w)
            if g is None:
                continue
            if g["memory"] < floor_gb:
                warnings.append(f"{w} has {g['memory']} GB, below the {floor_gb} GB minimum; skipped")
                continue
            if g.get("availability") not in (None, "NONE"):
                out.append(g)
        return out, warnings
    fits = [g for g in in_stock if g["memory"] >= floor_gb]
    return sorted(fits, key=lambda g: g["price"][key]), warnings


def precheck(projects_dir: str, name: str, project: dict, extra_env: dict | None = None,
             inherit: dict | None = None) -> tuple[dict | None, dict | None]:
    """Everything that may refuse a remote start, outside process_manager's lock (makes API calls).

    inherit is a pipeline step's inherited paths (see process_manager.start_training); they
    are seeded into the run folder here and uploaded with it, so they count toward the limit.
    Returns (error, plan). error is {"error", "code"} like any start refusal.
    """
    settings = runpod_config.load()
    client = get_client()
    if client is None:
        return {"error": "Runpod has no API key. Set it under Admin → Compute Providers.",
                "code": "RUNPOD_NOT_CONFIGURED"}, None
    if project.get("env_type") == "conda":
        return {"error": "Conda projects can't run on Runpod yet; use a venv project.",
                "code": "REMOTE_CONDA_UNSUPPORTED"}, None
    data_dir = project.get("data_dir_remote") if project.get("data_dir_enabled") else ""
    if data_dir and not project.get("data_dir_sync"):
        return {"error": (f"This project has a data dir ({data_dir}) but 'Sync data dir on startup' is off, "
                          "so a Runpod pod would have no data. Turn sync on or point the project elsewhere."),
                "code": "DATA_DIR_NOT_SYNCED"}, None
    if data_dir and not os.path.isdir(data_dir):
        return {"error": f"Data dir {data_dir} does not exist on this server.", "code": "DATA_DIR_MISSING"}, None

    refusal = spend_guard.launch_refusal(settings)
    if refusal:
        return refusal, None

    workspace = os.path.join(projects_dir, name, "workspace")
    sizes = {"workspace": _dir_size(workspace)}
    if data_dir:
        sizes["data dir"] = _dir_size(data_dir)
    inputs = _input_dirs(extra_env)
    if inputs:
        sizes["pipeline inputs"] = sum(_dir_size(p, _input_skip(inputs, p)) for p in set(inputs.values()))
    if inherit and inherit.get("paths"):
        from services.inherit import source_bytes
        sizes["inherited"] = source_bytes(inherit["source_run_dir"], inherit["paths"])
    total = sum(sizes.values())
    if settings.max_upload_gb and total > settings.max_upload_gb * 1e9:
        biggest = max(sizes, key=sizes.get)
        return {"error": (f"Upload would be {_fmt_gb(total)}, over the {settings.max_upload_gb:g} GB limit "
                          f"(the {biggest} is {_fmt_gb(sizes[biggest])}). Point the project at a smaller "
                          "folder or raise the limit."),
                "code": "UPLOAD_TOO_LARGE"}, None

    wanted = runpod_config.parse_gpu_list(project.get("runpod_gpu_types")) or settings.gpu_types
    floor = min_vram_gb(project)
    try:
        catalog = client.gpu_types(settings.cloud_type)
    except RunpodError as e:
        return {"error": f"Runpod catalog unavailable: {e}", "code": "NO_GPU_AVAILABLE"}, None
    candidates, warnings = choose_gpus(catalog, wanted, floor, settings.cloud_type)
    if not candidates:
        what = ", ".join(wanted) if wanted else f"GPU with {floor} GB+"
        return {"error": f"No {what} available on Runpod {settings.cloud_type.lower()} cloud right now",
                "code": "NO_GPU_AVAILABLE"}, None

    return None, {
        "candidates": [{"id": g["id"], "memory": g["memory"],
                        "price": g["price"][settings.cloud_type.lower()]} for g in candidates[:4]],
        "warnings": warnings,
        "image": (project.get("runpod_image") or "").strip() or settings.default_image,
        "cloud": settings.cloud_type,
        "upload_bytes": total,
        "upload_sizes": sizes,
        "inputs": inputs,
    }


def input_targets(inputs: dict) -> dict:
    """{env key: pod path}. Variables naming the same folder share one upload (named after the first key)."""
    by_path, out = {}, {}
    for key in sorted(inputs):
        out[key] = by_path.setdefault(os.path.realpath(inputs[key]), f"/bk/inputs/{key}")
    return out


def pool_capacity_error(running: dict) -> dict | None:
    """Called under process_manager._lock with its _running dict."""
    limit = runpod_config.load().max_concurrent_pods
    active = sum(1 for info in running.values() if info.get("remote"))
    if active >= limit:
        return {"error": f"Runpod pool at capacity ({limit} pod{'s' if limit != 1 else ''})", "code": "AT_CAPACITY"}
    return None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class _Stop(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code, self.reason = code, reason


class RemoteProcess:
    """Popen stand-in for a run on a pod. pid is 0, so nothing tries to signal it locally."""

    is_remote = True
    pid = 0

    def __init__(self, ctx: dict, resume_row: dict | None = None):
        self.ctx = ctx
        self.run_pk = ctx["run_pk"]
        self.returncode = None
        self._done = threading.Event()
        self._stop_event = threading.Event()
        self._stop_reason = None
        self._hard_stop = False
        self._resume_row = resume_row
        self.transport = None
        self.client = get_client()
        self.settings = runpod_config.load()
        self._log_offset = 0
        self._exit_code = None
        with _live_lock:
            _live[self.run_pk] = self
        self._thread = threading.Thread(target=self._main, name=f"remote-run-{self.run_pk}", daemon=True)
        self._thread.start()

    # Popen interface -------------------------------------------------------
    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("remote-run", timeout)
        return self.returncode

    def request_stop(self, reason: str, hard: bool = False):
        if self._stop_reason is None or hard:
            self._stop_reason = reason
        self._hard_stop = self._hard_stop or hard
        self._stop_event.set()

    def describe(self) -> dict:
        row = remote_store.get(self.run_pk) or {}
        return {k: row.get(k) for k in ("pod_name", "pod_id", "gpu_type", "phase", "phase_detail",
                                        "cost_per_hr", "last_sync_at", "created_at")}

    # helpers ---------------------------------------------------------------
    def _log(self, msg: str):
        try:
            with open(self.ctx["log_path"], "a") as f:
                f.write(f"[beekeeper] {msg}\n")
        except OSError:
            pass

    def _phase(self, phase: str, detail: str | None = None):
        remote_store.update(self.run_pk, phase=phase, phase_detail=detail)

    def _detail(self, detail: str):
        remote_store.update(self.run_pk, phase_detail=detail)

    def _check_stop(self):
        if self._stop_event.is_set():
            raise _Stop(-15, self._stop_reason or "stopped")

    def _sleep(self, seconds):
        if self._stop_event.wait(seconds):
            raise _Stop(-15, self._stop_reason or "stopped")

    # main ------------------------------------------------------------------
    def _main(self):
        code = 1
        try:
            if self._resume_row is not None:
                code = self._resume()
            else:
                self._provision()
                self._upload()
                self._prepare()
                code = self._train()
        except _Stop as s:
            code = s.code
            self._log(f"Stopping remote run: {s.reason}")
            remote_store.update(self.run_pk, stop_reason=s.reason)
            self._kill_training()
        except Exception as e:  # noqa: BLE001 — any failure must still terminate the pod
            log.exception("Remote run %s failed", self.run_pk)
            self._log(f"Remote run failed: {e}")
            remote_store.update(self.run_pk, stop_reason=f"error: {e}"[:300])
            code = 1
        finally:
            code = self._finalize(code)
            with _live_lock:
                _live.pop(self.run_pk, None)
            self.returncode = code
            self._done.set()

    # phases ----------------------------------------------------------------
    def _new_key(self, pod_name: str) -> str:
        key_dir = os.path.join(_home or "/tmp", "secrets", "pods")  # NOSONAR — only reached without init in tests
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        key_path = os.path.join(key_dir, pod_name)
        for p in (key_path, key_path + ".pub"):
            if os.path.exists(p):
                os.unlink(p)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", pod_name, "-f", key_path],
                       check=True, capture_output=True)
        return key_path

    def _provision(self):
        ctx, plan = self.ctx, self.ctx["plan"]
        row = remote_store.get(self.run_pk)
        pod_name = row["pod_name"]
        self._phase("provision", "requesting a pod")
        for w in plan.get("warnings", []):
            self._log(f"WARNING: {w}")
        key_path = self._new_key(pod_name)
        with open(key_path + ".pub") as f:
            pubkey = f.read().strip()
        with open(os.path.join(os.path.dirname(__file__), "node_bootstrap.sh")) as f:
            bootstrap = f.read()
        deadline_s = int((self.settings.max_runtime_hours + self.settings.hold_for_sync_hours) * 3600)

        pod, gpu = None, None
        for cand in plan["candidates"]:
            self._check_stop()
            body = {
                "name": pod_name, "image": plan["image"], "cloud": plan["cloud"],
                "gpu": {"id": cand["id"], "count": 1}, "disk": self.settings.container_disk_gb,
                "ports": ["22/tcp"],
                "env": {"BK_PUBLIC_KEY": pubkey, "BK_DEADLINE_S": str(deadline_s),
                        "BK_BOOTSTRAP": base64.b64encode(bootstrap.encode()).decode()},
                "entrypoint": ["/bin/bash", "-c"],
                "cmd": ['echo "$BK_BOOTSTRAP" | base64 -d > /bk-bootstrap.sh && exec bash /bk-bootstrap.sh'],
            }
            try:
                pod = self.client.create_pod(body)
                gpu = cand
                break
            except RunpodError as e:
                self._log(f"Runpod refused {cand['id']}: {e}")
        if pod is None:
            raise RuntimeError("No candidate GPU could be created (sold out during launch). Queue the run again.")

        now = time.time()
        cost = pod.get("cost") or gpu["price"]
        remote_store.update(
            self.run_pk, pod_id=pod["id"], gpu_type=gpu["id"], image=plan["image"], cost_per_hr=cost,
            key_path=key_path, created_at=now, last_accrual_at=now,
            deadline_at=now + self.settings.max_runtime_hours * 3600)
        self._log(f"Runpod pod {pod['id']} ({pod_name}): {gpu['id']} {gpu['memory']} GB, "
                  f"${cost:.2f}/hr, {plan['cloud'].lower()} cloud, image {plan['image']}")
        _record_gpu(self.run_pk, gpu)
        self._detail("waiting for the pod to start")

        started = time.time()
        while True:
            self._check_stop()
            p = self.client.get_pod(pod["id"])
            if p is None or p.get("status") in ("TERMINATED", "ERROR"):
                raise RuntimeError(f"Pod went {p.get('status') if p else 'missing'} while starting")
            direct = (p.get("ssh") or {}).get("direct")
            if p.get("status") == "RUNNING" and direct:
                remote_store.update(self.run_pk, ssh_host=direct.get("ip") or direct.get("host"),
                                    ssh_port=int(direct["port"]))
                self.transport = make_transport(remote_store.get(self.run_pk))
                r = self.transport.run("test -f /bk/ready", timeout=30)
                if r.ok:
                    break
            if time.time() - started > PROVISION_TIMEOUT:
                raise RuntimeError(f"Pod didn't become reachable in {PROVISION_TIMEOUT // 60} minutes")
            self._sleep(5)
        self._log(f"Pod reachable after {time.time() - started:.0f}s")

    def _workspace_excludes(self):
        ctx = self.ctx
        ex = ["/venv/", "__pycache__/"]
        for rel in [ctx["tb_rel"], *ctx["output_paths"]]:
            ex.append("/" + rel.strip("/"))
        if ctx.get("data_dir_local") and ctx.get("data_dir"):
            ex.append("/" + ctx["data_dir_local"].strip("/"))
        return ex

    def _push(self, label, local, remote, excludes=None, skip_top=()):
        """skip_top: top-level folders left out of the upload (and of its size)."""
        self._check_stop()
        size = _dir_size(local, skip_top)
        excludes = [*(excludes or []), *(f"/{d}/" for d in skip_top)]
        self._detail(f"uploading {label} ({_fmt_gb(size)})")
        self._log(f"Uploading {label}: {_fmt_gb(size)}")
        t = time.time()
        r = self.transport.push(local, remote, excludes)
        if not r.ok:
            raise RuntimeError(f"Upload of {label} failed: {r.stderr.strip()[-400:]}")
        self._log(f"Uploaded {label} in {time.time() - t:.0f}s")

    def _upload(self):
        ctx = self.ctx
        self._phase("upload")
        self._push("workspace", ctx["workspace_dir"], "/bk/workspace", self._workspace_excludes())
        inputs = ctx["plan"].get("inputs", {})
        targets = input_targets(inputs)
        for key in sorted(inputs):
            if targets[key] == f"/bk/inputs/{key}":   # first key for this folder does the upload
                self._push(f"pipeline input {key}", inputs[key], targets[key],
                           skip_top=_input_skip(inputs, inputs[key]))
        if ctx.get("inherited"):
            # The seeded run folder: .beekeeper/ and the inherited paths, at the same relative paths.
            self._push("run folder (inherited paths)", ctx["persistent_run_dir"], "/bk/run")
        if ctx.get("data_dir"):
            self._push("data dir", ctx["data_dir"], "/bk/data")

    def _prepare(self):
        ctx = self.ctx
        self._phase("prepare", "setup script and pip install")
        lines = ["set -e", "cd /bk/workspace"]

        def link(rel, target):
            rel_q = shlex.quote(rel)
            lines.append(f"mkdir -p {shlex.quote(target)} \"$(dirname {rel_q})\" && rm -rf {rel_q} "
                         f"&& ln -s {shlex.quote(target)} {rel_q}")

        link(ctx["tb_rel"], "/bk/run")
        for rel in ctx["output_paths"]:
            link(rel, f"/bk/run/{rel}")
        if ctx.get("data_dir"):
            link(ctx["data_dir_local"], "/bk/data")
            lines.append('[ -n "$(ls -A /bk/data)" ] || { echo "Data dir is empty on the node"; exit 3; }')
        setup = ctx.get("setup_script")
        if setup:
            lines.append(f"if [ -f {shlex.quote(setup)} ]; then echo '[beekeeper] Running setup script'; "
                         f"bash {shlex.quote(setup)}; fi")
        req = shlex.quote(ctx.get("requirements_file") or "requirements.txt")
        lines += [
            f"if [ -f {req} ]; then",
            "  python3 -m venv --system-site-packages /bk/venv",
            f"  /bk/venv/bin/python -m pip install -q --disable-pip-version-check -r {req}",
            "  echo /bk/venv/bin/python > /bk/python",
            "else echo python3 > /bk/python; fi",
            'echo "[beekeeper] Python: $($(cat /bk/python) --version 2>&1)"',
            "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/[beekeeper] GPU: /' || true",
        ]
        t = time.time()
        r = self.transport.run("\n".join(lines) + "\n", timeout=3600)
        self._append_output(r.stdout + r.stderr)
        if not r.ok:
            raise RuntimeError(f"Prepare failed on the node (exit {r.returncode})")
        self._log(f"Prepare finished in {time.time() - t:.0f}s")

    def _append_output(self, text: str):
        if not text:
            return
        try:
            with open(self.ctx["log_path"], "a") as f:
                f.write(text if text.endswith("\n") else text + "\n")
        except OSError:
            pass

    def _write_env(self):
        env = dict(self.ctx["env"])
        env.update(input_targets(self.ctx["plan"].get("inputs", {})))
        env.update({
            "BEEKEEPER_RUN_DIR": "/bk/run", "BEEKEEPER_TENSORBOARD_DIR": "/bk/run",
            "TENSORBOARD_LOG_DIR": "/bk/run", "PYTHONUNBUFFERED": "1",
            "BEEKEEPER_RUN_ID": str(self.ctx["run_id"]),
            "BEEKEEPER_RUN_REF": run_ref(self.ctx["project_name"], self.ctx["run_id"]),
        })
        body = "".join(f"export {k}={shlex.quote(str(v))}\n" for k, v in sorted(env.items())
                       if k.replace("_", "").isalnum())
        r = self.transport.run(f"cat > /bk/env.sh <<'BK_ENV_EOF'\n{body}BK_ENV_EOF\n", timeout=60)
        if not r.ok:
            raise RuntimeError(f"Could not write run env: {r.stderr.strip()}")

    def _train(self) -> int:
        self._check_stop()
        self._write_env()
        train = shlex.quote(self.ctx["train_file"])
        # setsid: the training gets its own process group, so a stop can signal all of it.
        script = (
            # Separate lines, not "cd && ... &": that would background a subshell holding our stdout open.
            "cd /bk/workspace\nrm -f /bk/exit_code\n"
            "setsid nohup bash -c '. /bk/env.sh; $(cat /bk/python) -u " + train.replace("'", "'\\''") +
            "; echo $? > /bk/exit_code' > /bk/train.log 2>&1 < /dev/null &\n"
            "echo $! > /bk/train.pid\n"
        )
        r = self.transport.run(script, timeout=60)
        if not r.ok:
            raise RuntimeError(f"Could not start training: {r.stderr.strip()}")
        self._phase("train", "training")
        self._log("Training started on the pod")
        return self._monitor()

    def _pull_log(self) -> bool:
        cmd = (f"tail -c +{self._log_offset + 1} /bk/train.log 2>/dev/null | head -c 4000000 | base64 -w0; "
               "echo; cat /bk/exit_code 2>/dev/null || true\n")
        r = self.transport.run(cmd, timeout=60)
        if not r.ok:
            return False
        first, _, rest = r.stdout.partition("\n")
        chunk = base64.b64decode(first) if first.strip() else b""
        if chunk:
            with open(self.ctx["log_path"], "ab") as f:
                f.write(chunk)
            self._log_offset += len(chunk)
            remote_store.update(self.run_pk, log_offset=self._log_offset)
        code = rest.strip()
        if code and len(chunk) < 4000000:
            try:
                self._exit_code = int(code.splitlines()[0])
            except ValueError:
                self._exit_code = 1
        return True

    def _sync(self, timeout=3600) -> bool:
        r = self.transport.pull("/bk/run", self.ctx["persistent_run_dir"], timeout=timeout)
        if r.ok:
            remote_store.update(self.run_pk, last_sync_at=time.time())
        else:
            self._log(f"WARNING: result sync failed: {r.stderr.strip()[-300:]}")
        return r.ok

    def _monitor(self) -> int:
        interval = self.settings.sync_interval_min * 60
        last_sync = time.time()
        failures = 0
        row = remote_store.get(self.run_pk)
        while True:
            if self._pull_log():
                failures = 0
            else:
                failures += 1
                if failures >= 5:
                    pod = self.client.get_pod(row["pod_id"]) if row.get("pod_id") else None
                    if pod is None or pod.get("status") in ("TERMINATED", "EXITED", "ERROR"):
                        raise RuntimeError("Lost the pod (it is gone or stopped)")
            if self._exit_code is not None:
                self._log(f"Training exited with code {self._exit_code}")
                return self._exit_code
            if row.get("deadline_at") and time.time() > row["deadline_at"]:
                raise _Stop(-15, f"max runtime ({self.settings.max_runtime_hours:g} h) reached")
            if time.time() - last_sync >= interval:
                self._detail("syncing results")
                self._sync()
                self._detail("training")
                last_sync = time.time()
            self._sleep(POLL_INTERVAL)

    def _kill_training(self):
        if self.transport is None:
            return
        self.transport.run(
            "pid=$(cat /bk/train.pid 2>/dev/null) || exit 0\n"
            "kill -TERM -- -$pid 2>/dev/null || exit 0\n"
            "for i in 1 2 3 4 5; do kill -0 -- -$pid 2>/dev/null || exit 0; sleep 1; done\n"
            "kill -KILL -- -$pid 2>/dev/null || true\n", timeout=30)
        self._pull_log()

    def _manifest_missing(self) -> list[str]:
        r = self.transport.run("cd /bk/run && find . -type f -printf '%P %s\\n'\n", timeout=120)
        if not r.ok:
            return ["(could not list files on the pod)"]
        missing = []
        base = self.ctx["persistent_run_dir"]
        for line in r.stdout.splitlines():
            rel, _, size = line.rpartition(" ")
            path = os.path.join(base, rel)
            if not os.path.isfile(path) or str(os.path.getsize(path)) != size:
                missing.append(rel)
        return missing

    def _finalize(self, code: int) -> int:
        row = remote_store.get(self.run_pk) or {}
        if self.transport is not None:
            try:
                self._phase("finalize", "final sync")
                self._pull_log()
                timeout = HARD_STOP_SYNC_TIMEOUT if self._hard_stop else 3600
                missing = []
                for attempt in range(1 if self._hard_stop else 3):
                    self._sync(timeout=timeout)
                    missing = self._manifest_missing()
                    if not missing:
                        break
                    time.sleep(5 * (attempt + 1))
                if missing:
                    self._log(f"WARNING: {len(missing)} file(s) did not come back from the pod, e.g. {missing[:3]}")
                    if code == 0:
                        code = 1
                else:
                    self._log("Results synced and verified")
                self.transport.run("touch /bk/release\n", timeout=30)
            except Exception as e:  # noqa: BLE001
                self._log(f"WARNING: final sync failed: {e}")
                if code == 0:
                    code = 1
        if row.get("pod_id") and self.client is not None:
            try:
                self.client.terminate(row["pod_id"])
                self._log(f"Pod {row['pod_id']} terminated")
            except RunpodError as e:
                self._log(f"WARNING: could not terminate pod {row['pod_id']}: {e}. The orphan sweep will retry.")
        _accrue_row(remote_store.get(self.run_pk) or {}, time.time())
        spent = remote_store.spent_for_run(self.run_pk)
        self._log(f"Runpod cost for this run: ${spent:.4f} (Beekeeper's estimate)")
        remote_store.update(self.run_pk, phase=remote_store.DONE, phase_detail=None)
        key_path = row.get("key_path")
        for p in (key_path, f"{key_path}.pub") if key_path else ():
            try:
                os.unlink(p)
            except OSError:
                pass
        return code

    # restart recovery ------------------------------------------------------
    def _resume(self) -> int:
        row = self._resume_row
        self._log("Beekeeper restarted; reattaching to the remote run")
        pod = self.client.get_pod(row["pod_id"]) if (row.get("pod_id") and self.client) else None
        if pod is None or pod.get("status") in ("TERMINATED", "EXITED", "ERROR"):
            raise RuntimeError("Pod was lost while Beekeeper was down; keeping whatever was synced")
        self.transport = make_transport(row)
        if row["phase"] not in ("train", "finalize"):
            raise RuntimeError(f"Interrupted by restart during {row['phase']}")
        self._log_offset = row.get("log_offset") or 0
        if row["phase"] == "finalize":
            r = self.transport.run("cat /bk/exit_code 2>/dev/null\n", timeout=30)
            return int(r.stdout.strip() or 1)
        return self._monitor()


def _record_gpu(run_pk, gpu):
    import json
    from services.db_service import get_db
    get_db().update_training_run(run_pk, gpu_info=json.dumps([f"{gpu['id']} ({gpu['memory']} GB, Runpod)"]),
                                 hostname="runpod")


def launch(ctx: dict) -> RemoteProcess:
    """Create the remote_runs row and start the lifecycle thread."""
    remote_store.create(
        ctx["run_pk"], ctx["project_name"], ctx["run_id"], PROVIDER,
        pod_name=f"bk-{runpod_config.instance_id()}-{ctx['project_name']}-{ctx['run_id']}"[:60],
        phase="provision", context={k: v for k, v in ctx.items() if k != "env"},
    )
    return RemoteProcess(ctx)


def live_processes() -> list[RemoteProcess]:
    with _live_lock:
        return list(_live.values())


# ---------------------------------------------------------------------------
# Housekeeping (called from the dispatcher tick)
# ---------------------------------------------------------------------------

def _accrue_row(row: dict, now: float):
    with _accrual_lock:
        fresh = remote_store.get(row.get("run_pk")) if row.get("run_pk") is not None else None
        if not fresh or not fresh.get("cost_per_hr") or not fresh.get("last_accrual_at"):
            return
        if fresh["phase"] not in _BILLING_PHASES:
            return
        spend_guard.accrue(fresh["run_pk"], fresh["cost_per_hr"], fresh["last_accrual_at"], now)
        remote_store.update(fresh["run_pk"], last_accrual_at=now)


def tick(now: float | None = None):
    if not runpod_config.enabled():
        return
    now = now if now is not None else time.time()
    for row in remote_store.active():
        _accrue_row(row, now)
    breach = spend_guard.hard_cap_breach()
    if breach:
        for rp in live_processes():
            if not rp._hard_stop:
                log.warning("Runpod hard cap (%s) reached; stopping run %s", breach, rp.run_pk)
                rp.request_stop(f"hard cap reached ({breach})", hard=True)
    _maybe_sweep(now)


def _maybe_sweep(now):
    global _last_sweep
    if now - _last_sweep < ORPHAN_SWEEP_INTERVAL:
        return
    _last_sweep = now
    try:
        sweep_orphans()
    except Exception:
        log.exception("Runpod orphan sweep failed")


def sweep_orphans() -> list[str]:
    """Terminate pods named for this Beekeeper that no unfinished run owns. Other pods are never touched."""
    client = get_client()
    if client is None:
        return []
    prefix = f"bk-{runpod_config.instance_id()}-"
    owned = {r["pod_id"] for r in remote_store.active() if r.get("pod_id")}
    owned_names = {r["pod_name"] for r in remote_store.active()}
    killed = []
    for pod in client.list_pods():
        name = pod.get("name", "")
        if not name.startswith(prefix) or pod["id"] in owned or name in owned_names:
            continue
        if _age_seconds(pod) < ORPHAN_MIN_AGE:
            continue
        log.warning("Terminating orphaned Runpod pod %s (%s)", pod["id"], name)
        client.terminate(pod["id"])
        killed.append(pod["id"])
    return killed


def _age_seconds(pod: dict) -> float:
    """Seconds since the pod was created; 0 (too young to sweep) if Runpod didn't say."""
    import datetime
    created = pod.get("createdAt")
    if not created:
        return 0.0
    try:
        ts = datetime.datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
    return time.time() - ts


def terminate_by_name(pod_name: str) -> bool:
    """Admin 'Terminate' button: stop the run that owns the pod, or kill a stray pod outright."""
    row = remote_store.by_pod_name(pod_name)
    if row:
        with _live_lock:
            rp = _live.get(row["run_pk"])
        if rp:
            rp.request_stop("terminated from the Nodes page", hard=True)
            return True
    client = get_client()
    if client is None:
        return False
    for pod in client.list_pods():
        if pod.get("name") == pod_name:
            client.terminate(pod["id"])
            return True
    return False


def recover(projects_dir: str, adopt) -> int:
    """At startup: reattach to unfinished remote runs. adopt(row, RemoteProcess) registers it with process_manager."""
    if not runpod_config.enabled():
        return 0
    count = 0
    for row in remote_store.active():
        ctx = dict(row.get("context") or {})
        if not ctx.get("log_path"):
            remote_store.update(row["run_pk"], phase=remote_store.DONE, stop_reason="lost (no context)")
            continue
        ctx["env"] = {}
        try:
            proc = RemoteProcess(ctx, resume_row=row)
            adopt(row, ctx, proc)
            count += 1
        except Exception:
            log.exception("Could not recover remote run %s", row["run_pk"])
    try:
        sweep_orphans()
    except Exception:
        log.exception("Startup orphan sweep failed")
    return count


# ---------------------------------------------------------------------------
# Status for the Nodes page / API
# ---------------------------------------------------------------------------

def status() -> dict:
    settings = runpod_config.load()
    pods = []
    now = time.time()
    for row in remote_store.active():
        pods.append({
            "pod_name": row["pod_name"], "pod_id": row.get("pod_id"),
            "project": row["project_name"], "run_number": row["run_number"],
            "run_ref": f"{row['project_name']}-{row['run_number']}",
            "gpu_type": row.get("gpu_type"), "image": row.get("image"),
            "cost_per_hr": row.get("cost_per_hr"), "phase": row["phase"],
            "phase_detail": row.get("phase_detail"),
            "uptime_s": (now - row["created_at"]) if row.get("created_at") else None,
            "last_sync_at": row.get("last_sync_at"),
            "spent": round(remote_store.spent_for_run(row["run_pk"]), 4),
        })
    return {
        "enabled": settings.enabled,
        "settings": settings.public(),
        "spend": spend_guard.spend_summary(settings),
        "burn_rate": round(sum(p["cost_per_hr"] or 0 for p in pods if p["pod_id"]), 4),
        "pods": pods,
        "daily": remote_store.daily_spend(PROVIDER, now - 30 * 86400),
        "recent": [
            {"run_ref": f"{r['project_name']}-{r['run_number']}", "gpu_type": r.get("gpu_type"),
             "stop_reason": r.get("stop_reason"), "spent": round(remote_store.spent_for_run(r["run_pk"]), 4),
             "created_at": r.get("created_at")}
            for r in remote_store.recent(10) if r["phase"] == remote_store.DONE
        ],
    }

