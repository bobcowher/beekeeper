"""Runpod provider settings: config.properties keys, the API key file, the instance id.

Everything Runpod-related in the UI and API is hidden unless enabled() is true.
"""
import os
from dataclasses import dataclass

from services.config_service import get_config, get_config_bool, save_config, set_config

PREFIX = "compute.runpod."
DEFAULT_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
CLOUD_TYPES = ("COMMUNITY", "SECURE")
CAP_PERIODS = ("daily", "monthly", "total")

# (key, default) for the numeric limits; floats so caps can be cents.
LIMITS = {
    "max_concurrent_pods": 2,
    "max_runtime_hours": 24.0,
    "max_upload_gb": 20.0,
    "sync_interval_min": 10.0,
    "hold_for_sync_hours": 2.0,
    "container_disk_gb": 50,
}

_home = None


def init(beekeeper_home: str):
    global _home
    _home = beekeeper_home


def _key_path() -> str:
    return os.path.join(_home or "", "secrets", "runpod_api_key")


def get_api_key() -> str:
    try:
        with open(_key_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def set_api_key(value: str):
    path = _key_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value.strip())


def enabled() -> bool:
    try:
        return get_config_bool(PREFIX + "enabled", False)
    except RuntimeError:
        return False


def instance_id() -> str:
    """Id stamped into pod names so two Beekeepers on one account never touch each other's pods."""
    from services import remote_store
    return remote_store.instance_id()


def _float(key, default):
    try:
        return float(get_config(PREFIX + key, default))
    except (TypeError, ValueError):
        return default


def cap(period: str, kind: str) -> float | None:
    raw = get_config(PREFIX + f"cap.{period}.{kind}")
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_gpu_list(value) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = (value or "").replace(",", "\n").splitlines()
    return [s.strip() for s in items if isinstance(s, str) and s.strip()]


@dataclass
class Settings:
    enabled: bool
    has_key: bool
    default_image: str
    cloud_type: str
    gpu_types: list
    caps: dict           # {"daily": {"soft": x|None, "hard": y|None}, ...}
    max_concurrent_pods: int
    max_runtime_hours: float
    max_upload_gb: float
    sync_interval_min: float
    hold_for_sync_hours: float
    container_disk_gb: int
    total_reset_at: float

    def has_hard_cap(self) -> bool:
        return any(self.caps[p]["hard"] is not None for p in CAP_PERIODS)

    def public(self) -> dict:
        d = dict(self.__dict__)
        d.pop("has_key")
        d["api_key_set"] = self.has_key
        return d


def load() -> Settings:
    cloud = (get_config(PREFIX + "cloud_type", "COMMUNITY") or "COMMUNITY").upper()
    return Settings(
        enabled=enabled(),
        has_key=bool(get_api_key()),
        default_image=get_config(PREFIX + "default_image", DEFAULT_IMAGE) or DEFAULT_IMAGE,
        cloud_type=cloud if cloud in CLOUD_TYPES else "COMMUNITY",
        gpu_types=parse_gpu_list(get_config(PREFIX + "gpu_types", "")),
        caps={p: {"soft": cap(p, "soft"), "hard": cap(p, "hard")} for p in CAP_PERIODS},
        max_concurrent_pods=max(1, int(_float("max_concurrent_pods", 2))),
        max_runtime_hours=max(0.05, _float("max_runtime_hours", 24.0)),
        max_upload_gb=max(0.0, _float("max_upload_gb", 20.0)),
        sync_interval_min=max(0.25, _float("sync_interval_min", 10.0)),
        hold_for_sync_hours=max(0.05, _float("hold_for_sync_hours", 2.0)),
        container_disk_gb=max(10, int(_float("container_disk_gb", 50))),
        total_reset_at=_float("total_reset_at", 0.0),
    )


def save(values: dict):
    """Persist provider settings. values uses Settings field names; caps as cap_<period>_<kind>."""
    if "enabled" in values:
        set_config(PREFIX + "enabled", "true" if values["enabled"] else "false")
    for key in ("default_image", "cloud_type"):
        if key in values:
            set_config(PREFIX + key, str(values[key]).strip())
    if "gpu_types" in values:
        set_config(PREFIX + "gpu_types", ",".join(parse_gpu_list(values["gpu_types"])))
    for key in LIMITS:
        if key in values:
            set_config(PREFIX + key, str(values[key]))
    for period in CAP_PERIODS:
        for kind in ("soft", "hard"):
            k = f"cap_{period}_{kind}"
            if k in values:
                v = values[k]
                set_config(PREFIX + f"cap.{period}.{kind}", "" if v in (None, "") else str(float(v)))
    save_config()


def reset_total(now: float):
    set_config(PREFIX + "total_reset_at", str(now))
    save_config()
