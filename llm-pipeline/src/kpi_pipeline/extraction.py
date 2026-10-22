"""LLM extraction with Claude tool use, plus citation grounding.

The model is forced to call a ``record_kpi`` tool, so output is always
schema-shaped JSON. Every value must carry the chunk id and a verbatim quote;
``ground_citation`` then checks the quote really exists in that chunk and
contains the number. Unverifiable citations are flagged for human review.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from .config import KPIDefinition
from .models import SCALE, Chunk, Citation, ExtractedValue, FilingMeta

RECORD_KPI_TOOL: dict[str, Any] = {
    "name": "record_kpi",
    "description": "Record every reported value of one KPI found in the provided filing excerpts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean", "description": "False if the KPI is not reported in the excerpts."},
            "values": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "period_end": {"type": "string", "description": "Period end date, YYYY-MM-DD."},
                        "period_duration": {
                            "type": "string",
                            "enum": ["3M", "6M", "9M", "12M", "instant"],
                            "description": "Length of the reporting period; 'instant' for balance-sheet items.",
                        },
                        "raw_text": {"type": "string", "description": "The number exactly as printed, e.g. '(1,234)' or '2.18'."},
                        "value": {"type": "number", "description": "Number as printed, as a signed float (parentheses = negative), NOT rescaled."},
                        "unit_scale": {
                            "type": "string",
                            "enum": ["units", "thousands", "millions", "billions"],
                            "description": "Scale stated for the table/text, e.g. '(in millions)'. Per-share amounts are 'units'.",
                        },
                        "chunk_id": {"type": "string", "description": "Id of the excerpt the number came from."},
                        "quote": {
                            "type": "string",
                            "description": "Verbatim span (<= 300 chars) copied from that excerpt that contains raw_text and its row label.",
                        },
                    },
                    "required": ["period_end", "period_duration", "raw_text", "value", "unit_scale", "chunk_id", "quote"],
                },
            },
            "notes": {"type": "string", "description": "Brief caveats (restatements, non-GAAP, segment-only, etc.)."},
        },
        "required": ["found", "values"],
    },
}

SYSTEM_PROMPT = """You are a meticulous financial analyst extracting KPIs from SEC filings.
Rules:
- Use ONLY numbers explicitly printed in the excerpts. Never compute, estimate, or recall from memory.
- Prefer the primary financial statements over narrative text when both exist.
- Report every period column presented for the KPI (current period and the comparative periods), each as its own value.
- Use consolidated, GAAP, total-company figures. Ignore segment, non-GAAP, or pro forma figures.
- 'quote' must be copied character-for-character from the cited excerpt and include the number.
- Determine unit_scale from the table caption or text (e.g. '(in millions, except per share data)').
- If the KPI is not in the excerpts, call the tool with found=false and an empty values list."""


class Extractor(Protocol):
    model: str

    def extract(self, kpi: KPIDefinition, chunks: list[Chunk], meta: FilingMeta) -> dict[str, Any]:
        """Return the record_kpi tool input: {found, values, notes}."""


def build_user_prompt(kpi: KPIDefinition, chunks: list[Chunk], meta: FilingMeta) -> str:
    excerpts = "\n\n".join(
        f'<excerpt id="{c.id}" section="{c.section}">\n{c.text}\n</excerpt>' for c in chunks
    )
    return (
        f"Filing: {meta.company} {meta.form}, period of report {meta.period_of_report}"
        f" (fiscal {meta.fiscal_period or '?'} {meta.fiscal_year or ''}).\n"
        f"KPI: {kpi.label} — {kpi.description}\n\n"
        f"{excerpts}\n\nCall record_kpi with every value of this KPI you find."
    )


class ClaudeExtractor:
    def __init__(self, model: str, client=None, max_tokens: int = 2048):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.model = model
        self.max_tokens = max_tokens

    def extract(self, kpi: KPIDefinition, chunks: list[Chunk], meta: FilingMeta) -> dict[str, Any]:
        if not chunks:
            return {"found": False, "values": [], "notes": "no relevant excerpts retrieved"}
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=0,
            system=SYSTEM_PROMPT,
            tools=[RECORD_KPI_TOOL],
            tool_choice={"type": "tool", "name": "record_kpi"},
            messages=[{"role": "user", "content": build_user_prompt(kpi, chunks, meta)}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "record_kpi":
                data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                data.setdefault("values", [])
                data.setdefault("notes", "")
                return data
        return {"found": False, "values": [], "notes": "model returned no tool call"}


# --- citation grounding -----------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _digits(s: str) -> str:
    return re.sub(r"[^0-9.]", "", s).strip(".")


def ground_citation(raw: dict[str, Any], chunks_by_id: dict[str, Chunk], source_url: str | None) -> Citation:
    chunk = chunks_by_id.get(raw.get("chunk_id", ""))
    quote = raw.get("quote", "") or ""
    if chunk is None:
        return Citation(raw.get("chunk_id", ""), "", quote, None, None, source_url, verified=False)

    nq, nc = _norm(quote), _norm(chunk.text)
    in_chunk = bool(nq) and nq in nc
    num = _digits(raw.get("raw_text", ""))
    has_number = bool(num) and num in _digits_keep_sep(quote)

    start = end = None
    if in_chunk:
        # Map the match back to an approximate absolute offset in the document.
        idx = _find_loose(chunk.text, quote)
        if idx is not None:
            start, end = chunk.start + idx[0], chunk.start + idx[1]
    return Citation(chunk.id, chunk.section, quote, start, end, source_url, verified=in_chunk and has_number)


def _digits_keep_sep(s: str) -> str:
    # Concatenated digit groups per number token, e.g. "1,234.5 and 99" -> "1234.5 99".
    return " ".join(_digits(tok) for tok in re.findall(r"[\d][\d,\.]*", s))


def _find_loose(text: str, quote: str) -> tuple[int, int] | None:
    pattern = r"\s+".join(re.escape(p) for p in quote.split())
    m = re.search(pattern, text, re.I)
    return (m.start(), m.end()) if m else None


def to_extracted_values(
    kpi: KPIDefinition, data: dict[str, Any], chunks: list[Chunk], meta: FilingMeta
) -> list[ExtractedValue]:
    by_id = {c.id: c for c in chunks}
    out: list[ExtractedValue] = []
    seen: set[tuple[str, str]] = set()
    for raw in data.get("values", []):
        try:
            scale = "units" if kpi.is_per_share else raw.get("unit_scale", "units")
            value = float(raw["value"]) * SCALE.get(scale, 1)
            raw_text = str(raw.get("raw_text", ""))
            if raw_text.strip().startswith("(") and value > 0:
                value = -value  # parentheses mean negative
            duration = "instant" if kpi.period_type == "instant" else raw["period_duration"]
            key = (raw["period_end"], duration)
        except (KeyError, TypeError, ValueError):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ExtractedValue(
                kpi_id=kpi.id,
                period_end=raw["period_end"],
                period_duration=duration,
                value=value,
                raw_text=raw_text,
                unit_scale=scale,
                citation=ground_citation(raw, by_id, meta.source_url),
                is_current_period=(raw["period_end"] == meta.period_of_report),
            )
        )
    return out
