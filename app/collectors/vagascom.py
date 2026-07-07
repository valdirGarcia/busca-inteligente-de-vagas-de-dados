from __future__ import annotations

import json
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


VAGASCOM_BASE_URL = "https://www.vagas.com.br"
VAGASCOM_SEARCH_TERMS = [
    "analista de dados",
    "cientista de dados",
    "engenheiro de dados",
    "analista de bi",
    "business intelligence",
    "power bi",
    "analytics",
    "analista de insights",
    "insights analyst",
    "data analyst",
    "data scientist",
    "machine learning",
    "analista de indicadores",
    "analista de inteligencia",
    "analista de planejamento",
    "analista de performance",
    "analista de credito",
    "analista de risco",
    "analista de pricing",
    "analista de crm",
    "analista de mis",
]


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.lower())
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_punctuation = re.sub(r"[^a-z0-9]+", " ", without_accents)
    return re.sub(r"\s+", " ", without_punctuation).strip()


def _slugify(value: str) -> str:
    return _normalize(value).replace(" ", "-") or "dados"


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _headers(referer: str = VAGASCOM_BASE_URL) -> dict[str, str]:
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


def _parse_iso_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value[:10]).date().isoformat()
    except ValueError:
        return ""


class _VagasComListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._li_depth = 0
        self._capture_field = ""
        self._capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        classes = set(attr_map.get("class", "").lower().split())

        if self._current is None:
            if tag == "li" and "vaga" in classes:
                self._current = {}
                self._li_depth = 1
            return

        if tag == "li":
            self._li_depth += 1

        if self._capture_field:
            self._capture_depth += 1

        if tag == "a" and "link-detalhes-vaga" in classes:
            self._current["url"] = urljoin(VAGASCOM_BASE_URL, attr_map.get("href", ""))
            self._current["vagascom_id"] = attr_map.get("data-id-vaga", "")
            self._current["title"] = attr_map.get("title", "")
            self._start_capture("title")
        elif tag == "span" and "emprvaga" in classes:
            self._start_capture("company")
        elif tag == "span" and "nivelvaga" in classes:
            self._start_capture("seniority")
        elif tag == "div" and "detalhes" in classes:
            self._start_capture("snippet")
        elif tag == "div" and "vaga-local" in classes:
            self._start_capture("location")
        elif tag == "span" and "data-publicacao" in classes:
            self._start_capture("date_label")

    def _start_capture(self, field: str) -> None:
        self._capture_field = field
        self._capture_depth = 1

    def handle_data(self, data: str) -> None:
        if self._current is None or not self._capture_field:
            return
        clean = _clean(data)
        if not clean:
            return
        previous = self._current.get(self._capture_field, "")
        self._current[self._capture_field] = _clean(f"{previous} {clean}")

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return

        if self._capture_field:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                self._capture_field = ""
                self._capture_depth = 0

        if tag == "li":
            self._li_depth -= 1
            if self._li_depth <= 0:
                card = {key: _clean(value) for key, value in self._current.items() if _clean(value)}
                if card.get("title") and card.get("url"):
                    self.cards.append(card)
                self._current = None
                self._li_depth = 0
                self._capture_field = ""
                self._capture_depth = 0


class _JsonLdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[str] = []
        self._capturing = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): value or "" for key, value in attrs}
        if tag == "script" and attr_map.get("type", "").lower() == "application/ld+json":
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            script = "".join(self._parts).strip()
            if script:
                self.scripts.append(script)
            self._capturing = False
            self._parts = []


def _parse_cards(html: str) -> list[dict[str, str]]:
    parser = _VagasComListParser()
    parser.feed(html)
    return parser.cards


def _job_posting_from_html(html: str) -> dict:
    parser = _JsonLdParser()
    parser.feed(html)
    for script in parser.scripts:
        try:
            payload = json.loads(script)
        except json.JSONDecodeError:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("@type") == "JobPosting":
                return candidate
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict) and node.get("@type") == "JobPosting":
                        return node
    return {}


def _fetch_page(term: str, page: int, timeout: int) -> str:
    params = {"ordenar_por": "mais_recentes"}
    if page > 1:
        params["pagina"] = str(page)
    url = f"{VAGASCOM_BASE_URL}/vagas-de-{_slugify(term)}?{urlencode(params)}"
    request = Request(url, headers=_headers(url))
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _fetch_detail(url: str, timeout: int) -> dict:
    request = Request(url, headers=_headers(url))
    with urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")
    return _job_posting_from_html(html)


def _clean_location(value: str) -> str:
    value = re.sub(r"\bA empresa aceita candidaturas\b.*$", "", value, flags=re.IGNORECASE).strip()
    return _clean(value.replace(" / ", ", "))


def _text_value(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@value") or "")
    return str(value or "")


def _organization_name(posting: dict, fallback: str) -> str:
    organization = posting.get("hiringOrganization")
    if isinstance(organization, dict):
        return _text_value(organization.get("name")).strip() or fallback
    return fallback


def _location_from_posting(posting: dict, fallback: str) -> str:
    if _normalize(str(posting.get("jobLocationType") or "")) == "telecommute":
        return "Remoto, Brasil"

    locations = posting.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]
    if isinstance(locations, list):
        parsed_locations = []
        for location in locations:
            if not isinstance(location, dict):
                continue
            address = location.get("address") if isinstance(location.get("address"), dict) else {}
            city = _text_value(address.get("addressLocality")).strip()
            state = _text_value(address.get("addressRegion")).strip()
            country = _text_value(address.get("addressCountry")).strip()
            parsed = ", ".join(part for part in [city, state, country] if part)
            if parsed:
                parsed_locations.append(parsed)
        if parsed_locations:
            return "; ".join(parsed_locations)

    return _clean_location(fallback)


def _infer_job_type(title: str, location: str, description: str, posting: dict) -> str:
    if _normalize(str(posting.get("jobLocationType") or "")) == "telecommute":
        return "remoto"

    text = _normalize(" ".join([title, location, description[:1000]]))
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
            signal_text = " ".join([card.get("title", ""), card.get("snippet", ""), card.get("seniority", "")])
            if looks_like_data_job(signal_text):
                card["search_term"] = term
                cards.append(card)

        if not page_has_recent_card:
            break
    return cards


def _build_job(card: dict[str, str], timeout: int) -> Job | None:
    posting: dict = {}
    try:
        posting = _fetch_detail(card.get("url", ""), timeout)
    except Exception:
        posting = {}

    title = _text_value(posting.get("title")).strip() or card.get("title", "")
    description = strip_html(_text_value(posting.get("description"))) or card.get("snippet", "")
    location = _location_from_posting(posting, card.get("location", ""))
    company = _organization_name(posting, card.get("company", ""))
    published_at = _parse_iso_date(_text_value(posting.get("datePosted"))) or _published_date_from_label(card.get("date_label", ""))

    searchable = " ".join([title, company, location, description])
    if not looks_like_data_job(searchable):
        return None

    return Job(
        title=title,
        company=company,
        location=location,
        url=card.get("url", ""),
        description=description,
        source="vagascom",
        published_at=published_at,
        categories={
            "date_label": card.get("date_label", ""),
            "vagascom_id": card.get("vagascom_id", ""),
            "search_term": card.get("search_term", ""),
            "seniority": card.get("seniority", ""),
            "job_type": _infer_job_type(title, location, description, posting),
        },
    )


def fetch_vagascom_jobs(
    pages_per_term: int = 2,
    terms: list[str] | None = None,
    max_age_days: int = 7,
    timeout: int = 20,
) -> list[Job]:
    cards: dict[str, dict[str, str]] = {}
    search_terms = terms or VAGASCOM_SEARCH_TERMS
    safe_pages = max(1, pages_per_term)

    with ThreadPoolExecutor(max_workers=6) as executor:
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
