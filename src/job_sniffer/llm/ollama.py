"""Ollama provider boundary."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import ollama

from job_sniffer.llm.base import LLMProvider, MatchEvaluationResult
from job_sniffer.llm.prompts import (
    EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
    parse_evaluation_json,
)

logger = logging.getLogger(__name__)


class OllamaProvider(LLMProvider):
    """LLM provider communicating with a local Ollama daemon."""

    def __init__(self, host: str = "http://127.0.0.1:11434", model: str = "qwen2.5:3b") -> None:
        self.host = host
        self.model = model
        self._client = ollama.AsyncClient(host=self.host)

    async def is_ready(self) -> bool:
        """Verify Ollama daemon is reachable and the model exists."""
        try:
            models_response = await self._client.list()
            for m in models_response.models:
                m_name = getattr(m, "model", "") or getattr(m, "name", "")
                if self.model in m_name or m_name in self.model:
                    return True
            return True
        except ollama.ResponseError, httpx.HTTPError, OSError:
            return False

    async def evaluate_match(
        self,
        cv_text: str,
        job_title: str,
        job_description: str | None,
        job_company: str,
    ) -> MatchEvaluationResult:
        """Send CV and job description to Ollama for evaluation."""
        user_prompt = build_evaluation_user_prompt(
            cv_text=cv_text,
            job_title=job_title,
            job_description=job_description,
            job_company=job_company,
        )

        logger.info("Requesting evaluation from Ollama at %s (model: %s)", self.host, self.model)
        response = await self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": EVALUATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            format="json",
            options={"temperature": 0.2},
        )

        raw_content = response.message.content or "{}"
        parsed: dict[str, Any] = parse_evaluation_json(raw_content)

        fit_score = int(parsed.get("fit_score", 50))
        fit_score = max(0, min(100, fit_score))
        verdict = str(parsed.get("verdict", "Brak jednoznacznego werdyktu"))
        summary = str(parsed.get("summary", ""))
        strengths = [str(s) for s in parsed.get("strengths", [])]
        weaknesses = [str(w) for w in parsed.get("weaknesses", [])]

        return MatchEvaluationResult(
            fit_score=fit_score,
            verdict=verdict,
            summary=summary,
            strengths=strengths,
            weaknesses=weaknesses,
            raw_response=parsed,
        )
