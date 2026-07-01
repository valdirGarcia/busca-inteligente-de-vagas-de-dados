from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


def _timestamp_to_iso(value: object) -> str:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def fetch_arbeitnow_jobs(pages: int = 5, timeout: int = 20) -> list[Job]:
    jobs = []
    for page in range(1, pages + 1):
        query = urlencode({"page": page})
        request = Request(
            f"https://www.arbeitnow.com/api/job-board-api?{query}",
            headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1"},
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for item in payload.get("data", []):
            tags = item.get("tags") or []
            title = item.get("title") or ""
            searchable = " ".join([title, " ".join(str(tag) for tag in tags)])
            if not looks_like_data_job(searchable):
                continue

            remote = "Remote" if item.get("remote") else ""
            location = ", ".join(part for part in [item.get("location") or "", remote] if part)
            jobs.append(
                Job(
                    title=title,
                    company=item.get("company_name") or "",
                    location=location,
                    url=item.get("url") or "",
                    description="\n".join([" ".join(str(tag) for tag in tags), strip_html(item.get("description"))]),
                    source="arbeitnow",
                    published_at=_timestamp_to_iso(item.get("created_at")),
                    categories={
                        "tags": ", ".join(str(tag) for tag in tags),
                        "remote": str(bool(item.get("remote"))),
                    },
                )
            )

    return jobs
