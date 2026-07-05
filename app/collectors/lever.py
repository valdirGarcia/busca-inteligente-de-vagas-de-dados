from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def fetch_lever_jobs(company_slug: str, timeout: int = 20, max_age_days: int = 7) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company_slug}?mode=json"
    with urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload:
        published_at = _created_at_to_iso(item.get("createdAt"))
        if not _published_within_days(published_at, max_age_days):
            continue

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
                published_at=published_at,
                categories={key: str(value) for key, value in categories.items()},
            )
        )

    return jobs
