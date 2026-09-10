"""Application configuration boundary."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

import platformdirs
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

EngineType = Literal["llama_cpp", "ollama"]


def default_models_directory() -> str:
    """Return default models storage directory in user app data."""
    return str(Path(platformdirs.user_data_dir("job-sniffer")) / "models")


def config_file_path() -> Path:
    """Return persistent config JSON file path."""
    config_dir = Path(platformdirs.user_config_dir("job-sniffer"))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


class AppConfig(BaseModel):
    """Global configuration settings for Job Sniffer."""

    models_dir: str = Field(default_factory=default_models_directory)
    engine: EngineType = "llama_cpp"
    selected_model_id: str = "qwen2.5-3b-instruct"
    ollama_url: str = "http://127.0.0.1:11434"
    llama_server_port: int = 8080
    cv_path: str | None = None
    auto_evaluate: bool = True


def load_config() -> AppConfig:
    """Load configuration from disk, falling back to defaults if not found."""
    path = config_file_path()
    if path.is_file():
        try:
            raw_text = path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            return AppConfig.model_validate(data)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            logger.warning("Failed to load config from %s: %s; using defaults", path, error)
    return AppConfig()


def save_config(config: AppConfig) -> None:
    """Save configuration to disk."""
    path = config_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Saved configuration to %s", path)
    except (OSError, ValueError) as error:
        logger.error("Failed to save config to %s: %s", path, error)
