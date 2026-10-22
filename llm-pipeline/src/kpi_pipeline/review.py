"""Human-in-the-loop review queue.

Flagged values go to ``review/pending``. Results for a filing stay in
``results/staged`` until every flagged item is resolved; only then is the
filing published to ``results/approved`` (with reviewer corrections applied)
and its values added to the company history used for QoQ trends.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from .models import ReviewItem
from .storage import Storage, read_json, write_json

DECISIONS = ("approved", "corrected", "rejected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ReviewQueue:
    def __init__(self, store: Storage):
        self.store = store

    def enqueue(self, items: list[ReviewItem]) -> None:
        for it in items:
            write_json(self.store, f"review/pending/{it.filing_id}/{it.item_id}.json", asdict(it))

    def pending(self, filing_id: Optional[str] = None) -> list[dict[str, Any]]:
        prefix = f"review/pending/{filing_id}/" if filing_id else "review/pending/"
        return [read_json(self.store, k) for k in self.store.list(prefix) if k.endswith(".json")]

    def get(self, filing_id: str, item_id: str) -> dict[str, Any]:
        key = f"review/pending/{filing_id}/{item_id}.json"
        item = read_json(self.store, key)
        if item is None:
            raise KeyError(f"No pending review item {filing_id}/{item_id}")
        return item

    def resolve(
        self,
        filing_id: str,
        item_id: str,
        decision: str,
        reviewer: str,
        corrected_value: Optional[float] = None,
        note: str = "",
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError(f"decision must be one of {DECISIONS}")
        if decision == "corrected" and corrected_value is None:
            raise ValueError("corrected_value is required when decision='corrected'")
        item = self.get(filing_id, item_id)
        item.update(status=decision, reviewer=reviewer, corrected_value=corrected_value, note=note, resolved_at=_now())
        write_json(self.store, f"review/resolved/{filing_id}/{item_id}.json", item)
        self.store.delete(f"review/pending/{filing_id}/{item_id}.json")

        published = None
        if not self.pending(filing_id):
            published = finalize(self.store, filing_id)
        return {"item": item, "published": published is not None}


def _resolved(store: Storage, filing_id: str) -> list[dict[str, Any]]:
    return [read_json(store, k) for k in store.list(f"review/resolved/{filing_id}/")]


def _key(d: dict[str, Any]) -> tuple:
    return (d["kpi_id"], d["period_end"], d["period_duration"])


def finalize(store: Storage, filing_id: str) -> dict[str, Any]:
    """Apply review decisions to the staged result and publish it."""
    result = read_json(store, f"results/staged/{filing_id}.json")
    if result is None:
        raise KeyError(f"No staged result for {filing_id}")

    decisions = {}
    for item in _resolved(store, filing_id):
        if item.get("extracted"):
            decisions[_key(item["extracted"])] = item

    for kpi in result["kpis"]:
        kept_values, kept_vals = [], []
        for ev, val in zip(kpi["values"], kpi["validations"]):
            d = decisions.pop(_key(ev), None)
            if d:
                ev["review"] = {"status": d["status"], "reviewer": d["reviewer"], "note": d.get("note", "")}
                if d["status"] == "rejected":
                    continue
                if d["status"] == "corrected":
                    ev["original_value"] = ev["value"]
                    ev["value"] = float(d["corrected_value"])
            kept_values.append(ev)
            kept_vals.append(val)
        kpi["values"], kpi["validations"] = kept_values, kept_vals

    # Reviewer-supplied values for KPIs the model missed.
    for d in decisions.values():
        if d["status"] == "corrected" and d["reason"] == "MISSED":
            ex = d["extracted"]
            for kpi in result["kpis"]:
                if kpi["kpi_id"] == ex["kpi_id"]:
                    kpi["found"] = True
                    kpi["values"].append({
                        **ex,
                        "value": float(d["corrected_value"]),
                        "raw_text": "",
                        "unit_scale": "units",
                        "citation": None,
                        "is_current_period": ex["period_end"] == result["meta"]["period_of_report"],
                        "review": {"status": "corrected", "reviewer": d["reviewer"], "note": d.get("note", "")},
                    })
                    kpi["validations"].append({"status": "REVIEWER_ENTERED", "note": "added in review"})

    result["status"] = "approved"
    result["approved_at"] = _now()
    write_json(store, f"results/approved/{filing_id}.json", result)
    _update_history(store, result)
    return result


def _update_history(store: Storage, result: dict[str, Any]) -> None:
    cik = str(int(result["meta"]["cik"]))
    key = f"history/{cik}.json"
    hist = {_key(p): p for p in read_json(store, key, default=[])}
    for kpi in result["kpis"]:
        for ev in kpi["values"]:
            p = {
                "kpi_id": ev["kpi_id"],
                "period_end": ev["period_end"],
                "period_duration": ev["period_duration"],
                "value": ev["value"],
                "accession": result["meta"]["accession"],
            }
            hist[_key(p)] = p
    write_json(store, key, sorted(hist.values(), key=lambda p: (p["kpi_id"], p["period_end"], p["period_duration"])))
