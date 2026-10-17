"""SSH + rsync to a node. Knows nothing about Runpod; a persistent node would reuse it as is.

Node-side paths are absolute under /bk. LocalTransport maps /bk onto a local
folder and runs commands with bash, which the lifecycle tests use as a fake pod.
"""
import os
import re
import shlex
import subprocess

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=15",
    "-o", "BatchMode=yes",
]


class Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    @property
    def ok(self):
        return self.returncode == 0


def _run(args, timeout, input_text=None):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_text)
        return Result(r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout}s")
    except OSError as e:
        return Result(127, "", str(e))


class SshTransport:
    def __init__(self, host: str, port: int, key_path: str, user: str = "root"):
        self.host, self.port, self.key_path, self.user = host, int(port), key_path, user

    def _ssh_cmd(self):
        return ["ssh", "-i", self.key_path, "-p", str(self.port), *_SSH_OPTS, f"{self.user}@{self.host}"]

    def run(self, command: str, timeout: int = 120) -> Result:
        """Run a bash script on the node. The script goes over stdin, so no quoting games."""
        return _run(self._ssh_cmd() + ["bash -s"], timeout, input_text=command)

    def _rsync(self, src, dst, excludes, timeout):
        e = " ".join(shlex.quote(a) for a in ["ssh", "-i", self.key_path, "-p", str(self.port), *_SSH_OPTS])
        args = ["rsync", "-a", "--partial", "-e", e]
        for ex in excludes or []:
            args += ["--exclude", ex]
        return _run(args + [src, dst], timeout)

    def push(self, local_dir: str, remote_dir: str, excludes=None, timeout: int = 6 * 3600) -> Result:
        return self._rsync(local_dir.rstrip("/") + "/", f"{self.user}@{self.host}:{remote_dir.rstrip('/')}/",
                           excludes, timeout)

    def pull(self, remote_dir: str, local_dir: str, timeout: int = 3600) -> Result:
        os.makedirs(local_dir, exist_ok=True)
        return self._rsync(f"{self.user}@{self.host}:{remote_dir.rstrip('/')}/", local_dir.rstrip("/") + "/",
                           None, timeout)


class LocalTransport:
    """A local folder standing in for a node: /bk/... is rewritten to <root>/bk/..."""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(os.path.join(root, "bk"), exist_ok=True)

    def _map(self, text: str) -> str:
        return re.sub(r"(?<![\w/.])/bk(?=/|\b|$)", os.path.join(self.root, "bk"), text)

    def run(self, command: str, timeout: int = 120) -> Result:
        return _run(["bash", "-s"], timeout, input_text=self._map(command))

    def _rsync(self, src, dst, excludes, timeout):
        args = ["rsync", "-a"]
        for ex in excludes or []:
            args += ["--exclude", ex]
        return _run(args + [src, dst], timeout)

    def push(self, local_dir, remote_dir, excludes=None, timeout=600):
        dst = self._map(remote_dir.rstrip("/") + "/")
        os.makedirs(dst, exist_ok=True)
        return self._rsync(local_dir.rstrip("/") + "/", dst, excludes, timeout)

    def pull(self, remote_dir, local_dir, timeout=600):
        os.makedirs(local_dir, exist_ok=True)
        return self._rsync(self._map(remote_dir.rstrip("/") + "/"), local_dir.rstrip("/") + "/", None, timeout)
