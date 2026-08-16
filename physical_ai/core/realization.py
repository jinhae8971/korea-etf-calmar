"""실현 축 — SEC XBRL 에서 매출·재고·영업이익을 분기 시계열로.

`frame` 필드는 희소하므로 쓰지 않는다. start/end 기간 80~100일로 분기 항목을
직접 골라내고, 같은 분기가 여러 번 보고되면 가장 늦게 제출된 값을 쓴다.
태그는 기업마다 다르므로 폴백 체인을 돈다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from .http_client import fetch_json

CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "inventory": ("InventoryNet", "InventoryGross"),
    "operating_income": ("OperatingIncomeLoss",),
}
INSTANT = {"inventory"}


def _quarter(end: str) -> str:
    d = datetime.strptime(end, "%Y-%m-%d").date()
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def concept_series(cik: str, concept: str) -> dict[str, float]:
    instant = concept in INSTANT
    for tag in CONCEPTS[concept]:
        try:
            payload = fetch_json(
                f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json",
                headers={"Accept": "application/json"},
            )
        except Exception:  # noqa: BLE001
            continue
        picked: dict[str, tuple[str, float]] = {}
        for entry in payload.get("units", {}).get("USD") or []:
            end, val = entry.get("end"), entry.get("val")
            if not end or val is None:
                continue
            if not instant:
                start = entry.get("start")
                if not start:
                    continue
                span = (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
                if not (80 <= span <= 100):
                    continue
            q = _quarter(end)
            filed = entry.get("filed", "")
            if q not in picked or filed > picked[q][0]:
                picked[q] = (filed, float(val))
        series = {q: v for q, (_, v) in picked.items()}
        if len(series) >= 6:
            return series
    return {}


def collect(ciks: dict[str, str], cache_path: str) -> tuple[dict, dict]:
    """반환: ({ticker: {concept: {quarter: value}}}, 상태)"""
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cache = json.load(handle)
        except (json.JSONDecodeError, OSError):
            cache = {}

    fresh = stale = 0
    for ticker, cik in ciks.items():
        got = {}
        for concept in CONCEPTS:
            series = concept_series(cik, concept)
            time.sleep(0.15)
            if series:
                got[concept] = series
        if got:
            cache[ticker] = got
            fresh += 1
        elif ticker in cache:
            stale += 1

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = f"{cache_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, cache_path)

    mode = "OK" if fresh and not stale else ("DEGRADED" if fresh else "CACHE_ONLY")
    return cache, {"mode": mode, "fresh": fresh, "from_cache": stale}
