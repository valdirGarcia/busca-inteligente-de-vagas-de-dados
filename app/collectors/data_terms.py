from __future__ import annotations

import re
import unicodedata


DATA_JOB_TERMS = [
    "data analyst",
    "analista de dados",
    "analista de dados junior",
    "analista de dados jr",
    "analista de dados pleno",
    "analista de dados pl",
    "assistente de dados",
    "auxiliar de dados",
    "data scientist",
    "cientista de dados",
    "cientista de dados junior",
    "cientista de dados jr",
    "cientista de dados pleno",
    "cientista de dados pl",
    "analytics engineer",
    "analytics consultant",
    "analista de analytics",
    "analista analytics",
    "business intelligence",
    "bi analyst",
    "bi consultant",
    "analista de bi",
    "analista bi",
    "analista de business intelligence",
    "analista power bi",
    "assistente de bi",
    "data engineer",
    "engenheiro de dados",
    "data analytics",
    "data science",
    "data visualization",
    "machine learning",
    "ml engineer",
    "risk analyst",
    "risk analytics",
    "analista de risco de credito",
    "credit risk analyst",
    "credit analyst",
    "analista de credito",
    "analista de politicas de credito",
    "politicas de credito",
    "analista de fraude",
    "analista antifraude",
    "fraud analyst",
    "planning analyst",
    "analista de planejamento",
    "performance analyst",
    "analista de performance",
    "power bi",
    "analytics",
    "reporting analyst",
    "analista de relatorios",
    "analista de indicadores",
    "analista de inteligencia de dados",
    "analista de inteligencia",
    "analista de informacoes gerenciais",
    "analista de informacoes",
    "analista de inteligencia comercial",
    "analista de inteligencia de negocios",
    "analista de inteligencia de mercado",
    "analista de mis",
    "analista de crm",
    "analista de pricing",
    "analista de growth",
    "dashboard",
    "dashboards",
    "growth analyst",
    "business analyst",
    "analista de negocios",
    "analista de negocio",
    "product data analyst",
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
