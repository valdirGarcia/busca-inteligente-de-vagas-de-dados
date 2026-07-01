from __future__ import annotations

import json
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


def fetch_remoteok_jobs(timeout: int = 20) -> list[Job]:
    request = Request(
        "https://remoteok.com/api",
        headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload[1:]:
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
                published_at=item.get("date") or "",
                categories={
                    "tags": ", ".join(str(tag) for tag in tags),
                    "salary": salary,
                },
            )
        )

    return jobs
