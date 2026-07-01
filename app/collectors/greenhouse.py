from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import urlopen

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


def fetch_greenhouse_jobs(board_token: str, timeout: int = 20, max_age_days: int = 30) -> list[Job]:
    query = urlencode({"content": "true"})
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?{query}"
    with urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload.get("jobs", []):
        published_at = item.get("first_published") or item.get("updated_at") or ""
        if not _published_within_days(published_at, max_age_days):
            continue

        location = item.get("location") or {}
        departments = item.get("departments") or []
        offices = item.get("offices") or []
        metadata = item.get("metadata") or []
        categories = {
            "departments": ", ".join(department.get("name", "") for department in departments),
            "offices": ", ".join(office.get("name", "") for office in offices),
            "metadata": ", ".join(str(entry.get("value", "")) for entry in metadata),
        }

        jobs.append(
            Job(
                title=item.get("title") or "",
                company=board_token,
                location=location.get("name") or "",
                url=item.get("absolute_url") or "",
                description=strip_html(item.get("content")),
                source="greenhouse",
                published_at=published_at,
                categories=categories,
            )
        )

    return jobs
