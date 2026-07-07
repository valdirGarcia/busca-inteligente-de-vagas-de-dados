from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from app.collectors.data_terms import looks_like_data_job
from app.models import Job
from app.text_utils import strip_html


NETVAGAS_BASE_URL = "https://www.netvagas.com.br"
NETVAGAS_DESCRIPTION_URL = f"{NETVAGAS_BASE_URL}/ajax/anuncio-descricao/"
NETVAGAS_TITLE_TERMS = [
    "analista de dados",
    "assistente de dados",
    "auxiliar de dados",
    "cientista de dados",
    "data analyst",
    "data scientist",
    "data analytics",
    "data science",
    "analytics engineer",
    "engenheiro de dados",
    "analista de bi",
    "assistente de bi",
    "power bi",
    "business intelligence",
    "analytics",
    "machine learning",
    "dados",
    "analista de indicadores",
    "analista de inteligencia",
    "analista de inteligencia comercial",
    "analista de inteligencia de negocios",
    "analista de inteligencia de mercado",
    "analista de informacoes",
    "analista de mis",
    "analista de relatorios",
    "analista de pricing",
    "analista de crm",
    "analista de planejamento",
    "analista de performance",
    "analista de risco de credito",
    "analista de credito",
    "politicas de credito",
    "analista de fraude",
    "analista antifraude",
    "product data analyst",
]


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _slugify(value: str) -> str:
    normalized = _normalize(value)
    return normalized.replace(" ", "-") or "dados"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _infer_job_type(title: str, location: str, description: str) -> str:
    location_text = _normalize(location)
    text = _normalize(" ".join([title, description[:800]]))
    if "home office" in location_text or "remoto" in location_text or "remote" in location_text:
        return "remoto"

    hybrid_cues = (
        "hibrid" in text
        or "dias presenciais" in text
        or "dia presencial" in text
        or ("home office" in text and "presencial" in text)
    )
    if hybrid_cues:
        return "hibrido"

    if "home office" in text or "remoto" in text or "remote" in text or "teletrabalho" in text:
        return "remoto"
    return ""


def _headers(referer: str = NETVAGAS_BASE_URL) -> dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 busca-vagas-app/0.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": referer,
    }


def _age_days_from_label(value: str) -> int | None:
    normalized = _normalize(value)
    if not normalized:
        return None
    if "hoje" in normalized or re.search(r"\b(minuto|minutos|hora|horas)\b", normalized):
        return 0
    if "ontem" in normalized:
        return 1

    days_match = re.search(r"\bha\s+(\d+)\s+dias?\b", normalized)
    if days_match:
        return int(days_match.group(1))

    weeks_match = re.search(r"\bha\s+(\d+)\s+semanas?\b", normalized)
    if weeks_match:
        return int(weeks_match.group(1)) * 7

    months_match = re.search(r"\bha\s+(\d+)\s+mes(?:es)?\b", normalized)
    if months_match:
        return int(months_match.group(1)) * 31

    return None


def _published_date_from_label(value: str) -> str:
    age_days = _age_days_from_label(value)
    if age_days is not None:
        return (datetime.now().date() - timedelta(days=age_days)).isoformat()

    explicit_date = re.search(r"(\d{2})/(\d{2})/(\d{4})", value)
    if explicit_date:
        day, month, year = explicit_date.groups()
        try:
            return date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return ""
    return ""


def _published_within_days(label: str, max_age_days: int) -> bool:
    age_days = _age_days_from_label(label)
    if age_days is not None:
        return age_days <= max_age_days

    published = _published_date_from_label(label)
    if not published:
        return False
    try:
        parsed = datetime.fromisoformat(published)
    except ValueError:
        return False
    cutoff = datetime.now() - timedelta(days=max_age_days)
    return parsed >= cutoff


class _NetvagasListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._div_depth = 0
        self._title_div_depth = 0
        self._city_active = False
        self._capture_field = ""
        self._await_company = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        classes = set(attr_map.get("class", "").lower().split())

        if self._current is None:
            if tag == "div" and "advs" in classes:
                self._current = {}
                self._div_depth = 1
            return

        if tag == "div":
            self._div_depth += 1
            if "advs_title" in classes:
                self._title_div_depth = self._div_depth

        if tag == "span" and "advs_data_inter" in classes:
            self._capture_field = "date_label"
        elif tag == "span" and "advs_city" in classes:
            self._city_active = True
        elif tag == "a" and self._title_div_depth:
            self._current["url"] = urljoin(NETVAGAS_BASE_URL, attr_map.get("href", ""))
            self._capture_field = "title"
        elif tag == "a" and self._city_active:
            self._capture_field = "location"
        elif tag == "button" and attr_map.get("id") == "anuncio_descricao":
            self._current["netvagas_id"] = attr_map.get("id_anuncio", "")

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return

        clean = _clean(data)
        if not clean:
            return

        if self._capture_field:
            previous = self._current.get(self._capture_field, "")
            self._current[self._capture_field] = _clean(f"{previous} {clean}")
            return

        upper = clean.upper()
        if "EMPRESA:" in upper:
            _, _, after = clean.partition(":")
            after = after.strip()
            if after:
                self._current["company"] = after
            else:
                self._await_company = True
            return

        if self._await_company:
            if upper.startswith("VAGA"):
                return
            self._current["company"] = clean
            self._await_company = False

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return

        if tag == "a" and self._capture_field in {"title", "location"}:
            self._capture_field = ""
        elif tag == "span":
            if self._capture_field == "date_label":
                self._capture_field = ""
            if self._city_active:
                self._city_active = False
        elif tag == "div":
            if self._title_div_depth == self._div_depth:
                self._title_div_depth = 0
            self._div_depth -= 1
            if self._div_depth <= 0:
                card = {key: _clean(value) for key, value in self._current.items() if _clean(value)}
                if card.get("title") and card.get("url"):
                    self.cards.append(card)
                self._current = None
                self._div_depth = 0
                self._title_div_depth = 0
                self._city_active = False
                self._capture_field = ""
                self._await_company = False


def _parse_cards(html: str) -> list[dict[str, str]]:
    parser = _NetvagasListParser()
    parser.feed(html)
    return parser.cards


def _fetch_page(term: str, page: int, timeout: int) -> str:
    slug = _slugify(term)
    url = f"{NETVAGAS_BASE_URL}/empresa/anuncios/cargo/{slug}/"
    if page > 1:
        url = f"{url}?{urlencode({'pagina': page})}"
    request = Request(url, headers=_headers(url))
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_description(netvagas_id: str, referer: str, timeout: int) -> str:
    if not netvagas_id:
        return ""

    payload = urlencode({"id_anuncio": netvagas_id}).encode("utf-8")
    headers = {
        **_headers(referer),
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": NETVAGAS_BASE_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    request = Request(NETVAGAS_DESCRIPTION_URL, data=payload, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")
    lines = [line.strip() for line in strip_html(html).splitlines() if line.strip()]
    while lines and _normalize(lines[0]) == "fechar":
        lines.pop(0)
    if lines and _normalize(lines[0]).startswith("descricao"):
        _, _, after = lines[0].partition(":")
        lines[0] = after.strip()
        if not lines[0]:
            lines.pop(0)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _fetch_term_cards(term: str, pages: int, max_age_days: int, timeout: int) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for page in range(1, pages + 1):
        page_cards = _parse_cards(_fetch_page(term, page, timeout))
        if not page_cards:
            break

        page_has_recent_card = False
        for card in page_cards:
            if not _published_within_days(card.get("date_label", ""), max_age_days):
                continue
            page_has_recent_card = True
            if looks_like_data_job(" ".join([card.get("title", ""), card.get("location", "")])):
                card["search_term"] = term
                cards.append(card)

        if not page_has_recent_card:
            break

    return cards


def _build_job(card: dict[str, str], timeout: int) -> Job | None:
    title = card.get("title", "")
    location = card.get("location", "")
    url = card.get("url", "")
    netvagas_id = card.get("netvagas_id", "")
    description = ""
    try:
        description = _fetch_description(netvagas_id, url, timeout)
    except Exception:
        description = ""

    searchable = " ".join([title, card.get("company", ""), location, description])
    if not looks_like_data_job(searchable):
        return None

    return Job(
        title=title,
        company=card.get("company", ""),
        location=location,
        url=url,
        description=description,
        source="netvagas",
        published_at=_published_date_from_label(card.get("date_label", "")),
        categories={
            "date_label": card.get("date_label", ""),
            "netvagas_id": netvagas_id,
            "search_term": card.get("search_term", ""),
            "job_type": _infer_job_type(title, location, description),
        },
    )


def fetch_netvagas_jobs(
    pages_per_term: int = 3,
    terms: list[str] | None = None,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    cards: dict[str, dict[str, str]] = {}
    search_terms = terms or NETVAGAS_TITLE_TERMS
    safe_pages = max(1, pages_per_term)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(_fetch_term_cards, term, safe_pages, max_age_days, timeout): term
            for term in search_terms
        }
        for future in as_completed(futures):
            try:
                term_cards = future.result()
            except Exception:
                continue

            for card in term_cards:
                cards[card["url"]] = card

    jobs: dict[str, Job] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_build_job, card, timeout): url for url, card in cards.items()}
        for future in as_completed(futures):
            try:
                job = future.result()
            except Exception:
                continue
            if job:
                jobs[job.url] = job

    return sorted(jobs.values(), key=lambda job: (job.published_at, job.title), reverse=True)
