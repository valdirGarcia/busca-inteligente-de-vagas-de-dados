from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.models import Job
from app.text_utils import strip_html


def fetch_remotive_jobs(category: str = "data", timeout: int = 20) -> list[Job]:
    query = urlencode({"category": category})
    url = f"https://remotive.com/api/remote-jobs?{query}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload.get("jobs", []):
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
                published_at=item.get("publication_date") or "",
                categories={
                    "category": str(item.get("category") or ""),
                    "job_type": str(item.get("job_type") or ""),
                    "tags": ", ".join(str(tag) for tag in tags),
                    "salary": str(item.get("salary") or ""),
                },
            )
        )

    return jobs
