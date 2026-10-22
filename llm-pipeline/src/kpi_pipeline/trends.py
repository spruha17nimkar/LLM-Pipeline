"""YoY and QoQ trend computation.

Comparatives come first from the filing itself (10-Qs print the prior-year
quarter next to the current one, so YoY usually needs nothing else). QoQ needs
the immediately preceding quarter, which lives in an earlier filing, so it is
looked up in the per-company history of previously approved results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from .models import ExtractedValue, Trend


@dataclass
class Point:
    kpi_id: str
    period_end: str
    period_duration: str
    value: float
    source: str  # filing | history


def _gap(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def _find(points: list[Point], kpi_id: str, duration: str, end: str, days: int, tol: int) -> Optional[Point]:
    best = None
    for p in points:
        if p.kpi_id != kpi_id or p.period_duration != duration:
            continue
        g = _gap(end, p.period_end)
        if abs(g - days) <= tol:
            # Prefer the comparative printed in this filing over history.
            if best is None or (p.source == "filing" and best.source != "filing"):
                best = p
    return best


def _trend(cur: ExtractedValue, prior: Point, kind: str) -> Trend:
    change = cur.value - prior.value
    pct = change / abs(prior.value) if prior.value else None
    return Trend(
        kpi_id=cur.kpi_id,
        period_end=cur.period_end,
        period_duration=cur.period_duration,
        kind=kind,
        current=cur.value,
        prior=prior.value,
        prior_period_end=prior.period_end,
        change=change,
        pct_change=round(pct, 6) if pct is not None else None,
        prior_source=prior.source,
    )


def compute_trends(values: list[ExtractedValue], history: Iterable[Point] = ()) -> list[Trend]:
    points = [Point(v.kpi_id, v.period_end, v.period_duration, v.value, "filing") for v in values]
    points += list(history)

    trends: list[Trend] = []
    for cur in values:
        if not cur.is_current_period:
            continue
        yoy = _find(points, cur.kpi_id, cur.period_duration, cur.period_end, 365, 10)
        if yoy:
            trends.append(_trend(cur, yoy, "YoY"))
        if cur.period_duration in ("3M", "instant"):
            qoq = _find(points, cur.kpi_id, cur.period_duration, cur.period_end, 91, 15)
            if qoq:
                trends.append(_trend(cur, qoq, "QoQ"))
    return trends
