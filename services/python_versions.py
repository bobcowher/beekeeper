import shutil
import subprocess
import json as _json

CANDIDATES = ["3.13", "3.12", "3.11", "3.10", "3.9"]


def has_conda():
    """Check if conda is available on this system."""
    return shutil.which("conda") is not None


def find_available():
    """Return list of dicts with version info for system and conda Pythons.

    Each entry: {"version": "3.12", "source": "system"|"conda", "path": ...}
    """
    found = []
    seen = set()

    # System pythons
    for ver in CANDIDATES:
        path = shutil.which(f"python{ver}")
        if path:
            found.append({"version": ver, "source": "system", "path": path})
            seen.add(ver)

    # Fallback: plain python3
    if not seen:
        path = shutil.which("python3")
        if path:
            try:
                out = subprocess.run(
                    [path, "--version"], capture_output=True, text=True
                )
                ver = ".".join(out.stdout.strip().split()[1].split(".")[:2])
                if ver not in seen:
                    found.append({"version": ver, "source": "system", "path": path})
                    seen.add(ver)
            except Exception:
                pass

    # Conda pythons
    if has_conda():
        try:
            out = subprocess.run(
                ["conda", "search", "python", "--json"],
                capture_output=True, text=True, timeout=30,
            )
            data = _json.loads(out.stdout)
            conda_versions = sorted(
                set(
                    ".".join(p["version"].split(".")[:2])
                    for p in data.get("python", [])
                ),
                key=lambda v: [int(x) for x in v.split(".")],
                reverse=True,
            )
            for ver in conda_versions:
                if ver not in seen and ver in CANDIDATES:
                    found.append({"version": ver, "source": "conda", "path": None})
                    seen.add(ver)
        except Exception:
            pass

    return found


def find_python(version):
    """Find the best python binary for a requested version. Returns path or None."""
    path = shutil.which(f"python{version}")
    if path:
        return path

    path = shutil.which("python3")
    if path:
        return path

    return None
