from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from app.db import (
    DEFAULT_DB_PATH,
    count_jobs,
    dashboard_counts,
    init_db,
    list_jobs,
    make_job_id,
    parse_json_list,
    upsert_match_results,
    update_job_notes,
    update_job_status,
)
from app.job_service import refresh_recommendations, rescore_existing_jobs
from app.matcher import DEFAULT_MATCH_SETTINGS, score_job
from app.models import Job
from app.profile_loader import load_profile


BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "data" / "profile.yaml"
SOURCES_PATH = BASE_DIR / "data" / "sources.yaml"
DB_PATH = BASE_DIR / DEFAULT_DB_PATH

STATUS_LABELS = {
    "new": "Nova",
    "saved": "Salva",
    "applied": "Candidatado",
    "ignored": "Ignorada",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(read_text(path)) or {}


def save_yaml_text(path: Path, content: str) -> None:
    yaml.safe_load(content)
    path.write_text(content, encoding="utf-8")


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def list_to_text(values: list[str] | None) -> str:
    return "\n".join(values or [])


def text_to_list(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def format_date(value: str | None) -> str:
    if not value:
        return "Data nao informada"
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return value[:10]
    return parsed.date().isoformat()


def set_status(job_id: str, status: str) -> None:
    update_job_status(job_id, status, DB_PATH)
    st.rerun()


def row_list(row, column: str) -> list[str]:
    return parse_json_list(row[column])


def show_job_card(row, index: int) -> None:
    with st.container(border=True):
        title_col, score_col = st.columns([5, 1])
        with title_col:
            st.markdown(f"#### {row['title']}")
            st.caption(
                f"{row['company']} | {row['location'] or 'Local nao informado'} | "
                f"{row['source']} | Publicada: {format_date(row['published_at'])} | "
                f"{STATUS_LABELS.get(row['status'], row['status'])}"
            )
        with score_col:
            st.metric("Match", f"{row['score']}%")

        st.progress(max(0, min(100, int(row["score"]))) / 100)

        matched_skills = row_list(row, "matched_skills_json")
        matched_domains = row_list(row, "matched_domains_json")
        gaps = row_list(row, "gaps_json")
        reasons = row_list(row, "reasons_json")

        details = []
        if matched_skills:
            details.append(f"Skills: {', '.join(matched_skills[:10])}")
        if matched_domains:
            details.append(f"Dominio: {', '.join(matched_domains[:8])}")
        if gaps:
            details.append(f"Skills suas nao citadas na vaga: {', '.join(gaps[:6])}")
        if reasons:
            details.append(f"Motivos: {', '.join(reasons)}")
        st.write("  \n".join(details) if details else "Sem sinais fortes encontrados.")

        contact = row["contact_email"] or "Nao encontrado na descricao"
        st.caption(f"Contato/e-mail: {contact}")

        action_cols = st.columns([1.2, 1, 1, 1, 3])
        if row["url"]:
            action_cols[0].link_button("Abrir vaga", row["url"])
        if action_cols[1].button("Salvar", key=f"save-{row['id']}-{index}"):
            set_status(row["id"], "saved")
        if action_cols[2].button("Candidatei", key=f"applied-{row['id']}-{index}", type="primary"):
            set_status(row["id"], "applied")
        if action_cols[3].button("Ignorar", key=f"ignore-{row['id']}-{index}"):
            set_status(row["id"], "ignored")


def recommendations_tab() -> None:
    st.subheader("Recomendacoes")
    profile = read_yaml(PROFILE_PATH)
    match_settings = profile.get("match_settings", {})
    configured_min_score = max(1, int(match_settings.get("min_score_to_show", 1)))
    filter_cols = st.columns([1.6, 1.8, 1, 1.1, 1])
    status_option = filter_cols[0].selectbox(
        "Status",
        ["Novas e salvas", "Todas", "Novas", "Salvas", "Candidatadas", "Ignoradas"],
    )
    query = filter_cols[1].text_input("Buscar", placeholder="cargo, empresa ou local")
    min_score = filter_cols[2].number_input(
        "Match minimo",
        min_value=1,
        max_value=100,
        value=configured_min_score,
        step=5,
    )
    age_option = filter_cols[3].selectbox(
        "Publicada",
        ["24 horas", "7 dias", "14 dias", "30 dias"],
        index=3,
    )
    display_limit = filter_cols[4].number_input("Qtd. exibida", min_value=20, max_value=1000, value=300, step=20)
    include_unknown_dates = st.checkbox("Incluir vagas sem data informada", value=False)
    include_international = st.checkbox("Mostrar vagas internacionais", value=False)

    status_map = {
        "Novas e salvas": ["new", "saved"],
        "Todas": None,
        "Novas": ["new"],
        "Salvas": ["saved"],
        "Candidatadas": ["applied"],
        "Ignoradas": ["ignored"],
    }
    age_map = {
        "24 horas": ("hours", 24),
        "7 dias": 7,
        "14 dias": 14,
        "30 dias": 30,
    }
    age_value = age_map[age_option]
    max_age_hours = age_value[1] if isinstance(age_value, tuple) and age_value[0] == "hours" else None
    max_age_days = age_value if isinstance(age_value, int) else None
    total_found = count_jobs(
        statuses=status_map[status_option],
        min_score=int(min_score),
        query=query,
        max_age_hours=max_age_hours,
        max_age_days=max_age_days,
        include_unknown_dates=include_unknown_dates,
        include_international=include_international,
        db_path=DB_PATH,
    )
    rows = list_jobs(
        statuses=status_map[status_option],
        min_score=int(min_score),
        query=query,
        max_age_hours=max_age_hours,
        max_age_days=max_age_days,
        include_unknown_dates=include_unknown_dates,
        include_international=include_international,
        db_path=DB_PATH,
        limit=int(display_limit),
    )
    st.caption(f"{total_found} vagas encontradas nos filtros. Exibindo {len(rows)}.")

    if not rows:
        st.info("Nenhuma vaga encontrada para esses filtros.")
        return

    for index, row in enumerate(rows):
        show_job_card(row, index)


def applications_tab() -> None:
    st.subheader("Candidaturas")
    rows = list_jobs(statuses=["applied"], min_score=0, db_path=DB_PATH)
    if not rows:
        st.info("Nenhuma candidatura registrada ainda.")
        return

    for index, row in enumerate(rows):
        with st.container(border=True):
            st.markdown(f"#### {row['title']}")
            st.caption(f"{row['company']} | {row['location']} | Candidatado em {row['applied_at'] or '-'}")
            if row["url"]:
                st.link_button("Abrir vaga", row["url"])
            notes = st.text_area("Notas", value=row["notes"] or "", key=f"notes-{row['id']}", height=90)
            cols = st.columns([1, 1, 4])
            if cols[0].button("Salvar nota", key=f"save-note-{row['id']}"):
                update_job_notes(row["id"], notes, DB_PATH)
                st.success("Nota salva.")
            if cols[1].button("Reabrir", key=f"reopen-{row['id']}"):
                set_status(row["id"], "saved")


def manual_job_tab() -> None:
    st.subheader("Adicionar vaga manual")
    st.caption("Use quando encontrar uma vaga no LinkedIn ou em outro site que ainda nao aparece nas fontes.")
    with st.form("manual-job-form"):
        c1, c2 = st.columns(2)
        with c1:
            title = st.text_input("Cargo")
            company = st.text_input("Empresa")
            location = st.text_input("Localizacao", placeholder="Remoto, Brasil, Sao Paulo...")
        with c2:
            url = st.text_input("Link da vaga")
            has_date = st.checkbox("Informar data de publicacao", value=True)
            published_date = st.date_input("Data de publicacao") if has_date else None
        description = st.text_area("Descricao da vaga", height=260)
        submit_cols = st.columns(2)
        submitted = submit_cols[0].form_submit_button("Salvar vaga", type="primary")
        submitted_applied = submit_cols[1].form_submit_button("Salvar como candidatada")

    if submitted or submitted_applied:
        if not title or not company or not url:
            st.error("Preencha pelo menos cargo, empresa e link.")
            return
        profile = load_profile(PROFILE_PATH)
        job = Job(
            title=title,
            company=company,
            location=location,
            url=url,
            description=description,
            source="manual",
            published_at=published_date.isoformat() if published_date else "",
            categories={},
        )
        result = score_job(profile, job)
        upsert_match_results([result], DB_PATH)
        if submitted_applied:
            update_job_status(make_job_id(job), "applied", DB_PATH)
            st.success(f"Vaga salva como candidatura realizada com {result.score}% de match.")
        else:
            st.success(f"Vaga salva com {result.score}% de match.")


def dashboard_tab() -> None:
    st.subheader("Dashboard")
    counts = dashboard_counts(DB_PATH)
    by_status = counts["by_status"]

    cols = st.columns(6)
    cols[0].metric("Vagas", counts["total"])
    cols[1].metric("Match medio", counts["avg_score"])
    cols[2].metric("Salvas", by_status.get("saved", 0))
    cols[3].metric("Candidatadas", by_status.get("applied", 0))
    cols[4].metric("Ignoradas", by_status.get("ignored", 0))
    cols[5].metric("Ultimas 24h", count_jobs(max_age_hours=24, include_unknown_dates=False, db_path=DB_PATH))

    chart_cols = st.columns(2)
    status_df = pd.DataFrame(
        [{"status": STATUS_LABELS.get(status, status), "total": total} for status, total in by_status.items()]
    )
    if not status_df.empty:
        chart_cols[0].bar_chart(status_df, x="status", y="total")

    source_df = pd.DataFrame(counts["by_source"])
    if not source_df.empty:
        chart_cols[1].bar_chart(source_df, x="source", y="total")

    companies_df = pd.DataFrame(counts["top_companies"])
    if not companies_df.empty:
        st.dataframe(companies_df, hide_index=True, use_container_width=True)


def settings_tab() -> None:
    st.subheader("Configuracoes")
    profile = read_yaml(PROFILE_PATH)
    sources = read_yaml(SOURCES_PATH)
    skills = profile.get("skills", {})
    salary_preferences = profile.get("salary_preferences", {})
    match_settings = {**DEFAULT_MATCH_SETTINGS, **profile.get("match_settings", {})}

    profile_section, sources_section = st.tabs(["Perfil de match", "Fontes de vagas"])

    with profile_section:
        st.caption("Estes campos sao os que entram diretamente no score de recomendacao.")
        with st.form("profile-form"):
            c1, c2 = st.columns(2)
            with c1:
                name = st.text_input("Nome", value=profile.get("name", ""))
                priority_roles = st.text_area(
                    "Cargos prioritarios",
                    value=list_to_text(profile.get("priority_roles")),
                    height=180,
                )
                target_roles = st.text_area(
                    "Cargos semelhantes aceitos",
                    value=list_to_text(profile.get("target_roles")),
                    height=180,
                )
                seniority_options = [
                    "estagio",
                    "internship",
                    "trainee",
                    "junior",
                    "jr",
                    "pleno",
                    "pl",
                    "mid-level",
                    "mid level",
                    "associate",
                    "senior",
                    "sr",
                ]
                seniority_default = profile.get("seniority", [])
                seniority_options = list(dict.fromkeys([*seniority_options, *seniority_default]))
                seniority = st.multiselect(
                    "Senioridade",
                    seniority_options,
                    default=seniority_default,
                )
                locations = st.text_area(
                    "Cidades aceitas para presencial/hibrido",
                    value=list_to_text(profile.get("locations")),
                    height=130,
                )
                junior_salary = st.number_input(
                    "Salario minimo para junior (BRL)",
                    min_value=0,
                    max_value=50000,
                    value=int(salary_preferences.get("junior_min_monthly_brl") or 0),
                    step=500,
                )
            with c2:
                core_skills = st.text_area(
                    "Skills fortes",
                    value=list_to_text(skills.get("core")),
                    height=220,
                )
                nice_skills = st.text_area(
                    "Skills complementares",
                    value=list_to_text(skills.get("nice_to_have")),
                    height=180,
                )
                business_domains = st.text_area(
                    "Dominios de negocio",
                    value=list_to_text(profile.get("business_domains")),
                    height=130,
                )
                avoid = st.text_area(
                    "Termos para penalizar",
                    value=list_to_text(profile.get("avoid")),
                    height=130,
                )

            flexible_junior_roles = st.text_area(
                "Vagas junior com flexibilidade salarial",
                value=list_to_text(salary_preferences.get("flexible_junior_roles")),
                height=100,
            )
            with st.expander("Regras do match"):
                r1, r2, r3 = st.columns(3)
                min_score_to_show = r1.number_input(
                    "Match minimo exibido",
                    min_value=1,
                    max_value=100,
                    value=max(1, int(match_settings["min_score_to_show"])),
                    step=1,
                )
                min_score_to_store = r1.number_input(
                    "Match minimo salvo no banco",
                    min_value=1,
                    max_value=100,
                    value=max(1, int(match_settings["min_score_to_store"])),
                    step=1,
                )
                max_job_age_days_to_store = r1.number_input(
                    "Idade maxima salva no banco (dias)",
                    min_value=1,
                    max_value=30,
                    value=min(30, int(match_settings["max_job_age_days_to_store"])),
                    step=1,
                )
                priority_role_weight = r1.number_input(
                    "Peso cargo prioritario",
                    min_value=0,
                    max_value=100,
                    value=int(match_settings["priority_role_weight"]),
                    step=1,
                )
                target_role_weight = r1.number_input(
                    "Peso cargo semelhante",
                    min_value=0,
                    max_value=100,
                    value=int(match_settings["target_role_weight"]),
                    step=1,
                )
                core_skills_weight = r2.number_input(
                    "Peso skills fortes",
                    min_value=0,
                    max_value=100,
                    value=int(match_settings["core_skills_weight"]),
                    step=1,
                )
                nice_to_have_skills_weight = r2.number_input(
                    "Peso skills complementares",
                    min_value=0,
                    max_value=100,
                    value=int(match_settings["nice_to_have_skills_weight"]),
                    step=1,
                )
                business_domain_weight = r2.number_input(
                    "Peso dominio de negocio",
                    min_value=0,
                    max_value=100,
                    value=int(match_settings["business_domain_weight"]),
                    step=1,
                )
                seniority_weight = r3.number_input(
                    "Bonus senioridade",
                    min_value=0,
                    max_value=50,
                    value=int(match_settings["seniority_weight"]),
                    step=1,
                )
                location_weight = r3.number_input(
                    "Bonus localizacao",
                    min_value=0,
                    max_value=50,
                    value=int(match_settings["location_weight"]),
                    step=1,
                )
                missing_location_penalty = r3.number_input(
                    "Penalidade localizacao fora",
                    min_value=-100,
                    max_value=0,
                    value=int(match_settings["missing_location_penalty"]),
                    step=1,
                )
                p1, p2, p3 = st.columns(3)
                no_role_penalty = p1.number_input(
                    "Penalidade cargo fora do foco",
                    min_value=-100,
                    max_value=0,
                    value=int(match_settings["no_role_penalty"]),
                    step=1,
                )
                avoid_penalty = p1.number_input(
                    "Penalidade termos bloqueio",
                    min_value=-100,
                    max_value=0,
                    value=int(match_settings["avoid_penalty"]),
                    step=1,
                )
                junior_salary_bonus = p2.number_input(
                    "Bonus salario junior ok",
                    min_value=0,
                    max_value=50,
                    value=int(match_settings["junior_salary_bonus"]),
                    step=1,
                )
                junior_salary_penalty = p2.number_input(
                    "Penalidade salario junior baixo",
                    min_value=-100,
                    max_value=0,
                    value=int(match_settings["junior_salary_penalty"]),
                    step=1,
                )

            if st.form_submit_button("Salvar perfil", type="primary"):
                profile["name"] = name
                profile["priority_roles"] = text_to_list(priority_roles)
                profile["target_roles"] = text_to_list(target_roles)
                profile["seniority"] = seniority
                profile["locations"] = text_to_list(locations)
                profile["skills"] = {
                    **skills,
                    "core": text_to_list(core_skills),
                    "nice_to_have": text_to_list(nice_skills),
                }
                profile["business_domains"] = text_to_list(business_domains)
                profile["avoid"] = text_to_list(avoid)
                profile["salary_preferences"] = {
                    **salary_preferences,
                    "junior_min_monthly_brl": int(junior_salary),
                    "flexible_junior_roles": text_to_list(flexible_junior_roles),
                }
                profile["match_settings"] = {
                    "min_score_to_show": int(min_score_to_show),
                    "min_score_to_store": int(min_score_to_store),
                    "max_job_age_days_to_store": int(max_job_age_days_to_store),
                    "core_skills_weight": int(core_skills_weight),
                    "nice_to_have_skills_weight": int(nice_to_have_skills_weight),
                    "business_domain_weight": int(business_domain_weight),
                    "priority_role_weight": int(priority_role_weight),
                    "target_role_weight": int(target_role_weight),
                    "seniority_weight": int(seniority_weight),
                    "location_weight": int(location_weight),
                    "missing_location_penalty": int(missing_location_penalty),
                    "no_role_penalty": int(no_role_penalty),
                    "avoid_penalty": int(avoid_penalty),
                    "junior_salary_bonus": int(junior_salary_bonus),
                    "junior_salary_penalty": int(junior_salary_penalty),
                }
                write_yaml(PROFILE_PATH, profile)
                st.success("Perfil salvo. Clique em Recalcular banco para atualizar os scores das vagas ja salvas.")

        with st.expander("Editor avancado do profile.yaml"):
            profile_text = st.text_area("YAML do perfil", value=read_text(PROFILE_PATH), height=360)
            if st.button("Salvar YAML do perfil"):
                try:
                    save_yaml_text(PROFILE_PATH, profile_text)
                except yaml.YAMLError as error:
                    st.error(f"YAML invalido: {error}")
                else:
                    st.success("YAML salvo.")

    with sources_section:
        st.caption("Greenhouse e Lever sao ATS por empresa. Remotive, RemoteOK, Arbeitnow e Solides ampliam a busca.")
        with st.form("sources-form"):
            greenhouse = st.text_area("Greenhouse boards", value=list_to_text(sources.get("greenhouse")), height=260)
            lever = st.text_area("Lever slugs", value=list_to_text(sources.get("lever")), height=120)
            remotive = st.text_area("Remotive categorias", value=list_to_text(sources.get("remotive")), height=80)
            remoteok = st.checkbox("Usar RemoteOK", value=bool(sources.get("remoteok")))
            arbeitnow_pages = st.number_input(
                "Paginas do Arbeitnow",
                min_value=0,
                max_value=20,
                value=int((sources.get("arbeitnow") or [5])[0]),
                step=1,
            )
            solides_pages = st.number_input(
                "Paginas por termo na Solides",
                min_value=0,
                max_value=10,
                value=int((sources.get("solides") or [3])[0]),
                step=1,
            )
            if st.form_submit_button("Salvar fontes", type="primary"):
                write_yaml(
                    SOURCES_PATH,
                    {
                        "greenhouse": text_to_list(greenhouse),
                        "lever": text_to_list(lever),
                        "remotive": text_to_list(remotive),
                        "remoteok": ["data"] if remoteok else [],
                        "arbeitnow": [str(int(arbeitnow_pages))] if arbeitnow_pages else [],
                        "solides": [str(int(solides_pages))] if solides_pages else [],
                    },
                )
                st.success("Fontes salvas. Clique em Buscar vagas agora para coletar dessas fontes.")

        with st.expander("Editor avancado do sources.yaml"):
            sources_text = st.text_area("YAML das fontes", value=read_text(SOURCES_PATH), height=260)
            if st.button("Salvar YAML das fontes"):
                try:
                    save_yaml_text(SOURCES_PATH, sources_text)
                except yaml.YAMLError as error:
                    st.error(f"YAML invalido: {error}")
                else:
                    st.success("YAML salvo.")


def main() -> None:
    st.set_page_config(page_title="Busca Vagas Dados", layout="wide")
    init_db(DB_PATH)

    st.title("Busca Vagas Dados")
    st.caption("Recomendacoes, candidaturas e configuracoes do seu perfil.")

    with st.sidebar:
        st.header("Busca")
        if st.button("Buscar vagas agora", type="primary", use_container_width=True):
            with st.spinner("Buscando e ranqueando vagas..."):
                summary = refresh_recommendations(PROFILE_PATH, SOURCES_PATH, DB_PATH)
            st.success(
                f"{summary['ranked']} vagas ranqueadas. "
                f"{summary['saved']} registros atualizados no banco. "
                f"{summary.get('pruned', 0)} irrelevantes removidas. "
                f"{summary.get('stale_pruned', 0)} antigas removidas."
            )
            if summary["errors"]:
                with st.expander("Avisos das fontes"):
                    for error in summary["errors"]:
                        st.write(error)

        if st.button("Recalcular banco", use_container_width=True):
            with st.spinner("Recalculando scores..."):
                summary = rescore_existing_jobs(PROFILE_PATH, DB_PATH)
            st.success(
                f"{summary['rescored']} vagas recalculadas. "
                f"{summary.get('pruned', 0)} irrelevantes removidas. "
                f"{summary.get('stale_pruned', 0)} antigas removidas."
            )

        st.divider()
        st.caption(f"Banco: {DB_PATH}")

    tabs = st.tabs(["Recomendacoes", "Candidaturas", "Adicionar vaga", "Dashboard", "Configuracoes"])
    with tabs[0]:
        recommendations_tab()
    with tabs[1]:
        applications_tab()
    with tabs[2]:
        manual_job_tab()
    with tabs[3]:
        dashboard_tab()
    with tabs[4]:
        settings_tab()


if __name__ == "__main__":
    main()
