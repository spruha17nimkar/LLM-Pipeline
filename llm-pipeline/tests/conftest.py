import json
import re
from pathlib import Path

import pytest

from kpi_pipeline.config import Settings
from kpi_pipeline.models import FilingMeta
from kpi_pipeline.parsing import parse_html

FIX = Path(__file__).parent / "fixtures"

# Row label in the fixture statements, and the column -> (period_end, duration) map.
ROWS = {
    "revenue": ("Net revenue", "is"),
    "gross_profit": ("Gross profit", "is"),
    "operating_income": ("Operating income", "is"),
    "net_income": ("Net income", "is"),
    "eps_diluted": ("Net income per share — diluted", "is"),
    "operating_cash_flow": ("Net cash provided by operating activities", "cf"),
    "cash_and_equivalents": ("Cash and cash equivalents", "bs"),
}
COLS = {
    "is": [("2025-06-30", "3M"), ("2024-06-30", "3M"), ("2025-06-30", "6M"), ("2024-06-30", "6M")],
    "cf": [("2025-06-30", "6M"), ("2024-06-30", "6M")],
    "bs": [("2025-06-30", "instant"), ("2024-12-31", "instant")],
}


class RuleExtractor:
    """Deterministic stand-in for Claude: reads the fixture's table rows from
    whichever retrieved chunk contains them. ``overrides`` lets a test inject
    a wrong value or a bad quote to exercise validation and review."""

    model = "rule-based-test-extractor"

    def __init__(self, overrides=None, skip=()):
        self.overrides = overrides or {}
        self.skip = set(skip)
        self.calls = []

    def extract(self, kpi, chunks, meta):
        self.calls.append((kpi.id, [c.id for c in chunks]))
        if kpi.id in self.skip:
            return {"found": False, "values": [], "notes": ""}
        label, kind = ROWS[kpi.id]
        for c in chunks:
            for line in c.text.split("\n"):
                cells = [x.strip() for x in line.split("|")]
                if cells[0] == label and len(cells) > 1:
                    values = []
                    for raw, (end, dur) in zip(cells[1:], COLS[kind]):
                        num = float(re.sub(r"[^\d.]", "", raw))
                        values.append({
                            "period_end": end, "period_duration": dur, "raw_text": raw, "value": num,
                            "unit_scale": "units" if kpi.is_per_share else "millions",
                            "chunk_id": c.id, "quote": line,
                        })
                    for v in values:
                        v.update(self.overrides.get((kpi.id, v["period_end"], v["period_duration"]), {}))
                    return {"found": True, "values": values, "notes": ""}
        return {"found": False, "values": [], "notes": "row not in retrieved chunks"}


@pytest.fixture
def html():
    return (FIX / "acme_10q.htm").read_text()


@pytest.fixture
def doc(html):
    return parse_html(html)


@pytest.fixture
def facts():
    return json.loads((FIX / "acme_companyfacts.json").read_text())


@pytest.fixture
def meta():
    return FilingMeta.from_dict(json.loads((FIX / "acme_meta.json").read_text()))


@pytest.fixture
def settings():
    s = Settings()
    s.chunk_chars = 1200
    return s
