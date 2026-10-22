"""Thin client for SEC EDGAR: tickers, filing index, documents, XBRL company facts.

SEC requires a descriptive User-Agent with contact info and asks clients to stay
under 10 requests/second. See https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

from .models import FilingMeta

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{doc}"


class EdgarClient:
    def __init__(self, user_agent: str, min_interval: float = 0.12, session=None):
        if "@" not in user_agent:
            raise ValueError("SEC_USER_AGENT must include a contact email, e.g. 'Name you@mail.com'")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.min_interval = min_interval
        self._last = 0.0

    def _get(self, url: str) -> requests.Response:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            resp = self.session.get(url, timeout=30)
            self._last = time.monotonic()
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def ticker_to_cik(self, ticker: str) -> str:
        data = self._get(_TICKERS_URL).json()
        t = ticker.upper()
        for row in data.values():
            if row["ticker"].upper() == t:
                return str(row["cik_str"])
        raise KeyError(f"Ticker not found on EDGAR: {ticker}")

    def company_facts(self, cik: str) -> dict[str, Any]:
        return self._get(_FACTS_URL.format(cik=int(cik))).json()

    def list_filings(self, cik: str, forms=("10-Q", "10-K"), limit: int = 10) -> list[FilingMeta]:
        sub = self._get(_SUBMISSIONS_URL.format(cik=int(cik))).json()
        recent = sub["filings"]["recent"]
        out: list[FilingMeta] = []
        for i, form in enumerate(recent["form"]):
            if form not in forms:
                continue
            acc = recent["accessionNumber"][i]
            doc = recent["primaryDocument"][i]
            out.append(
                FilingMeta(
                    cik=str(int(cik)),
                    company=sub.get("name", ""),
                    form=form,
                    accession=acc,
                    period_of_report=recent["reportDate"][i],
                    filing_date=recent["filingDate"][i],
                    source_url=_DOC_URL.format(cik=int(cik), acc_nodash=acc.replace("-", ""), doc=doc),
                )
            )
            if len(out) >= limit:
                break
        return out

    def latest_filing(self, cik: str, form: str) -> FilingMeta:
        filings = self.list_filings(cik, forms=(form,), limit=1)
        if not filings:
            raise LookupError(f"No {form} found for CIK {cik}")
        return filings[0]

    def find_filing(self, cik: str, accession: str) -> Optional[FilingMeta]:
        for f in self.list_filings(cik, limit=200):
            if f.accession == accession:
                return f
        return None

    def fetch_document(self, meta: FilingMeta) -> str:
        if not meta.source_url:
            raise ValueError("FilingMeta.source_url is required to fetch the document")
        return self._get(meta.source_url).text
