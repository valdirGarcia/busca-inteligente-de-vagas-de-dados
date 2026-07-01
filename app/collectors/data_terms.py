from __future__ import annotations

import re
import unicodedata


DATA_JOB_TERMS = [
    "data analyst",
    "analista de dados",
    "data scientist",
    "cientista de dados",
    "analytics engineer",
    "analytics consultant",
    "business intelligence",
    "bi analyst",
    "bi consultant",
    "analista de bi",
    "assistente de bi",
    "data engineer",
    "engenheiro de dados",
    "data analytics",
    "data science",
    "data visualization",
    "machine learning",
    "ml engineer",
    "risk analyst",
    "analista de risco de credito",
    "credit analyst",
    "analista de credito",
    "analista de politicas de credito",
    "politicas de credito",
    "planning analyst",
    "analista de planejamento",
    "performance analyst",
    "analista de performance",
    "power bi",
    "analytics",
    "reporting analyst",
    "analista de relatorios",
    "analista de indicadores",
    "analista de informacoes gerenciais",
    "analista de inteligencia de mercado",
    "analista de mis",
]


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def looks_like_data_job(text: str) -> bool:
    normalized = _normalize(text)
    return any(
        re.search(r"(?<![a-z0-9])" + re.escape(_normalize(term)) + r"(?![a-z0-9])", normalized)
        for term in DATA_JOB_TERMS
    )
