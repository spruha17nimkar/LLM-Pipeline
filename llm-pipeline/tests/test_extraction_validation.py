from types import SimpleNamespace

from kpi_pipeline.config import KPI_BY_ID
from kpi_pipeline.extraction import ClaudeExtractor, RECORD_KPI_TOOL, to_extracted_values
from kpi_pipeline.models import Chunk, Citation, ExtractedValue
from kpi_pipeline.trends import Point, compute_trends
from kpi_pipeline.validation import validate_value
from kpi_pipeline.xbrl import CompanyFacts

CHUNK = Chunk("c0007", "Statements (In millions)\nNet revenue | 1,250 | 1,100\nNet income | 225 | 180", 500, 579, "Item 1. Financial Statements")


def _raw(**kw):
    base = {"period_end": "2025-06-30", "period_duration": "3M", "raw_text": "1,250", "value": 1250,
            "unit_scale": "millions", "chunk_id": "c0007", "quote": "Net revenue | 1,250 | 1,100"}
    base.update(kw)
    return base


def test_scaling_and_verified_citation_with_offsets(meta):
    [ev] = to_extracted_values(KPI_BY_ID["revenue"], {"values": [_raw()]}, [CHUNK], meta)
    assert ev.value == 1_250_000_000 and ev.is_current_period
    c = ev.citation
    assert c.verified and c.section == "Item 1. Financial Statements"
    assert CHUNK.text[c.char_start - CHUNK.start:c.char_end - CHUNK.start] == "Net revenue | 1,250 | 1,100"
    assert c.source_url == meta.source_url


def test_hallucinated_quote_or_number_is_unverified(meta):
    kpi = KPI_BY_ID["revenue"]
    [a] = to_extracted_values(kpi, {"values": [_raw(quote="Total revenue was 1,250")]}, [CHUNK], meta)
    [b] = to_extracted_values(kpi, {"values": [_raw(raw_text="1,300", value=1300)]}, [CHUNK], meta)
    [c] = to_extracted_values(kpi, {"values": [_raw(chunk_id="c9999")]}, [CHUNK], meta)
    assert not a.citation.verified and not b.citation.verified and not c.citation.verified


def test_parentheses_negative_and_per_share_never_scaled(meta):
    neg = to_extracted_values(KPI_BY_ID["net_income"], {"values": [_raw(raw_text="(12)", value=12)]}, [CHUNK], meta)
    eps = to_extracted_values(KPI_BY_ID["eps_diluted"], {"values": [_raw(raw_text="1.12", value=1.12)]}, [CHUNK], meta)
    assert neg[0].value == -12_000_000
    assert eps[0].value == 1.12 and eps[0].unit_scale == "units"


def test_malformed_and_duplicate_values_dropped(meta):
    vals = [_raw(), _raw(), {"value": 1}, _raw(value="n/a")]
    assert len(to_extracted_values(KPI_BY_ID["revenue"], {"values": vals}, [CHUNK], meta)) == 1


def _ev(kpi_id, value, end="2025-06-30", dur="3M", verified=True, current=True):
    cit = Citation("c1", "s", "q", 0, 1, None, verified)
    return ExtractedValue(kpi_id, end, dur, value, str(value), "units", cit, current)


def test_xbrl_lookup_prefers_this_filing_and_respects_duration(facts):
    cf = CompanyFacts(facts)
    kpi = KPI_BY_ID["revenue"]
    f3 = cf.lookup(kpi, "2024-06-30", "3M", accession="0001234567-25-000042")
    assert f3.value == 1.1e9 and f3.accession == "0001234567-25-000042"
    f_orig = cf.lookup(kpi, "2024-06-30", "3M")  # no accession: earliest filed
    assert f_orig.accession == "0001234567-24-000040"
    assert cf.lookup(kpi, "2025-06-30", "6M").value == 2.41e9
    assert cf.lookup(kpi, "2025-06-30", "9M") is None
    assert cf.lookup(KPI_BY_ID["cash_and_equivalents"], "2024-12-31", "instant").value == 1.85e9


def test_validation_statuses(facts, settings):
    cf, kpi = CompanyFacts(facts), KPI_BY_ID["revenue"]
    assert validate_value(kpi, _ev("revenue", 1.25e9), cf, settings).status == "MATCH"
    assert validate_value(kpi, _ev("revenue", 1.2549e9), cf, settings).status == "MATCH"  # within 0.5%
    mm = validate_value(kpi, _ev("revenue", 1.25e12), cf, settings)
    assert mm.status == "MISMATCH" and "scale" in mm.note
    assert validate_value(kpi, _ev("revenue", 1.25e9, verified=False), cf, settings).status == "CITATION_UNVERIFIED"
    assert validate_value(KPI_BY_ID["gross_profit"], _ev("gross_profit", 7.5e8), cf, settings).status == "NO_GROUND_TRUTH"
    eps = KPI_BY_ID["eps_diluted"]
    assert validate_value(eps, _ev("eps_diluted", 1.12), cf, settings).status == "MATCH"
    assert validate_value(eps, _ev("eps_diluted", 1.15), cf, settings).status == "MISMATCH"


def test_trends_yoy_from_filing_and_qoq_from_history():
    values = [
        _ev("revenue", 1250.0),
        _ev("revenue", 1100.0, end="2024-06-30", current=False),
        _ev("cash_and_equivalents", 2010.0, dur="instant"),
        _ev("cash_and_equivalents", 1850.0, end="2024-12-31", dur="instant", current=False),
    ]
    history = [Point("revenue", "2025-03-31", "3M", 1160.0, "history"),
               Point("cash_and_equivalents", "2025-03-31", "instant", 1900.0, "history")]
    t = {(x.kpi_id, x.kind): x for x in compute_trends(values, history)}
    yoy = t[("revenue", "YoY")]
    assert yoy.prior == 1100 and yoy.prior_source == "filing" and abs(yoy.pct_change - 0.136364) < 1e-5
    qoq = t[("revenue", "QoQ")]
    assert qoq.prior == 1160 and qoq.prior_source == "history"
    assert ("cash_and_equivalents", "QoQ") in t
    assert ("cash_and_equivalents", "YoY") not in t  # Dec-31 comparative is not a year apart


def test_claude_extractor_uses_forced_tool_and_parses_output(meta):
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            block = SimpleNamespace(type="tool_use", name="record_kpi", input={"found": True, "values": [_raw()]})
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok"), block])

    ex = ClaudeExtractor("claude-test", client=SimpleNamespace(messages=FakeMessages()))
    out = ex.extract(KPI_BY_ID["revenue"], [CHUNK], meta)
    assert out["found"] and out["values"][0]["value"] == 1250 and out["notes"] == ""
    assert captured["tool_choice"] == {"type": "tool", "name": "record_kpi"}
    assert captured["tools"] == [RECORD_KPI_TOOL] and captured["temperature"] == 0
    assert 'id="c0007"' in captured["messages"][0]["content"]
    # no chunks -> no API call
    assert ex.extract(KPI_BY_ID["revenue"], [], meta)["found"] is False
