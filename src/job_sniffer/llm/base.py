"""Large language model provider contract boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class MatchEvaluationResult(BaseModel):
    """Normalized structured evaluation produced by LLM."""

    fit_score: int = Field(ge=0, le=100)
    verdict: str
    summary: str
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    raw_response: dict[str, Any] | None = None


class LLMProvider(ABC):
    """Abstract provider for LLM completion and evaluation."""

    @abstractmethod
    async def evaluate_match(
        self,
        cv_text: str,
        job_title: str,
        job_description: str | None,
        job_company: str,
    ) -> MatchEvaluationResult:
        """Evaluate candidate CV against a job offer."""

    @abstractmethod
    async def is_ready(self) -> bool:
        """Check if provider backend is online and ready for requests."""
