from __future__ import annotations

from unittest.mock import MagicMock, patch

from job_sniffer.llm.hardware import GPUInfo, detect_gpu, format_gpu_summary


def test_detect_gpu_nvidia_mocked() -> None:
    mock_run = MagicMock()
    mock_run.returncode = 0
    mock_run.stdout = "NVIDIA GeForce RTX 4050 Laptop GPU, 6141\n"

    with (
        patch("shutil.which", return_value="/fake/nvidia-smi"),
        patch("subprocess.run", return_value=mock_run),
    ):
        info = detect_gpu()
        assert info.has_gpu is True
        assert "RTX 4050" in info.name
        assert info.vram_mb == 6141
        assert info.backend in ("vulkan", "cuda_13")
        summary = format_gpu_summary(info)
        assert "6.0 GB VRAM" in summary
        assert "akceleracja aktywna" in summary


def test_detect_gpu_fallback_cpu() -> None:
    with (
        patch("shutil.which", return_value=None),
        patch("ctypes.CDLL", side_effect=OSError("vulkan not found")),
    ):
        info = detect_gpu()
        assert info.has_gpu is False
        assert info.backend == "cpu"
        summary = format_gpu_summary(info)
        assert "Brak akceleracji GPU" in summary


def test_format_gpu_summary_without_vram() -> None:
    info = GPUInfo(has_gpu=True, name="Intel Arc GPU", vram_mb=None, backend="vulkan")
    summary = format_gpu_summary(info)
    assert "Intel Arc GPU" in summary
    assert "akceleracja aktywna" in summary
