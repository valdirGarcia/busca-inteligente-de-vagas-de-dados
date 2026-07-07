from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job


REMOTE_ROCKETSHIP_BASE_URL = "https://www.remoterocketship.com"
REMOTE_ROCKETSHIP_SLUGS = [
    "analista-de-dados",
    "cientista-de-dados",
    "engenheiro-de-dados",
    "analista-de-business-intelligence",
]


def _published_within_days(value: str, max_age_days: int) -> bool:
    if not value:
        return False
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return False
    cutoff = datetime.utcnow() - timedelta(days=max_age_days)
    return published >= cutoff


def _source_url(slug: str) -> str:
    return f"{REMOTE_ROCKETSHIP_BASE_URL}/br/vagas/{slug}/"


def _fetch_slug_payload(slug: str, timeout: int) -> dict:
    request = Request(
        _source_url(slug),
        headers={
            "User-Agent": "Mozilla/5.0 busca-vagas-app/0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")

    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        return {}
    return json.loads(match.group(1)).get("props", {}).get("pageProps", {})


def _company_name(item: dict) -> str:
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    return str(company.get("name") or "").strip()


def _salary_text(item: dict) -> str:
    salary = item.get("salaryRange") if isinstance(item.get("salaryRange"), dict) else {}
    if not salary:
        return ""
    return str(salary.get("salaryHumanReadableText") or "").strip()


def _location(item: dict) -> str:
    location = str(item.get("location") or "").strip()
    location_type = str(item.get("locationType") or "").lower()
    city = str(item.get("locationCity") or "").strip()
    base = city or location or "Brasil"
    if location_type == "remote":
        return ", ".join(part for part in ["Remoto", base] if part)
    if location_type == "hybrid":
        return ", ".join(part for part in ["Hibrido", base] if part)
    return base


def _seniority(item: dict) -> str:
    flags = []
    if item.get("isEntryLevel"):
        flags.append("entry level")
    if item.get("isJunior"):
        flags.append("junior")
    if item.get("isMidLevel"):
        flags.append("pleno")
    if item.get("isSenior"):
        flags.append("senior")
    if item.get("isLead"):
        flags.append("lead")
    return ", ".join(flags)


def _description(item: dict) -> str:
    tech_stack = [str(value) for value in item.get("techStack") or [] if value]
    scoring_keywords = [str(value) for value in item.get("scoringKeywords") or [] if value]
    company = item.get("company") if isinstance(item.get("company"), dict) else {}
    industries = [str(value) for value in company.get("chatGPTIndustries") or [] if value]
    parts = [
        str(item.get("jobDescriptionSummaryBrazil") or item.get("jobDescriptionSummary") or "").strip(),
        str(item.get("twoLineJobDescriptionSummaryBrazil") or item.get("twoLineJobDescriptionSummary") or "").strip(),
        str(item.get("roleDescriptionBrazil") or "").strip(),
        str(item.get("roleRequirementsBrazil") or "").strip(),
        str(item.get("benefitsBrazil") or "").strip(),
        "Stack: " + ", ".join(tech_stack) if tech_stack else "",
        "Palavras-chave: " + ", ".join(scoring_keywords[:12]) if scoring_keywords else "",
        "Setores: " + ", ".join(industries) if industries else "",
        "Salario: " + _salary_text(item) if _salary_text(item) else "",
    ]
    return "\n".join(part for part in parts if part)


def _build_job(item: dict, slug: str, max_age_days: int) -> Job | None:
    title = str(item.get("roleTitleBrazil") or item.get("roleTitle") or "").strip()
    url = str(item.get("url") or "").strip()
    published_at = str(item.get("created_at") or "")
    if not title or not url or not _published_within_days(published_at, max_age_days):
        return None

    description = _description(item)
    category = str(item.get("categorizedJobTitle") or item.get("categorizedJobFunction") or "")
    searchable = " ".join([title, category, description, " ".join(str(value) for value in item.get("techStack") or [])])
    if not looks_like_data_job(searchable):
        return None

    source_url = _source_url(slug)
    return Job(
        title=title,
        company=_company_name(item),
        location=_location(item),
        url=url,
        description=description,
        source="remoterocketship",
        published_at=published_at,
        categories={
            "remoterocketship_id": str(item.get("id") or ""),
            "source_url": source_url,
            "slug": slug,
            "location_type": str(item.get("locationType") or ""),
            "workplace_type": "remote" if str(item.get("locationType") or "").lower() == "remote" else "",
            "categorized_job_title": category,
            "employment_type": str(item.get("employmentType") or ""),
            "seniority": _seniority(item),
            "required_languages": ", ".join(str(value) for value in item.get("requiredLanguages") or []),
            "salary": _salary_text(item),
            "tech_stack": ", ".join(str(value) for value in item.get("techStack") or []),
            "on_linkedin": str(bool(item.get("isOnLinkedIn"))),
        },
    )


def _fetch_slug_jobs(slug: str, max_age_days: int, timeout: int) -> list[Job]:
    payload = _fetch_slug_payload(slug, timeout)
    jobs = []
    for item in payload.get("initialJobOpenings") or []:
        if not _published_within_days(str(item.get("created_at") or ""), max_age_days):
            continue
        job = _build_job(item, slug, max_age_days)
        if job:
            jobs.append(job)
    return jobs


def fetch_remoterocketship_jobs(
    slugs: list[str] | None = None,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    jobs: dict[str, Job] = {}
    selected_slugs = slugs or REMOTE_ROCKETSHIP_SLUGS

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_slug_jobs, slug, max_age_days, timeout): slug for slug in selected_slugs}
        for future in as_completed(futures):
            try:
                slug_jobs = future.result()
            except Exception:
                continue
            for job in slug_jobs:
                jobs[job.url or f"{job.company}:{job.title}:{job.location}"] = job

    return sorted(jobs.values(), key=lambda job: (job.published_at, job.title), reverse=True)
