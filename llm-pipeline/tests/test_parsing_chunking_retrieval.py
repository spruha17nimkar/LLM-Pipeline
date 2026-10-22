from kpi_pipeline.chunking import chunk_document
from kpi_pipeline.config import KPI_BY_ID, KPIS
from kpi_pipeline.parsing import detect_scale, parse_html
from kpi_pipeline.pipeline import meta_from_dei
from kpi_pipeline.retrieval import BM25Index


def test_tables_flatten_into_labelled_rows(doc):
    assert "Net revenue | 1,250 | 1,100 | 2,410 | 2,150" in doc.text
    assert "Interest and other income (expense), net | (10) | (15) | (20) | (30)" in doc.text
    assert "999999" not in doc.text  # hidden ix:header stripped


def test_sections_detected(doc):
    labels = [s.label for s in doc.sections]
    assert labels[0] == "Item 1. Financial Statements"
    assert labels[1].startswith("Item 2. Management")
    assert "Item 1A. Risk Factors" in labels


def test_dei_metadata(doc):
    m = meta_from_dei(doc, "1234567", "0001234567-25-000042")
    assert (m.form, m.period_of_report, m.fiscal_year, m.fiscal_period) == ("10-Q", "2025-06-30", 2025, "Q2")
    assert m.company == "Acme Analytics, Inc." and m.cik == "1234567"


def test_chunk_offsets_are_exact_and_sections_not_mixed(doc):
    chunks = chunk_document(doc, max_chars=800)
    assert len(chunks) > 3
    for c in chunks:
        assert doc.text[c.start:c.end] == c.text
        assert len(c.text) <= 800 or "\n" not in c.text
        assert doc.section_at(c.end - 1) == c.section  # never spans two sections
    covered = set()
    for c in chunks:
        covered.update(range(c.start, c.end))
    assert all(i in covered or doc.text[i] == "\n" for i in range(len(doc.text)))


def test_oversized_table_is_split():
    rows = "".join(f"<tr><td>Line item {i}</td><td>{i},000</td></tr>" for i in range(400))
    doc = parse_html(f"<html><body><table>{rows}</table></body></html>")
    chunks = chunk_document(doc, max_chars=1000)
    assert len(chunks) > 5
    assert all(len(c.text) <= 1000 for c in chunks)


def test_retrieval_finds_statement_chunk(doc):
    chunks = chunk_document(doc, max_chars=1200)
    index = BM25Index(chunks)
    for kpi_id, label in [
        ("revenue", "Net revenue |"),
        ("operating_cash_flow", "Net cash provided by operating activities |"),
        ("cash_and_equivalents", "Cash and cash equivalents |"),
    ]:
        top = index.search(KPI_BY_ID[kpi_id].query, k=3)
        assert any(label in c.text for c in top), kpi_id
    assert index.search("zebra giraffe") == []


def test_detect_scale():
    assert detect_scale("(In millions, except per share amounts)") == "millions"
    assert detect_scale("(in thousands)") == "thousands"
    assert detect_scale("no scale") is None


def test_kpi_definitions_are_consistent():
    assert len({k.id for k in KPIS}) == len(KPIS)
    for k in KPIS:
        assert k.xbrl_tags and k.period_type in ("duration", "instant")
