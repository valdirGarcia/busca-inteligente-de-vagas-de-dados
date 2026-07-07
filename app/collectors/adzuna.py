from __future__ import annotations

import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


ADZUNA_API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
ADZUNA_SEARCH_TERMS = [
    "analista de dados",
    "cientista de dados",
    "engenheiro de dados",
    "analytics engineer",
    "analista de bi",
    "business intelligence",
    "power bi",
    "data analyst",
    "data scientist",
    "data engineer",
    "machine learning",
    "analista de indicadores",
    "analista de inteligencia",
    "analista de planejamento",
    "analista de performance",
    "analista de credito",
    "analista de risco de credito",
    "analista de pricing",
]


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dotenv_values() -> dict[str, str]:
    env_path = _project_root() / ".env"
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _credential(name: str) -> str:
    return os.environ.get(name, "").strip() or _dotenv_values().get(name, "").strip()


def has_adzuna_credentials() -> bool:
    return bool(_credential("ADZUNA_APP_ID") and _credential("ADZUNA_APP_KEY"))


def _credentials() -> tuple[str, str]:
    app_id = _credential("ADZUNA_APP_ID")
    app_key = _credential("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError(
            "Adzuna precisa das credenciais ADZUNA_APP_ID e ADZUNA_APP_KEY no ambiente ou no arquivo .env."
        )
    return app_id, app_key


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _location(item: dict) -> str:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    display_name = str(location.get("display_name") or "").strip()
    if display_name:
        return display_name

    area = location.get("area") or []
    if isinstance(area, list):
        return ", ".join(str(part).strip() for part in area if str(part).strip())
    return ""


def _company(item: dict) -> str:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    return str(company.get("display_name") or "").strip()


def _category(item: dict) -> str:
    category = item.get("category") if isinstance(item.get("category"), dict) else {}
    return str(category.get("label") or category.get("tag") or "").strip()


def _salary_text(item: dict) -> str:
    salary_min = item.get("salary_min")
    salary_max = item.get("salary_max")
    if salary_min and salary_max:
        return f"Salario anual estimado: R$ {salary_min} a R$ {salary_max}"
    if salary_min:
        return f"Salario anual estimado a partir de R$ {salary_min}"
    if salary_max:
        return f"Salario anual estimado ate R$ {salary_max}"
    return ""


def _infer_workplace_type(title: str, location: str, description: str) -> str:
    location_text = _normalize(location)
    text = _normalize(" ".join([title, description[:800]]))
    if "home office" in location_text or "remoto" in location_text or "remote" in location_text:
        return "remote"
    if "hibrid" in text or ("home office" in text and "presencial" in text):
        return "hybrid"
    if "home office" in text or "remoto" in text or "remote" in text or "teletrabalho" in text:
        return "remote"
    return ""


def _build_job(item: dict, search_term: str, max_age_days: int) -> Job | None:
    title = str(item.get("title") or "").strip()
    url = str(item.get("redirect_url") or "").strip()
    published_at = str(item.get("created") or "")
    if not title or not url or not _published_within_days(published_at, max_age_days):
        return None

    description = strip_html(str(item.get("description") or ""))
    category = _category(item)
    company = _company(item)
    location = _location(item)
    salary = _salary_text(item)
    searchable = " ".join([title, company, location, category, description])
    if not looks_like_data_job(searchable):
        return None

    workplace_type = _infer_workplace_type(title, location, description)
    if workplace_type == "remote" and "remot" not in _normalize(location) and "home office" not in _normalize(location):
        location = ", ".join(part for part in ["Remoto", location] if part)
    elif workplace_type == "hybrid" and "hibrid" not in _normalize(location):
        location = ", ".join(part for part in ["Hibrido", location] if part)

    return Job(
        title=title,
        company=company,
        location=location,
        url=url,
        description="\n".join(part for part in [description, salary] if part),
        source="adzuna",
        published_at=published_at,
        categories={
            "adzuna_id": str(item.get("id") or ""),
            "search_term": search_term,
            "category": category,
            "contract_type": str(item.get("contract_type") or ""),
            "contract_time": str(item.get("contract_time") or ""),
            "salary": salary,
            "workplace_type": workplace_type,
        },
    )


def _fetch_page(
    term: str,
    page: int,
    country: str,
    results_per_page: int,
    max_age_days: int,
    timeout: int,
) -> dict:
    app_id, app_key = _credentials()
    params = urlencode(
        {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": results_per_page,
            "what": term,
            "sort_by": "date",
            "max_days_old": max_age_days,
            "content-type": "application/json",
        }
    )
    url = f"{ADZUNA_API_URL.format(country=country, page=page)}?{params}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_term_jobs(
    term: str,
    pages: int,
    country: str,
    results_per_page: int,
    max_age_days: int,
    timeout: int,
) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        payload = _fetch_page(term, page, country, results_per_page, max_age_days, timeout)
        items = payload.get("results") or []
        if not items:
            break

        page_has_recent_job = False
        for item in items:
            published_at = str(item.get("created") or "")
            if not _published_within_days(published_at, max_age_days):
                continue
            page_has_recent_job = True
            job = _build_job(item, term, max_age_days)
            if job:
                jobs.append(job)

        if not page_has_recent_job or len(items) < results_per_page:
            break

    return jobs


def fetch_adzuna_jobs(
    pages_per_term: int = 2,
    terms: list[str] | None = None,
    country: str = "br",
    results_per_page: int = 50,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    _credentials()
    jobs: dict[str, Job] = {}
    search_terms = terms or ADZUNA_SEARCH_TERMS
    safe_pages = max(1, pages_per_term)
    safe_results = max(1, min(50, results_per_page))

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_term_jobs, term, safe_pages, country, safe_results, max_age_days, timeout): term
            for term in search_terms
        }
        for future in as_completed(futures):
            term_jobs = future.result()
            for job in term_jobs:
                key = job.url or job.categories.get("adzuna_id") or f"{job.company}:{job.title}:{job.location}"
                jobs[key] = job

    return sorted(jobs.values(), key=lambda job: (job.published_at, job.title), reverse=True)
