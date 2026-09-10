"""OpenAI-compatible provider boundary (e.g. llama-server)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from openai import AsyncOpenAI

from job_sniffer.llm.base import LLMProvider, MatchEvaluationResult
from job_sniffer.llm.prompts import (
    EVALUATION_SYSTEM_PROMPT,
    build_evaluation_user_prompt,
    parse_evaluation_json,
)

logger = logging.getLogger(__name__)


class OpenAICompatProvider(LLMProvider):
    """LLM provider communicating with an OpenAI-compatible HTTP API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8080/v1", model: str = "default") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = AsyncOpenAI(base_url=self.base_url, api_key="local-server")

    async def is_ready(self) -> bool:
        """Verify server readiness by pinging health endpoint."""
        health_url = self.base_url.replace("/v1", "") + "/health"
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(health_url)
                if res.status_code == 200:
                    return True
                models_res = await client.get(f"{self.base_url}/models")
                return models_res.status_code == 200
        except httpx.HTTPError, OSError:
            return False

    async def evaluate_match(
        self,
        cv_text: str,
        job_title: str,
        job_description: str | None,
        job_company: str,
    ) -> MatchEvaluationResult:
        """Send CV and job description to local llama-server for evaluation."""
        user_prompt = build_evaluation_user_prompt(
            cv_text=cv_text,
            job_title=job_title,
            job_description=job_description,
            job_company=job_company,
        )

        logger.info("Requesting evaluation from OpenAI-compatible server at %s", self.base_url)
        completion = await self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": EVALUATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )

        raw_content = completion.choices[0].message.content or "{}"
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
