"""Spend tally and cap checks for the Runpod pool.

Beekeeper keeps its own tally (pod price x time alive) because Runpod's billing
data lags by minutes or more. Daily resets at local midnight, monthly on the
1st, total only when an admin resets it.
"""
import datetime
import time

from services import remote_store, runpod_config

PROVIDER = "runpod"


def period_start(period: str, now: float | None = None, total_reset_at: float = 0.0) -> float:
    now = now if now is not None else time.time()
    local = datetime.datetime.fromtimestamp(now)
    if period == "daily":
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "monthly":
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        return total_reset_at
    return start.timestamp()


def spend_summary(settings=None, now: float | None = None) -> dict:
    """{period: {"spent", "soft", "hard"}} for daily, monthly and total."""
    settings = settings or runpod_config.load()
    out = {}
    for period in runpod_config.CAP_PERIODS:
        since = period_start(period, now, settings.total_reset_at)
        out[period] = {
            "spent": round(remote_store.spent_since(PROVIDER, since), 4),
            "soft": settings.caps[period]["soft"],
            "hard": settings.caps[period]["hard"],
        }
    return out


def _period_label(period):
    return {"daily": "today", "monthly": "this month", "total": "total"}[period]


def launch_refusal(settings=None, now: float | None = None) -> dict | None:
    """None if a new pod may launch, else {"code", "error"}.

    No hard cap anywhere refuses permanently. A reached soft or hard cap
    refuses with SOFT_CAP, which the run queue treats as "wait and retry".
    """
    settings = settings or runpod_config.load()
    if not settings.has_hard_cap():
        return {"code": "NO_SPEND_CAP",
                "error": "Runpod has no hard spend cap set. Set one under Admin → Compute Providers."}
    summary = spend_summary(settings, now)
    for period, s in summary.items():
        for kind in ("hard", "soft"):
            limit = s[kind]
            if limit is not None and s["spent"] >= limit:
                return {"code": "SOFT_CAP",
                        "error": f"Runpod {kind} cap reached (${s['spent']:.2f} of ${limit:.2f} "
                                 f"{_period_label(period)})"}
    return None


def hard_cap_breach(settings=None, now: float | None = None) -> str | None:
    """Name of the first period whose hard cap is reached, or None."""
    settings = settings or runpod_config.load()
    for period, s in spend_summary(settings, now).items():
        if s["hard"] is not None and s["spent"] >= s["hard"]:
            return period
    return None


def accrue(run_pk: int, cost_per_hr: float, from_ts: float, to_ts: float):
    """Book cost for [from_ts, to_ts) into per-minute ledger buckets."""
    if not cost_per_hr or to_ts <= from_ts:
        return
    t = from_ts
    while t < to_ts:
        bucket = t - (t % 60)
        end = min(to_ts, bucket + 60)
        remote_store.add_spend(PROVIDER, run_pk, bucket, cost_per_hr * (end - t) / 3600)
        t = end
