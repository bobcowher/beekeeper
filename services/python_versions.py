import shutil
import subprocess

CANDIDATES = ["3.13", "3.12", "3.11", "3.10", "3.9"]


def find_available():
    """Return list of (version, path) tuples for Python versions found on this system."""
    found = []
    for ver in CANDIDATES:
        path = shutil.which(f"python{ver}")
        if path:
            found.append((ver, path))

    # Fallback: if none of the specific versions found, try plain python3
    if not found:
        path = shutil.which("python3")
        if path:
            try:
                out = subprocess.run(
                    [path, "--version"], capture_output=True, text=True
                )
                # "Python 3.12.7" -> "3.12"
                ver = ".".join(out.stdout.strip().split()[1].split(".")[:2])
                found.append((ver, path))
            except Exception:
                pass

    return found


def find_python(version):
    """Find the best python binary for a requested version. Returns path or None."""
    # Try exact match first
    path = shutil.which(f"python{version}")
    if path:
        return path

    # Fall back to python3
    path = shutil.which("python3")
    if path:
        return path

    return None
