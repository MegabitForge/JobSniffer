"""Downloader for local LLM models and server runtime."""

import asyncio
import logging
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import httpx
import ollama

from job_sniffer.llm.catalog import ModelOption
from job_sniffer.llm.hardware import BackendType

logger = logging.getLogger(__name__)

LLAMA_CPP_BUILD = "b10946"
LLAMA_CPP_RELEASE_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_BUILD}"

_WINDOWS_ASSETS: dict[BackendType, str] = {
    "cuda_13": "bin-win-cuda-13.3-x64.zip",
    "cuda_12": "bin-win-cuda-12.4-x64.zip",
    "vulkan": "bin-win-vulkan-x64.zip",
    "cpu": "bin-win-cpu-x64.zip",
}
_LINUX_ASSETS: dict[BackendType, str] = {
    "vulkan": "bin-ubuntu-vulkan-x64.tar.gz",
    "cpu": "bin-ubuntu-x64.tar.gz",
}

_BACKEND_LABELS: dict[BackendType, str] = {
    "cuda_13": "z akceleracją GPU (CUDA 13.x)",
    "cuda_12": "z akceleracją GPU (CUDA 12.x)",
    "vulkan": "z akceleracją GPU (Vulkan)",
    "cpu": "(CPU)",
}

ProgressCallback = Callable[[float, str], None]


async def download_file_with_progress(
    url: str,
    destination: Path,
    progress_callback: ProgressCallback | None = None,
    chunk_size: int = 1024 * 1024,  # 1MB
) -> Path:
    """Download a file with streaming progress reporting."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_file = destination.with_suffix(destination.suffix + ".part")

    logger.info("Starting download from %s to %s", url, destination)
    async with (
        httpx.AsyncClient(follow_redirects=True, timeout=None) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        total_bytes = int(response.headers.get("content-length", 0))
        downloaded = 0

        with temp_file.open("wb") as f:
            async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                f.write(chunk)
                downloaded += len(chunk)
                if total_bytes > 0 and progress_callback:
                    fraction = downloaded / total_bytes
                    mb_down = downloaded / (1024 * 1024)
                    mb_total = total_bytes / (1024 * 1024)
                    progress_callback(
                        fraction, f"{mb_down:.1f} MB / {mb_total:.1f} MB ({fraction * 100:.0f}%)"
                    )
                elif progress_callback:
                    mb_down = downloaded / (1024 * 1024)
                    progress_callback(0.0, f"Pobrano {mb_down:.1f} MB")

    temp_file.replace(destination)
    logger.info("Download completed: %s", destination)
    if progress_callback:
        progress_callback(1.0, "Pobieranie zakończone pomyślnie.")
    return destination


def llama_server_executable_name(platform: str = sys.platform) -> str:
    return "llama-server.exe" if platform == "win32" else "llama-server"


def llama_server_download_url(backend: BackendType, platform: str = sys.platform) -> str:
    """Return the llama.cpp release archive URL for the given backend and platform."""
    if platform == "win32":
        assets = _WINDOWS_ASSETS
    elif platform.startswith("linux"):
        assets = _LINUX_ASSETS
    else:
        raise RuntimeError(f"Unsupported platform for llama-server: {platform}")
    asset = assets.get(backend)
    if asset is None:
        raise RuntimeError(f"Backend {backend} is not available on {platform}")
    return f"{LLAMA_CPP_RELEASE_URL}/llama-{LLAMA_CPP_BUILD}-{asset}"


def extract_llama_server_archive(archive_path: Path, destination: Path) -> None:
    """Extract a llama.cpp release archive (zip or tar.gz) flat into the destination."""
    if archive_path.name.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(destination)
        return

    def strip_top_level(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
        # Linux archives wrap everything in a single "llama-<build>/" directory.
        parts = PurePosixPath(member.name).parts
        if len(parts) <= 1:
            return None
        linkname = member.linkname
        if member.islnk():
            linkname = "/".join(PurePosixPath(linkname).parts[1:])
        stripped = member.replace(name="/".join(parts[1:]), linkname=linkname, deep=False)
        return tarfile.data_filter(stripped, dest_path)

    with tarfile.open(archive_path, "r:*") as tf:
        tf.extractall(destination, filter=strip_top_level)


async def ensure_llama_server(
    models_dir: Path,
    use_gpu: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Ensure llama-server is downloaded and extracted, with GPU support if enabled."""
    from job_sniffer.llm.hardware import detect_gpu

    bin_dir = models_dir / "bin"
    server_exe = bin_dir / llama_server_executable_name()

    backend: BackendType = "cpu"
    if use_gpu:
        backend = detect_gpu().backend

    # The marker encodes both backend and build, so bumping LLAMA_CPP_BUILD reinstalls the server.
    marker = bin_dir / f".backend_{backend}_{LLAMA_CPP_BUILD}"

    # Check if existing installation matches requested GPU mode
    if server_exe.is_file() and marker.is_file():
        logger.info(
            "llama-server already present and configured (gpu=%s, backend=%s)", use_gpu, backend
        )
        return server_exe

    # Clean previous if backend or build changed
    if server_exe.is_file():
        shutil.rmtree(bin_dir, ignore_errors=True)

    bin_dir.mkdir(parents=True, exist_ok=True)
    download_url = llama_server_download_url(backend)
    archive_suffix = ".zip" if download_url.endswith(".zip") else ".tar.gz"
    zip_path = bin_dir / f"llama-server{archive_suffix}"
    msg = f"Pobieranie silnika AI {_BACKEND_LABELS[backend]}..."

    if progress_callback:
        progress_callback(0.0, msg)

    await download_file_with_progress(
        download_url,
        zip_path,
        progress_callback=progress_callback,
    )

    if progress_callback:
        progress_callback(1.0, "Wypakowywanie silnika llama-server...")

    loop = asyncio.get_running_loop()

    def _extract() -> None:
        extract_llama_server_archive(zip_path, bin_dir)
        if not server_exe.is_file():
            raise FileNotFoundError(
                f"llama-server executable missing after extraction: {server_exe}"
            )
        marker.touch()
        if zip_path.is_file():
            zip_path.unlink()

    await loop.run_in_executor(None, _extract)
    return server_exe


async def ensure_gguf_model(
    model_option: ModelOption,
    models_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Ensure selected GGUF model file is downloaded and verified."""
    model_path = models_dir / model_option.gguf_filename
    if model_path.is_file() and model_path.stat().st_size > 10 * 1024 * 1024:
        logger.info("Model file already present at %s", model_path)
        return model_path

    if progress_callback:
        progress_callback(0.0, f"Pobieranie modelu {model_option.name}...")

    await download_file_with_progress(
        model_option.gguf_url,
        model_path,
        progress_callback=progress_callback,
    )
    return model_path


async def pull_ollama_model(
    model_option: ModelOption,
    host: str,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Pull model via Ollama daemon API with streaming progress."""
    logger.info("Pulling model %s on Ollama host %s", model_option.ollama_tag, host)
    client = ollama.AsyncClient(host=host)

    if progress_callback:
        progress_callback(0.0, f"Pobieranie modelu {model_option.ollama_tag} w Ollama...")

    stream = await client.pull(model=model_option.ollama_tag, stream=True)
    async for chunk in stream:
        status = chunk.get("status", "")
        completed = chunk.get("completed", 0)
        total = chunk.get("total", 0)
        if total > 0 and progress_callback:
            frac = completed / total
            progress_callback(frac, f"{status} ({frac * 100:.0f}%)")
        elif progress_callback and status:
            progress_callback(0.0, status)

    if progress_callback:
        progress_callback(1.0, f"Model {model_option.ollama_tag} jest gotowy w Ollama.")
