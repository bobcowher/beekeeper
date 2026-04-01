"""Track per-process resource usage for training jobs."""

import subprocess
import logging

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

log = logging.getLogger(__name__)


def get_process_resources(pid):
    """
    Get resource usage for a specific process.

    Returns dict with:
        - cpu_percent: CPU usage percentage (0-100 per core, so can exceed 100)
        - memory_mb: Memory usage in MB (RSS)
        - gpu_memory_mb: GPU memory allocated to this process (if available)
        - gpu_id: GPU device ID (if using GPU)

    Returns None if process not found or psutil not available.
    """
    if not PSUTIL_AVAILABLE or not pid:
        return None

    try:
        proc = psutil.Process(pid)

        # Get CPU and memory
        # interval=0.1 for quick sampling (default interval=None blocks for 1 second)
        cpu_percent = proc.cpu_percent(interval=0.1)
        memory_info = proc.memory_info()
        memory_mb = memory_info.rss / (1024 * 1024)  # Convert to MB

        # Get GPU memory usage (if nvidia-smi available)
        gpu_info = _get_gpu_memory_for_pid(pid)

        return {
            "cpu_percent": round(cpu_percent, 1),
            "memory_mb": round(memory_mb, 1),
            "gpu_memory_mb": gpu_info.get("memory_mb") if gpu_info else None,
            "gpu_id": gpu_info.get("gpu_id") if gpu_info else None,
        }

    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception as e:
        log.warning(f"Error getting resources for PID {pid}: {e}")
        return None


def _get_gpu_memory_for_pid(pid):
    """
    Query nvidia-smi for GPU memory used by a specific PID.

    Returns dict with gpu_id and memory_mb, or None if not using GPU.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,gpu_uuid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
        )

        if result.returncode != 0:
            return None

        # Parse output - format: "pid, gpu_uuid, memory_mb"
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                proc_pid = int(parts[0])
                gpu_uuid = parts[1]
                memory_mb = int(parts[2])

                if proc_pid == pid:
                    # Get GPU ID from UUID
                    gpu_id = _get_gpu_id_from_uuid(gpu_uuid)
                    return {
                        "gpu_id": gpu_id,
                        "memory_mb": memory_mb,
                    }

        return None

    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        return None
    except Exception as e:
        log.debug(f"Error querying GPU for PID {pid}: {e}")
        return None


def _get_gpu_id_from_uuid(target_uuid):
    """Map GPU UUID to GPU ID (0, 1, 2, etc.)."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,gpu_uuid", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
        )

        if result.returncode != 0:
            return None

        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[1] == target_uuid:
                return int(parts[0])

        return None

    except Exception:
        return None
