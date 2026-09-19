from __future__ import annotations

import json

import pytest

from job_sniffer.llm.prompts import (
    build_evaluation_user_prompt,
    parse_evaluation_json,
)


def test_build_evaluation_user_prompt() -> None:
    prompt = build_evaluation_user_prompt(
        cv_text="Doświadczony programista Python, FastAPI, Docker.",
        job_title="Senior Python Backend Developer",
        job_description="Wymagamy 5 lat doświadczenia w Python i znajomości relacyjnych baz danych.",
        job_company="TechCorp",
    )
    assert "TechCorp" in prompt
    assert "Senior Python Backend Developer" in prompt
    assert "Doświadczony programista Python" in prompt


def test_parse_evaluation_json_clean() -> None:
    raw = '{"fit_score": 85, "verdict": "Wysoka szansa", "summary": "Rola Python", "strengths": ["Python"], "weaknesses": ["Brak AWS"]}'
    data = parse_evaluation_json(raw)
    assert data["fit_score"] == 85
    assert data["verdict"] == "Wysoka szansa"
    assert data["strengths"] == ["Python"]


def test_parse_evaluation_json_markdown_wrapped() -> None:
    raw = """Oto ocena kandydata:
```json
{
  "fit_score": 90,
  "verdict": "Bardzo wysoka szansa",
  "summary": "Projektowanie systemów",
  "strengths": ["Doświadczenie"],
  "weaknesses": []
}
```
Mam nadzieję, że to pomoże!"""
    data = parse_evaluation_json(raw)
    assert data["fit_score"] == 90
    assert data["verdict"] == "Bardzo wysoka szansa"


def test_parse_evaluation_json_invalid() -> None:
    with pytest.raises((ValueError, TypeError, json.JSONDecodeError)):
        parse_evaluation_json("Niepoprawny tekst bez klamer JSON")
