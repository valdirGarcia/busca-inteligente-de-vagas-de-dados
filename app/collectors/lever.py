from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.request import urlopen

from app.models import Job


def _created_at_to_iso(value: object) -> str:
    if not value:
        return ""
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat()


def fetch_lever_jobs(company_slug: str, timeout: int = 20) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    with urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload:
        categories = item.get("categories") or {}
        lists = item.get("lists") or []
        description_parts = [item.get("descriptionPlain") or ""]
        for block in lists:
            description_parts.append(block.get("text") or "")
            description_parts.extend(block.get("content") or [])

        jobs.append(
            Job(
                title=item.get("text") or "",
                company=company_slug,
                location=categories.get("location") or "",
                url=item.get("hostedUrl") or item.get("applyUrl") or "",
                description="\n".join(description_parts),
                source="lever",
                published_at=_created_at_to_iso(item.get("createdAt")),
                categories={key: str(value) for key, value in categories.items()},
            )
        )

    return jobs
