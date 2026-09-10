from __future__ import annotations

from job_sniffer.llm.catalog import AVAILABLE_MODELS, get_model_by_id


def test_available_models_catalog() -> None:
    assert len(AVAILABLE_MODELS) >= 3
    qwen = get_model_by_id("qwen2.5-3b-instruct")
    assert "Qwen" in qwen.name
    assert qwen.gguf_filename.endswith(".gguf")
    assert qwen.size_mb > 1000


def test_fallback_model() -> None:
    model = get_model_by_id("non-existent-model")
    assert model.id == "qwen2.5-3b-instruct"
