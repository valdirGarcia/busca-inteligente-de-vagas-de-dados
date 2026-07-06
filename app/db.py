from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from app.models import Job, MatchResult


DEFAULT_DB_PATH = Path("data/app.db")
STATUSES = {"new", "saved", "applied", "ignored"}


def utc_now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    return connection


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                url TEXT,
                description TEXT,
                source TEXT,
                published_at TEXT,
                categories_json TEXT,
                score INTEGER NOT NULL DEFAULT 0,
                matched_skills_json TEXT,
                matched_domains_json TEXT,
                gaps_json TEXT,
                reasons_json TEXT,
                score_details_json TEXT,
                contact_email TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                notes TEXT,
                ignored_reason TEXT,
                ignored_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                applied_at TEXT
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company)")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
        if "published_at" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN published_at TEXT")
        if "score_details_json" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN score_details_json TEXT")
        if "ignored_reason" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN ignored_reason TEXT")
        if "ignored_at" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN ignored_at TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                fetched INTEGER NOT NULL DEFAULT 0,
                ranked INTEGER NOT NULL DEFAULT 0,
                eligible INTEGER NOT NULL DEFAULT 0,
                saved INTEGER NOT NULL DEFAULT 0,
                pruned INTEGER NOT NULL DEFAULT 0,
                stale_pruned INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT,
                fetched_by_source_json TEXT,
                eligible_by_source_json TEXT
            )
            """
        )


def make_job_id(job: Job) -> str:
    stable_key = job.url or f"{job.source}|{job.company}|{job.title}|{job.location}"
    return hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]


def extract_contact_email(job: Job) -> str:
    searchable = "\n".join([job.description, job.url, " ".join(job.categories.values())])
    matches = re.findall(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", searchable)
    return matches[0] if matches else ""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _append_age_filter(
    clauses: list[str],
    params: list[object],
    cutoff: str,
    include_unknown_dates: bool,
) -> None:
    dated_clause = """
        (
            published_at IS NOT NULL
            AND published_at != ''
            AND (
                (LENGTH(published_at) = 10 AND DATE(published_at) >= DATE(?))
                OR (LENGTH(published_at) != 10 AND DATETIME(published_at) >= DATETIME(?))
            )
        )
    """
    if include_unknown_dates:
        clauses.append(f"(published_at IS NULL OR published_at = '' OR {dated_clause})")
    else:
        clauses.append(dated_clause)
    params.extend([cutoff, cutoff])


def _term_variants(value: str) -> list[str]:
    lowered = value.strip().lower()
    if not lowered:
        return []
    decomposed = unicodedata.normalize("NFKD", lowered)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    variants = {lowered, without_accents}
    if "sao " in without_accents:
        variants.add(without_accents.replace("sao ", "são "))
    return sorted(variants)


def _preferred_region_terms(preferred_locations: list[str] | None) -> list[str]:
    broad_terms = {
        "anywhere",
        "brazil",
        "brasil",
        "home office",
        "remote",
        "remoto",
        "worldwide",
    }
    terms = []
    for location in preferred_locations or []:
        for variant in _term_variants(location):
            if variant not in broad_terms and variant not in terms:
                terms.append(variant)
    return terms


def _remote_clause() -> str:
    return """
        (
            source IN ('jobicy', 'remotive', 'remoteok')
            OR LOWER(COALESCE(location, '')) LIKE '%remot%'
            OR LOWER(COALESCE(location, '')) LIKE '%home office%'
            OR LOWER(COALESCE(location, '')) LIKE '%teletrabalho%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"is_remote": "true"%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"workplace_type": "remote"%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"job_type": "remoto"%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%home office%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%teletrabalho%'
        )
    """


def _hybrid_clause() -> str:
    return """
        (
            LOWER(COALESCE(location, '')) LIKE '%hibrid%'
            OR LOWER(COALESCE(location, '')) LIKE '%hybrid%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"workplace_type": "hybrid"%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"job_type": "hibrido"%'
            OR LOWER(COALESCE(categories_json, '')) LIKE '%"job_type": "híbrido"%'
        )
    """


def _region_clause(preferred_locations: list[str] | None) -> tuple[str, list[object]]:
    terms = _preferred_region_terms(preferred_locations)
    if not terms:
        return "0", []
    clauses = ["LOWER(COALESCE(location, '')) LIKE ?" for _ in terms]
    return "(" + " OR ".join(clauses) + ")", [f"%{term}%" for term in terms]


def _append_job_mode_filter(
    clauses: list[str],
    params: list[object],
    job_modes: list[str] | None,
    preferred_locations: list[str] | None,
    selected_locations: list[str] | None = None,
) -> None:
    if job_modes is None:
        return

    valid_modes = {
        "remote",
        "home_office",
        "region",
        "region_onsite",
        "region_hybrid",
        "hybrid",
        "outside_region",
        "onsite",
    }
    selected = {mode for mode in job_modes if mode in valid_modes}
    if not selected:
        clauses.append("0")
        return

    current_ui_modes = {"remote", "region_onsite", "region_hybrid", "outside_region"}
    if current_ui_modes.issubset(selected) and not selected_locations:
        return

    remote_clause = _remote_clause()
    hybrid_clause = _hybrid_clause()
    region_filter_locations = selected_locations or preferred_locations
    region_clause, region_params = _region_clause(region_filter_locations)
    full_region_clause, full_region_params = _region_clause(preferred_locations)
    mode_clauses = []
    mode_params: list[object] = []

    if {"remote", "home_office"} & selected:
        mode_clauses.append(remote_clause)
    if "region" in selected:
        mode_clauses.append(region_clause)
        mode_params.extend(region_params)
    if "region_onsite" in selected:
        mode_clauses.append(f"({region_clause} AND NOT {remote_clause} AND NOT {hybrid_clause})")
        mode_params.extend(region_params)
    if "region_hybrid" in selected:
        mode_clauses.append(f"({region_clause} AND {hybrid_clause})")
        mode_params.extend(region_params)
    if "hybrid" in selected:
        mode_clauses.append(hybrid_clause)
    if {"outside_region", "onsite"} & selected:
        outside_clause = f"(NOT {remote_clause}"
        if full_region_clause != "0":
            outside_clause += f" AND NOT {full_region_clause}"
            mode_params.extend(full_region_params)
        outside_clause += ")"
        mode_clauses.append(outside_clause)

    clauses.append("(" + " OR ".join(mode_clauses) + ")")
    params.extend(mode_params)


def prune_irrelevant_jobs(min_score: int, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            DELETE FROM jobs
            WHERE score < ?
              AND status NOT IN ('saved', 'applied')
              AND source != 'manual'
            """,
            (min_score,),
        )
        return cursor.rowcount


def prune_stale_jobs(max_age_days: int, db_path: str | Path = DEFAULT_DB_PATH) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            DELETE FROM jobs
            WHERE status NOT IN ('saved', 'applied')
              AND source != 'manual'
              AND (
                    published_at IS NULL
                    OR published_at = ''
                    OR (LENGTH(published_at) = 10 AND DATE(published_at) < DATE(?))
                    OR (LENGTH(published_at) != 10 AND DATETIME(published_at) < DATETIME(?))
              )
            """,
            (cutoff, cutoff),
        )
        return cursor.rowcount


def vacuum_db(db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.execute("VACUUM")


def upsert_match_results(results: Iterable[MatchResult], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    now = utc_now()
    rows = []
    for result in results:
        job = result.job
        rows.append(
            (
                make_job_id(job),
                job.title,
                job.company,
                job.location,
                job.url,
                job.description,
                job.source,
                job.published_at,
                _json(job.categories),
                result.score,
                _json(result.matched_skills),
                _json(result.matched_domains),
                _json(result.gaps),
                _json(result.reasons),
                _json(result.score_details),
                extract_contact_email(job),
                now,
                now,
            )
        )

    with connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO jobs (
                id, title, company, location, url, description, source, published_at, categories_json,
                score, matched_skills_json, matched_domains_json, gaps_json, reasons_json,
                score_details_json, contact_email, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                company = excluded.company,
                location = excluded.location,
                url = excluded.url,
                description = excluded.description,
                source = excluded.source,
                published_at = excluded.published_at,
                categories_json = excluded.categories_json,
                score = excluded.score,
                matched_skills_json = excluded.matched_skills_json,
                matched_domains_json = excluded.matched_domains_json,
                gaps_json = excluded.gaps_json,
                reasons_json = excluded.reasons_json,
                score_details_json = excluded.score_details_json,
                contact_email = excluded.contact_email,
                last_seen_at = excluded.last_seen_at
            """,
            rows,
        )
    return len(rows)


def update_job_status(
    job_id: str,
    status: str,
    db_path: str | Path = DEFAULT_DB_PATH,
    ignored_reason: str = "",
) -> None:
    if status not in STATUSES:
        raise ValueError(f"Status invalido: {status}")
    applied_at = utc_now() if status == "applied" else None
    ignored_at = utc_now() if status == "ignored" else None
    reason = ignored_reason.strip() if status == "ignored" else ""
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?,
                applied_at = COALESCE(?, applied_at),
                ignored_at = CASE
                    WHEN ? = 'ignored' THEN ?
                    WHEN ? IN ('new', 'saved', 'applied') THEN NULL
                    ELSE ignored_at
                END,
                ignored_reason = CASE
                    WHEN ? = 'ignored' THEN ?
                    WHEN ? IN ('new', 'saved', 'applied') THEN ''
                    ELSE ignored_reason
                END
            WHERE id = ?
            """,
            (status, applied_at, status, ignored_at, status, status, reason, status, job_id),
        )


def update_job_notes(job_id: str, notes: str, db_path: str | Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as connection:
        connection.execute("UPDATE jobs SET notes = ? WHERE id = ?", (notes, job_id))


def create_search_run(summary: dict[str, object], db_path: str | Path = DEFAULT_DB_PATH) -> int:
    started_at = str(summary.get("started_at") or utc_now())
    finished_at = str(summary.get("finished_at") or utc_now())
    with connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO search_runs (
                started_at, finished_at, fetched, ranked, eligible, saved,
                pruned, stale_pruned, errors_json, fetched_by_source_json, eligible_by_source_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                int(summary.get("fetched", 0)),
                int(summary.get("ranked", 0)),
                int(summary.get("eligible", 0)),
                int(summary.get("saved", 0)),
                int(summary.get("pruned", 0)),
                int(summary.get("stale_pruned", 0)),
                _json(summary.get("errors", [])),
                _json(summary.get("fetched_by_source", {})),
                _json(summary.get("eligible_by_source", {})),
            ),
        )
        return int(cursor.lastrowid)


def list_search_runs(db_path: str | Path = DEFAULT_DB_PATH, limit: int = 10) -> list[sqlite3.Row]:
    with connect(db_path) as connection:
        return list(
            connection.execute(
                """
                SELECT *
                FROM search_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def list_available_sources(db_path: str | Path = DEFAULT_DB_PATH) -> list[str]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT source
            FROM jobs
            WHERE source IS NOT NULL AND source != ''
            ORDER BY source
            """
        )
        return [str(row["source"]) for row in rows]


def _append_source_filter(
    clauses: list[str],
    params: list[object],
    sources: list[str] | None,
) -> None:
    if sources is None:
        return
    selected = [source for source in sources if source]
    if not selected:
        clauses.append("0")
        return
    placeholders = ", ".join("?" for _ in selected)
    clauses.append(f"source IN ({placeholders})")
    params.extend(selected)


def list_jobs(
    statuses: list[str] | None = None,
    min_score: int = 0,
    query: str = "",
    max_age_hours: int | None = None,
    max_age_days: int | None = None,
    include_unknown_dates: bool = True,
    include_international: bool = True,
    job_modes: list[str] | None = None,
    preferred_locations: list[str] | None = None,
    selected_locations: list[str] | None = None,
    sources: list[str] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
    limit: int = 200,
) -> list[sqlite3.Row]:
    clauses = ["score >= ?"]
    params: list[object] = [min_score]

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if query.strip():
        clauses.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        term = f"%{query.strip()}%"
        params.extend([term, term, term])

    if max_age_hours is not None:
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
        _append_age_filter(clauses, params, cutoff, include_unknown_dates)
    elif max_age_days is not None:
        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        _append_age_filter(clauses, params, cutoff, include_unknown_dates)

    if not include_international:
        clauses.append(
            """
            (
                location IS NULL OR location = ''
                OR LOWER(location) LIKE '%brasil%'
                OR LOWER(location) LIKE '%brazil%'
                OR LOWER(location) LIKE '%sao paulo%'
                OR LOWER(location) LIKE '%são paulo%'
                OR LOWER(location) LIKE '%araras%'
                OR LOWER(location) LIKE '%limeira%'
                OR LOWER(location) LIKE '%leme%'
                OR LOWER(location) LIKE '%piracicaba%'
                OR LOWER(location) LIKE '%rio claro%'
                OR LOWER(location) LIKE '%campinas%'
                OR LOWER(location) LIKE '%remoto%'
            )
            """
        )

    _append_job_mode_filter(clauses, params, job_modes, preferred_locations, selected_locations)
    _append_source_filter(clauses, params, sources)

    params.append(limit)
    sql = f"""
        SELECT *
        FROM jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY
            score DESC,
            DATETIME(published_at) DESC,
            last_seen_at DESC,
            CASE status
                WHEN 'new' THEN 0
                WHEN 'saved' THEN 1
                WHEN 'applied' THEN 2
                ELSE 3
            END
        LIMIT ?
    """
    with connect(db_path) as connection:
        return list(connection.execute(sql, params))


def dashboard_counts(db_path: str | Path = DEFAULT_DB_PATH) -> dict[str, object]:
    with connect(db_path) as connection:
        total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        avg_score = connection.execute("SELECT COALESCE(ROUND(AVG(score), 1), 0) FROM jobs").fetchone()[0]
        by_status = {
            row["status"]: row["total"]
            for row in connection.execute("SELECT status, COUNT(*) AS total FROM jobs GROUP BY status")
        }
        by_source = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    source,
                    COUNT(*) AS total,
                    COALESCE(ROUND(AVG(score), 1), 0) AS avg_score,
                    MAX(score) AS best_score,
                    SUM(CASE WHEN score >= 40 THEN 1 ELSE 0 END) AS strong_matches
                FROM jobs
                GROUP BY source
                ORDER BY total DESC
                """
            )
        ]
        top_companies = [
            dict(row)
            for row in connection.execute(
                """
                SELECT company, COUNT(*) AS total, MAX(score) AS best_score
                FROM jobs
                GROUP BY company
                ORDER BY best_score DESC, total DESC
                LIMIT 10
                """
            )
        ]
        ignored_reasons = [
            dict(row)
            for row in connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(ignored_reason, ''), 'Sem motivo informado') AS reason,
                    COUNT(*) AS total
                FROM jobs
                WHERE status = 'ignored'
                GROUP BY reason
                ORDER BY total DESC, reason
                """
            )
        ]
    return {
        "total": total,
        "avg_score": avg_score,
        "by_status": by_status,
        "by_source": by_source,
        "top_companies": top_companies,
        "ignored_reasons": ignored_reasons,
    }


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return []


def count_jobs(
    statuses: list[str] | None = None,
    min_score: int = 0,
    query: str = "",
    max_age_hours: int | None = None,
    max_age_days: int | None = None,
    include_unknown_dates: bool = True,
    include_international: bool = True,
    job_modes: list[str] | None = None,
    preferred_locations: list[str] | None = None,
    selected_locations: list[str] | None = None,
    sources: list[str] | None = None,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> int:
    clauses = ["score >= ?"]
    params: list[object] = [min_score]

    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)

    if query.strip():
        clauses.append("(title LIKE ? OR company LIKE ? OR location LIKE ?)")
        term = f"%{query.strip()}%"
        params.extend([term, term, term])

    if max_age_hours is not None:
        cutoff = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
        _append_age_filter(clauses, params, cutoff, include_unknown_dates)
    elif max_age_days is not None:
        cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
        _append_age_filter(clauses, params, cutoff, include_unknown_dates)

    if not include_international:
        clauses.append(
            """
            (
                location IS NULL OR location = ''
                OR LOWER(location) LIKE '%brasil%'
                OR LOWER(location) LIKE '%brazil%'
                OR LOWER(location) LIKE '%sao paulo%'
                OR LOWER(location) LIKE '%são paulo%'
                OR LOWER(location) LIKE '%araras%'
                OR LOWER(location) LIKE '%limeira%'
                OR LOWER(location) LIKE '%leme%'
                OR LOWER(location) LIKE '%piracicaba%'
                OR LOWER(location) LIKE '%rio claro%'
                OR LOWER(location) LIKE '%campinas%'
                OR LOWER(location) LIKE '%remoto%'
            )
            """
        )

    _append_job_mode_filter(clauses, params, job_modes, preferred_locations, selected_locations)
    _append_source_filter(clauses, params, sources)

    with connect(db_path) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM jobs WHERE {' AND '.join(clauses)}", params).fetchone()
    return int(row[0])
