from __future__ import annotations

import argparse
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
from app.matcher import score_job
from app.profile_loader import load_profile
from app.sources_loader import load_sources


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Busca e ranqueia vagas por match com o perfil.")
    parser.add_argument("--profile", required=True, help="Caminho para o arquivo YAML do perfil.")
    parser.add_argument("--sources", help="Caminho para um YAML com fontes para buscar.")
    parser.add_argument(
        "--lever",
        action="append",
        default=[],
        help="Slug de empresa no Lever. Pode ser usado varias vezes. Ex: --lever netflix",
    )
    parser.add_argument(
        "--greenhouse",
        action="append",
        default=[],
        help="Board token de empresa no Greenhouse. Pode ser usado varias vezes. Ex: --greenhouse nubank",
    )
    parser.add_argument("--gupy", type=int, help="Buscar vagas filtradas na Gupy. Valor = paginas por termo.")
    parser.add_argument(
        "--ashby",
        action="append",
        default=[],
        help="Board de empresa no Ashby. Pode ser usado varias vezes. Ex: --ashby openai",
    )
    parser.add_argument(
        "--smartrecruiters",
        action="append",
        default=[],
        help="Identificador de empresa no SmartRecruiters. Ex: --smartrecruiters NielsenIQ",
    )
    parser.add_argument(
        "--smartrecruiters-pages",
        type=int,
        default=3,
        help="Paginas por empresa no SmartRecruiters.",
    )
    parser.add_argument(
        "--remotive",
        action="append",
        default=[],
        help="Categoria do Remotive. Pode ser usado varias vezes. Ex: --remotive data",
    )
    parser.add_argument("--remoteok", action="store_true", help="Buscar vagas filtradas no RemoteOK.")
    parser.add_argument("--arbeitnow", type=int, help="Buscar vagas filtradas no Arbeitnow. Valor = paginas.")
    parser.add_argument("--solides", type=int, help="Buscar vagas filtradas na Solides. Valor = paginas por termo.")
    parser.add_argument("--limit", type=int, default=20, help="Quantidade de resultados exibidos.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    profile = load_profile(args.profile)

    if args.sources:
        configured_sources = load_sources(args.sources)
        args.ashby.extend(configured_sources["ashby"])
        args.lever.extend(configured_sources["lever"])
        args.greenhouse.extend(configured_sources["greenhouse"])
        if configured_sources["gupy"] and args.gupy is None:
            args.gupy = int(configured_sources["gupy"][0])
        args.smartrecruiters.extend(configured_sources["smartrecruiters"])
        args.remotive.extend(configured_sources["remotive"])
        args.remoteok = args.remoteok or bool(configured_sources["remoteok"])
        if configured_sources["smartrecruiters_pages"]:
            args.smartrecruiters_pages = int(configured_sources["smartrecruiters_pages"][0])
        if configured_sources["arbeitnow"] and args.arbeitnow is None:
            args.arbeitnow = int(configured_sources["arbeitnow"][0])
        if configured_sources["solides"] and args.solides is None:
            args.solides = int(configured_sources["solides"][0])

    jobs = []
    attempted_sources = 0
    for board_name in args.ashby:
        attempted_sources += 1
        try:
            board_jobs = fetch_ashby_jobs(board_name)
        except HTTPError as error:
            print(f"Aviso: Ashby '{board_name}' retornou HTTP {error.code}. Pulando fonte.")
            continue
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Ashby '{board_name}': {error.reason}. Pulando fonte.")
            continue
        jobs.extend(board_jobs)

    for company_slug in args.lever:
        attempted_sources += 1
        try:
            company_jobs = fetch_lever_jobs(company_slug)
        except HTTPError as error:
            print(f"Aviso: Lever '{company_slug}' retornou HTTP {error.code}. Pulando fonte.")
            continue
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Lever '{company_slug}': {error.reason}. Pulando fonte.")
            continue
        jobs.extend(company_jobs)

    for board_token in args.greenhouse:
        attempted_sources += 1
        try:
            board_jobs = fetch_greenhouse_jobs(board_token)
        except HTTPError as error:
            print(f"Aviso: Greenhouse '{board_token}' retornou HTTP {error.code}. Pulando fonte.")
            continue
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Greenhouse '{board_token}': {error.reason}. Pulando fonte.")
            continue
        jobs.extend(board_jobs)

    if args.gupy:
        attempted_sources += 1
        try:
            jobs.extend(fetch_gupy_jobs(pages_per_term=args.gupy))
        except HTTPError as error:
            print(f"Aviso: Gupy retornou HTTP {error.code}. Pulando fonte.")
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Gupy: {error.reason}. Pulando fonte.")

    for company_slug in args.smartrecruiters:
        attempted_sources += 1
        try:
            company_jobs = fetch_smartrecruiters_jobs(company_slug, pages=args.smartrecruiters_pages)
        except HTTPError as error:
            print(f"Aviso: SmartRecruiters '{company_slug}' retornou HTTP {error.code}. Pulando fonte.")
            continue
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar SmartRecruiters '{company_slug}': {error.reason}. Pulando fonte.")
            continue
        jobs.extend(company_jobs)

    for category in args.remotive:
        attempted_sources += 1
        try:
            remote_jobs = fetch_remotive_jobs(category)
        except HTTPError as error:
            print(f"Aviso: Remotive '{category}' retornou HTTP {error.code}. Pulando fonte.")
            continue
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Remotive '{category}': {error.reason}. Pulando fonte.")
            continue
        jobs.extend(remote_jobs)

    if args.remoteok:
        attempted_sources += 1
        try:
            jobs.extend(fetch_remoteok_jobs())
        except HTTPError as error:
            print(f"Aviso: RemoteOK retornou HTTP {error.code}. Pulando fonte.")
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar RemoteOK: {error.reason}. Pulando fonte.")

    if args.arbeitnow:
        attempted_sources += 1
        try:
            jobs.extend(fetch_arbeitnow_jobs(pages=args.arbeitnow))
        except HTTPError as error:
            print(f"Aviso: Arbeitnow retornou HTTP {error.code}. Pulando fonte.")
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Arbeitnow: {error.reason}. Pulando fonte.")

    if args.solides:
        attempted_sources += 1
        try:
            jobs.extend(fetch_solides_jobs(pages_per_term=args.solides))
        except HTTPError as error:
            print(f"Aviso: Solides retornou HTTP {error.code}. Pulando fonte.")
        except URLError as error:
            print(f"Aviso: erro de rede ao buscar Solides: {error.reason}. Pulando fonte.")

    if attempted_sources == 0:
        print("Nenhuma fonte informada. Exemplo: python -m app.search --profile data/profile.yaml --sources data/sources.yaml")
        return

    if not jobs:
        print("Nenhuma vaga encontrada nas fontes informadas.")
        return

    unique_jobs = {}
    for job in jobs:
        dedupe_key = job.url or f"{job.source}:{job.company}:{job.title}:{job.location}"
        unique_jobs[dedupe_key] = job

    results = sorted((score_job(profile, job) for job in unique_jobs.values()), key=lambda item: item.score, reverse=True)

    for result in results[: args.limit]:
        job = result.job
        matched = ", ".join(result.matched_skills) or "nenhuma skill principal detectada"
        domains = ", ".join(result.matched_domains)
        gaps = ", ".join(result.gaps) or "nenhum gap principal"
        reasons = ", ".join(result.reasons) or "sem sinais fortes"
        print(f"{result.score:3d}% | {job.title} | {job.company} | {job.location}")
        if job.published_at:
            print(f"     Publicada: {job.published_at[:10]}")
        print(f"     Match: {matched}")
        if domains:
            print(f"     Dominio: {domains}")
        print(f"     Gap: {gaps}")
        print(f"     Motivo: {reasons}")
        print(f"     Link: {job.url}")
        print()


if __name__ == "__main__":
    main()
