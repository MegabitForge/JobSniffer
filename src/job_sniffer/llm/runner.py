"""Process management for local llama-server."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class LlamaServerProcess:
    """Manages the lifecycle of a local llama-server subprocess."""

    def __init__(self, port: int = 8080) -> None:
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.current_model_path: Path | None = None

    @property
    def is_running(self) -> bool:
        """Check if server process is currently active."""
        return self.process is not None and self.process.poll() is None

    async def wait_until_ready(self, timeout_seconds: float = 30.0) -> bool:
        """Poll the server health endpoint until it responds with 200 OK."""
        url = f"http://127.0.0.1:{self.port}/health"
        loop = asyncio.get_running_loop()
        start_time = loop.time()

        async with httpx.AsyncClient(timeout=2.0) as client:
            while loop.time() - start_time < timeout_seconds:
                if not self.is_running:
                    logger.warning("llama-server process exited unexpectedly")
                    return False
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        logger.info("llama-server is healthy and ready on port %s", self.port)
                        return True
                except (httpx.HTTPError, OSError) as poll_err:
                    logger.debug("Health check polling not ready yet: %s", poll_err)
                await asyncio.sleep(0.5)

        logger.error("llama-server failed to become ready within %s seconds", timeout_seconds)
        return False

    async def start(
        self,
        server_exe: Path,
        model_path: Path,
        gpu_layers: int = 0,
        context_size: int = 4096,
    ) -> bool:
        """Start or restart the llama-server process with the given model."""
        if self.is_running and self.current_model_path == model_path:
            logger.info("llama-server already running with requested model: %s", model_path.name)
            return True

        self.stop()

        if not server_exe.is_file():
            raise FileNotFoundError(f"llama-server executable not found at {server_exe}")
        if not model_path.is_file():
            raise FileNotFoundError(f"Model file not found at {model_path}")

        cmd = [
            str(server_exe),
            "-m",
            str(model_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "-c",
            str(context_size),
        ]
        if gpu_layers > 0:
            cmd.extend(["-ngl", str(gpu_layers)])

        flags = 0
        if sys.platform == "win32":
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

        logger.info("Launching llama-server: %s", " ".join(cmd))
        loop = asyncio.get_running_loop()

        def _launch() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )

        self.process = await loop.run_in_executor(None, _launch)
        self.current_model_path = model_path

        ready = await self.wait_until_ready()
        if not ready:
            self.stop()
            return False
        return True

    def stop(self) -> None:
        """Terminate the server process if running."""
        if self.process is not None:
            logger.info("Stopping llama-server process (PID %s)", self.process.pid)
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except (subprocess.SubprocessError, OSError) as stop_err:
                logger.debug("Subprocess terminate failed: %s; attempting kill", stop_err)
                try:
                    self.process.kill()
                except (subprocess.SubprocessError, OSError) as kill_err:
                    logger.debug("Subprocess kill failed: %s", kill_err)
            self.process = None
            self.current_model_path = None
