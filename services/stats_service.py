import shutil
import subprocess

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
        try:
            major, minor = dev.cuda_compute_capability()
            compute_capability = f"{major}.{minor}"
        except Exception:
            compute_capability = None
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
            "compute_capability": compute_capability,
        })
    return gpus


def get_gpu_platform_info():
    """
    Host-wide compute platform info, shared across all GPUs (one driver per host).

    This is what an agent should check before picking a PyTorch/JAX build:
    max_cuda_version is the highest CUDA version the installed *driver* supports
    (what `nvidia-smi` shows in its header), not any CUDA toolkit version
    installed inside a project's venv — those can differ.

    ROCm detection is best-effort via `rocm-smi` and untested against real AMD
    hardware (none in this fleet) — treat rocm_version as unverified.
    """
    if _HAS_NVITOP:
        try:
            devices = nvitop.Device.all()
        except Exception:
            devices = []
        if devices:
            try:
                driver_version = devices[0].driver_version()
            except Exception:
                driver_version = None
            try:
                max_cuda_version = nvitop.Device.max_cuda_version()
            except Exception:
                max_cuda_version = None
            return {
                "platform": "nvidia",
                "driver_version": driver_version,
                "max_cuda_version": max_cuda_version,
                "rocm_version": None,
            }

    rocm_version = _get_rocm_version()
    if rocm_version:
        return {
            "platform": "rocm",
            "driver_version": None,
            "max_cuda_version": None,
            "rocm_version": rocm_version,
        }

    return {
        "platform": "none",
        "driver_version": None,
        "max_cuda_version": None,
        "rocm_version": None,
    }


def _get_rocm_version():
    """Best-effort ROCm driver version via `rocm-smi --showdriverversion`."""
    if not shutil.which("rocm-smi"):
        return None
    try:
        result = subprocess.run(
            ["rocm-smi", "--showdriverversion"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "Driver version" in line:
                return line.split(":", 1)[-1].strip()
    except Exception:
        pass
    return None


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
        "gpu_platform": get_gpu_platform_info(),
        "cpu": get_cpu_stats(),
        "memory": get_memory_stats(),
    }
