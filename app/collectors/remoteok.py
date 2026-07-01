from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def fetch_remoteok_jobs(timeout: int = 20, max_age_days: int = 30) -> list[Job]:
    request = Request(
        "https://remoteok.com/api",
        headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload[1:]:
        published_at = item.get("date") or ""
        if not _published_within_days(published_at, max_age_days):
            continue

        tags = item.get("tags") or []
        title = item.get("position") or ""
        searchable = " ".join([title, " ".join(str(tag) for tag in tags)])
        if not looks_like_data_job(searchable):
            continue

        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        salary = ""
        if salary_min or salary_max:
            salary = f"{salary_min or ''}-{salary_max or ''}".strip("-")

        jobs.append(
            Job(
                title=title,
                company=item.get("company") or "",
                location=item.get("location") or "Remote",
                url=item.get("apply_url") or item.get("url") or "",
                description="\n".join([salary, " ".join(str(tag) for tag in tags), strip_html(item.get("description"))]),
                source="remoteok",
                published_at=published_at,
                categories={
                    "tags": ", ".join(str(tag) for tag in tags),
                    "salary": salary,
                },
            )
        )

    return jobs
