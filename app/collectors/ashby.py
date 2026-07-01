from __future__ import annotations

import json
from datetime import datetime, timedelta
from urllib.parse import quote
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


ASHBY_API_URL = "https://api.ashbyhq.com/posting-api/job-board"


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _location(item: dict) -> str:
    locations = [str(item.get("location") or "").strip()]
    for secondary in item.get("secondaryLocations") or []:
        if isinstance(secondary, dict) and secondary.get("location"):
            locations.append(str(secondary["location"]).strip())

    clean_locations = [location for location in locations if location]
    location_text = ", ".join(dict.fromkeys(clean_locations))
    workplace_type = str(item.get("workplaceType") or "").strip()
    if item.get("isRemote") or workplace_type.lower() == "remote":
        return ", ".join(part for part in ["Remote", location_text] if part)
    if workplace_type.lower() == "hybrid":
        return ", ".join(part for part in ["Hybrid", location_text] if part)
    return location_text


def _compensation_text(item: dict) -> str:
    values = []
    for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary"):
        if item.get(key):
            values.append(str(item[key]))
    compensation = item.get("compensation")
    if isinstance(compensation, dict):
        for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary"):
            if compensation.get(key):
                values.append(str(compensation[key]))
    return "\n".join(dict.fromkeys(values))


def fetch_ashby_jobs(board_name: str, timeout: int = 20, max_age_days: int = 30) -> list[Job]:
    url = f"{ASHBY_API_URL}/{quote(board_name)}?includeCompensation=true"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    jobs = []
    for item in payload.get("jobs", []):
        if item.get("isListed") is False:
            continue
        published_at = str(item.get("publishedAt") or "")
        if not _published_within_days(published_at, max_age_days):
            continue

        description = item.get("descriptionPlain") or strip_html(item.get("descriptionHtml"))
        compensation = _compensation_text(item)
        searchable = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("department") or ""),
                str(item.get("team") or ""),
            ]
        )
        if not looks_like_data_job(searchable):
            continue

        jobs.append(
            Job(
                title=str(item.get("title") or ""),
                company=board_name,
                location=_location(item),
                url=str(item.get("jobUrl") or item.get("applyUrl") or ""),
                description="\n".join(part for part in [description, compensation] if part),
                source="ashby",
                published_at=published_at,
                categories={
                    "department": str(item.get("department") or ""),
                    "team": str(item.get("team") or ""),
                    "employment_type": str(item.get("employmentType") or ""),
                    "workplace_type": str(item.get("workplaceType") or ""),
                    "is_remote": str(bool(item.get("isRemote"))),
                    "compensation": compensation,
                },
            )
        )

    return jobs
