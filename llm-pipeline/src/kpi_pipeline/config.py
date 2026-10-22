"""KPI definitions and runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class KPIDefinition:
    id: str
    label: str
    description: str
    # Candidate us-gaap XBRL tags, in priority order. Companies tag the same
    # concept differently (e.g. ASC 606 revenue tags), so we try several.
    xbrl_tags: tuple[str, ...]
    xbrl_unit: str  # "USD" or "USD/shares"
    period_type: str  # "duration" (income / cash flow) or "instant" (balance sheet)
    query: str  # retrieval query terms
    is_per_share: bool = False


KPIS: tuple[KPIDefinition, ...] = (
    KPIDefinition(
        id="revenue",
        label="Total Revenue",
        description="Total net revenue / net sales for the period.",
        xbrl_tags=(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        xbrl_unit="USD",
        period_type="duration",
        query="total revenue net revenues net sales total net sales",
    ),
    KPIDefinition(
        id="gross_profit",
        label="Gross Profit",
        description="Revenue minus cost of revenue / cost of sales (gross margin in dollars).",
        xbrl_tags=("GrossProfit",),
        xbrl_unit="USD",
        period_type="duration",
        query="gross profit gross margin cost of sales cost of revenue",
    ),
    KPIDefinition(
        id="operating_income",
        label="Operating Income",
        description="Operating income (loss) / income from operations.",
        xbrl_tags=("OperatingIncomeLoss",),
        xbrl_unit="USD",
        period_type="duration",
        query="operating income income from operations operating loss",
    ),
    KPIDefinition(
        id="net_income",
        label="Net Income",
        description="Net income (loss) attributable to the company.",
        xbrl_tags=("NetIncomeLoss", "ProfitLoss"),
        xbrl_unit="USD",
        period_type="duration",
        query="net income net loss net earnings attributable",
    ),
    KPIDefinition(
        id="eps_diluted",
        label="Diluted EPS",
        description="Diluted earnings (loss) per share.",
        xbrl_tags=("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"),
        xbrl_unit="USD/shares",
        period_type="duration",
        query="diluted earnings per share diluted net income per share",
        is_per_share=True,
    ),
    KPIDefinition(
        id="operating_cash_flow",
        label="Operating Cash Flow",
        description="Net cash provided by (used in) operating activities.",
        xbrl_tags=(
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        ),
        xbrl_unit="USD",
        period_type="duration",
        query="net cash provided by operating activities cash flows operating",
    ),
    KPIDefinition(
        id="cash_and_equivalents",
        label="Cash and Cash Equivalents",
        description="Cash and cash equivalents at period end (balance sheet).",
        xbrl_tags=(
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
        xbrl_unit="USD",
        period_type="instant",
        query="cash and cash equivalents balance sheet total current assets",
    ),
)

KPI_BY_ID = {k.id: k for k in KPIS}


@dataclass
class Settings:
    anthropic_model: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    )
    sec_user_agent: str = field(
        default_factory=lambda: os.getenv(
            "SEC_USER_AGENT", "kpi-pipeline research contact@example.com"
        )
    )
    chunk_chars: int = int(os.getenv("CHUNK_CHARS", "3000"))
    top_k: int = int(os.getenv("RETRIEVAL_TOP_K", "6"))
    # Relative tolerance for dollar figures; absolute tolerance for per-share.
    rel_tolerance: float = float(os.getenv("VALIDATION_REL_TOL", "0.005"))
    eps_abs_tolerance: float = float(os.getenv("VALIDATION_EPS_TOL", "0.011"))
    # Send values with no XBRL ground truth to review as well.
    review_unverified: bool = os.getenv("REVIEW_UNVERIFIED", "true").lower() == "true"
