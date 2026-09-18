"""LLM prompts and JSON output extraction."""

from __future__ import annotations

import json
import re
from typing import Any

EVALUATION_SYSTEM_PROMPT = """Jesteś ekspertem rekrutacji IT. Porównujesz ofertę pracy z CV kandydata.

Zasady:
1. OFERTA to wymagania pracodawcy. MOJE CV to profil kandydata. Nie myl ich ról.
2. Zwróć uwagę na faktyczne, twarde wymagania techniczne z oferty (must-have vs nice-to-have, stack, seniority) ale nie ignoruj wymagań miękkich.
3. fit_score (0-100): realna szansa na zaproszenie na rozmowę rekrutacyjną (bądź bardzo rygorystyczny) i zwracaj uwagę na tytuł stanowiska i jego wymagania i używaj pełnej skali (10% kąpletnie się nie nadaje, 20% ma jedną z kluczowych umiejętności, 30% ma pare kluczowych, 50% większość kluczowych, 60% ma wszystkie umiejętności kluczowe, 70% ma też większość z mile widzianych, 90% ma wszystko idealny kandydat, 100% ma rodzinę w tej firmie (z czego solidniejsze umiejętności albo umiejętności mile widziane dają więcej punktów, a mile widziane dodają je)).
4. strengths: technologie i doświadczenia z CV pokrywające wymagania oferty.
5. weaknesses: kluczowe wymagania z oferty, których brak w CV kandydata lub też braki w wymaganiach miękkich.

Zwróć wyłącznie JSON:
{
  "summary": "Zwięzłe podsumowanie profilu oferty i kluczowych technologii (parę słów)",
  "strengths": ["mocna strona z CV pokrywająca ofertę"],
  "weaknesses": ["brak kandydata względem wymagań oferty"],
  "verdict": "Krótki opis werdyktu dopasowania (parę słów)",
  "explanation": "Szczegółowy wyjaśnienie oceny (parę zdań)",
  "fit_score": 0
}"""


def build_evaluation_user_prompt(
    cv_text: str,
    job_title: str,
    job_description: str | None,
    job_company: str,
) -> str:
    """Build concise user prompt clearly delineating job offer requirements from candidate CV."""
    desc = (job_description or "").strip()
    if not desc:
        desc = "Brak szczegółowego opisu (dokonaj szacunku na podstawie tytułu i firmy)."
    else:
        desc = desc[:4000]

    cv_excerpt = cv_text.strip()[:5000]

    return f"""OFERTA PRACY:
Stanowisko: {job_title}
Firma: {job_company}
Wymagania i opis:
{desc}

MOJE CV:
{cv_excerpt}

Zadanie:
Oceń dopasowanie MOJEGO CV do faktycznych wymagań OFERTY PRACY. Zwróć JSON."""


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
