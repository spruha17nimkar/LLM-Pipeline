"""Turn filing HTML (incl. inline XBRL) into clean text with section markers.

Tables are flattened row-by-row into ``cell | cell | cell`` lines so numbers
stay next to their row labels, which is what both retrieval and the LLM need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

_BLOCK_TAGS = ["p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"]
_ITEM_RE = re.compile(r"^\s*(?:part\s+[iv]+[\s,.\-–—]*)?item\s+(\d{1,2}[a-c]?)\s*[.:\-–—]?\s*(.*)$", re.I)
_SCALE_RE = re.compile(r"in\s+(thousands|millions|billions)", re.I)


@dataclass
class Section:
    start: int
    label: str


@dataclass
class ParsedDocument:
    text: str
    sections: list[Section] = field(default_factory=list)
    dei: dict[str, str] = field(default_factory=dict)  # document & entity info from iXBRL

    def section_at(self, offset: int) -> str:
        label = "Cover"
        for s in self.sections:
            if s.start <= offset:
                label = s.label
            else:
                break
        return label


def _clean_cells(cells: list[str]) -> list[str]:
    out: list[str] = []
    for c in cells:
        c = re.sub(r"\s+", " ", c).strip()
        if not c or c == "$":
            continue
        if c in {")", "%", ")%"} and out:
            out[-1] += c
            continue
        out.append(c)
    return out


def parse_html(html: str) -> ParsedDocument:
    soup = BeautifulSoup(html, "lxml")

    dei: dict[str, str] = {}
    for tag in soup.find_all(["ix:nonnumeric", "ix:nonNumeric"]):
        name = tag.get("name", "")
        if name.startswith("dei:"):
            dei.setdefault(name[4:], tag.get_text(" ", strip=True))

    # Hidden iXBRL header holds contexts/units, not readable content.
    for tag in soup.find_all(["ix:header", "script", "style", "head"]):
        tag.decompose()

    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = _clean_cells([td.get_text(" ") for td in tr.find_all(["td", "th"])])
            if cells:
                rows.append(" | ".join(cells))
        table.replace_with(soup.new_string("\n" + "\n".join(rows) + "\n"))

    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_after("\n")

    raw = soup.get_text()
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    text = "\n".join(lines)

    sections: list[Section] = []
    offset = 0
    for ln in lines:
        m = _ITEM_RE.match(ln)
        if m and len(ln) < 150 and "|" not in ln:
            title = m.group(2).strip().rstrip(".")
            label = f"Item {m.group(1).upper()}" + (f". {title}" if title else "")
            sections.append(Section(start=offset, label=label[:120]))
        offset += len(ln) + 1

    return ParsedDocument(text=text, sections=sections, dei=dei)


def detect_scale(text: str) -> Optional[str]:
    m = _SCALE_RE.search(text)
    return m.group(1).lower() if m else None
