"""LLM provider and evaluation modules."""

from __future__ import annotations

from job_sniffer.llm.base import LLMProvider, MatchEvaluationResult
from job_sniffer.llm.catalog import AVAILABLE_MODELS, ModelOption, get_model_by_id
from job_sniffer.llm.downloader import ensure_gguf_model, ensure_llama_server, pull_ollama_model
from job_sniffer.llm.ollama import OllamaProvider
from job_sniffer.llm.openai_compat import OpenAICompatProvider
from job_sniffer.llm.runner import LlamaServerProcess

__all__ = [
    "AVAILABLE_MODELS",
    "LLMProvider",
    "LlamaServerProcess",
    "MatchEvaluationResult",
    "ModelOption",
    "OllamaProvider",
    "OpenAICompatProvider",
    "ensure_gguf_model",
    "ensure_llama_server",
    "get_model_by_id",
    "pull_ollama_model",
]
