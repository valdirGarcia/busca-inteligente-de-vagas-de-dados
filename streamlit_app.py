from __future__ import annotations

import html
import json
import re
import unicodedata
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
    list_available_sources,
    list_jobs,
    list_search_runs,
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

JOB_MODE_OPTIONS = {
    "Remoto": "remote",
    "Presencial na regiao": "region_onsite",
    "Hibrido na regiao": "region_hybrid",
    "Fora da regiao": "outside_region",
}

IGNORE_REASON_OPTIONS = [
    "Localidade fora da preferencia",
    "Senioridade desalinhada",
    "Stack pouco aderente",
    "Cargo fora do foco",
    "Salario nao compensa",
    "Empresa/setor pouco interessante",
    "Vaga repetida",
    "Descricao fraca ou vaga confusa",
    "Outro",
]


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


def yaml_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return text_to_list(str(value))


def first_int(values: object, default: int) -> int:
    items = yaml_list(values)
    if not items:
        return default
    try:
        return int(items[0])
    except ValueError:
        return default


def format_date(value: str | None) -> str:
    if not value:
        return "Data nao informada"
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return value[:10]
    return parsed.date().isoformat()


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def contains_term(text: str, term: str) -> bool:
    normalized_text = normalize_text(text)
    normalized_term = normalize_text(term)
    if not normalized_term:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(normalized_term) + r"(?![a-z0-9])"
    return re.search(pattern, normalized_text) is not None


def set_status(job_id: str, status: str, ignored_reason: str = "") -> None:
    update_job_status(job_id, status, DB_PATH, ignored_reason=ignored_reason)
    st.rerun()


def row_list(row, column: str) -> list[str]:
    return parse_json_list(row[column])


def parse_json_dict(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {str(key): item for key, item in parsed.items()}
    return {}


def row_score_details(row) -> list[dict[str, object]]:
    try:
        value = row["score_details_json"]
    except (IndexError, KeyError):
        value = ""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    details = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            points = int(item.get("points", 0))
        except (TypeError, ValueError):
            points = 0
        details.append(
            {
                "component": str(item.get("component") or ""),
                "points": points,
                "detail": str(item.get("detail") or ""),
            }
        )
    return details


def row_categories(row) -> dict[str, str]:
    try:
        parsed = json.loads(row["categories_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {str(key): str(value) for key, value in parsed.items()}
    return {}


def row_is_remote(row, categories: dict[str, str]) -> bool:
    location = normalize_text(row["location"] or "")
    is_remote = normalize_text(categories.get("is_remote", ""))
    workplace_type = normalize_text(categories.get("workplace_type", ""))
    job_type = normalize_text(categories.get("job_type", ""))
    category_text = normalize_text(" ".join(categories.values()))
    return (
        row["source"] in {"jobicy", "remotive", "remoteok"}
        or "remot" in location
        or "home office" in location
        or "teletrabalho" in location
        or is_remote == "true"
        or workplace_type == "remote"
        or job_type == "remoto"
        or "home office" in category_text
        or "teletrabalho" in category_text
    )


def row_is_hybrid(row, categories: dict[str, str]) -> bool:
    location = normalize_text(row["location"] or "")
    workplace_type = normalize_text(categories.get("workplace_type", ""))
    job_type = normalize_text(categories.get("job_type", ""))
    return "hibrid" in location or "hybrid" in location or workplace_type == "hybrid" or job_type == "hibrido"


def row_is_region(row, preferred_locations: list[str]) -> bool:
    location = row["location"] or ""
    return any(contains_term(location, location_term) for location_term in preferred_locations)


def pill(label: str, tone: str = "default") -> str:
    colors = {
        "default": ("#252936", "#d8dee9"),
        "good": ("#123524", "#8df0bc"),
        "warn": ("#3a2f12", "#ffd36e"),
        "bad": ("#3a1821", "#ff9bad"),
        "info": ("#152c45", "#8ecbff"),
    }
    background, foreground = colors.get(tone, colors["default"])
    safe_label = html.escape(label)
    return (
        f"<span style='display:inline-block;padding:3px 9px;margin:0 6px 6px 0;"
        f"border-radius:999px;background:{background};color:{foreground};"
        f"font-size:12px;font-weight:700;'>{safe_label}</span>"
    )


def job_badges(row, preferred_locations: list[str]) -> str:
    categories = row_categories(row)
    badges = [pill(str(row["source"]).upper(), "info")]
    remote = row_is_remote(row, categories)
    hybrid = row_is_hybrid(row, categories)
    region = row_is_region(row, preferred_locations)

    if remote:
        badges.append(pill("Remoto", "good"))
        badges.append(pill("Aceito remoto", "good"))
    elif hybrid and region:
        badges.append(pill("Hibrido na regiao", "good"))
    elif hybrid:
        badges.append(pill("Hibrido fora da regiao", "bad"))
    elif region:
        badges.append(pill("Presencial na regiao", "good"))
    elif not remote and row["location"]:
        badges.append(pill("Fora da regiao", "bad"))
    elif not remote:
        badges.append(pill("Local incerto", "warn"))

    return "".join(badges)


def match_component_lines(row) -> list[str]:
    matched_skills = row_list(row, "matched_skills_json")
    matched_domains = row_list(row, "matched_domains_json")
    gaps = row_list(row, "gaps_json")
    reasons = row_list(row, "reasons_json")

    cargo = "alinhado" if "cargo alinhado" in reasons or "cargo prioritario" in reasons else "fora do foco"
    seniority = "alinhada" if "senioridade alinhada" in reasons else "nao detectada"
    location = "alinhada" if "localizacao alinhada" in reasons else "fora da preferencia"
    domains = ", ".join(matched_domains[:5]) if matched_domains else "sem dominio forte detectado"
    penalties = [reason for reason in reasons if "exclusao" in reason or "fora" in reason]

    return [
        f"Cargo: {cargo}.",
        f"Skills: {len(matched_skills)} encontradas; {len(gaps)} skills suas nao citadas.",
        f"Senioridade: {seniority}.",
        f"Localidade: {location}.",
        f"Dominio: {domains}.",
        f"Penalidades/sinais fracos: {', '.join(penalties) if penalties else 'nenhum sinal critico detectado'}.",
    ]


def match_detail_table(row) -> tuple[pd.DataFrame, int]:
    details = row_score_details(row)
    raw_score = sum(int(item["points"]) for item in details)
    table = pd.DataFrame(
        [
            {
                "Componente": item["component"],
                "Impacto": f"{int(item['points']):+d}",
                "Detalhe": item["detail"],
            }
            for item in details
        ]
    )
    return table, raw_score


def show_job_card(row, index: int, preferred_locations: list[str]) -> None:
    with st.container(border=True):
        title_col, score_col = st.columns([5, 1])
        with title_col:
            st.markdown(f"#### {row['title']}")
            st.markdown(job_badges(row, preferred_locations), unsafe_allow_html=True)
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
        with st.expander("Ver explicacao do match"):
            score_table, raw_score = match_detail_table(row)
            if not score_table.empty:
                st.table(score_table)
                st.caption(f"Score bruto: {raw_score}. Score exibido: {row['score']}%, limitado entre 0 e 100.")
            else:
                st.caption("Detalhamento numerico disponivel apos buscar ou recalcular o banco.")
                for line in match_component_lines(row):
                    st.write(line)

        contact = row["contact_email"] or "Nao encontrado na descricao"
        st.caption(f"Contato/e-mail: {contact}")
        if row["status"] == "ignored" and row["ignored_reason"]:
            st.caption(f"Motivo para ignorar: {row['ignored_reason']}")

        action_cols = st.columns([1.2, 1, 1, 1, 3])
        if row["url"]:
            action_cols[0].link_button("Abrir vaga", row["url"])
        if action_cols[1].button("Salvar", key=f"save-{row['id']}-{index}"):
            set_status(row["id"], "saved")
        if action_cols[2].button("Candidatei", key=f"applied-{row['id']}-{index}", type="primary"):
            set_status(row["id"], "applied")
        with action_cols[3]:
            with st.popover("Ignorar"):
                ignore_reason = st.selectbox(
                    "Motivo",
                    IGNORE_REASON_OPTIONS,
                    key=f"ignore-reason-{row['id']}-{index}",
                )
                ignore_detail = st.text_input(
                    "Detalhe opcional",
                    key=f"ignore-detail-{row['id']}-{index}",
                    placeholder="Ex: pede senior, longe demais...",
                )
                reason_text = ignore_reason if not ignore_detail.strip() else f"{ignore_reason}: {ignore_detail.strip()}"
                if st.button("Confirmar ignorar", key=f"confirm-ignore-{row['id']}-{index}", type="primary"):
                    set_status(row["id"], "ignored", reason_text)


def recommendations_tab() -> None:
    st.subheader("Recomendacoes")
    profile = read_yaml(PROFILE_PATH)
    match_settings = profile.get("match_settings", {})
    configured_min_score = max(1, int(match_settings.get("min_score_to_show", 1)))
    preferred_locations = yaml_list(profile.get("locations"))
    source_options = list_available_sources(DB_PATH)
    with st.container(border=True):
        filter_cols = st.columns([1.7, 2.4, 1.0, 1.0, 0.9])
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
            ["24 horas", "3 dias", "7 dias"],
            index=2,
        )
        display_limit = filter_cols[4].number_input("Qtd. exibida", min_value=20, max_value=1000, value=300, step=20)

        st.markdown("**Modalidade e local**")
        mode_cols = st.columns(4)
        selected_job_mode_labels = []
        if mode_cols[0].checkbox("Remoto", value=True):
            selected_job_mode_labels.append("Remoto")
        if mode_cols[1].checkbox("Presencial na regiao", value=True):
            selected_job_mode_labels.append("Presencial na regiao")
        if mode_cols[2].checkbox("Hibrido na regiao", value=True):
            selected_job_mode_labels.append("Hibrido na regiao")
        if mode_cols[3].checkbox("Fora da regiao", value=False):
            selected_job_mode_labels.append("Fora da regiao")
        option_cols = st.columns(2)
        include_unknown_dates = option_cols[0].checkbox("Incluir vagas sem data informada", value=False)
        include_international = option_cols[1].checkbox("Mostrar vagas internacionais", value=False)
        extra_filter_cols = st.columns([1.2, 1.8])
        selected_locations = extra_filter_cols[0].multiselect(
            "Cidades da regiao",
            preferred_locations,
            default=[],
            placeholder="Todas as cidades",
            help="Vazio usa todas as cidades do perfil. Este filtro refina apenas o bloco Regiao.",
        )
        selected_sources = extra_filter_cols[1].multiselect(
            "Fontes",
            source_options,
            default=[],
            placeholder="Todas as fontes",
            help="Vazio usa todas as fontes disponiveis no banco.",
        )

    job_modes = [JOB_MODE_OPTIONS[label] for label in selected_job_mode_labels]
    source_filter = selected_sources or None

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
        "3 dias": 3,
        "7 dias": 7,
    }
    age_value = age_map[age_option]
    max_age_hours = age_value[1] if isinstance(age_value, tuple) and age_value[0] == "hours" else None
    max_age_days = age_value if isinstance(age_value, int) else None
    if status_option in {"Salvas", "Candidatadas"}:
        max_age_hours = None
        max_age_days = None
    total_found = count_jobs(
        statuses=status_map[status_option],
        min_score=int(min_score),
        query=query,
        max_age_hours=max_age_hours,
        max_age_days=max_age_days,
        include_unknown_dates=include_unknown_dates,
        include_international=include_international,
        job_modes=job_modes,
        preferred_locations=preferred_locations,
        selected_locations=selected_locations,
        sources=source_filter,
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
        job_modes=job_modes,
        preferred_locations=preferred_locations,
        selected_locations=selected_locations,
        sources=source_filter,
        db_path=DB_PATH,
        limit=int(display_limit),
    )
    st.caption(f"{total_found} vagas encontradas nos filtros. Exibindo {len(rows)}.")

    if not rows:
        st.info("Nenhuma vaga encontrada para esses filtros.")
        return

    for index, row in enumerate(rows):
        show_job_card(row, index, preferred_locations)


def jobs_export_df(rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "cargo": row["title"],
                "empresa": row["company"],
                "localizacao": row["location"],
                "fonte": row["source"],
                "publicada_em": format_date(row["published_at"]),
                "match": row["score"],
                "status": row["status"],
                "candidatado_em": row["applied_at"] or "",
                "contato_email": row["contact_email"] or "",
                "link": row["url"] or "",
                "notas": row["notes"] or "",
            }
            for row in rows
        ]
    )


def applications_tab() -> None:
    st.subheader("Candidaturas")
    rows = list_jobs(statuses=["applied"], min_score=0, db_path=DB_PATH)
    if not rows:
        st.info("Nenhuma candidatura registrada ainda.")
        return

    export_df = jobs_export_df(rows)
    st.download_button(
        "Baixar candidaturas CSV",
        data=export_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"candidaturas_{datetime.now().date().isoformat()}.csv",
        mime="text/csv",
    )

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
    profile = read_yaml(PROFILE_PATH)
    match_settings = {**DEFAULT_MATCH_SETTINGS, **profile.get("match_settings", {})}
    min_score_to_show = max(1, int(match_settings["min_score_to_show"]))
    min_score_to_store = max(1, int(match_settings["min_score_to_store"]))
    preferred_locations = yaml_list(profile.get("locations"))
    default_job_modes = ["remote", "region_onsite", "region_hybrid"]

    counts = dashboard_counts(DB_PATH)
    by_status = counts["by_status"]
    recent_count = count_jobs(max_age_days=7, include_unknown_dates=False, db_path=DB_PATH)
    eligible_count = count_jobs(
        min_score=min_score_to_store,
        max_age_days=7,
        include_unknown_dates=False,
        db_path=DB_PATH,
    )
    recommended_count = count_jobs(
        statuses=["new", "saved"],
        min_score=min_score_to_show,
        max_age_days=7,
        include_unknown_dates=False,
        include_international=False,
        job_modes=default_job_modes,
        preferred_locations=preferred_locations,
        db_path=DB_PATH,
    )
    last_24h_count = count_jobs(max_age_hours=24, include_unknown_dates=False, db_path=DB_PATH)

    cols = st.columns(6)
    cols[0].metric("Banco", counts["total"])
    cols[1].metric("Recentes", recent_count)
    cols[2].metric("Recomendadas", recommended_count)
    cols[3].metric("Match medio", counts["avg_score"])
    cols[4].metric("Candidatadas", by_status.get("applied", 0))
    cols[5].metric("Ultimas 24h", last_24h_count)

    st.markdown("**Funil de recomendacao**")
    funnel_df = pd.DataFrame(
        [
            {"fase": "No banco", "total": counts["total"]},
            {"fase": "Recentes ate 7 dias", "total": recent_count},
            {"fase": f"Acima do minimo salvo ({min_score_to_store}%)", "total": eligible_count},
            {"fase": f"Filtro padrao da tela ({min_score_to_show}%+)", "total": recommended_count},
            {"fase": "Salvas", "total": by_status.get("saved", 0)},
            {"fase": "Candidatadas", "total": by_status.get("applied", 0)},
        ]
    )
    st.dataframe(
        funnel_df.rename(columns={"fase": "Fase", "total": "Total"}),
        hide_index=True,
        use_container_width=True,
    )
    st.bar_chart(funnel_df, x="fase", y="total")

    chart_cols = st.columns(2)
    status_df = pd.DataFrame(
        [{"status": STATUS_LABELS.get(status, status), "total": total} for status, total in by_status.items()]
    )
    if not status_df.empty:
        chart_cols[0].bar_chart(status_df, x="status", y="total")

    source_df = pd.DataFrame(counts["by_source"])
    if not source_df.empty:
        source_df["recommended"] = source_df["source"].apply(
            lambda source: count_jobs(
                statuses=["new", "saved"],
                min_score=min_score_to_show,
                max_age_days=7,
                include_unknown_dates=False,
                include_international=False,
                job_modes=default_job_modes,
                preferred_locations=preferred_locations,
                sources=[source],
                db_path=DB_PATH,
            )
        )
        source_df["recommendation_rate"] = (
            (source_df["recommended"] / source_df["total"].replace(0, pd.NA)) * 100
        ).fillna(0).round(1)
        chart_cols[1].bar_chart(source_df, x="source", y="recommended")
        st.markdown("**Qualidade por fonte**")
        source_view = source_df.rename(
            columns={
                "source": "Fonte",
                "total": "No banco",
                "recommended": "Recomendadas",
                "recommendation_rate": "Taxa recomendada (%)",
                "avg_score": "Match medio",
                "best_score": "Melhor match",
                "strong_matches": "Matches 40%+",
            }
        )
        st.dataframe(source_view, hide_index=True, use_container_width=True)

    companies_df = pd.DataFrame(counts["top_companies"])
    if not companies_df.empty:
        st.markdown("**Empresas com melhor sinal**")
        st.dataframe(companies_df, hide_index=True, use_container_width=True)

    ignored_reasons_df = pd.DataFrame(counts["ignored_reasons"])
    if not ignored_reasons_df.empty:
        st.markdown("**Motivos das vagas ignoradas**")
        st.dataframe(
            ignored_reasons_df.rename(columns={"reason": "Motivo", "total": "Total"}),
            hide_index=True,
            use_container_width=True,
        )

    last_refresh = st.session_state.get("last_refresh_summary")
    if last_refresh:
        with st.expander("Ultima busca desta sessao"):
            st.write(
                f"{last_refresh.get('fetched', 0)} vagas coletadas, "
                f"{last_refresh.get('ranked', 0)} ranqueadas, "
                f"{last_refresh.get('eligible', 0)} elegiveis e "
                f"{last_refresh.get('saved', 0)} gravadas/atualizadas."
            )
            by_source = last_refresh.get("fetched_by_source") or {}
            if by_source:
                st.dataframe(
                    pd.DataFrame(
                        [{"Fonte": source, "Coletadas": total} for source, total in sorted(by_source.items())]
                    ),
                    hide_index=True,
                    use_container_width=True,
                )

    search_runs = list_search_runs(DB_PATH, limit=10)
    if search_runs:
        st.markdown("**Historico de buscas**")
        runs_df = pd.DataFrame(
            [
                {
                    "Inicio": format_date(run["started_at"]),
                    "Coletadas": run["fetched"],
                    "Ranqueadas": run["ranked"],
                    "Elegiveis": run["eligible"],
                    "Atualizadas": run["saved"],
                    "Removidas": int(run["pruned"]) + int(run["stale_pruned"]),
                    "Avisos": len(parse_json_list(run["errors_json"])),
                }
                for run in search_runs
            ]
        )
        st.dataframe(runs_df, hide_index=True, use_container_width=True)

        latest_run = search_runs[0]
        fetched_by_source = parse_json_dict(latest_run["fetched_by_source_json"])
        eligible_by_source = parse_json_dict(latest_run["eligible_by_source_json"])
        if fetched_by_source or eligible_by_source:
            source_names = sorted({*fetched_by_source.keys(), *eligible_by_source.keys()})
            latest_source_df = pd.DataFrame(
                [
                    {
                        "Fonte": source,
                        "Coletadas": int(fetched_by_source.get(source, 0)),
                        "Elegiveis": int(eligible_by_source.get(source, 0)),
                    }
                    for source in source_names
                ]
            )
            with st.expander("Ultima busca gravada por fonte"):
                st.dataframe(latest_source_df, hide_index=True, use_container_width=True)


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
                    max_value=7,
                    value=min(7, int(match_settings["max_job_age_days_to_store"])),
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
        st.caption(
            "Greenhouse, Lever, Ashby e SmartRecruiters sao ATS por empresa. "
            "Gupy, Solides, Netvagas, Remotive, RemoteOK, Arbeitnow e Jobicy ampliam a busca."
        )
        with st.form("sources-form"):
            st.markdown("**Fontes ativas**")
            active_cols = st.columns(4)
            use_greenhouse = active_cols[0].checkbox("Greenhouse", value=bool(sources.get("greenhouse")))
            use_lever = active_cols[1].checkbox("Lever", value=bool(sources.get("lever")))
            use_ashby = active_cols[2].checkbox("Ashby", value=bool(sources.get("ashby")))
            use_smartrecruiters = active_cols[3].checkbox(
                "SmartRecruiters",
                value=bool(sources.get("smartrecruiters")),
            )
            active_cols_2 = st.columns(4)
            use_gupy = active_cols_2[0].checkbox("Gupy", value=bool(sources.get("gupy")))
            use_solides = active_cols_2[1].checkbox("Solides", value=bool(sources.get("solides")))
            use_remotive = active_cols_2[2].checkbox("Remotive", value=bool(sources.get("remotive")))
            use_arbeitnow = active_cols_2[3].checkbox("Arbeitnow", value=bool(sources.get("arbeitnow")))
            active_cols_3 = st.columns(4)
            use_jobicy = active_cols_3[0].checkbox("Jobicy", value=bool(sources.get("jobicy")))
            use_netvagas = active_cols_3[1].checkbox("Netvagas", value=bool(sources.get("netvagas")))

            source_cols = st.columns(2)
            with source_cols[0]:
                greenhouse = st.text_area("Greenhouse boards", value=list_to_text(sources.get("greenhouse")), height=220)
                lever = st.text_area("Lever slugs", value=list_to_text(sources.get("lever")), height=100)
                ashby = st.text_area("Ashby boards", value=list_to_text(sources.get("ashby")), height=120)
            with source_cols[1]:
                smartrecruiters = st.text_area(
                    "SmartRecruiters empresas",
                    value=list_to_text(sources.get("smartrecruiters")),
                    height=180,
                )
                remotive = st.text_area("Remotive categorias", value=list_to_text(sources.get("remotive")), height=80)
                jobicy = st.text_area("Jobicy regioes", value=list_to_text(sources.get("jobicy")), height=80)
                remoteok = st.checkbox("Usar RemoteOK", value=bool(sources.get("remoteok")))
                page_cols = st.columns(4)
                arbeitnow_pages = page_cols[0].number_input(
                    "Paginas Arbeitnow",
                    min_value=0,
                    max_value=20,
                    value=first_int(sources.get("arbeitnow"), 20),
                    step=1,
                )
                solides_pages = page_cols[1].number_input(
                    "Paginas Solides",
                    min_value=0,
                    max_value=20,
                    value=first_int(sources.get("solides"), 20),
                    step=1,
                )
                netvagas_pages = page_cols[2].number_input(
                    "Paginas Netvagas",
                    min_value=0,
                    max_value=10,
                    value=first_int(sources.get("netvagas"), 3),
                    step=1,
                )
                smartrecruiters_pages = page_cols[3].number_input(
                    "Paginas SmartRecruiters",
                    min_value=1,
                    max_value=10,
                    value=first_int(sources.get("smartrecruiters_pages"), 5),
                    step=1,
                )
                gupy_pages = st.number_input(
                    "Paginas por termo na Gupy",
                    min_value=0,
                    max_value=20,
                    value=first_int(sources.get("gupy"), 8),
                    step=1,
                )
            if st.form_submit_button("Salvar fontes", type="primary"):
                write_yaml(
                    SOURCES_PATH,
                    {
                        "ashby": text_to_list(ashby) if use_ashby else [],
                        "greenhouse": text_to_list(greenhouse) if use_greenhouse else [],
                        "gupy": [str(int(gupy_pages))] if use_gupy and gupy_pages else [],
                        "jobicy": text_to_list(jobicy) if use_jobicy else [],
                        "netvagas": [str(int(netvagas_pages))] if use_netvagas and netvagas_pages else [],
                        "lever": text_to_list(lever) if use_lever else [],
                        "smartrecruiters": text_to_list(smartrecruiters) if use_smartrecruiters else [],
                        "smartrecruiters_pages": [str(int(smartrecruiters_pages))],
                        "remotive": text_to_list(remotive) if use_remotive else [],
                        "remoteok": ["data"] if remoteok else [],
                        "arbeitnow": [str(int(arbeitnow_pages))] if use_arbeitnow and arbeitnow_pages else [],
                        "solides": [str(int(solides_pages))] if use_solides and solides_pages else [],
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
            st.session_state["last_refresh_summary"] = summary
            st.success(
                f"{summary['ranked']} vagas ranqueadas. "
                f"{summary.get('eligible', summary['saved'])} passaram no match minimo. "
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
            st.session_state["last_rescore_summary"] = summary
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
