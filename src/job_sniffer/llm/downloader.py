"""Downloader for local LLM models and server runtime."""

from __future__ import annotations

import asyncio
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import ollama

from job_sniffer.llm.catalog import ModelOption

logger = logging.getLogger(__name__)

LLAMA_SERVER_VULKAN_ZIP_URL = (
    "https://github.com/ggerganov/llama.cpp/releases/download/b10946/llama-b10946-bin-win-vulkan-x64.zip"
)
LLAMA_SERVER_CPU_ZIP_URL = (
    "https://github.com/ggerganov/llama.cpp/releases/download/b10946/llama-b10946-bin-win-cpu-x64.zip"
)

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


async def ensure_llama_server(
    models_dir: Path,
    use_gpu: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Ensure llama-server.exe is downloaded and extracted, with GPU support if enabled."""
    bin_dir = models_dir / "bin"
    server_exe = bin_dir / "llama-server.exe"
    vulkan_dll = bin_dir / "ggml-vulkan.dll"

    # Check if existing installation matches requested GPU mode
    if server_exe.is_file() and (not use_gpu or vulkan_dll.is_file()):
        logger.info("llama-server.exe already present and configured (gpu=%s)", use_gpu)
        return server_exe

    bin_dir.mkdir(parents=True, exist_ok=True)
    zip_path = bin_dir / "llama-server.zip"
    download_url = LLAMA_SERVER_VULKAN_ZIP_URL if use_gpu else LLAMA_SERVER_CPU_ZIP_URL

    msg = (
        "Pobieranie silnika AI z akceleracją GPU (Vulkan)..."
        if use_gpu
        else "Pobieranie silnika AI (CPU)..."
    )
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
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(bin_dir)
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
