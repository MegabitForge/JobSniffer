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

LLAMA_SERVER_ZIP_URL = "https://github.com/ggerganov/llama.cpp/releases/download/b10900/llama-b10900-bin-win-cpu-x64.zip"

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
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Ensure llama-server.exe is downloaded and extracted."""
    bin_dir = models_dir / "bin"
    server_exe = bin_dir / "llama-server.exe"

    if server_exe.is_file():
        logger.info("llama-server.exe already present at %s", server_exe)
        return server_exe

    bin_dir.mkdir(parents=True, exist_ok=True)
    zip_path = bin_dir / "llama-server.zip"

    if progress_callback:
        progress_callback(0.0, "Pobieranie silnika llama-server.exe...")

    await download_file_with_progress(
        LLAMA_SERVER_ZIP_URL,
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

    if not server_exe.is_file():
        raise RuntimeError(f"Failed to find llama-server.exe after extracting {zip_path}")

    logger.info("llama-server.exe extracted to %s", server_exe)
    return server_exe


async def ensure_gguf_model(
    model: ModelOption,
    models_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """Ensure GGUF model file is downloaded."""
    model_path = models_dir / model.gguf_filename
    if model_path.is_file() and model_path.stat().st_size > 10 * 1024 * 1024:
        logger.info("GGUF model already present at %s", model_path)
        return model_path

    if progress_callback:
        progress_callback(0.0, f"Pobieranie modelu {model.name}...")

    await download_file_with_progress(
        model.gguf_url,
        model_path,
        progress_callback=progress_callback,
    )
    return model_path


async def pull_ollama_model(
    model: ModelOption,
    host: str = "http://127.0.0.1:11434",
    progress_callback: ProgressCallback | None = None,
) -> None:
    """Pull model into Ollama instance with progress updates."""
    client = ollama.AsyncClient(host=host)
    logger.info("Initiating Ollama pull for model tag: %s", model.ollama_tag)

    if progress_callback:
        progress_callback(0.0, f"Pobieranie modelu {model.ollama_tag} przez Ollama...")

    response = await client.pull(model=model.ollama_tag, stream=True)
    async for item in response:
        status_text = getattr(item, "status", "") or "Pobieranie..."
        total = getattr(item, "total", 0) or 0
        completed = getattr(item, "completed", 0) or 0
        fraction = (completed / total) if total > 0 else 0.0

        if progress_callback:
            if total > 0:
                mb_c = completed / (1024 * 1024)
                mb_t = total / (1024 * 1024)
                msg = f"{status_text} - {mb_c:.1f} MB / {mb_t:.1f} MB ({fraction * 100:.0f}%)"
            else:
                msg = status_text
            progress_callback(fraction, msg)

    if progress_callback:
        progress_callback(1.0, f"Model {model.ollama_tag} jest gotowy w Ollama.")
