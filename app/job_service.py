from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError

from app.collectors.ashby import fetch_ashby_jobs
from app.collectors.greenhouse import fetch_greenhouse_jobs
from app.collectors.gupy import fetch_gupy_jobs
from app.collectors.lever import fetch_lever_jobs
from app.collectors.remotive import fetch_remotive_jobs
from app.collectors.remoteok import fetch_remoteok_jobs
from app.collectors.arbeitnow import fetch_arbeitnow_jobs
from app.collectors.smartrecruiters import fetch_smartrecruiters_jobs
from app.collectors.solides import fetch_solides_jobs
from app.db import (
    DEFAULT_DB_PATH,
    connect,
    create_search_run,
    init_db,
    prune_irrelevant_jobs,
    prune_stale_jobs,
    upsert_match_results,
    utc_now,
    vacuum_db,
)
from app.matcher import DEFAULT_MATCH_SETTINGS, score_job
from app.models import Job, MatchResult
from app.profile_loader import load_profile
from app.sources_loader import load_sources


def _fetch_source(source_type: str, token: str, settings: dict[str, int]) -> tuple[str, str, list[Job]]:
    max_age_days = settings.get("max_age_days", DEFAULT_MATCH_SETTINGS["max_job_age_days_to_store"])
    if source_type == "ashby":
        return source_type, token, fetch_ashby_jobs(token, max_age_days=max_age_days)
    if source_type == "greenhouse":
        return source_type, token, fetch_greenhouse_jobs(token, max_age_days=max_age_days)
    if source_type == "gupy":
        pages = int(token) if token.isdigit() else 8
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_gupy_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    if source_type == "lever":
        return source_type, token, fetch_lever_jobs(token, max_age_days=max_age_days)
    if source_type == "remotive":
        return source_type, token, fetch_remotive_jobs(token, max_age_days=max_age_days)
    if source_type == "remoteok":
        return source_type, token, fetch_remoteok_jobs(max_age_days=max_age_days)
    if source_type == "arbeitnow":
        pages = int(token) if token.isdigit() else 20
        return source_type, token, fetch_arbeitnow_jobs(pages=pages, max_age_days=max_age_days)
    if source_type == "smartrecruiters":
        return source_type, token, fetch_smartrecruiters_jobs(
            token,
            pages=settings.get("smartrecruiters_pages", 5),
            max_age_days=max_age_days,
        )
    if source_type == "solides":
        pages = int(token) if token.isdigit() else 12
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_solides_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    raise ValueError(f"Fonte desconhecida: {source_type}")


def _first_int(values: list[str] | None, default: int) -> int:
    if not values:
        return default
    try:
        return int(values[0])
    except ValueError:
        return default


def _max_age_days_from_profile(profile_path: str | Path) -> int:
    profile = load_profile(profile_path)
    configured = int(
        profile.match_settings.get("max_job_age_days_to_store", DEFAULT_MATCH_SETTINGS["max_job_age_days_to_store"])
    )
    return max(1, min(7, configured))


def collect_jobs(sources_path: str | Path, max_age_days: int = 7) -> tuple[list[Job], list[str]]:
    sources = load_sources(sources_path)
    jobs: list[Job] = []
    errors: list[str] = []
    tasks: list[tuple[str, str]] = []
    settings = {
        "smartrecruiters_pages": _first_int(sources.get("smartrecruiters_pages"), 5),
        "max_age_days": max(1, min(7, max_age_days)),
    }

    for source_type, tokens in sources.items():
        if source_type in {"smartrecruiters_pages"}:
            continue
        for token in tokens:
            tasks.append((source_type, token))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_source, source_type, token, settings): (source_type, token)
            for source_type, token in tasks
        }
        for future in as_completed(futures):
            source_type, token = futures[future]
            try:
                _, _, fetched_jobs = future.result()
            except HTTPError as error:
                errors.append(f"{source_type}:{token} retornou HTTP {error.code}")
                continue
            except URLError as error:
                errors.append(f"{source_type}:{token} erro de rede: {error.reason}")
                continue
            except Exception as error:
                errors.append(f"{source_type}:{token} falhou: {error}")
                continue
            jobs.extend(fetched_jobs)

    return jobs, errors


def rank_jobs(profile_path: str | Path, jobs: list[Job]) -> list[MatchResult]:
    profile = load_profile(profile_path)
    unique_jobs = {}
    for job in jobs:
        dedupe_key = job.url or f"{job.source}:{job.company}:{job.title}:{job.location}"
        unique_jobs[dedupe_key] = job
    return sorted(
        (score_job(profile, job) for job in unique_jobs.values()),
        key=lambda item: item.score,
        reverse=True,
    )


def refresh_recommendations(
    profile_path: str | Path,
    sources_path: str | Path,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, object]:
    init_db(db_path)
    started_at = utc_now()
    profile = load_profile(profile_path)
    max_age_days = _max_age_days_from_profile(profile_path)
    jobs, errors = collect_jobs(sources_path, max_age_days=max_age_days)
    results = rank_jobs(profile_path, jobs)
    fetched_by_source: dict[str, int] = {}
    for job in jobs:
        fetched_by_source[job.source] = fetched_by_source.get(job.source, 0) + 1

    min_score_to_store = int(
        profile.match_settings.get("min_score_to_store", DEFAULT_MATCH_SETTINGS["min_score_to_store"])
    )
    storable_results = [result for result in results if result.score >= min_score_to_store]
    eligible_by_source: dict[str, int] = {}
    for result in storable_results:
        source = result.job.source
        eligible_by_source[source] = eligible_by_source.get(source, 0) + 1
    saved = upsert_match_results(storable_results, db_path)
    pruned = prune_irrelevant_jobs(
        min_score_to_store,
        db_path,
    )
    stale_pruned = prune_stale_jobs(
        max_age_days,
        db_path,
    )
    if pruned or stale_pruned:
        vacuum_db(db_path)
    summary = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "fetched": len(jobs),
        "ranked": len(results),
        "eligible": len(storable_results),
        "saved": saved,
        "pruned": pruned,
        "stale_pruned": stale_pruned,
        "errors": errors,
        "fetched_by_source": fetched_by_source,
        "eligible_by_source": eligible_by_source,
    }
    summary["run_id"] = create_search_run(summary, db_path)
    return summary


def rescore_existing_jobs(
    profile_path: str | Path,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    import json

    init_db(db_path)
    with connect(db_path) as connection:
        rows = list(connection.execute("SELECT * FROM jobs"))

    jobs = []
    for row in rows:
        try:
            categories = json.loads(row["categories_json"] or "{}")
        except json.JSONDecodeError:
            categories = {}
        jobs.append(
            Job(
                title=row["title"],
                company=row["company"],
                location=row["location"] or "",
                url=row["url"] or "",
                description=row["description"] or "",
                source=row["source"] or "",
                published_at=row["published_at"] or "",
                categories={str(key): str(value) for key, value in categories.items()},
            )
        )

    results = rank_jobs(profile_path, jobs)
    saved = upsert_match_results(results, db_path)
    profile = load_profile(profile_path)
    pruned = prune_irrelevant_jobs(
        int(profile.match_settings.get("min_score_to_store", DEFAULT_MATCH_SETTINGS["min_score_to_store"])),
        db_path,
    )
    stale_pruned = prune_stale_jobs(
        _max_age_days_from_profile(profile_path),
        db_path,
    )
    if pruned or stale_pruned:
        vacuum_db(db_path)
    return {"rescored": len(results), "saved": saved, "pruned": pruned, "stale_pruned": stale_pruned}
