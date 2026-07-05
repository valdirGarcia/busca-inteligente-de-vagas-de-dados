from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


SMARTRECRUITERS_API_URL = "https://api.smartrecruiters.com/v1/companies"


def _get_json(url: str, timeout: int) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 busca-vagas-app/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _label(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("label") or value.get("name") or value.get("id") or "")
    return ""


def _custom_field_values(item: dict) -> list[str]:
    values = []
    for field in item.get("customField") or []:
        if not isinstance(field, dict):
            continue
        for key in ("fieldLabel", "valueLabel", "valueId"):
            if field.get(key):
                values.append(str(field[key]))
    return values


def _location(item: dict) -> str:
    location = item.get("location") if isinstance(item.get("location"), dict) else {}
    full_location = str(location.get("fullLocation") or "").strip()
    if not full_location:
        full_location = ", ".join(
            str(part)
            for part in [location.get("city"), location.get("region"), location.get("country")]
            if part
        )

    prefixes = []
    if location.get("remote"):
        prefixes.append("Remote")
    if location.get("hybrid"):
        prefixes.append("Hybrid")
    return ", ".join(dict.fromkeys([*prefixes, full_location]))


def _description(item: dict) -> str:
    sections = ((item.get("jobAd") or {}).get("sections") or {}) if isinstance(item.get("jobAd"), dict) else {}
    parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key)
        if isinstance(section, dict):
            parts.append(strip_html(section.get("text")))
    return "\n".join(part for part in parts if part)


def _categories(item: dict) -> dict[str, str]:
    return {
        "industry": _label(item.get("industry")),
        "department": _label(item.get("department")),
        "function": _label(item.get("function")),
        "employment_type": _label(item.get("typeOfEmployment")),
        "experience_level": _label(item.get("experienceLevel")),
        "custom_fields": ", ".join(_custom_field_values(item)),
    }


def _looks_relevant(item: dict) -> bool:
    searchable = " ".join(
        [
            str(item.get("name") or ""),
            _label(item.get("industry")),
            _label(item.get("department")),
            _label(item.get("function")),
            _label(item.get("experienceLevel")),
            " ".join(_custom_field_values(item)),
        ]
    )
    return looks_like_data_job(searchable)


def _fetch_detail(company_slug: str, item: dict, timeout: int) -> dict:
    detail_url = item.get("ref") or f"{SMARTRECRUITERS_API_URL}/{company_slug}/postings/{item.get('id')}"
    try:
        detail = _get_json(str(detail_url), timeout)
    except Exception:
        return item
    return {**item, **detail}


def _build_job(company_slug: str, item: dict) -> Job:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    categories = _categories(item)
    posting_url = str(item.get("postingUrl") or item.get("applyUrl") or "")
    if not posting_url and item.get("id"):
        posting_url = f"https://jobs.smartrecruiters.com/{company_slug}/{item['id']}"

    return Job(
        title=str(item.get("name") or ""),
        company=str(company.get("name") or company_slug),
        location=_location(item),
        url=posting_url,
        description=_description(item),
        source="smartrecruiters",
        published_at=str(item.get("releasedDate") or item.get("postedDate") or ""),
        categories=categories,
    )


def fetch_smartrecruiters_jobs(
    company_slug: str,
    pages: int = 5,
    limit: int = 100,
    timeout: int = 20,
    max_age_days: int = 7,
) -> list[Job]:
    candidate_items: dict[str, dict] = {}
    safe_pages = max(1, min(pages, 10))
    for page in range(safe_pages):
        query = urlencode({"limit": limit, "offset": page * limit})
        payload = _get_json(f"{SMARTRECRUITERS_API_URL}/{company_slug}/postings?{query}", timeout)
        items = payload.get("content") or []
        if not items:
            break
        for item in items:
            published_at = str(item.get("releasedDate") or item.get("postedDate") or "")
            if not _published_within_days(published_at, max_age_days):
                continue
            if _looks_relevant(item):
                candidate_items[str(item.get("id") or item.get("uuid") or len(candidate_items))] = item

    jobs: dict[str, Job] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_detail, company_slug, item, timeout): item_id
            for item_id, item in candidate_items.items()
        }
        for future in as_completed(futures):
            detail = future.result()
            job = _build_job(company_slug, detail)
            if job.title and job.url:
                jobs[job.url] = job

    return list(jobs.values())
