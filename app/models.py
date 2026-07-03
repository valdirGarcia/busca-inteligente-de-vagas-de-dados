from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    name: str
    priority_roles: list[str]
    target_roles: list[str]
    seniority: list[str]
    locations: list[str]
    languages: list[str]
    core_skills: list[str]
    nice_to_have_skills: list[str]
    business_domains: list[str]
    industries: list[str]
    avoid: list[str]
    min_junior_salary_brl: int | None
    flexible_junior_roles: list[str]
    match_settings: dict[str, int]

    @property
    def all_skills(self) -> list[str]:
        return [*self.core_skills, *self.nice_to_have_skills]


@dataclass(frozen=True)
class Job:
    title: str
    company: str
    location: str
    url: str
    description: str
    source: str
    published_at: str = ""
    categories: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MatchResult:
    job: Job
    score: int
    matched_skills: list[str]
    matched_domains: list[str]
    gaps: list[str]
    reasons: list[str]
    score_details: list[dict[str, object]]
