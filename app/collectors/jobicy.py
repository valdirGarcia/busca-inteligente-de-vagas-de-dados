from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


JOBICY_API_URL = "https://jobicy.com/api/v2/remote-jobs"


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _salary_text(item: dict) -> str:
    salary_min = item.get("salaryMin")
    salary_max = item.get("salaryMax")
    currency = str(item.get("salaryCurrency") or "").strip()
    period = str(item.get("salaryPeriod") or "").strip()
    if salary_min and salary_max:
        return f"Salary: {currency} {salary_min} to {salary_max} {period}".strip()
    if salary_min:
        return f"Salary from: {currency} {salary_min} {period}".strip()
    if salary_max:
        return f"Salary up to: {currency} {salary_max} {period}".strip()
    return ""


def _build_job(item: dict, geo_filter: str) -> Job | None:
    title = str(item.get("jobTitle") or "").strip()
    url = str(item.get("url") or "").strip()
    if not title or not url:
        return None

    description = strip_html(item.get("jobDescription"))
    excerpt = strip_html(item.get("jobExcerpt"))
    industry = str(item.get("jobIndustry") or "").strip()
    searchable = " ".join([title, description, excerpt, industry])
    if not looks_like_data_job(searchable):
        return None

    salary = _salary_text(item)
    location = str(item.get("jobGeo") or "Anywhere").strip()
    if not location.lower().startswith("remoto"):
        location = ", ".join(part for part in ["Remoto", location] if part)
    description_parts = [
        description,
        excerpt,
        f"Industry: {industry}" if industry else "",
        f"Level: {item.get('jobLevel')}" if item.get("jobLevel") else "",
        f"Type: {item.get('jobType')}" if item.get("jobType") else "",
        salary,
    ]

    return Job(
        title=title,
        company=str(item.get("companyName") or ""),
        location=location,
        url=url,
        description="\n".join(part for part in description_parts if part),
        source="jobicy",
        published_at=str(item.get("pubDate") or ""),
        categories={
            "industry": industry,
            "job_type": str(item.get("jobType") or ""),
            "job_level": str(item.get("jobLevel") or ""),
            "geo": str(item.get("jobGeo") or ""),
            "geo_filter": geo_filter,
            "workplace_type": "remote",
            "salary": salary,
        },
    )


def fetch_jobicy_jobs(
    geo: str | None = None,
    count: int = 100,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    geo_filter = (geo or "").strip().lower()
    params = {
        "count": max(1, min(count, 100)),
        "industry": "data-science",
    }
    if geo_filter and geo_filter not in {"all", "any", "global"}:
        params["geo"] = geo_filter

    request = Request(
        f"{JOBICY_API_URL}?{urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs: dict[str, Job] = {}
    for item in payload.get("jobs") or []:
        published_at = str(item.get("pubDate") or "")
        if not _published_within_days(published_at, max_age_days):
            continue
        job = _build_job(item, geo_filter or "all")
        if job:
            jobs[job.url] = job
    return list(jobs.values())
