from flask import Blueprint, jsonify

from services.stats_service import get_all_stats

stats_bp = Blueprint("stats", __name__, url_prefix="/api")


@stats_bp.route("/stats")
def stats():
    return jsonify(get_all_stats())
