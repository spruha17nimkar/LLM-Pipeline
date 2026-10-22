import json

import boto3
import pytest
from moto import mock_aws

from kpi_pipeline import lambda_handler
from kpi_pipeline.cli import main as cli_main
from kpi_pipeline.pipeline import run_filing
from kpi_pipeline.review import ReviewQueue
from kpi_pipeline.storage import LocalStorage, S3Storage, read_json, write_json

from conftest import FIX, RuleExtractor

FID = "0001234567_0001234567-25-000042"


def test_clean_run_auto_approves_and_records_history(html, meta, facts, settings, tmp_path):
    settings.review_unverified = False  # gross profit has no XBRL tag in the fixture
    store = LocalStorage(tmp_path)
    r = run_filing(html, meta, RuleExtractor(), store, facts, settings)

    assert r.status == "approved" and not r.review_items
    m = r.metrics
    assert m["mismatch"] == 0 and m["kpis_missed"] == 0 and m["kpi_coverage"] == 1.0
    assert m["value_accuracy_vs_xbrl"] == 1.0 and m["citation_verified_rate"] == 1.0
    assert m["match"] == 20 and m["no_ground_truth"] == 4  # gross profit x4 columns

    yoy = {t.kpi_id: t for t in r.trends if t.kind == "YoY" and t.period_duration == "3M"}
    assert round(yoy["revenue"].pct_change, 4) == 0.1364
    assert round(yoy["net_income"].pct_change, 4) == 0.25

    approved = read_json(store, f"results/approved/{FID}.json")
    assert approved["status"] == "approved"
    hist = read_json(store, "history/1234567.json")
    assert any(p["kpi_id"] == "revenue" and p["period_duration"] == "3M" and p["value"] == 1.25e9 for p in hist)


def test_mismatch_is_held_for_review_then_corrected(html, meta, facts, settings, tmp_path):
    store = LocalStorage(tmp_path)
    bad = RuleExtractor(overrides={("net_income", "2025-06-30", "3M"): {"value": 252, "raw_text": "252"}})
    r = run_filing(html, meta, bad, store, facts, settings)

    reasons = {(i.kpi_id, i.reason) for i in r.review_items}
    assert ("net_income", "MISMATCH") in reasons  # wrong number vs XBRL
    assert ("gross_profit", "NO_GROUND_TRUTH") in reasons
    assert r.status == "pending_review"
    assert store.exists(f"results/staged/{FID}.json")
    assert not store.exists(f"results/approved/{FID}.json")  # not usable until reviewed

    q = ReviewQueue(store)
    pending = q.pending(FID)
    mism = next(i for i in pending if i["reason"] == "MISMATCH")
    assert mism["validation"]["xbrl_value"] == 225e6

    q.resolve(FID, mism["item_id"], "corrected", "spruha", corrected_value=225e6, note="typo in extraction")
    for it in q.pending(FID)[:-1]:
        assert q.resolve(FID, it["item_id"], "approved", "spruha")["published"] is False
    last = q.pending(FID)[0]
    assert q.resolve(FID, last["item_id"], "approved", "spruha")["published"] is True

    approved = read_json(store, f"results/approved/{FID}.json")
    ni = next(k for k in approved["kpis"] if k["kpi_id"] == "net_income")
    cur = next(v for v in ni["values"] if v["period_end"] == "2025-06-30" and v["period_duration"] == "3M")
    assert cur["value"] == 225e6 and cur["original_value"] == 252e6 and cur["review"]["reviewer"] == "spruha"


def test_missed_kpi_is_flagged_and_reviewer_can_fill_it(html, meta, facts, settings, tmp_path):
    settings.review_unverified = False
    store = LocalStorage(tmp_path)
    r = run_filing(html, meta, RuleExtractor(skip={"operating_cash_flow"}), store, facts, settings)
    [missed] = [i for i in r.review_items if i.reason == "MISSED"]
    assert missed.kpi_id == "operating_cash_flow" and missed.validation["xbrl_value"] == 610e6
    assert r.metrics["kpis_missed"] == 1

    ReviewQueue(store).resolve(FID, missed.item_id, "corrected", "spruha", corrected_value=610e6)
    approved = read_json(store, f"results/approved/{FID}.json")
    ocf = next(k for k in approved["kpis"] if k["kpi_id"] == "operating_cash_flow")
    assert ocf["found"] and ocf["values"][0]["value"] == 610e6 and ocf["values"][0]["period_duration"] == "6M"


def test_rejected_value_is_dropped(html, meta, facts, settings, tmp_path):
    store = LocalStorage(tmp_path)
    settings.review_unverified = False
    bad = RuleExtractor(overrides={("revenue", "2025-06-30", "3M"): {"quote": "Revenue was huge"}})
    r = run_filing(html, meta, bad, store, facts, settings)
    [item] = r.review_items
    assert item.reason == "CITATION_UNVERIFIED"
    ReviewQueue(store).resolve(FID, item.item_id, "rejected", "spruha")
    rev = next(k for k in read_json(store, f"results/approved/{FID}.json")["kpis"] if k["kpi_id"] == "revenue")
    assert len(rev["values"]) == 3


def test_rerun_resets_review_state(html, meta, facts, settings, tmp_path):
    store = LocalStorage(tmp_path)
    run_filing(html, meta, RuleExtractor(), store, facts, settings)
    n1 = len(ReviewQueue(store).pending(FID))
    run_filing(html, meta, RuleExtractor(), store, facts, settings)
    assert len(ReviewQueue(store).pending(FID)) == n1 == 4


def test_qoq_uses_history_from_previous_approved_filing(html, meta, facts, settings, tmp_path):
    store = LocalStorage(tmp_path)
    write_json(store, "history/1234567.json", [
        {"kpi_id": "revenue", "period_end": "2025-03-31", "period_duration": "3M", "value": 1.16e9, "accession": "0001234567-25-000021"}
    ])
    r = run_filing(html, meta, RuleExtractor(), store, facts, settings)
    [qoq] = [t for t in r.trends if t.kind == "QoQ" and t.kpi_id == "revenue"]
    assert qoq.prior == 1.16e9 and qoq.prior_source == "history"


def test_cli_review_flow(html, meta, facts, settings, tmp_path, capsys):
    store = LocalStorage(tmp_path)
    run_filing(html, meta, RuleExtractor(), store, facts, settings)
    assert cli_main(["--data-dir", str(tmp_path), "review", "list"]) == 0
    out = capsys.readouterr().out
    assert "NO_GROUND_TRUTH" in out and "gross_profit" in out
    items = ReviewQueue(store).pending(FID)
    for it in items:
        cli_main(["--data-dir", str(tmp_path), "review", "approve", f"{FID}/{it['item_id']}", "--reviewer", "spruha"])
    assert "published" in capsys.readouterr().out
    assert store.exists(f"results/approved/{FID}.json")


@mock_aws
def test_s3_storage_and_lambda_trigger(html, facts, monkeypatch):
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="filings")
    store = S3Storage("filings", client=s3)

    store.write_text("a/b.json", '{"x": 1}')
    assert store.exists("a/b.json") and not store.exists("a/c.json")
    assert store.list("a/") == ["a/b.json"]
    store.delete("a/b.json")
    assert store.list("a/") == []

    key = "filings/raw/1234567/0001234567-25-000042.htm"
    store.write_text(key, html)  # no meta sidecar: metadata comes from the iXBRL cover page
    write_json(store, "xbrl/1234567.json", facts)

    monkeypatch.setattr(lambda_handler, "build_extractor", lambda settings: RuleExtractor())
    monkeypatch.setattr(lambda_handler, "_store", lambda bucket=None: S3Storage(bucket or "filings", client=s3))
    event = {"Records": [{"s3": {"bucket": {"name": "filings"}, "object": {"key": key}}}]}
    out = lambda_handler.process_handler(event)

    [res] = out["processed"]
    assert res["filing_id"] == FID and res["status"] == "pending_review"
    assert res["metrics"]["match"] == 20
    assert store.exists(f"results/staged/{FID}.json")
    assert len(store.list(f"review/pending/{FID}/")) == 4

    # unrelated keys are ignored
    assert lambda_handler.process_handler({"Records": [{"s3": {"bucket": {"name": "filings"}, "object": {"key": "results/x.json"}}}]}) == {"processed": []}


def test_edgar_client_requires_contact_email():
    from kpi_pipeline.edgar import EdgarClient

    with pytest.raises(ValueError):
        EdgarClient("no email here")
