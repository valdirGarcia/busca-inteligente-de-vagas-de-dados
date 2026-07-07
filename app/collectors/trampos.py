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


TRAMPOS_API_URL = "https://trampos.co/api/v2/opportunities"
TRAMPOS_SITE_URL = "https://trampos.co"
TRAMPOS_SEARCH_TERMS = [
    "analista de dados",
    "cientista de dados",
    "analista de bi",
    "business intelligence",
    "power bi",
    "analytics",
    "analista de crm",
    "analista de performance",
    "analista de indicadores",
    "analista de planejamento",
    "analista de pricing",
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


def _company(item: dict) -> str:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    return str(item.get("custom_company_name") or company.get("name") or "").strip()


def _location(item: dict) -> str:
    city = str(item.get("city") or "").strip()
    state = str(item.get("state") or "").strip()
    location = ", ".join(part for part in [city, state, "Brasil"] if part)
    if item.get("hybrid"):
        return ", ".join(part for part in ["Hibrido", location] if part)
    return location


def _job_url(item: dict) -> str:
    opportunity_id = str(item.get("id") or "").strip()
    if not opportunity_id:
        return ""
    return f"{TRAMPOS_SITE_URL}/oportunidades/{opportunity_id}-{_slugify(str(item.get('name') or 'vaga'))}"


def _build_job(item: dict, search_term: str, max_age_days: int) -> Job | None:
    title = str(item.get("name") or "").strip()
    published_at = str(item.get("published_at") or "")
    if not title or not _published_within_days(published_at, max_age_days):
        return None

    category = str(item.get("category_name") or "").strip()
    salary = str(item.get("salary") or "").strip()
    signal_text = " ".join([title, category])
    if not looks_like_data_job(signal_text):
        return None

    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    company_description = strip_html(str(company.get("description") or ""))
    description = "\n".join(
        part
        for part in [
            f"Categoria: {category}" if category else "",
            f"Salario: {salary}" if salary else "",
            company_description,
        ]
        if part
    )
    workplace_type = "hybrid" if item.get("hybrid") else ""
    return Job(
        title=title,
        company=_company(item),
        location=_location(item),
        url=_job_url(item),
        description=description,
        source="trampos",
        published_at=published_at,
        categories={
            "trampos_id": str(item.get("id") or ""),
            "search_term": search_term,
            "category": category,
            "type": str(item.get("type_name") or ""),
            "salary": salary,
            "workplace_type": workplace_type,
            "badges": ", ".join(str(badge) for badge in item.get("badges") or []),
        },
    )


def _fetch_page(term: str, page: int, timeout: int) -> dict:
    params = urlencode({"tr": term, "page": page})
    request = Request(
        f"{TRAMPOS_API_URL}?{params}",
        headers={
            "User-Agent": "Mozilla/5.0 busca-vagas-app/0.1",
            "Accept": "application/json",
            "Referer": f"{TRAMPOS_SITE_URL}/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_term_jobs(term: str, pages: int, max_age_days: int, timeout: int) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        payload = _fetch_page(term, page, timeout)
        items = payload.get("opportunities") or []
        if not items:
            break

        page_has_recent_job = False
        for item in items:
            published_at = str(item.get("published_at") or "")
            if not _published_within_days(published_at, max_age_days):
                continue
            page_has_recent_job = True
            job = _build_job(item, term, max_age_days)
            if job:
                jobs.append(job)

        pagination = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        total_pages = int(pagination.get("total_pages") or page)
        if not page_has_recent_job or page >= total_pages:
            break

    return jobs


def fetch_trampos_jobs(
    pages_per_term: int = 2,
    terms: list[str] | None = None,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    jobs: dict[str, Job] = {}
    search_terms = terms or TRAMPOS_SEARCH_TERMS
    safe_pages = max(1, pages_per_term)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_term_jobs, term, safe_pages, max_age_days, timeout): term
            for term in search_terms
        }
        for future in as_completed(futures):
            try:
                term_jobs = future.result()
            except Exception:
                continue
            for job in term_jobs:
                jobs[job.url or f"{job.company}:{job.title}:{job.location}"] = job

    return sorted(jobs.values(), key=lambda job: (job.published_at, job.title), reverse=True)
