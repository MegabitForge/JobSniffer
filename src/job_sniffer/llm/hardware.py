"""Hardware and GPU acceleration detection."""

import ctypes
import glob
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

BackendType = Literal["vulkan", "cuda_12", "cuda_13", "cpu"]


@dataclass(frozen=True)
class GPUInfo:
    """Information about detected GPU acceleration capabilities."""

    has_gpu: bool
    name: str
    vram_mb: int | None
    backend: BackendType


def detect_gpu() -> GPUInfo:
    """Detect presence of dedicated GPU and Vulkan runtime on the host system."""
    # 1. Try querying NVIDIA GPU via nvidia-smi
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                first_line = res.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first_line.split(",")]
                gpu_name = parts[0]
                vram_mb = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                backend_type = _detect_cuda_backend()

                return GPUInfo(
                    has_gpu=True,
                    name=gpu_name,
                    vram_mb=vram_mb,
                    backend=backend_type,
                )
        except (subprocess.SubprocessError, OSError, ValueError) as err:
            logger.debug("nvidia-smi query failed: %s", err)

    # 2. Check for the Vulkan loader (shipped with modern AMD/Intel/NVIDIA drivers)
    if _has_vulkan_loader():
        return GPUInfo(
            has_gpu=True,
            name="Zgodna karta graficzna (Vulkan GPU)",
            vram_mb=None,
            backend="vulkan",
        )

    # 3. Fallback to CPU
    return GPUInfo(
        has_gpu=False,
        name="Brak dedykowanej karty GPU (tryb CPU)",
        vram_mb=None,
        backend="cpu",
    )


def _has_vulkan_loader() -> bool:
    library = "vulkan-1.dll" if sys.platform == "win32" else "libvulkan.so.1"
    try:
        ctypes.CDLL(library)
    except OSError, AttributeError:
        return False
    return True


def _detect_cuda_backend() -> BackendType:
    """Pick the llama-server backend for an NVIDIA GPU based on the installed CUDA runtime."""
    # Prebuilt CUDA binaries of llama-server are published only for Windows;
    # on other platforms NVIDIA GPUs are driven through Vulkan.
    if sys.platform != "win32":
        return "vulkan"

    has_13 = False
    has_12 = False

    cuda_path = os.environ.get("CUDA_PATH", "")
    if cuda_path and os.path.exists(cuda_path):
        has_13 = bool(
            glob.glob(os.path.join(cuda_path, "bin", "**", "cudart64_13*.dll"), recursive=True)
        )
        has_12 = bool(
            glob.glob(os.path.join(cuda_path, "bin", "**", "cudart64_12*.dll"), recursive=True)
        )

    if not has_13 and not has_12:
        has_13 = _can_load_windll("cudart64_13.dll")
        if not has_13:
            has_12 = _can_load_windll("cudart64_12.dll") or _can_load_windll("cudart64_11.dll")

    if has_13:
        return "cuda_13"
    if has_12:
        return "cuda_12"
    return "vulkan"


def _can_load_windll(name: str) -> bool:
    """Check whether a Windows DLL can be loaded."""
    try:
        ctypes.WinDLL(name)  # type: ignore[attr-defined,unused-ignore]
    except OSError, AttributeError:
        return False
    return True


def format_gpu_summary(gpu: GPUInfo) -> str:
    """Format human-readable summary of GPU acceleration state."""
    if not gpu.has_gpu:
        return "Brak akceleracji GPU (praca na procesorze CPU)."
    if gpu.vram_mb:
        gb = gpu.vram_mb / 1024
        return f"Wykryto GPU: {gpu.name} ({gb:.1f} GB VRAM) - akceleracja aktywna"
    return f"Wykryto GPU: {gpu.name} - akceleracja aktywna"
