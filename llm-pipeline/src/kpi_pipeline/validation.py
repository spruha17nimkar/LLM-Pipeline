"""Eval harness: check every extracted figure against XBRL ground truth."""

from __future__ import annotations

from typing import Optional

from .config import KPIDefinition, Settings
from .models import ExtractedValue, ValidationResult
from .xbrl import CompanyFacts


def _scale_hint(extracted: float, truth: float) -> str:
    if not extracted or not truth:
        return ""
    ratio = abs(extracted / truth)
    for factor, name in ((1e3, "thousand"), (1e6, "million"), (1e9, "billion")):
        if abs(ratio - factor) / factor < 0.01:
            return f"extracted is ~{name}x XBRL: likely unit-scale error"
        if abs(ratio - 1 / factor) * factor < 0.01:
            return f"extracted is ~1/{name} of XBRL: likely unit-scale error"
    if abs(extracted + truth) <= abs(truth) * 0.005:
        return "sign flipped vs XBRL"
    return ""


def validate_value(
    kpi: KPIDefinition,
    ev: ExtractedValue,
    facts: Optional[CompanyFacts],
    settings: Settings,
    accession: Optional[str] = None,
) -> ValidationResult:
    fact = facts.lookup(kpi, ev.period_end, ev.period_duration, accession) if facts else None

    if fact is None:
        status = "NO_GROUND_TRUTH" if ev.citation.verified else "CITATION_UNVERIFIED"
        return ValidationResult(status=status, note="no matching XBRL fact for this period")

    diff = ev.value - fact.value
    rel = abs(diff) / abs(fact.value) if fact.value else (0.0 if diff == 0 else float("inf"))
    ok = abs(diff) <= settings.eps_abs_tolerance if kpi.is_per_share else rel <= settings.rel_tolerance

    if ok and not ev.citation.verified:
        status, note = "CITATION_UNVERIFIED", "value matches XBRL but quote not found verbatim in cited excerpt"
    elif ok:
        status, note = "MATCH", ""
    else:
        status, note = "MISMATCH", _scale_hint(ev.value, fact.value)

    return ValidationResult(
        status=status,
        xbrl_value=fact.value,
        xbrl_tag=fact.tag,
        xbrl_accession=fact.accession,
        diff=diff,
        rel_diff=rel,
        note=note,
    )


def summarize(validations: list[ValidationResult], kpis_expected: int, kpis_found: int, missed: int) -> dict:
    """Run-level eval metrics. Value accuracy counts only values that had an XBRL fact."""
    statuses = [v.status for v in validations]
    with_truth = [v for v in validations if v.xbrl_value is not None]
    correct = sum(1 for v in with_truth if v.status != "MISMATCH")
    return {
        "values_extracted": len(statuses),
        "values_with_ground_truth": len(with_truth),
        "match": statuses.count("MATCH"),
        "mismatch": statuses.count("MISMATCH"),
        "no_ground_truth": statuses.count("NO_GROUND_TRUTH"),
        "citation_unverified": statuses.count("CITATION_UNVERIFIED"),
        "value_accuracy_vs_xbrl": round(correct / len(with_truth), 4) if with_truth else None,
        "citation_verified_rate": round(1 - statuses.count("CITATION_UNVERIFIED") / len(statuses), 4) if statuses else None,
        "kpi_coverage": round(kpis_found / kpis_expected, 4) if kpis_expected else None,
        "kpis_missed": missed,
    }
