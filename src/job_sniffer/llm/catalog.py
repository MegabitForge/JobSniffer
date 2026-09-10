"""Catalog of supported predefined LLM models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelOption:
    """Predefined model specification."""

    id: str
    name: str
    description: str
    gguf_filename: str
    gguf_url: str
    ollama_tag: str
    size_mb: int


AVAILABLE_MODELS: list[ModelOption] = [
    ModelOption(
        id="qwen2.5-3b-instruct",
        name="Qwen 2.5 3B Instruct (Rekomendowany)",
        description="Szybki, bardzo dobra analiza w języku polskim, format JSON (~2.0 GB)",
        gguf_filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        gguf_url="https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        ollama_tag="qwen2.5:3b",
        size_mb=2020,
    ),
    ModelOption(
        id="qwen2.5-7b-instruct",
        name="Qwen 2.5 7B Instruct (Wysoka dokładność)",
        description="Dogłębna ocena dopasowania i krytyczna analiza CV (~4.7 GB)",
        gguf_filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        gguf_url="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf",
        ollama_tag="qwen2.5:7b",
        size_mb=4680,
    ),
    ModelOption(
        id="llama-3.2-3b-instruct",
        name="Llama 3.2 3B Instruct (Meta)",
        description="Kompaktowy i wydajny model ogólnego przeznaczenia (~2.0 GB)",
        gguf_filename="llama-3.2-3b-instruct-q4_k_m.gguf",
        gguf_url="https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        ollama_tag="llama3.2:3b",
        size_mb=2020,
    ),
]


def get_model_by_id(model_id: str) -> ModelOption:
    """Retrieve model option by its unique identifier."""
    for model in AVAILABLE_MODELS:
        if model.id == model_id:
            return model
    return AVAILABLE_MODELS[0]
