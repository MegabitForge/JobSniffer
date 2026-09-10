"""Application service boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session

from job_sniffer.config import AppConfig
from job_sniffer.cv_parser import CVParseError, parse_cv_file
from job_sniffer.database import save_evaluation
from job_sniffer.llm.base import LLMProvider
from job_sniffer.llm.catalog import get_model_by_id
from job_sniffer.llm.downloader import ensure_gguf_model, ensure_llama_server, pull_ollama_model
from job_sniffer.llm.ollama import OllamaProvider
from job_sniffer.llm.openai_compat import OpenAICompatProvider
from job_sniffer.llm.runner import LlamaServerProcess
from job_sniffer.models import JobOffer, JobOfferEvaluation

logger = logging.getLogger(__name__)


class EvaluationService:
    """Coordinates CV parsing, model downloading, runner lifecycle, and evaluations."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.runner = LlamaServerProcess(port=config.llama_server_port)
        self._provider: LLMProvider | None = None
        self._cached_cv_text: str | None = None

    def get_cv_text(self, reload: bool = False) -> str | None:
        """Retrieve the parsed text of the configured CV."""
        if not self.config.cv_path:
            return None
        if self._cached_cv_text is not None and not reload:
            return self._cached_cv_text

        try:
            self._cached_cv_text = parse_cv_file(self.config.cv_path)
            return self._cached_cv_text
        except CVParseError as error:
            logger.warning("Failed to parse configured CV: %s", error)
            return None

    def get_provider(self) -> LLMProvider:
        """Get or initialize the current LLM provider."""
        model_opt = get_model_by_id(self.config.selected_model_id)
        if self.config.engine == "ollama":
            return OllamaProvider(host=self.config.ollama_url, model=model_opt.ollama_tag)

        base_url = f"http://127.0.0.1:{self.config.llama_server_port}/v1"
        return OpenAICompatProvider(base_url=base_url, model=model_opt.name)

    async def is_ready(self) -> bool:
        """Check if LLM backend is ready for evaluation requests."""
        provider = self.get_provider()
        return await provider.is_ready()

    async def prepare_backend(
        self,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> bool:
        """Ensure necessary binaries and models are downloaded and the service is started."""
        models_dir = Path(self.config.models_dir)
        models_dir.mkdir(parents=True, exist_ok=True)
        model_opt = get_model_by_id(self.config.selected_model_id)

        if self.config.engine == "llama_cpp":
            server_exe = await ensure_llama_server(models_dir, progress_callback=progress_callback)
            model_path = await ensure_gguf_model(
                model_opt, models_dir, progress_callback=progress_callback
            )

            if progress_callback:
                progress_callback(0.95, "Uruchamianie lokalnego silnika llama-server...")

            started = await self.runner.start(server_exe=server_exe, model_path=model_path)
            if not started:
                logger.error("Failed to start llama-server")
                if progress_callback:
                    progress_callback(1.0, "Nie udało się uruchomić llama-server.")
                return False

            if progress_callback:
                progress_callback(1.0, "Silnik llama-server gotowy do pracy.")
            return True

        if self.config.engine == "ollama":
            await pull_ollama_model(
                model_opt,
                host=self.config.ollama_url,
                progress_callback=progress_callback,
            )
            return True

        return False

    async def evaluate_offer(
        self,
        offer: JobOffer,
        offer_id: int | None,
        session: Session,
    ) -> JobOfferEvaluation | None:
        """Evaluate a single job offer against the candidate's CV and persist the result."""
        cv_text = self.get_cv_text()
        if not cv_text:
            logger.info("Skipping evaluation: No CV configured or CV could not be read.")
            return None

        provider = self.get_provider()
        try:
            result = await provider.evaluate_match(
                cv_text=cv_text,
                job_title=offer.title,
                job_description=offer.description_text,
                job_company=offer.company,
            )
        except Exception:
            logger.exception("LLM evaluation failed for '%s' - '%s'", offer.title, offer.company)
            return None

        evaluation = JobOfferEvaluation(
            offer_id=offer_id,
            fit_score=result.fit_score,
            verdict=result.verdict,
            summary=result.summary,
            strengths=result.strengths,
            weaknesses=result.weaknesses,
            raw_response=result.raw_response,
        )

        if offer_id is not None:
            save_evaluation(session, evaluation)
            logger.info(
                "Saved match evaluation for offer %s: score=%s%%, verdict='%s'",
                offer_id,
                evaluation.fit_score,
                evaluation.verdict,
            )

        return evaluation

    def shutdown(self) -> None:
        """Clean up background processes."""
        self.runner.stop()
