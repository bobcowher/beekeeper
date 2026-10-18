from flask import Blueprint, jsonify, render_template, request

from services import run_activity
from services.db_service import get_db

history_bp = Blueprint("history", __name__, url_prefix="/history")

PAGE_SIZE = 50
FILTERS = ("all", "pipeline", "training", "failed")


def _page(kind: str, before: str | None) -> dict:
    """One page of finished runs, newest first, and the cursor for the next page (None at the end)."""
    db = get_db()
    failed_only = kind == "failed"
    items = []
    if kind != "pipeline":
        items += [run_activity.finished_training_item(run)
                  for run in db.finished_training_runs(PAGE_SIZE + 1, before, failed_only)]
    if kind != "training":
        items += [run_activity.finished_pipeline_item(db, row)
                  for row in db.finished_pipeline_runs(PAGE_SIZE + 1, before, failed_only)]
    items.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    more = len(items) > PAGE_SIZE
    items = items[:PAGE_SIZE]
    return {"items": items, "next": items[-1]["timestamp"] if more and items else None}


def _filter_arg() -> str:
    kind = request.args.get("filter", "all")
    return kind if kind in FILTERS else "all"


@history_bp.route("", methods=["GET"])
def index():
    kind = _filter_arg()
    return render_template("history.html", page=_page(kind, None), filter=kind)


@history_bp.route("/data", methods=["GET"])
def data():
    return jsonify(_page(_filter_arg(), request.args.get("before") or None))
