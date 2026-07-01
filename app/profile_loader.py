from __future__ import annotations

from pathlib import Path

import yaml

from app.models import Profile


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def load_profile(path: str | Path) -> Profile:
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    skills = raw.get("skills", {})
    salary_preferences = raw.get("salary_preferences", {})
    min_junior_salary = salary_preferences.get("junior_min_monthly_brl")
    match_settings = raw.get("match_settings", {})

    return Profile(
        name=str(raw.get("name", "")),
        priority_roles=_as_list(raw.get("priority_roles")),
        target_roles=_as_list(raw.get("target_roles")),
        seniority=_as_list(raw.get("seniority")),
        locations=_as_list(raw.get("locations")),
        languages=_as_list(raw.get("languages")),
        core_skills=_as_list(skills.get("core")),
        nice_to_have_skills=_as_list(skills.get("nice_to_have")),
        business_domains=_as_list(raw.get("business_domains")),
        industries=_as_list(raw.get("industries")),
        avoid=_as_list(raw.get("avoid")),
        min_junior_salary_brl=int(min_junior_salary) if min_junior_salary else None,
        flexible_junior_roles=_as_list(salary_preferences.get("flexible_junior_roles")),
        match_settings={str(key): int(value) for key, value in match_settings.items()},
    )
