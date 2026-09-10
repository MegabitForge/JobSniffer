from __future__ import annotations

from pathlib import Path

import pytest

from job_sniffer.config import AppConfig, load_config, save_config


def test_config_save_and_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_config_path = tmp_path / "config.json"
    import job_sniffer.config as config_module

    monkeypatch.setattr(config_module, "config_file_path", lambda: test_config_path)

    cfg = AppConfig(
        models_dir=str(tmp_path / "models"),
        engine="ollama",
        selected_model_id="qwen2.5-7b-instruct",
        cv_path=str(tmp_path / "my_cv.pdf"),
        auto_evaluate=False,
    )
    save_config(cfg)

    assert test_config_path.is_file()
    loaded = load_config()

    assert loaded.engine == "ollama"
    assert loaded.selected_model_id == "qwen2.5-7b-instruct"
    assert loaded.cv_path == str(tmp_path / "my_cv.pdf")
    assert loaded.auto_evaluate is False
