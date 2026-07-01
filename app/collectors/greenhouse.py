from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import urlopen

from app.models import Job
from app.text_utils import strip_html


def fetch_greenhouse_jobs(board_token: str, timeout: int = 20) -> list[Job]:
    query = urlencode({"content": "true"})
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?{query}"
    with urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload.get("jobs", []):
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
                published_at=item.get("first_published") or item.get("updated_at") or "",
                categories=categories,
            )
        )

    return jobs
