import psutil

try:
    import nvitop
    _HAS_NVITOP = True
except Exception:
    _HAS_NVITOP = False


def get_gpu_stats():
    """Return list of GPU stat dicts, one per device."""
    if not _HAS_NVITOP:
        return []

    gpus = []
    for dev in nvitop.Device.all():
        gpus.append({
            "index": dev.index,
            "name": dev.name(),
            "gpu_util": dev.gpu_utilization(),
            "mem_used": dev.memory_used(),
            "mem_total": dev.memory_total(),
            "mem_used_h": dev.memory_used_human(),
            "mem_total_h": dev.memory_total_human(),
            "mem_percent": round(dev.memory_used() / dev.memory_total() * 100, 1) if dev.memory_total() else 0,
            "temp": dev.temperature(),
            "fan": dev.fan_speed(),
            "power": round(dev.power_usage() / 1000, 1),
            "power_limit": round(dev.power_limit() / 1000, 1),
        })
    return gpus


def get_cpu_stats():
    """Return CPU usage info."""
    return {
        "percent": psutil.cpu_percent(interval=0),
        "count": psutil.cpu_count(),
        "freq": round(psutil.cpu_freq().current, 0) if psutil.cpu_freq() else None,
    }


def get_memory_stats():
    """Return system RAM info."""
    mem = psutil.virtual_memory()
    return {
        "percent": mem.percent,
        "used_gb": round(mem.used / (1024 ** 3), 1),
        "total_gb": round(mem.total / (1024 ** 3), 1),
    }


def get_all_stats():
    """Single call to get everything."""
    return {
        "gpus": get_gpu_stats(),
        "cpu": get_cpu_stats(),
        "memory": get_memory_stats(),
    }
