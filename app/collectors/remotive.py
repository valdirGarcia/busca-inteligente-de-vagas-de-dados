from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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


def fetch_remotive_jobs(category: str = "data", timeout: int = 20, max_age_days: int = 30) -> list[Job]:
    query = urlencode({"category": category})
    url = f"https://remotive.com/api/remote-jobs?{query}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload.get("jobs", []):
        published_at = item.get("publication_date") or ""
        if not _published_within_days(published_at, max_age_days):
            continue

        tags = item.get("tags") or []
        description_parts = [
            item.get("salary") or "",
            " ".join(str(tag) for tag in tags),
            strip_html(item.get("description")),
        ]
        jobs.append(
            Job(
                title=item.get("title") or "",
                company=item.get("company_name") or "",
                location=item.get("candidate_required_location") or "Remote",
                url=item.get("url") or "",
                description="\n".join(description_parts),
                source="remotive",
                published_at=published_at,
                categories={
                    "category": str(item.get("category") or ""),
                    "job_type": str(item.get("job_type") or ""),
                    "tags": ", ".join(str(tag) for tag in tags),
                    "salary": str(item.get("salary") or ""),
                },
            )
        )

    return jobs
