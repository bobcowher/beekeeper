from unittest.mock import MagicMock, patch

from services import stats_service


def _mock_device(driver_version="550.54.14", compute_capability=(8, 6)):
    dev = MagicMock()
    dev.index = 0
    dev.name.return_value = "Fake GPU"
    dev.gpu_utilization.return_value = 10
    dev.memory_used.return_value = 1000
    dev.memory_total.return_value = 2000
    dev.memory_used_human.return_value = "1000MiB"
    dev.memory_total_human.return_value = "2000MiB"
    dev.temperature.return_value = 40
    dev.fan_speed.return_value = 30
    dev.power_usage.return_value = 100000
    dev.power_limit.return_value = 300000
    dev.driver_version.return_value = driver_version
    dev.cuda_compute_capability.return_value = compute_capability
    return dev


def test_get_gpu_stats_includes_compute_capability():
    dev = _mock_device()
    with patch.object(stats_service, "_HAS_NVITOP", True), \
         patch.object(stats_service, "nvitop") as mock_nvitop:
        mock_nvitop.Device.all.return_value = [dev]
        gpus = stats_service.get_gpu_stats()

    assert gpus[0]["compute_capability"] == "8.6"


def test_get_gpu_stats_returns_empty_list_without_nvitop():
    with patch.object(stats_service, "_HAS_NVITOP", False):
        assert stats_service.get_gpu_stats() == []


def test_get_gpu_platform_info_reports_nvidia_driver_and_max_cuda():
    dev = _mock_device(driver_version="550.54.14")
    with patch.object(stats_service, "_HAS_NVITOP", True), \
         patch.object(stats_service, "nvitop") as mock_nvitop:
        mock_nvitop.Device.all.return_value = [dev]
        mock_nvitop.Device.max_cuda_version.return_value = "12.4"
        info = stats_service.get_gpu_platform_info()

    assert info == {
        "platform": "nvidia",
        "driver_version": "550.54.14",
        "max_cuda_version": "12.4",
        "rocm_version": None,
    }


def test_get_gpu_platform_info_falls_back_to_rocm_when_no_nvidia_devices(mocker):
    mocker.patch.object(stats_service, "_HAS_NVITOP", False)
    mocker.patch.object(stats_service, "_get_rocm_version", return_value="6.1.2")

    info = stats_service.get_gpu_platform_info()

    assert info == {
        "platform": "rocm",
        "driver_version": None,
        "max_cuda_version": None,
        "rocm_version": "6.1.2",
    }


def test_get_gpu_platform_info_returns_none_platform_when_nothing_detected(mocker):
    mocker.patch.object(stats_service, "_HAS_NVITOP", False)
    mocker.patch.object(stats_service, "_get_rocm_version", return_value=None)

    info = stats_service.get_gpu_platform_info()

    assert info == {
        "platform": "none",
        "driver_version": None,
        "max_cuda_version": None,
        "rocm_version": None,
    }


def test_get_rocm_version_returns_none_when_rocm_smi_missing(mocker):
    mocker.patch("shutil.which", return_value=None)
    assert stats_service._get_rocm_version() is None


def test_get_rocm_version_parses_driver_version_line(mocker):
    mocker.patch("shutil.which", return_value="/usr/bin/rocm-smi")
    fake_result = MagicMock(stdout="Driver version: 6.1.2\nOther line\n")
    mocker.patch("subprocess.run", return_value=fake_result)

    assert stats_service._get_rocm_version() == "6.1.2"
