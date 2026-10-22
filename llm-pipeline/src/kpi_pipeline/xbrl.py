"""Ground-truth lookups in SEC XBRL company facts (data.sec.gov companyfacts JSON).

Each fact has ``end`` (and ``start`` for durations), ``val``, ``accn`` (the
accession number of the filing that reported it), ``fy``, ``fp`` and ``form``.
The same number is usually reported by several filings (as a comparative
column later), so we prefer the fact from the filing under test, then the
earliest filing that reported it (i.e. the as-originally-reported figure).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .config import KPIDefinition
from .models import DURATION_DAYS


@dataclass
class XbrlFact:
    tag: str
    value: float
    start: Optional[str]
    end: str
    accession: str
    form: str
    filed: str


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


class CompanyFacts:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.gaap = data.get("facts", {}).get("us-gaap", {})

    def facts_for(self, tag: str, unit: str) -> list[dict[str, Any]]:
        return self.gaap.get(tag, {}).get("units", {}).get(unit, [])

    def lookup(
        self,
        kpi: KPIDefinition,
        period_end: str,
        period_duration: str,
        accession: Optional[str] = None,
        day_tolerance: int = 20,
    ) -> Optional[XbrlFact]:
        target = DURATION_DAYS.get(period_duration)
        for tag in kpi.xbrl_tags:
            candidates = []
            for f in self.facts_for(tag, kpi.xbrl_unit):
                if f.get("end") != period_end:
                    continue
                if kpi.period_type == "instant":
                    if "start" in f:
                        continue
                else:
                    if "start" not in f or target is None:
                        continue
                    if abs(_days(f["start"], f["end"]) - target) > day_tolerance:
                        continue
                candidates.append(f)
            if not candidates:
                continue
            candidates.sort(key=lambda f: (f.get("accn") != accession, f.get("filed", "")))
            f = candidates[0]
            return XbrlFact(
                tag=tag,
                value=float(f["val"]),
                start=f.get("start"),
                end=f["end"],
                accession=f.get("accn", ""),
                form=f.get("form", ""),
                filed=f.get("filed", ""),
            )
        return None

    def facts_in_filing(self, kpi: KPIDefinition, accession: str) -> list[XbrlFact]:
        """All facts for a KPI that a given filing reported (used to detect misses)."""
        out = []
        for tag in kpi.xbrl_tags:
            for f in self.facts_for(tag, kpi.xbrl_unit):
                if f.get("accn") == accession:
                    out.append(XbrlFact(tag, float(f["val"]), f.get("start"), f["end"], accession, f.get("form", ""), f.get("filed", "")))
            if out:
                break
        return out
