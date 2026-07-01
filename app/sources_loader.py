from __future__ import annotations

from pathlib import Path

import yaml


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def load_sources(path: str | Path) -> dict[str, list[str]]:
    sources_path = Path(path)
    with sources_path.open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}

    return {
        "ashby": _as_list(raw.get("ashby")),
        "lever": _as_list(raw.get("lever")),
        "greenhouse": _as_list(raw.get("greenhouse")),
        "remotive": _as_list(raw.get("remotive")),
        "remoteok": _as_list(raw.get("remoteok")),
        "arbeitnow": _as_list(raw.get("arbeitnow")),
        "smartrecruiters": _as_list(raw.get("smartrecruiters")),
        "smartrecruiters_pages": _as_list(raw.get("smartrecruiters_pages")),
        "solides": _as_list(raw.get("solides")),
    }
