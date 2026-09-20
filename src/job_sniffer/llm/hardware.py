"""Hardware and GPU acceleration detection."""

import ctypes
import logging
import shutil
import subprocess
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
                # Check which CUDA runtime is actually installed
                import glob
                import os

                cuda_path = os.environ.get("CUDA_PATH", "")
                has_13 = False
                has_12 = False

                if cuda_path and os.path.exists(cuda_path):
                    has_13 = bool(
                        glob.glob(
                            os.path.join(cuda_path, "bin", "**", "cudart64_13*.dll"), recursive=True
                        )
                    )
                    has_12 = bool(
                        glob.glob(
                            os.path.join(cuda_path, "bin", "**", "cudart64_12*.dll"), recursive=True
                        )
                    )

                if not has_13 and not has_12:
                    for dll in ["cudart64_13.dll"]:
                        try:
                            ctypes.WinDLL(dll)
                            has_13 = True
                            break
                        except OSError:
                            pass
                    if not has_13:
                        for dll in ["cudart64_12.dll", "cudart64_11.dll"]:
                            try:
                                ctypes.WinDLL(dll)
                                has_12 = True
                                break
                            except OSError:
                                pass

                backend_type = "vulkan"
                if has_13:
                    backend_type = "cuda_13"
                elif has_12:
                    backend_type = "cuda_12"

                return GPUInfo(
                    has_gpu=True,
                    name=gpu_name,
                    vram_mb=vram_mb,
                    backend=backend_type,
                )
        except (subprocess.SubprocessError, OSError, ValueError) as err:
            logger.debug("nvidia-smi query failed: %s", err)

    # 2. Check for Vulkan loader dll (available on Windows with modern AMD/Intel/NVIDIA drivers)
    has_vulkan = False
    try:
        ctypes.CDLL("vulkan-1.dll")
        has_vulkan = True
    except OSError, AttributeError:
        has_vulkan = False

    if has_vulkan:
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


def format_gpu_summary(gpu: GPUInfo) -> str:
    """Format human-readable summary of GPU acceleration state."""
    if not gpu.has_gpu:
        return "Brak akceleracji GPU (praca na procesorze CPU)."
    if gpu.vram_mb:
        gb = gpu.vram_mb / 1024
        return f"Wykryto GPU: {gpu.name} ({gb:.1f} GB VRAM) - akceleracja aktywna"
    return f"Wykryto GPU: {gpu.name} - akceleracja aktywna"
