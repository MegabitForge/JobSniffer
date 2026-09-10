"""LLM prompts and JSON output extraction."""

from __future__ import annotations

import json
import re
from typing import Any

EVALUATION_SYSTEM_PROMPT = """Jesteś obiektywnym, precyzyjnym ekspertem rekrutacji IT i doradcą kariery.
Twoim zadaniem jest ocena dopasowania CV kandydata do oferty pracy.

Wymagania dotyczące odpowiedzi:
1. Odpowiedz WYŁĄCZNIE w formacie czystego JSON (bez dodatkowego wstępu czy zakończenia).
2. JSON musi mieć dokładnie poniższe pola:
{
  "fit_score": <liczba całkowita od 0 do 100, szansa na dostanie się lub odpowiedź na aplikację>,
  "verdict": "<krótki werdykt, np. 'Wysoka szansa - silne dopasowanie profilu' lub 'Umiarkowana szansa' lub 'Niska szansa'>",
  "summary": "<2-3 zdaniowe zwięzłe podsumowanie: kogo poszukuje pracodawca, jakie są kluczowe technologie i zakres roli>",
  "strengths": [
    "<konkretna mocna strona kandydata z CV względem wymagań w ofercie>",
    "<kolejna mocna strona...>"
  ],
  "weaknesses": [
    "<konkretny brak technologiczny, brakujące doświadczenie lub rozbieżność względem wymagań>",
    "<kolejny brak...>"
  ]
}
"""


def build_evaluation_user_prompt(
    cv_text: str,
    job_title: str,
    job_description: str | None,
    job_company: str,
) -> str:
    """Build user message containing job details and candidate CV."""
    desc = (job_description or "").strip()
    if not desc:
        desc = (
            "(Brak szczegółowego opisu oferty pracy - oceń na podstawie tytułu stanowiska i firmy)"
        )
    else:
        desc = desc[:5000]

    cv_excerpt = cv_text.strip()[:6000]

    return f"""### OFERTA PRACY:
Stanowisko: {job_title}
Firma: {job_company}

Opis i wymagania:
{desc}

---

### CV KANDYDATA:
{cv_excerpt}

---
Przeanalizuj ofertę i CV, a następnie zwróć ocenę wyłącznie jako poprawny obiekt JSON."""


def parse_evaluation_json(raw_text: str) -> dict[str, Any]:
    """Extract and parse JSON object from LLM response."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        text = text[first_brace : last_brace + 1]

    data = json.loads(text)
    if not isinstance(data, dict):
        raise TypeError("LLM response is not a valid JSON object")
    return data
