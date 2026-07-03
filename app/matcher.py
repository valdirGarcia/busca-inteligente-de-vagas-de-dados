from __future__ import annotations

import re
import unicodedata

from app.models import Job, MatchResult, Profile


DEFAULT_MATCH_SETTINGS = {
    "min_score_to_show": 1,
    "min_score_to_store": 1,
    "max_job_age_days_to_store": 30,
    "core_skills_weight": 35,
    "nice_to_have_skills_weight": 10,
    "business_domain_weight": 10,
    "priority_role_weight": 30,
    "target_role_weight": 25,
    "seniority_weight": 5,
    "location_weight": 10,
    "missing_location_penalty": -20,
    "no_role_penalty": -25,
    "avoid_penalty": -25,
    "junior_salary_bonus": 5,
    "junior_salary_penalty": -10,
}


def match_setting(profile: Profile, key: str) -> int:
    return int(profile.match_settings.get(key, DEFAULT_MATCH_SETTINGS[key]))


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _contains(text: str, term: str) -> bool:
    return _contains_normalized(_normalize(text), term)


def _contains_normalized(normalized_text: str, term: str) -> bool:
    normalized_term = _normalize(term)
    if len(normalized_term) <= 1:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(normalized_term) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def _location_match(profile: Profile, job: Job) -> bool:
    normalized_location = _normalize(job.location)
    normalized_categories = _normalize(" ".join(job.categories.values()))
    if any(_contains_normalized(normalized_location, location) for location in profile.locations):
        return True

    remote_terms = [
        "remote",
        "remoto",
        "home office",
        "work from home",
        "anywhere",
        "worldwide",
    ]
    acceptable_remote_regions = [
        "worldwide",
        "anywhere",
        "brazil",
        "brasil",
        "latam",
        "latin america",
        "south america",
        "americas",
    ]
    remote_signal_text = _normalize(" ".join([job.title, job.location, " ".join(job.categories.values())]))
    remote_region_text = normalized_location
    has_remote_signal = job.source == "remotive" or any(
        _contains_normalized(remote_signal_text, term) for term in remote_terms
    )
    if not has_remote_signal:
        return False

    if normalized_location in {"", "remote", "remoto"}:
        return True

    if any(_contains_normalized(remote_region_text, region) for region in acceptable_remote_regions):
        return True

    return any(_contains_normalized(normalized_categories, region) for region in acceptable_remote_regions)


def _extract_brl_values(text: str) -> list[int]:
    values = []
    for match in re.finditer(r"(?:r\$|brl)\s*([0-9][0-9.,]*)", text.lower()):
        raw_value = match.group(1).replace(".", "").replace(",", ".")
        try:
            values.append(round(float(raw_value)))
        except ValueError:
            continue
    return values


def score_job(profile: Profile, job: Job) -> MatchResult:
    searchable = " ".join(
        [
            job.title,
            job.company,
            job.location,
            job.description,
            " ".join(job.categories.values()),
        ]
    )
    normalized_searchable = _normalize(searchable)
    normalized_title = _normalize(job.title)

    matched_core = [skill for skill in profile.core_skills if _contains_normalized(normalized_searchable, skill)]
    matched_nice = [
        skill for skill in profile.nice_to_have_skills if _contains_normalized(normalized_searchable, skill)
    ]
    matched_domains = [
        domain for domain in profile.business_domains if _contains_normalized(normalized_searchable, domain)
    ]
    gaps = [skill for skill in profile.core_skills if skill not in matched_core]

    priority_role_match = any(_contains_normalized(normalized_title, role) for role in profile.priority_roles)
    role_match = priority_role_match or any(
        _contains_normalized(normalized_title, role) for role in profile.target_roles
    )
    seniority_match = any(_contains_normalized(normalized_title, seniority) for seniority in profile.seniority)
    location_match = _location_match(profile, job)
    title_only_avoid_terms = {
        "senior",
        "sr",
        "staff",
        "principal",
        "lead",
        "especialista",
        "specialist",
        "gerente",
        "manager",
        "coordenador",
    }
    avoid_hit = any(
        _contains_normalized(normalized_title, term)
        if _normalize(term) in title_only_avoid_terms
        else _contains_normalized(normalized_searchable, term)
        for term in profile.avoid
    )

    score = 0
    score_details: list[dict[str, object]] = []

    def add_score(component: str, points: int, detail: str) -> None:
        nonlocal score
        score += points
        score_details.append(
            {
                "component": component,
                "points": points,
                "detail": detail,
            }
        )

    if profile.core_skills:
        points = round(match_setting(profile, "core_skills_weight") * len(matched_core) / len(profile.core_skills))
        add_score("Skills fortes", points, f"{len(matched_core)}/{len(profile.core_skills)} skills fortes citadas")
    if profile.nice_to_have_skills:
        points = round(
            match_setting(profile, "nice_to_have_skills_weight")
            * len(matched_nice)
            / len(profile.nice_to_have_skills)
        )
        add_score(
            "Skills complementares",
            points,
            f"{len(matched_nice)}/{len(profile.nice_to_have_skills)} skills complementares citadas",
        )
    if profile.business_domains:
        points = round(
            match_setting(profile, "business_domain_weight")
            * min(len(matched_domains), 4)
            / min(len(profile.business_domains), 4)
        )
        add_score("Dominio de negocio", points, f"{len(matched_domains)} dominio(s) alinhado(s)")
    if priority_role_match:
        add_score("Cargo", match_setting(profile, "priority_role_weight"), "cargo prioritario no titulo")
    elif role_match:
        add_score("Cargo", match_setting(profile, "target_role_weight"), "cargo semelhante aceito no titulo")
    else:
        add_score("Cargo", match_setting(profile, "no_role_penalty"), "titulo fora do foco principal")
    if seniority_match:
        add_score("Senioridade", match_setting(profile, "seniority_weight"), "senioridade alinhada")
    else:
        add_score("Senioridade", 0, "senioridade nao detectada no titulo")
    if location_match:
        add_score("Localidade", match_setting(profile, "location_weight"), "localidade aceita")
    else:
        add_score("Localidade", match_setting(profile, "missing_location_penalty"), "localidade fora da preferencia")
    if profile.min_junior_salary_brl and _contains(job.title, "junior"):
        salary_values = _extract_brl_values(searchable)
        flexible_junior = any(_contains_normalized(normalized_title, role) for role in profile.flexible_junior_roles)
        if salary_values and max(salary_values) >= profile.min_junior_salary_brl:
            add_score("Salario junior", match_setting(profile, "junior_salary_bonus"), "salario junior dentro do minimo")
        elif salary_values and not flexible_junior:
            add_score("Salario junior", match_setting(profile, "junior_salary_penalty"), "salario junior abaixo do minimo")
        else:
            add_score("Salario junior", 0, "salario nao informado ou cargo junior flexivel")
    if avoid_hit:
        add_score("Penalizacao", match_setting(profile, "avoid_penalty"), "termo de penalizacao encontrado")

    score = max(0, min(100, score))

    reasons = []
    if priority_role_match:
        reasons.append("cargo prioritario")
    if role_match:
        reasons.append("cargo alinhado")
    else:
        reasons.append("cargo fora do foco")
    if seniority_match:
        reasons.append("senioridade alinhada")
    if location_match:
        reasons.append("localizacao alinhada")
    else:
        reasons.append("localizacao fora da preferencia")
    if matched_core:
        reasons.append("skills principais encontradas")
    if matched_nice:
        reasons.append("skills desejaveis encontradas")
    if matched_domains:
        reasons.append("dominio de negocio alinhado")
    if avoid_hit:
        reasons.append("possivel criterio de exclusao encontrado")

    return MatchResult(
        job=job,
        score=score,
        matched_skills=[*matched_core, *matched_nice],
        matched_domains=matched_domains,
        gaps=gaps[:5],
        reasons=reasons,
        score_details=score_details,
    )
