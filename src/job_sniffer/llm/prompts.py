"""LLM prompts and JSON output extraction."""

import json
import re
from typing import Any

EVALUATION_SYSTEM_PROMPT = """Jesteś analitycznym algorytmem rekrutacyjnym. Oceniasz matematycznie dopasowanie kandydata (MOJE CV) do wymogów (OFERTA PRACY). Wynik to ścisła kalkulacja.

ALGORYTM PUNKTACJI (Max 100 pkt):
1. Podziel wymagania z oferty na 3 grupy: TWARDE KLUCZOWE (must-have), TECHNICZNE DODATKOWE (nice-to-have) oraz MIĘKKIE.
2. MUST-HAVE (Pula: 50 pkt). Podziel 50 przez ilość kluczowych wymagań. Za każde zbadaj CV. Jeśli stanowisko wyższego szczebla narzuca posiadanie dużego doświadczenia, rygorystycznie obniżaj ilość przyznanych punktów w przypadku zbyt małego udokumentowanego stażu u kandydata dla tej technologii.
3. NICE-TO-HAVE (Pula: 30 pkt). Podziel 30 przez ilość wymagań dodatkowych. Oblicz i przydziel adekwatnie za każdy dowód umiejętności.
4. MIĘKKIE (Pula: 20 pkt). Podziel 20 przez ilość wymagań miękkich (języki komunikacyjne, organizacja). Oblicz i przydziel punkty za każde spełnione.
*(Uwaga: w razie braku wymagań w grupie dodatkowej lub miękkiej, przenieś ich całą pulę punktową wprost na poczet wagi MUST-HAVE).*
5. W przypadku rażącego braku fundamentalnej technologii rdzennej dla profilu stanowiska - odejmij od końcowej sumy karę 30 pkt.

Zwróć WYŁĄCZNIE obiekt JSON po wyliczeniu:
{
  "summary": "Jedno zdanie o głównym celu stanowiska",
  "strengths": ["spełnione wymaganie + urywek udowadniający z CV"],
  "weaknesses": ["nazwy braków lub nazwy umiejętności gdzie drastycznie zabrakło wymaganego stażu"],
  "verdict": "Krótkie podsumowanie zysku punktowego",
  "explanation": "Brudnopis matematyczny: wypisz zdobyte kwoty per wymóg z każdej użytej puli, dodaj kary i zsumuj.",
  "fit_score": <typ int, suma obliczeń z 0 do 100>
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

    return f"""OFERTA PRACY:
Stanowisko: {job_title}
Firma: {job_company}
Wymagania i opis:
{desc}

MOJE CV:
{cv_text}

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
