from __future__ import annotations

from html.parser import HTMLParser


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if clean:
            self._parts.append(clean)

    def get_text(self) -> str:
        return "\n".join(self._parts)


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(value)
    return stripper.get_text()
