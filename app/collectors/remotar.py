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


REMOTAR_API_URL = "https://api.remotar.com.br/jobs"
REMOTAR_SITE_URL = "https://remotar.com.br"
REMOTAR_SEARCH_TERMS = [
    "analista de dados",
    "cientista de dados",
    "engenheiro de dados",
    "analytics",
    "digital analytics",
    "business intelligence",
    "analista de bi",
    "power bi",
    "data analyst",
    "data scientist",
    "data engineer",
    "machine learning",
    "analista de indicadores",
    "analista de insights",
    "analista de inteligencia",
    "analista de planejamento",
    "analista de performance",
    "analista de credito",
    "analista de risco",
    "analista de pricing",
    "analista de crm",
]


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _slugify(value: str) -> str:
    return _normalize(value).replace(" ", "-") or "vaga"


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    names = []
    for value in values:
        if isinstance(value, dict) and value.get("name"):
            names.append(str(value["name"]))
    return names


def _company(item: dict) -> str:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    return str(item.get("companyDisplayName") or company.get("name") or "").strip()


def _company_slug(item: dict) -> str:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    return str(company.get("slug") or _slugify(_company(item)) or "empresa")


def _location(item: dict) -> str:
    city = str(item.get("city") or "").strip()
    state = str(item.get("state") or "").strip()
    location = ", ".join(part for part in [city, state, "Brasil"] if part)
    workplace_type = str(item.get("type") or "").lower()
    if workplace_type == "remote":
        return ", ".join(part for part in ["Remoto", location or "Brasil"] if part)
    if workplace_type == "hybrid":
        return ", ".join(part for part in ["Hibrido", location or "Brasil"] if part)
    return location or "Brasil"


def _job_url(item: dict) -> str:
    external_link = str(item.get("externalLink") or "").strip()
    if external_link:
        return external_link
    title = str(item.get("title") or "vaga")
    return f"{REMOTAR_SITE_URL}/job/{item.get('id')}/{_company_slug(item)}/{_slugify(title)}"


def _salary_text(item: dict) -> str:
    salary = item.get("jobSalary") if isinstance(item.get("jobSalary"), dict) else {}
    if salary:
        salary_type = str(salary.get("type") or "").strip()
        salary_from = salary.get("from")
        salary_to = salary.get("to")
        currency = str(salary.get("currency") or "BRL").strip()
        if salary_from and salary_to:
            return f"Salario {salary_type}: {currency} {salary_from} a {salary_to}"
        if salary_from:
            return f"Salario {salary_type}: a partir de {currency} {salary_from}"
        if salary_to:
            return f"Salario {salary_type}: ate {currency} {salary_to}"
    return ""


def _build_job(item: dict, search_term: str, max_age_days: int) -> Job | None:
    title = str(item.get("title") or "").strip()
    published_at = str(item.get("createdAt") or "")
    if not title or not _published_within_days(published_at, max_age_days):
        return None

    tags = _names(item.get("jobTags"))
    categories = _names(item.get("jobCategories"))
    subtitle = str(item.get("subtitle") or "").strip()
    type_text = str(item.get("type") or "").strip()
    signal_text = " ".join([title, subtitle, " ".join(tags), " ".join(categories)])
    if not looks_like_data_job(signal_text):
        return None

    description = "\n".join(
        part
        for part in [
            subtitle,
            strip_html(item.get("description")),
            "Categorias: " + ", ".join(categories) if categories else "",
            "Tags: " + ", ".join(tags) if tags else "",
            _salary_text(item),
        ]
        if part
    )
    return Job(
        title=title,
        company=_company(item),
        location=_location(item),
        url=_job_url(item),
        description=description,
        source="remotar",
        published_at=published_at,
        categories={
            "remotar_id": str(item.get("id") or ""),
            "source_url": f"{REMOTAR_SITE_URL}/job/{item.get('id')}/{_company_slug(item)}/{_slugify(title)}",
            "search_term": search_term,
            "workplace_type": "hybrid" if type_text == "hybrid" else "remote" if type_text == "remote" else type_text,
            "integration_source": str(item.get("integrationSource") or ""),
            "tags": ", ".join(tags),
            "categories": ", ".join(categories),
        },
    )


def _fetch_page(query: dict[str, object], timeout: int) -> dict:
    params = urlencode({key: value for key, value in query.items() if value not in (None, "")})
    request = Request(
        f"{REMOTAR_API_URL}?{params}",
        headers={
            "User-Agent": "Mozilla/5.0 busca-vagas-app/0.1",
            "Accept": "application/json",
            "Origin": REMOTAR_SITE_URL,
            "Referer": f"{REMOTAR_SITE_URL}/search/jobs",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_query_jobs(query: dict[str, object], label: str, pages: int, max_age_days: int, timeout: int) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        payload = _fetch_page({**query, "page": page}, timeout)
        items = payload.get("data") or []
        if not items:
            break

        page_has_recent_job = False
        for item in items:
            published_at = str(item.get("createdAt") or "")
            if not _published_within_days(published_at, max_age_days):
                continue
            page_has_recent_job = True
            job = _build_job(item, label, max_age_days)
            if job:
                jobs.append(job)

        if not page_has_recent_job or len(items) < 50:
            break
    return jobs


def _dedupe_key(job: Job) -> str:
    company_title_location = "|".join(
        _normalize(part)
        for part in [job.company, job.title, job.location]
        if _normalize(part)
    )
    return company_title_location or job.url


def fetch_remotar_jobs(
    pages_per_term: int = 3,
    terms: list[str] | None = None,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    jobs: dict[str, Job] = {}
    search_terms = terms or REMOTAR_SEARCH_TERMS
    safe_pages = max(1, pages_per_term)
    queries = [({"search": term}, term) for term in search_terms]
    if terms is None:
        queries.append(({"categoryId": 4}, "categoria data science/analytics"))

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(_fetch_query_jobs, query, label, safe_pages, max_age_days, timeout): label
            for query, label in queries
        }
        for future in as_completed(futures):
            try:
                query_jobs = future.result()
            except Exception:
                continue
            for job in query_jobs:
                jobs[_dedupe_key(job)] = job

    return sorted(jobs.values(), key=lambda job: (job.published_at, job.title), reverse=True)
