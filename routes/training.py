import os
import time
from flask import Blueprint, current_app, jsonify, request, Response, send_file

from services.process_manager import start_training, stop_training, get_training_status

training_bp = Blueprint("training", __name__, url_prefix="/projects")


@training_bp.route("/<name>/start", methods=["POST"])
def start(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    config_path = os.path.join(projects_dir, name, "project.json")
    if not os.path.isfile(config_path):
        return jsonify({"error": "Project not found"}), 404

    result = start_training(projects_dir, name)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@training_bp.route("/<name>/stop", methods=["POST"])
def stop(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    result = stop_training(projects_dir, name)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@training_bp.route("/<name>/status")
def status(name):
    return jsonify(get_training_status(name))


def _tail_offset(filepath, lines):
    """Find the byte offset to start reading the last N lines of a file."""
    try:
        size = os.path.getsize(filepath)
    except OSError:
        return 0
    if size == 0:
        return 0

    buf_size = 8192
    found = 0
    offset = size

    with open(filepath, "rb") as f:
        while offset > 0 and found <= lines:
            read_size = min(buf_size, offset)
            offset -= read_size
            f.seek(offset)
            chunk = f.read(read_size)
            found += chunk.count(b"\n")

        # If we found enough lines, seek forward to the right start
        if found > lines:
            f.seek(offset)
            data = f.read()
            idx = 0
            skip = found - lines
            for _ in range(skip):
                idx = data.index(b"\n", idx) + 1
            return offset + idx

    return offset


@training_bp.route("/<name>/logs/stream")
def logs_stream(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    log_path = os.path.join(projects_dir, name, "train.log")

    # ?tail=N sends only the last N lines first, then streams new ones
    tail = request.args.get("tail", type=int)

    def generate():
        if tail and os.path.isfile(log_path):
            offset = _tail_offset(log_path, tail)
        else:
            offset = 0

        retries_without_data = 0
        max_idle = 300  # stop after 5 min of no data and no running process

        while True:
            try:
                if os.path.isfile(log_path):
                    size = os.path.getsize(log_path)
                    if size < offset:
                        # Log file was truncated/rewritten (new run)
                        offset = 0
                    if size > offset:
                        with open(log_path, "r") as f:
                            f.seek(offset)
                            chunk = f.read()
                            offset = f.tell()
                        if chunk:
                            for line in chunk.splitlines(True):
                                yield f"data: {line.rstrip()}\n\n"
                            retries_without_data = 0
                            continue

                retries_without_data += 1
                info = get_training_status(name)
                if info["status"] != "running" and retries_without_data > 2:
                    yield "data: \n\nevent: done\ndata: finished\n\n"
                    return
                if retries_without_data > max_idle:
                    return

            except Exception:
                pass

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@training_bp.route("/<name>/logs/download")
def logs_download(name):
    projects_dir = current_app.config["PROJECTS_DIR"]
    log_path = os.path.join(projects_dir, name, "train.log")
    if not os.path.isfile(log_path):
        return jsonify({"error": "No log file found"}), 404
    return send_file(log_path, as_attachment=True,
                     download_name=f"{name}-train.log")
