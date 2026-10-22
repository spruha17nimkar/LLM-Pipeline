"""Batch evaluation: run the pipeline over a manifest of real filings and report
accuracy against XBRL per KPI and overall.

Manifest format (JSON list):
    [{"ticker": "AAPL", "form": "10-Q"}, {"cik": "789019", "accession": "0000950170-24-048288"}]
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .edgar import EdgarClient
from .extraction import ClaudeExtractor
from .parsing import parse_html
from .pipeline import meta_from_dei, run_filing
from .storage import LocalStorage


def run_manifest(manifest: Path, data_dir: Path, settings: Settings, extractor=None, edgar=None) -> dict[str, Any]:
    entries = json.loads(manifest.read_text())
    edgar = edgar or EdgarClient(settings.sec_user_agent)
    extractor = extractor or ClaudeExtractor(settings.anthropic_model)
    store = LocalStorage(data_dir)

    per_kpi: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    filings = []
    for e in entries:
        cik = e.get("cik") or edgar.ticker_to_cik(e["ticker"])
        meta = edgar.find_filing(cik, e["accession"]) if e.get("accession") else edgar.latest_filing(cik, e.get("form", "10-Q"))
        html = edgar.fetch_document(meta)
        doc = parse_html(html)
        dei = meta_from_dei(doc, cik, meta.accession)
        meta.fiscal_year, meta.fiscal_period = dei.fiscal_year, dei.fiscal_period
        result = run_filing(html, meta, extractor, store, edgar.company_facts(cik), settings, doc=doc)
        for k in result.kpis:
            for val in k.validations:
                per_kpi[k.kpi_id][val.status] += 1
        for it in result.review_items:
            if it.reason == "MISSED":
                per_kpi[it.kpi_id]["MISSED"] += 1
        filings.append({"filing_id": meta.filing_id, "company": meta.company, "form": meta.form, **result.metrics})

    totals: dict[str, Any] = defaultdict(int)
    for f in filings:
        for key in ("values_extracted", "values_with_ground_truth", "match", "mismatch", "no_ground_truth", "citation_unverified", "kpis_missed"):
            totals[key] += f.get(key) or 0
    wt = totals["values_with_ground_truth"]
    totals["value_accuracy_vs_xbrl"] = round((wt - totals["mismatch"]) / wt, 4) if wt else None
    totals["filings"] = len(filings)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": getattr(extractor, "model", ""),
        "totals": dict(totals),
        "per_kpi": {k: dict(v) for k, v in per_kpi.items()},
        "filings": filings,
    }
    path = data_dir / "evals" / f"report_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    report["report_path"] = str(path)
    return report
