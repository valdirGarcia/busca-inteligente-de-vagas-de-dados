from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import re
from time import sleep
import unicodedata
from urllib.error import HTTPError, URLError

from app.collectors.ashby import fetch_ashby_jobs
from app.collectors.greenhouse import fetch_greenhouse_jobs
from app.collectors.gupy import fetch_gupy_jobs
from app.collectors.jobicy import fetch_jobicy_jobs
from app.collectors.lever import fetch_lever_jobs
from app.collectors.netvagas import fetch_netvagas_jobs
from app.collectors.remotar import fetch_remotar_jobs
from app.collectors.remoterocketship import fetch_remoterocketship_jobs
from app.collectors.remotive import fetch_remotive_jobs
from app.collectors.remoteok import fetch_remoteok_jobs
from app.collectors.arbeitnow import fetch_arbeitnow_jobs
from app.collectors.smartrecruiters import fetch_smartrecruiters_jobs
from app.collectors.solides import fetch_solides_jobs
from app.collectors.trampos import fetch_trampos_jobs
from app.collectors.vagascom import fetch_vagascom_jobs
from app.db import (
    DEFAULT_DB_PATH,
    connect,
    create_search_run,
    init_db,
    make_job_id,
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
    if source_type == "jobicy":
        geo = None if token.lower() in {"all", "any", "global"} else token
        return source_type, token, fetch_jobicy_jobs(geo=geo, max_age_days=max_age_days)
    if source_type == "lever":
        return source_type, token, fetch_lever_jobs(token, max_age_days=max_age_days)
    if source_type == "netvagas":
        pages = int(token) if token.isdigit() else 1
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_netvagas_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    if source_type == "remotar":
        pages = int(token) if token.isdigit() else 3
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_remotar_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    if source_type == "remoterocketship":
        return source_type, token, fetch_remoterocketship_jobs(slugs=[token], max_age_days=max_age_days)
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
        pages = int(token) if token.isdigit() else 20
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_solides_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    if source_type == "trampos":
        pages = int(token) if token.isdigit() else 2
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_trampos_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    if source_type == "vagascom":
        pages = int(token) if token.isdigit() else 2
        terms = None if token.isdigit() else [token]
        return source_type, token, fetch_vagascom_jobs(pages_per_term=pages, terms=terms, max_age_days=max_age_days)
    raise ValueError(f"Fonte desconhecida: {source_type}")


def _fetch_source_with_retry(
    source_type: str,
    token: str,
    settings: dict[str, int],
    attempts: int = 2,
) -> tuple[str, str, list[Job]]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return _fetch_source(source_type, token, settings)
        except Exception as error:
            last_error = error
            if attempt < attempts:
                sleep(1)
    if last_error:
        raise last_error
    raise RuntimeError(f"Falha desconhecida ao buscar {source_type}:{token}")


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
            executor.submit(_fetch_source_with_retry, source_type, token, settings): (source_type, token)
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


def _normalize_dedupe(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _decode_gupy_token(token: str) -> str:
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode((token + padding).encode("utf-8")).decode("utf-8")
        payload = json.loads(decoded)
    except Exception:
        return ""
    return str(payload.get("jobId") or payload.get("job_id") or "")


def _url_dedupe_key(url: str) -> str:
    raw_url = url.strip().rstrip("/")
    normalized = raw_url.lower()
    if not normalized:
        return ""

    gupy_match = re.search(r"https?://([a-z0-9-]+)\.gupy\.io/(?:jobs|job)/([^/?#]+)", raw_url, re.I)
    if gupy_match:
        board, token = gupy_match.groups()
        job_id = token if token.isdigit() else _decode_gupy_token(token)
        if job_id:
            return f"gupy:{board}:{job_id}"

    solides_match = re.search(r"https?://[^/]*solides\.[^/]+/(?:.*?/)?(?:vaga|vacancies)/(\d+)", normalized)
    if solides_match:
        return f"solides:{solides_match.group(1)}"

    return normalized


def _dedupe_keys(job: Job) -> list[str]:
    keys = []
    normalized_url = _url_dedupe_key(job.url)
    if normalized_url:
        keys.append(f"url:{normalized_url}")

    company_title_location = "|".join(
        part
        for part in [
            _normalize_dedupe(job.company),
            _normalize_dedupe(job.title),
            _normalize_dedupe(job.location),
        ]
        if part
    )
    if company_title_location:
        keys.append(f"signature:{company_title_location}")

    if not keys:
        keys.append(f"fallback:{job.source}:{_normalize_dedupe(job.title)}:{_normalize_dedupe(job.location)}")
    return keys


def _prefer_job(candidate: Job, existing: Job) -> Job:
    primary_sources = {"gupy", "solides", "netvagas", "remotar", "vagascom"}
    if candidate.source in primary_sources and existing.source not in primary_sources:
        return candidate
    if existing.source in primary_sources and candidate.source not in primary_sources:
        return existing
    if len(candidate.description or "") > len(existing.description or ""):
        return candidate
    return existing


def rank_jobs(profile_path: str | Path, jobs: list[Job]) -> list[MatchResult]:
    profile = load_profile(profile_path)
    unique_jobs: dict[str, Job] = {}
    key_owner: dict[str, str] = {}
    for job in jobs:
        dedupe_keys = _dedupe_keys(job)
        owner_key = next((key_owner[key] for key in dedupe_keys if key in key_owner), "")
        if owner_key:
            unique_jobs[owner_key] = _prefer_job(job, unique_jobs[owner_key])
        else:
            owner_key = dedupe_keys[0]
            unique_jobs[owner_key] = job
        for key in _dedupe_keys(unique_jobs[owner_key]):
            key_owner[key] = owner_key
        for key in dedupe_keys:
            key_owner[key] = owner_key
    return sorted(
        (score_job(profile, job) for job in unique_jobs.values()),
        key=lambda item: item.score,
        reverse=True,
    )


def prune_superseded_duplicates(results: list[MatchResult], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    keep_ids = {make_job_id(result.job) for result in results}
    active_keys = set()
    for result in results:
        active_keys.update(_dedupe_keys(result.job))

    if not active_keys:
        return 0

    delete_ids = []
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, title, company, location, url, description, source, published_at
            FROM jobs
            WHERE status NOT IN ('saved', 'applied')
              AND source != 'manual'
            """
        )
        for row in rows:
            row_id = str(row["id"])
            if row_id in keep_ids:
                continue
            row_job = Job(
                title=str(row["title"] or ""),
                company=str(row["company"] or ""),
                location=str(row["location"] or ""),
                url=str(row["url"] or ""),
                description=str(row["description"] or ""),
                source=str(row["source"] or ""),
                published_at=str(row["published_at"] or ""),
            )
            if active_keys.intersection(_dedupe_keys(row_job)):
                delete_ids.append(row_id)

        if delete_ids:
            placeholders = ", ".join("?" for _ in delete_ids)
            connection.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", delete_ids)
    return len(delete_ids)


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
    deduped = prune_superseded_duplicates(storable_results, db_path)
    pruned = prune_irrelevant_jobs(
        min_score_to_store,
        db_path,
    )
    stale_pruned = prune_stale_jobs(
        max_age_days,
        db_path,
    )
    if deduped or pruned or stale_pruned:
        vacuum_db(db_path)
    summary = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "fetched": len(jobs),
        "ranked": len(results),
        "eligible": len(storable_results),
        "saved": saved,
        "deduped": deduped,
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
