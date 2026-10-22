"""AWS Lambda entry points.

``ingest_handler``  — fetch the latest 10-Q/10-K for tickers from EDGAR into S3
                      (run on a schedule or invoke manually).
``process_handler`` — S3 ObjectCreated trigger on ``filings/raw/*.htm``: runs the
                      full pipeline and writes results/review items back to S3.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any

from .config import Settings
from .edgar import EdgarClient
from .extraction import ClaudeExtractor
from .models import FilingMeta
from .parsing import parse_html
from .pipeline import meta_from_dei, run_filing
from .storage import S3Storage, read_json, write_json

log = logging.getLogger()
log.setLevel(logging.INFO)

_KEY_RE = re.compile(r"filings/raw/(\d+)/(\d{10}-\d{2}-\d{6})\.htm$")
_api_key_loaded = False


def _load_api_key() -> None:
    """Pull the Anthropic key from Secrets Manager once per container."""
    global _api_key_loaded
    if _api_key_loaded or os.getenv("ANTHROPIC_API_KEY"):
        return
    arn = os.getenv("ANTHROPIC_API_KEY_SECRET_ARN")
    if arn:
        import boto3

        secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        try:
            secret = json.loads(secret).get("ANTHROPIC_API_KEY", secret)
        except (ValueError, AttributeError):
            pass
        os.environ["ANTHROPIC_API_KEY"] = secret
    _api_key_loaded = True


def build_extractor(settings: Settings):
    _load_api_key()
    return ClaudeExtractor(model=settings.anthropic_model)


def _store(bucket: str | None = None) -> S3Storage:
    return S3Storage(bucket or os.environ["BUCKET_NAME"])


def ingest_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    settings = Settings()
    edgar = EdgarClient(settings.sec_user_agent)
    store = _store()
    tickers = event.get("tickers") or os.getenv("TICKERS", "").split(",")
    form = event.get("form", "10-Q")
    written = []
    for t in filter(None, (x.strip() for x in tickers)):
        cik = edgar.ticker_to_cik(t)
        meta = edgar.latest_filing(cik, form)
        key = f"filings/raw/{int(cik)}/{meta.accession}.htm"
        if store.exists(key):
            continue
        write_json(store, f"xbrl/{int(cik)}.json", edgar.company_facts(cik))
        write_json(store, f"filings/raw/{int(cik)}/{meta.accession}.meta.json", meta.__dict__)
        store.write_text(key, edgar.fetch_document(meta))  # written last: this fires the trigger
        written.append(key)
    return {"ingested": written}


def process_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    settings = Settings()
    out = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        m = _KEY_RE.search(key)
        if not m:
            log.warning("Skipping unexpected key %s", key)
            continue
        cik, accession = m.groups()
        store = _store(bucket)
        html = store.read_text(key)
        doc = parse_html(html)

        meta_raw = read_json(store, key[: -len(".htm")] + ".meta.json")
        meta = FilingMeta.from_dict(meta_raw) if meta_raw else meta_from_dei(doc, cik, accession)

        facts = read_json(store, f"xbrl/{int(cik)}.json")
        if facts is None:
            facts = EdgarClient(settings.sec_user_agent).company_facts(cik)
            write_json(store, f"xbrl/{int(cik)}.json", facts)

        result = run_filing(html, meta, build_extractor(settings), store, facts, settings, doc=doc)
        out.append({"filing_id": meta.filing_id, "status": result.status, "metrics": result.metrics})
    return {"processed": out}
