import io
import os
import zipfile
from flask import Blueprint, current_app, jsonify, request, send_file, abort

files_bp = Blueprint("files", __name__, url_prefix="/projects")


def _fmt_size(size):
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _safe_path(projects_dir, name, subpath):
    """Resolve and validate a path inside projects/<name>/src/."""
    src_dir = os.path.realpath(os.path.join(projects_dir, name, "src"))
    if subpath:
        target = os.path.realpath(os.path.join(src_dir, subpath))
    else:
        target = src_dir
    # Prevent path traversal
    if not target.startswith(src_dir):
        return None, None
    return src_dir, target


@files_bp.route("/<name>/files/")
@files_bp.route("/<name>/files/<path:subpath>")
def browse(name, subpath=""):
    projects_dir = current_app.config["PROJECTS_DIR"]
    src_dir, target = _safe_path(projects_dir, name, subpath)
    if target is None:
        abort(403)

    if not os.path.exists(target):
        abort(404)

    # Download a file directly
    if os.path.isfile(target):
        return send_file(target, as_attachment=True)

    # Zip download for a directory
    if request.args.get("zip") == "1":
        return _zip_directory(target, subpath or name)

    # List directory contents
    entries = []
    try:
        items = sorted(os.listdir(target))
    except PermissionError:
        abort(403)

    for item in items:
        # Skip hidden files and __pycache__
        if item.startswith(".") or item == "__pycache__":
            continue
        full = os.path.join(target, item)
        rel = os.path.join(subpath, item) if subpath else item
        if os.path.isdir(full):
            entries.append({
                "name": item,
                "type": "dir",
                "path": rel,
                "size": None,
                "size_h": None,
            })
        else:
            sz = os.path.getsize(full)
            entries.append({
                "name": item,
                "type": "file",
                "path": rel,
                "size": sz,
                "size_h": _fmt_size(sz),
            })

    # Sort: dirs first, then files
    entries.sort(key=lambda e: (0 if e["type"] == "dir" else 1, e["name"].lower()))

    base_url = f"/projects/{name}/files"

    # Build curl examples for the response
    host = request.host
    curl_file = f"curl -O http://{host}{base_url}/<filepath>"
    curl_zip = f"curl -o {subpath or 'src'}.zip 'http://{host}{base_url}/{subpath}?zip=1'"

    return jsonify({
        "project": name,
        "path": subpath or "",
        "entries": entries,
        "base_url": base_url,
        "curl_examples": {
            "download_file": curl_file,
            "download_dir_zip": curl_zip,
        },
    })


def _zip_directory(dir_path, zip_name):
    """Stream a directory as a zip file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(dir_path):
            # Skip hidden dirs and __pycache__
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.startswith("."):
                    continue
                full = os.path.join(root, f)
                arcname = os.path.relpath(full, dir_path)
                zf.write(full, arcname)
    buf.seek(0)

    safe_name = zip_name.replace("/", "-").replace("\\", "-")
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_name}.zip",
    )
