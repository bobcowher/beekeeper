"""Compute providers (Runpod): admin settings, the Nodes page and its status feed."""
import time

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from services import remote_runs, runpod_config
from services.auth_service import admin_required
from services.runpod_client import RunpodClient, RunpodError

compute_bp = Blueprint("compute", __name__)

_ADMIN_ANCHOR = "admin.index"


def _admin_redirect():
    return redirect(url_for(_ADMIN_ANCHOR) + "#runpod")


def _parse_number(form_key, label, minimum=0.0, allow_blank=False):
    raw = (request.form.get(form_key) or "").strip()
    if raw == "" and allow_blank:
        return None, None
    try:
        value = float(raw)
    except ValueError:
        return None, f"{label} must be a number"
    if value < minimum:
        return None, f"{label} must be at least {minimum:g}"
    return value, None


@compute_bp.route("/admin/compute/runpod", methods=["POST"])
@admin_required
def save_runpod():
    values: dict = {"enabled": request.form.get("enabled") == "true"}
    errors = []
    for period in runpod_config.CAP_PERIODS:
        for kind in ("soft", "hard"):
            key = f"cap_{period}_{kind}"
            v, err = _parse_number(key, f"{period.title()} {kind} cap", 0, allow_blank=True)
            errors.append(err)
            values[key] = v
    limits = [
        ("max_concurrent_pods", "Max concurrent pods", 1),
        ("max_runtime_hours", "Max runtime per run", 0.05),
        ("max_upload_gb", "Max upload", 0),
        ("sync_interval_min", "Sync interval", 0.25),
        ("hold_for_sync_hours", "Hold for sync", 0.05),
        ("container_disk_gb", "Container disk", 10),
    ]
    for key, label, minimum in limits:
        v, err = _parse_number(key, label, minimum)
        errors.append(err)
        if v is not None:
            values[key] = int(v) if key in ("max_concurrent_pods", "container_disk_gb") else v
    for period in runpod_config.CAP_PERIODS:
        soft, hard = values[f"cap_{period}_soft"], values[f"cap_{period}_hard"]
        if soft is not None and hard is not None and soft > hard:
            errors.append(f"{period.title()} soft cap is above its hard cap")
    errors = [e for e in errors if e]
    if errors:
        for e in errors:
            flash(e, "error")
        return _admin_redirect()

    cloud = (request.form.get("cloud_type") or "COMMUNITY").upper()
    values["cloud_type"] = cloud if cloud in runpod_config.CLOUD_TYPES else "COMMUNITY"
    values["default_image"] = (request.form.get("default_image") or "").strip() or runpod_config.DEFAULT_IMAGE
    values["gpu_types"] = request.form.get("gpu_types", "")
    new_key = (request.form.get("api_key") or "").strip()
    if new_key:
        runpod_config.set_api_key(new_key)
    runpod_config.save(values)

    settings = runpod_config.load()
    if settings.enabled and not settings.has_key:
        flash("Runpod is enabled but has no API key yet. Runs will be refused until one is set.", "warning")
    elif settings.enabled and not settings.has_hard_cap():
        flash("No hard cap set. Runpod runs will be refused until one is.", "warning")
    else:
        flash("Runpod settings saved", "success")
    return _admin_redirect()


@compute_bp.route("/admin/compute/runpod/test", methods=["POST"])
@admin_required
def test_runpod():
    key = (request.form.get("api_key") or "").strip() or runpod_config.get_api_key()
    if not key:
        return jsonify({"ok": False, "error": "No API key entered or saved"}), 400
    cloud = (request.form.get("cloud_type") or runpod_config.load().cloud_type).upper()
    try:
        client = RunpodClient(key, timeout=20)
        gpus = client.gpu_types(cloud)
        pods = client.list_pods()
    except RunpodError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    price_key = cloud.lower()
    rows = sorted(
        ({"id": g["id"], "memory": g["memory"], "availability": g.get("availability"),
          "price": (g.get("price") or {}).get(price_key)}
         for g in gpus if (g.get("price") or {}).get(price_key)),
        key=lambda g: (g["availability"] == "NONE", g["price"]))
    return jsonify({"ok": True, "cloud": cloud, "gpus": rows, "pod_count": len(pods)})


@compute_bp.route("/admin/compute/runpod/reset-total", methods=["POST"])
@admin_required
def reset_runpod_total():
    runpod_config.reset_total(time.time())
    flash("Runpod total spend reset to $0", "success")
    return _admin_redirect()


def _require_enabled():
    if not runpod_config.enabled():
        abort(404)


@compute_bp.route("/nodes", methods=["GET"])
def nodes():
    _require_enabled()
    return render_template("nodes.html", initial=_status_payload())


def _status_payload():
    from services import process_manager
    from services.stats_service import get_gpu_stats
    with process_manager._lock:
        local_runs = sum(1 for i in process_manager._running.values() if not i.get("remote"))
    try:
        gpus = [g["name"] for g in get_gpu_stats() or []]
    except Exception:
        gpus = []
    data = remote_runs.status()
    data["local"] = {"gpus": gpus, "runs": local_runs}
    return data


@compute_bp.route("/nodes/status", methods=["GET"])
def nodes_status():
    _require_enabled()
    return jsonify(_status_payload())


@compute_bp.route("/nodes/pods/<pod_name>/terminate", methods=["POST"])
def terminate_pod(pod_name):
    _require_enabled()
    if not remote_runs.terminate_by_name(pod_name):
        return jsonify({"ok": False, "error": f"No pod named {pod_name}"}), 404
    current_app.logger.warning("Pod %s terminated from the Nodes page", pod_name)
    return jsonify({"ok": True})
