"""Core data structures. Everything serialises to plain JSON via ``asdict``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

DURATION_DAYS = {"3M": 91, "6M": 182, "9M": 273, "12M": 365}
SCALE = {"units": 1, "thousands": 1_000, "millions": 1_000_000, "billions": 1_000_000_000}


@dataclass
class FilingMeta:
    cik: str
    company: str
    form: str  # "10-Q" or "10-K"
    accession: str  # e.g. 0000320193-24-000081
    period_of_report: str  # YYYY-MM-DD
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None  # Q1/Q2/Q3/FY
    filing_date: Optional[str] = None
    source_url: Optional[str] = None

    @property
    def filing_id(self) -> str:
        return f"{int(self.cik):010d}_{self.accession}"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FilingMeta":
        fields_ = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in fields_})


@dataclass
class Chunk:
    id: str
    text: str
    start: int  # char offset into parsed document text
    end: int
    section: str


@dataclass
class Citation:
    chunk_id: str
    section: str
    quote: str
    char_start: Optional[int]  # offset of the quote in the parsed document
    char_end: Optional[int]
    source_url: Optional[str]
    verified: bool  # quote found verbatim in the chunk and contains the number


@dataclass
class ExtractedValue:
    kpi_id: str
    period_end: str
    period_duration: str  # 3M / 6M / 9M / 12M / instant
    value: float  # normalised to units (dollars, or dollars per share)
    raw_text: str  # the number as printed, e.g. "(1,234)"
    unit_scale: str  # scale as printed: units / thousands / millions / billions
    citation: Citation
    is_current_period: bool = False


@dataclass
class ValidationResult:
    status: str  # MATCH | MISMATCH | NO_GROUND_TRUTH | CITATION_UNVERIFIED
    xbrl_value: Optional[float] = None
    xbrl_tag: Optional[str] = None
    xbrl_accession: Optional[str] = None
    diff: Optional[float] = None
    rel_diff: Optional[float] = None
    note: str = ""


@dataclass
class Trend:
    kpi_id: str
    period_end: str
    period_duration: str
    kind: str  # YoY | QoQ
    current: float
    prior: float
    prior_period_end: str
    change: float
    pct_change: Optional[float]
    prior_source: str  # "filing" (comparative column) or "history" (earlier filing)


@dataclass
class KPIResult:
    kpi_id: str
    label: str
    found: bool
    values: list[ExtractedValue] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    notes: str = ""


@dataclass
class ReviewItem:
    item_id: str
    filing_id: str
    kpi_id: str
    reason: str  # MISMATCH | NO_GROUND_TRUTH | CITATION_UNVERIFIED | MISSED
    extracted: Optional[dict[str, Any]]
    validation: Optional[dict[str, Any]]
    status: str = "pending"  # pending | approved | corrected | rejected
    reviewer: Optional[str] = None
    corrected_value: Optional[float] = None
    note: str = ""


@dataclass
class FilingResult:
    meta: FilingMeta
    kpis: list[KPIResult]
    trends: list[Trend]
    review_items: list[ReviewItem]
    status: str  # "approved" (nothing to review) or "pending_review"
    model: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
