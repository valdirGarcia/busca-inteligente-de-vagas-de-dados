from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


SOLIDES_API_URL = "https://apigw.solides.com.br/jobs/v3/portal-vacancies-new"
SOLIDES_PORTAL_URL = "https://vagas.solides.com.br"
SOLIDES_TITLE_TERMS = [
    "dados",
    "analista de dados",
    "analista de dados junior",
    "analista de dados jr",
    "analista de dados pleno",
    "analista de dados pl",
    "assistente de dados",
    "auxiliar de dados",
    "cientista de dados",
    "cientista de dados junior",
    "cientista de dados jr",
    "cientista de dados pleno",
    "cientista de dados pl",
    "analista de bi",
    "analista bi",
    "analista de business intelligence",
    "analista power bi",
    "assistente de bi",
    "business intelligence",
    "data analyst",
    "data scientist",
    "analytics",
    "analista de analytics",
    "analista analytics",
    "power bi",
    "dashboard",
    "dashboards",
    "sql",
    "engenheiro de dados",
    "analista de planejamento",
    "analista de performance",
    "analista de risco de credito",
    "credit risk analyst",
    "risk analytics",
    "analista de credito",
    "politicas de credito",
    "analista de fraude",
    "analista antifraude",
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
    "growth analyst",
    "business analyst",
    "analista de negocios",
    "analista de relatorios",
    "analytics engineer",
    "product data analyst",
    "data analytics",
    "data science",
]


def _slugify(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", without_accents)
    return slug.strip("-") or "vaga"


def _portal_url(item: dict, title: str) -> str:
    vacancy_id = str(item.get("id") or "").strip()
    if not vacancy_id:
        return str(item.get("redirectLink") or "").strip()
    return f"{SOLIDES_PORTAL_URL}/vaga/{vacancy_id}/{_slugify(title)}"


def _names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    names = []
    for value in values:
        if isinstance(value, dict) and value.get("name"):
            names.append(str(value["name"]))
    return names


def _location(item: dict) -> str:
    city = item.get("city") if isinstance(item.get("city"), dict) else {}
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    address = item.get("address") if isinstance(item.get("address"), dict) else {}
    country = address.get("country") if isinstance(address.get("country"), dict) else {}

    parts = [
        city.get("name"),
        state.get("code") or state.get("name"),
        country.get("name"),
    ]
    location = ", ".join(str(part) for part in parts if part)
    if item.get("homeOffice") or str(item.get("jobType") or "").lower() == "remoto":
        return ", ".join(part for part in ["Remoto", location] if part)
    return location


def _salary_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    if not value.get("showRangeToApplicant"):
        return "Salario nao divulgado"
    initial = value.get("initialRange")
    final = value.get("finalRange")
    if initial and final:
        return f"Salario: R$ {initial} a R$ {final}"
    if initial:
        return f"Salario inicial: R$ {initial}"
    if final:
        return f"Salario ate: R$ {final}"
    if value.get("negotiable"):
        return "Salario negociavel"
    return ""


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _build_job(item: dict) -> Job | None:
    title = str(item.get("title") or "").strip()
    url = _portal_url(item, title)
    if not title or not url:
        return None

    hard_skills = _names(item.get("hardSkills"))
    occupation_areas = _names(item.get("occupationAreas"))
    seniority = _names(item.get("seniority"))
    contracts = _names(item.get("recruitmentContractType"))
    benefits = _names(item.get("benefits"))
    salary = _salary_text(item.get("salary"))
    description = "\n".join(
        part
        for part in [
            strip_html(item.get("description")),
            "Hard skills: " + ", ".join(hard_skills) if hard_skills else "",
            "Areas: " + ", ".join(occupation_areas) if occupation_areas else "",
            "Senioridade: " + ", ".join(seniority) if seniority else "",
            "Contrato: " + ", ".join(contracts) if contracts else "",
            "Beneficios: " + ", ".join(benefits[:8]) if benefits else "",
            salary,
        ]
        if part
    )
    searchable = " ".join([title, " ".join(hard_skills), " ".join(occupation_areas), description])
    if not looks_like_data_job(searchable):
        return None

    return Job(
        title=title,
        company=str(item.get("companyName") or ""),
        location=_location(item),
        url=url,
        description=description,
        source="solides",
        published_at=str(item.get("createdAt") or ""),
        categories={
            "job_type": str(item.get("jobType") or ""),
            "direct_link": str(item.get("redirectLink") or ""),
            "hard_skills": ", ".join(hard_skills),
            "occupation_areas": ", ".join(occupation_areas),
            "seniority": ", ".join(seniority),
            "contract": ", ".join(contracts),
            "salary": salary,
        },
    )


def _fetch_page(term: str, page: int, limit: int, timeout: int) -> list[dict]:
    params = urlencode({"page": page, "limit": limit, "title": term})
    request = Request(
        f"{SOLIDES_API_URL}?{params}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126 Safari/537.36"
            ),
            "Accept": "application/json",
            "Origin": "https://vagas.solides.com.br",
            "Referer": "https://vagas.solides.com.br/vagas",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data") or {}
    return data.get("data") or []


def _fetch_term_jobs(term: str, pages: int, limit: int, max_age_days: int, timeout: int) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        items = _fetch_page(term, page, limit, timeout)
        if not items:
            break

        page_has_recent_job = False
        for item in items:
            published_at = str(item.get("createdAt") or "")
            if not _published_within_days(published_at, max_age_days):
                continue
            page_has_recent_job = True
            job = _build_job(item)
            if job:
                jobs.append(job)

        if not page_has_recent_job:
            break
        if len(items) < limit:
            break
    return jobs


def fetch_solides_jobs(
    pages_per_term: int = 20,
    terms: list[str] | None = None,
    limit: int = 10,
    max_age_days: int = 7,
    timeout: int = 12,
) -> list[Job]:
    jobs: dict[str, Job] = {}
    search_terms = terms or SOLIDES_TITLE_TERMS

    safe_pages = max(1, pages_per_term)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_term_jobs, term, safe_pages, limit, max_age_days, timeout): term
            for term in search_terms
        }
        for future in as_completed(futures):
            try:
                term_jobs = future.result()
            except Exception:
                continue

            for job in term_jobs:
                jobs[job.url] = job

    return list(jobs.values())
