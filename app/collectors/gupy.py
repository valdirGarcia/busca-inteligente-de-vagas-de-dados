from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


GUPY_API_URL = "https://employability-portal.gupy.io/api/v1/jobs"
GUPY_PORTAL_URL = "https://portal.gupy.io"
GUPY_TITLE_TERMS = [
    "analista de dados",
    "cientista de dados",
    "analista de bi",
    "assistente de bi",
    "business intelligence",
    "data analyst",
    "data scientist",
    "analytics",
    "power bi",
    "engenheiro de dados",
    "analista de planejamento",
    "analista de performance",
    "analista de risco de credito",
    "analista de credito",
    "politicas de credito",
    "analista de indicadores",
    "analista de informacoes gerenciais",
    "analista de inteligencia de mercado",
    "analista de mis",
    "analista de relatorios",
    "analytics engineer",
    "data analytics",
    "data science",
]


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
    parts = [
        str(item.get("city") or "").strip(),
        str(item.get("state") or "").strip(),
        str(item.get("country") or "").strip(),
    ]
    location = ", ".join(dict.fromkeys(part for part in parts if part))
    workplace_type = str(item.get("workplaceType") or "").lower()
    if item.get("isRemoteWork") or workplace_type == "remote":
        return ", ".join(part for part in ["Remoto", location] if part)
    if workplace_type == "hybrid":
        return ", ".join(part for part in ["Hibrido", location] if part)
    return location


def _build_job(item: dict) -> Job | None:
    title = str(item.get("name") or "").strip()
    url = str(item.get("jobUrl") or "").strip()
    if not title or not url:
        return None

    skills = item.get("skills") or []
    skill_names = [str(skill.get("name") if isinstance(skill, dict) else skill) for skill in skills]
    description = strip_html(item.get("description"))
    searchable = " ".join([title, description, " ".join(skill_names)])
    if not looks_like_data_job(searchable):
        return None

    return Job(
        title=title,
        company=str(item.get("careerPageName") or ""),
        location=_location(item),
        url=url,
        description=description,
        source="gupy",
        published_at=str(item.get("publishedDate") or ""),
        categories={
            "career_page": str(item.get("careerPageName") or ""),
            "type": str(item.get("type") or ""),
            "workplace_type": str(item.get("workplaceType") or ""),
            "application_deadline": str(item.get("applicationDeadline") or ""),
            "is_remote": str(bool(item.get("isRemoteWork"))),
            "skills": ", ".join(skill_names),
        },
    )


def _dedupe_key(job: Job) -> str:
    return "|".join(
        part.strip().lower()
        for part in [job.company, job.title, job.location]
        if part.strip()
    )


def _is_newer(candidate: Job, existing: Job) -> bool:
    try:
        candidate_date = datetime.fromisoformat(candidate.published_at.replace("Z", "+00:00"))
        existing_date = datetime.fromisoformat(existing.published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return candidate_date > existing_date


def _fetch_page(term: str, offset: int, limit: int, timeout: int) -> dict:
    params = urlencode({"jobName": term, "limit": limit, "offset": offset})
    request = Request(
        f"{GUPY_API_URL}?{params}",
        headers={
            "User-Agent": "Mozilla/5.0 busca-vagas-app/0.1",
            "Accept": "application/json",
            "Origin": GUPY_PORTAL_URL,
            "Referer": f"{GUPY_PORTAL_URL}/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_gupy_jobs(
    pages_per_term: int = 4,
    terms: list[str] | None = None,
    limit: int = 50,
    max_age_days: int = 30,
    timeout: int = 20,
) -> list[Job]:
    jobs: dict[str, Job] = {}
    search_terms = terms or GUPY_TITLE_TERMS
    safe_pages = max(1, pages_per_term)

    tasks = [
        (term, (page - 1) * limit)
        for term in search_terms
        for page in range(1, safe_pages + 1)
    ]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_page, term, offset, limit, timeout): (term, offset)
            for term, offset in tasks
        }
        for future in as_completed(futures):
            try:
                payload = future.result()
            except Exception:
                continue

            for item in payload.get("data") or []:
                published_at = str(item.get("publishedDate") or "")
                if not _published_within_days(published_at, max_age_days):
                    continue
                job = _build_job(item)
                if job:
                    key = _dedupe_key(job) or job.url
                    if key not in jobs or _is_newer(job, jobs[key]):
                        jobs[key] = job

    return list(jobs.values())
