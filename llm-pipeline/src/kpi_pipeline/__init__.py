"""KPI extraction pipeline for SEC 10-Q / 10-K filings.

Flow: ingest -> parse -> chunk -> retrieve (BM25) -> extract (Claude) ->
ground citations -> trends (YoY / QoQ) -> validate vs XBRL -> human review.
"""

__version__ = "0.1.0"
