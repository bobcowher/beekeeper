"""SQLite access for remote runs and the spend ledger (tables created in db_service)."""
import json

from services.db_service import get_db

_FIELDS = {
    "pod_id", "gpu_type", "image", "cost_per_hr", "phase", "phase_detail", "ssh_host",
    "ssh_port", "key_path", "created_at", "deadline_at", "last_sync_at", "last_accrual_at",
    "stop_reason", "context", "log_offset",
}
DONE = "done"


def _row(row):
    if row is None:
        return None
    d = dict(row)
    if d.get("context"):
        try:
            d["context"] = json.loads(d["context"])
        except ValueError:
            d["context"] = {}
    return d


def create(run_pk: int, project_name: str, run_number: int, provider: str, pod_name: str, **fields):
    cols = {"run_pk": run_pk, "project_name": project_name, "run_number": run_number,
            "provider": provider, "pod_name": pod_name, "phase": fields.pop("phase", "admit")}
    for k, v in fields.items():
        if k not in _FIELDS:
            raise ValueError(f"unknown remote_runs field {k}")
        cols[k] = json.dumps(v) if k == "context" else v
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    with get_db()._get_connection() as conn:
        conn.execute(f"INSERT OR REPLACE INTO remote_runs ({names}) VALUES ({marks})", tuple(cols.values()))
        conn.commit()


def update(run_pk: int, **fields):
    if not fields:
        return
    for k in fields:
        if k not in _FIELDS:
            raise ValueError(f"unknown remote_runs field {k}")
    if "context" in fields:
        fields["context"] = json.dumps(fields["context"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_db()._get_connection() as conn:
        conn.execute(f"UPDATE remote_runs SET {sets} WHERE run_pk = ?", (*fields.values(), run_pk))
        conn.commit()


def get(run_pk: int) -> dict | None:
    with get_db()._get_connection() as conn:
        return _row(conn.execute("SELECT * FROM remote_runs WHERE run_pk = ?", (run_pk,)).fetchone())


def active() -> list[dict]:
    """Remote runs that are not finished (a pod may exist for each)."""
    with get_db()._get_connection() as conn:
        rows = conn.execute("SELECT * FROM remote_runs WHERE phase != ? ORDER BY run_pk", (DONE,)).fetchall()
    return [_row(r) for r in rows]


def recent(limit: int = 20) -> list[dict]:
    with get_db()._get_connection() as conn:
        rows = conn.execute("SELECT * FROM remote_runs ORDER BY run_pk DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


def by_pod_name(pod_name: str) -> dict | None:
    with get_db()._get_connection() as conn:
        return _row(conn.execute("SELECT * FROM remote_runs WHERE pod_name = ?", (pod_name,)).fetchone())


def instance_id() -> str:
    """Random id for this Beekeeper, kept in its database.

    Pod names carry it, and the orphan sweep only touches pods with it. Keeping it
    in the database (not config.properties) means a test or scratch database can
    never claim another Beekeeper's pods, even with the same config and API key.
    """
    import secrets
    with get_db()._get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO instance_meta (key, value) VALUES ('instance_id', ?)",
                     (secrets.token_hex(3),))
        conn.commit()
        return conn.execute("SELECT value FROM instance_meta WHERE key = 'instance_id'").fetchone()[0]


def add_spend(provider: str, run_pk: int | None, bucket: float, amount: float, source: str = "accrual"):
    """Add to one ledger row per (run, minute bucket, source)."""
    if amount <= 0:
        return
    with get_db()._get_connection() as conn:
        conn.execute(
            "INSERT INTO spend_ledger (provider, run_pk, bucket, amount_usd, source) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(provider, run_pk, bucket, source) DO UPDATE SET amount_usd = amount_usd + excluded.amount_usd",
            (provider, run_pk, bucket, amount, source))
        conn.commit()


def spent_since(provider: str, since: float) -> float:
    with get_db()._get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM spend_ledger WHERE provider = ? AND bucket >= ?",
            (provider, since)).fetchone()
    return float(row[0])


def spent_for_run(run_pk: int) -> float:
    with get_db()._get_connection() as conn:
        row = conn.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM spend_ledger WHERE run_pk = ?",
                           (run_pk,)).fetchone()
    return float(row[0])


def daily_spend(provider: str, since: float) -> list[tuple[str, float]]:
    """[(YYYY-MM-DD local, usd)] for days with spend since `since`."""
    with get_db()._get_connection() as conn:
        rows = conn.execute(
            "SELECT date(bucket, 'unixepoch', 'localtime') AS day, SUM(amount_usd) FROM spend_ledger "
            "WHERE provider = ? AND bucket >= ? GROUP BY day ORDER BY day", (provider, since)).fetchall()
    return [(r[0], float(r[1])) for r in rows]
