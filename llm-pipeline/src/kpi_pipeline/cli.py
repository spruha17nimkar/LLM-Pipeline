"""Command-line interface.

    kpi run --ticker AAPL --form 10-Q            # fetch latest from EDGAR and run locally
    kpi run --file f.htm --meta m.json --facts companyfacts.json
    kpi review list | show ID | approve ID | correct ID --value 123 | reject ID
    kpi eval --manifest evals/manifest.json      # batch eval vs XBRL
    kpi ingest --ticker AAPL --form 10-Q --bucket my-bucket   # push to S3 (fires Lambda)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings
from .edgar import EdgarClient
from .models import FilingMeta
from .parsing import parse_html
from .pipeline import meta_from_dei, run_filing
from .review import ReviewQueue
from .storage import LocalStorage, S3Storage


def _store(args):
    return S3Storage(args.bucket) if getattr(args, "bucket", None) else LocalStorage(args.data_dir)


def _print_summary(result) -> None:
    m = result.meta
    print(f"\n{m.company} {m.form} — period {m.period_of_report} ({m.accession})")
    print(f"status: {result.status}   model: {result.model}")
    print(f"{'KPI':<26}{'period':<18}{'value':>20}  check")
    for k in result.kpis:
        for v, val in zip(k.values, k.validations):
            if not v.is_current_period:
                continue
            print(f"{k.label:<26}{v.period_duration + ' ' + v.period_end:<18}{v.value:>20,.2f}  {val.status}")
        if not k.found:
            print(f"{k.label:<26}{'—':<18}{'not found':>20}")
    for t in result.trends:
        pct = f"{t.pct_change:+.1%}" if t.pct_change is not None else "n/a"
        print(f"  {t.kind:<4} {t.kpi_id:<22} {t.period_duration:<8} {pct:>8}  (vs {t.prior_period_end}, {t.prior_source})")
    print("metrics:", json.dumps(result.metrics))
    if result.review_items:
        print(f"{len(result.review_items)} item(s) sent to review — see `kpi review list`")


def cmd_run(args) -> int:
    settings = Settings()
    store = _store(args)
    if args.file:
        html = Path(args.file).read_text(encoding="utf-8", errors="replace")
        doc = parse_html(html)
        if args.meta:
            meta = FilingMeta.from_dict(json.loads(Path(args.meta).read_text()))
        else:
            if not (args.cik and args.accession):
                print("--file needs --meta, or --cik and --accession", file=sys.stderr)
                return 2
            meta = meta_from_dei(doc, args.cik, args.accession)
        facts = json.loads(Path(args.facts).read_text()) if args.facts else None
    else:
        edgar = EdgarClient(settings.sec_user_agent)
        cik = args.cik or edgar.ticker_to_cik(args.ticker)
        meta = edgar.find_filing(cik, args.accession) if args.accession else edgar.latest_filing(cik, args.form)
        html = edgar.fetch_document(meta)
        doc = parse_html(html)
        dei_meta = meta_from_dei(doc, cik, meta.accession)
        meta.fiscal_year, meta.fiscal_period = dei_meta.fiscal_year, dei_meta.fiscal_period
        facts = edgar.company_facts(cik)

    from .extraction import ClaudeExtractor

    result = run_filing(html, meta, ClaudeExtractor(settings.anthropic_model), store, facts, settings, doc=doc)
    _print_summary(result)
    return 0


def cmd_review(args) -> int:
    q = ReviewQueue(_store(args))
    if args.action == "list":
        items = q.pending(args.filing)
        if not items:
            print("Review queue is empty.")
        for it in items:
            ex, val = it.get("extracted") or {}, it.get("validation") or {}
            print(
                f"{it['filing_id']}/{it['item_id']}  [{it['reason']}]  "
                f"extracted={ex.get('value')}  xbrl={val.get('xbrl_value')}  {val.get('note', '')}"
            )
        return 0

    filing_id, item_id = args.item.split("/", 1)
    if args.action == "show":
        print(json.dumps(q.get(filing_id, item_id), indent=2))
        return 0
    decision = {"approve": "approved", "correct": "corrected", "reject": "rejected"}[args.action]
    res = q.resolve(filing_id, item_id, decision, args.reviewer, args.value, args.note or "")
    print(f"{item_id}: {decision}." + (" All items resolved — filing published to results/approved/." if res["published"] else ""))
    return 0


def cmd_eval(args) -> int:
    from .eval_runner import run_manifest

    report = run_manifest(Path(args.manifest), Path(args.data_dir), Settings())
    print(json.dumps(report["totals"], indent=2))
    print(f"Full report: {report['report_path']}")
    return 0


def cmd_ingest(args) -> int:
    from .lambda_handler import ingest_handler
    import os

    os.environ["BUCKET_NAME"] = args.bucket
    print(json.dumps(ingest_handler({"tickers": [args.ticker], "form": args.form}), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="kpi", description="SEC filing KPI extractor")
    p.add_argument("--data-dir", default="./data", help="local storage root (default ./data)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="extract KPIs from one filing")
    r.add_argument("--ticker")
    r.add_argument("--cik")
    r.add_argument("--form", default="10-Q", choices=["10-Q", "10-K"])
    r.add_argument("--accession")
    r.add_argument("--file", help="local filing HTML instead of fetching from EDGAR")
    r.add_argument("--meta", help="FilingMeta JSON for --file")
    r.add_argument("--facts", help="companyfacts JSON for --file (enables XBRL validation)")
    r.add_argument("--bucket", help="use S3 storage instead of --data-dir")
    r.set_defaults(func=cmd_run)

    rv = sub.add_parser("review", help="human-in-the-loop review queue")
    rv.add_argument("action", choices=["list", "show", "approve", "correct", "reject"])
    rv.add_argument("item", nargs="?", help="FILING_ID/ITEM_ID")
    rv.add_argument("--filing", help="filter list by filing id")
    rv.add_argument("--value", type=float, help="corrected value (for 'correct')")
    rv.add_argument("--reviewer", default="analyst")
    rv.add_argument("--note")
    rv.add_argument("--bucket")
    rv.set_defaults(func=cmd_review)

    e = sub.add_parser("eval", help="batch evaluation against XBRL")
    e.add_argument("--manifest", required=True)
    e.set_defaults(func=cmd_eval)

    i = sub.add_parser("ingest", help="fetch latest filing into S3 (triggers Lambda)")
    i.add_argument("--ticker", required=True)
    i.add_argument("--form", default="10-Q", choices=["10-Q", "10-K"])
    i.add_argument("--bucket", required=True)
    i.set_defaults(func=cmd_ingest)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.cmd == "run" and not (args.file or args.ticker or args.cik):
        p.error("run needs --ticker, --cik, or --file")
    if args.cmd == "review" and args.action != "list" and not args.item:
        p.error("review action needs FILING_ID/ITEM_ID")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
