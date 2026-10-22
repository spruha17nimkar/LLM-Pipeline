"""End-to-end orchestration for one filing."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any, Optional

from .chunking import chunk_document
from .config import KPIS, KPIDefinition, Settings
from .extraction import Extractor, to_extracted_values
from .models import FilingMeta, FilingResult, KPIResult, ReviewItem
from .parsing import ParsedDocument, parse_html
from .retrieval import BM25Index
from .review import ReviewQueue, finalize
from .storage import Storage, read_json, write_json
from .trends import Point, compute_trends
from .validation import summarize, validate_value
from .xbrl import CompanyFacts

log = logging.getLogger(__name__)


def meta_from_dei(doc: ParsedDocument, cik: str, accession: str, source_url: Optional[str] = None) -> FilingMeta:
    """Build filing metadata from the inline-XBRL cover page (dei:*) tags."""
    from datetime import datetime

    dei = doc.dei
    period = dei.get("DocumentPeriodEndDate", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            period = datetime.strptime(period.replace("Sept.", "Sep"), fmt).date().isoformat()
            break
        except ValueError:
            continue
    fy = dei.get("DocumentFiscalYearFocus")
    return FilingMeta(
        cik=str(int(dei.get("EntityCentralIndexKey", cik) or cik)),
        company=dei.get("EntityRegistrantName", ""),
        form=dei.get("DocumentType", ""),
        accession=accession,
        period_of_report=period,
        fiscal_year=int(fy) if fy and fy.isdigit() else None,
        fiscal_period=dei.get("DocumentFiscalPeriodFocus"),
        source_url=source_url,
    )


def _history(store: Storage, cik: str, exclude_accession: str) -> list[Point]:
    raw = read_json(store, f"history/{int(cik)}.json", default=[])
    return [
        Point(p["kpi_id"], p["period_end"], p["period_duration"], float(p["value"]), "history")
        for p in raw
        if p.get("accession") != exclude_accession
    ]


def run_filing(
    html: str,
    meta: FilingMeta,
    extractor: Extractor,
    store: Storage,
    company_facts: Optional[dict[str, Any]] = None,
    settings: Optional[Settings] = None,
    kpis: tuple[KPIDefinition, ...] = KPIS,
    doc: Optional[ParsedDocument] = None,
) -> FilingResult:
    settings = settings or Settings()
    doc = doc or parse_html(html)
    chunks = chunk_document(doc, max_chars=settings.chunk_chars)
    index = BM25Index(chunks)
    facts = CompanyFacts(company_facts) if company_facts else None
    log.info("%s: %d chars, %d chunks, %d sections", meta.filing_id, len(doc.text), len(chunks), len(doc.sections))

    kpi_results: list[KPIResult] = []
    review_items: list[ReviewItem] = []
    all_values = []
    all_validations = []
    missed = 0

    for kpi in kpis:
        hits = index.search(kpi.query, k=settings.top_k)
        data = extractor.extract(kpi, hits, meta)
        values = to_extracted_values(kpi, data, hits, meta)
        validations = [validate_value(kpi, v, facts, settings, meta.accession) for v in values]
        kr = KPIResult(kpi.id, kpi.label, bool(values), values, validations, data.get("notes", ""))
        kpi_results.append(kr)
        all_values += values
        all_validations += validations

        for v, val in zip(values, validations):
            flag = val.status in ("MISMATCH", "CITATION_UNVERIFIED") or (
                val.status == "NO_GROUND_TRUTH" and settings.review_unverified
            )
            if flag:
                review_items.append(
                    ReviewItem(
                        item_id=f"{kpi.id}__{v.period_duration}__{v.period_end}",
                        filing_id=meta.filing_id,
                        kpi_id=kpi.id,
                        reason=val.status,
                        extracted=asdict(v),
                        validation=asdict(val),
                    )
                )

        # A KPI the filing tagged in XBRL for its own current period, but we didn't extract.
        if facts:
            current_facts = [
                f for f in facts.facts_in_filing(kpi, meta.accession) if f.end == meta.period_of_report
            ]
            got = {(v.period_end) for v in values if v.is_current_period}
            if current_facts and meta.period_of_report not in got:
                missed += 1
                f = current_facts[0]
                dur = "instant" if kpi.period_type == "instant" else _duration_label(f.start, f.end)
                review_items.append(
                    ReviewItem(
                        item_id=f"{kpi.id}__MISSED",
                        filing_id=meta.filing_id,
                        kpi_id=kpi.id,
                        reason="MISSED",
                        extracted={"kpi_id": kpi.id, "period_end": f.end, "period_duration": dur, "value": None},
                        validation={"status": "MISSED", "xbrl_value": f.value, "xbrl_tag": f.tag},
                    )
                )

    trends = compute_trends(all_values, _history(store, meta.cik, meta.accession))
    metrics = summarize(all_validations, len(kpis), sum(k.found for k in kpi_results), missed)
    metrics["citation_verified_rate"] = (
        round(sum(v.citation.verified for v in all_values) / len(all_values), 4) if all_values else None
    )
    metrics["review_items"] = len(review_items)

    result = FilingResult(
        meta=meta,
        kpis=kpi_results,
        trends=trends,
        review_items=review_items,
        status="pending_review" if review_items else "approved",
        model=getattr(extractor, "model", ""),
        metrics=metrics,
    )

    # Re-running a filing replaces its previous review state.
    for prefix in (f"review/pending/{meta.filing_id}/", f"review/resolved/{meta.filing_id}/"):
        for key in store.list(prefix):
            store.delete(key)
    write_json(store, f"results/staged/{meta.filing_id}.json", result.to_dict())
    if review_items:
        ReviewQueue(store).enqueue(review_items)
    else:
        finalize(store, meta.filing_id)
    log.info("%s: %s", meta.filing_id, metrics)
    return result


def _duration_label(start: Optional[str], end: str) -> str:
    from datetime import date

    if not start:
        return "instant"
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return min(("3M", 91), ("6M", 182), ("9M", 273), ("12M", 365), key=lambda x: abs(x[1] - days))[0]
