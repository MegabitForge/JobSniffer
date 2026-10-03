import io
import stat
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from job_sniffer.llm import downloader
from job_sniffer.llm.downloader import (
    LLAMA_CPP_BUILD,
    ensure_llama_server,
    extract_llama_server_archive,
    llama_server_download_url,
    llama_server_executable_name,
)
from job_sniffer.llm.hardware import GPUInfo


def _add_tar_file(tf: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tf.addfile(info, io.BytesIO(data))


def test_extract_tar_strips_top_level_and_keeps_exec_bit(tmp_path: Path) -> None:
    archive = tmp_path / "llama.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        top = tarfile.TarInfo("llama-b1")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tf.addfile(top)
        _add_tar_file(tf, "llama-b1/llama-server", b"#!/bin/sh\n", 0o755)
        _add_tar_file(tf, "llama-b1/libllama.so.0.1", b"lib", 0o755)
        link = tarfile.TarInfo("llama-b1/libllama.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libllama.so.0.1"
        tf.addfile(link)

    destination = tmp_path / "bin"
    destination.mkdir()
    extract_llama_server_archive(archive, destination)

    server = destination / "llama-server"
    assert server.is_file()
    assert server.stat().st_mode & stat.S_IXUSR
    assert (destination / "libllama.so").resolve() == (destination / "libllama.so.0.1").resolve()
    assert not (destination / "llama-b1").exists()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeDownload:
    """Stand-in for download_file_with_progress that writes a minimal release archive."""

    def __init__(self, *, include_server: bool = True) -> None:
        self.include_server = include_server
        self.urls: list[str] = []

    async def __call__(self, url: str, destination: Path, **_: object) -> Path:
        self.urls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        member = llama_server_executable_name() if self.include_server else "README.md"
        if destination.name.endswith(".zip"):
            with zipfile.ZipFile(destination, "w") as zf:
                zf.writestr(member, b"bin")
        else:
            with tarfile.open(destination, "w:gz") as tf:
                _add_tar_file(tf, f"llama-b1/{member}", b"bin", 0o755)
        return destination


def _patch_gpu(monkeypatch: pytest.MonkeyPatch, backend: str) -> MagicMock:
    detect = MagicMock(
        return_value=GPUInfo(has_gpu=backend != "cpu", name="GPU", vram_mb=None, backend=backend)  # type: ignore[arg-type]
    )
    monkeypatch.setattr("job_sniffer.llm.hardware.detect_gpu", detect)
    return detect


@pytest.mark.anyio
async def test_ensure_llama_server_downloads_and_extracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDownload()
    monkeypatch.setattr(downloader, "download_file_with_progress", fake)
    _patch_gpu(monkeypatch, "vulkan")

    server = await ensure_llama_server(tmp_path, use_gpu=True)

    bin_dir = tmp_path / "bin"
    assert server == bin_dir / llama_server_executable_name()
    assert server.is_file()
    assert (bin_dir / f".backend_vulkan_{LLAMA_CPP_BUILD}").is_file()
    assert fake.urls == [llama_server_download_url("vulkan")]
    assert not list(bin_dir.glob("llama-server.zip")) + list(bin_dir.glob("llama-server.tar.gz"))


@pytest.mark.anyio
async def test_ensure_llama_server_skips_download_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDownload()
    monkeypatch.setattr(downloader, "download_file_with_progress", fake)
    _patch_gpu(monkeypatch, "vulkan")

    await ensure_llama_server(tmp_path, use_gpu=True)
    await ensure_llama_server(tmp_path, use_gpu=True)

    assert len(fake.urls) == 1


@pytest.mark.anyio
async def test_ensure_llama_server_reinstalls_when_backend_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDownload()
    monkeypatch.setattr(downloader, "download_file_with_progress", fake)
    _patch_gpu(monkeypatch, "vulkan")
    await ensure_llama_server(tmp_path, use_gpu=True)
    stale_file = tmp_path / "bin" / "libggml-vulkan.so"
    stale_file.touch()

    detect = _patch_gpu(monkeypatch, "vulkan")
    await ensure_llama_server(tmp_path, use_gpu=False)

    detect.assert_not_called()
    bin_dir = tmp_path / "bin"
    assert fake.urls[-1] == llama_server_download_url("cpu")
    assert (bin_dir / f".backend_cpu_{LLAMA_CPP_BUILD}").is_file()
    assert not (bin_dir / f".backend_vulkan_{LLAMA_CPP_BUILD}").exists()
    assert not stale_file.exists()


@pytest.mark.anyio
async def test_ensure_llama_server_reinstalls_when_build_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeDownload()
    monkeypatch.setattr(downloader, "download_file_with_progress", fake)
    _patch_gpu(monkeypatch, "vulkan")
    monkeypatch.setattr(downloader, "LLAMA_CPP_BUILD", "b1")
    await ensure_llama_server(tmp_path, use_gpu=True)

    monkeypatch.setattr(downloader, "LLAMA_CPP_BUILD", "b2")
    await ensure_llama_server(tmp_path, use_gpu=True)

    bin_dir = tmp_path / "bin"
    assert len(fake.urls) == 2
    assert (bin_dir / ".backend_vulkan_b2").is_file()
    assert not (bin_dir / ".backend_vulkan_b1").exists()


@pytest.mark.anyio
async def test_ensure_llama_server_fails_when_archive_lacks_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        downloader, "download_file_with_progress", FakeDownload(include_server=False)
    )
    _patch_gpu(monkeypatch, "cpu")

    with pytest.raises(FileNotFoundError):
        await ensure_llama_server(tmp_path, use_gpu=True)

    assert not (tmp_path / "bin" / f".backend_cpu_{LLAMA_CPP_BUILD}").exists()
