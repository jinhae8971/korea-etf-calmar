"""서사 축 — EDGAR 전문검색 분기별 언급 건수 + SIC 산업 다양성.

과거 분기는 변하지 않으므로 캐시에 누적하고, 매 실행 시 최근 2분기만 갱신한다.
(공시는 뒤늦게 들어오므로 직전 분기도 다시 읽는다)

주의: Q1 은 10-K 시즌이라 건수가 구조적으로 높다. 반드시 YoY 또는
4분기 합으로만 비교할 것 — 전분기 대비는 계절성에 오염된다.
SIC 버킷은 30개에서 잘리므로 다양성 지표의 상한은 30이다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from datetime import date

from .http_client import fetch_json

QUARTER_BOUNDS = [("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30"), ("10-01", "12-31")]
REFRESH_TAIL = 2  # 최근 N분기는 매번 다시 읽는다


def current_quarter() -> str:
    today = date.today()
    return f"{today.year}Q{(today.month - 1) // 3 + 1}"


def quarters_from(start_year: int) -> list[str]:
    out = []
    today = date.today()
    for year in range(start_year, today.year + 1):
        for i, (_, end) in enumerate(QUARTER_BOUNDS, 1):
            if date(year, int(end[:2]), int(end[3:])) > today:
                # 진행 중인 분기도 부분 집계로 포함한다 (partial 표시)
                if f"{year}Q{i}" == current_quarter():
                    out.append(f"{year}Q{i}")
                continue
            out.append(f"{year}Q{i}")
    return out


def _query(term: str, quarter: str) -> dict:
    year, idx = int(quarter[:4]), int(quarter[5])
    start, end = QUARTER_BOUNDS[idx - 1]
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{urllib.parse.quote(term)}%22&startdt={year}-{start}&enddt={year}-{end}"
    )
    payload = fetch_json(url, headers={"Accept": "application/json"})
    sic = (payload.get("aggregations") or {}).get("sic_filter", {}).get("buckets", [])
    return {"hits": payload["hits"]["total"]["value"], "sic_n": len(sic)}


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def save(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


def collect(terms: list[str], cache_path: str, start_year: int = 2018) -> tuple[dict, dict]:
    """반환: (용어별 시계열, 상태)"""
    cache = load(cache_path)
    quarters = quarters_from(start_year)
    tail = set(quarters[-REFRESH_TAIL:])
    fetched = failed = 0

    for term in terms:
        series = cache.setdefault(term, {})
        for quarter in quarters:
            if quarter in series and quarter not in tail:
                continue
            try:
                series[quarter] = _query(term, quarter)
                fetched += 1
            except Exception:  # noqa: BLE001
                failed += 1
            time.sleep(0.3)

    save(cache_path, cache)
    mode = "OK" if fetched and not failed else ("DEGRADED" if fetched else "CACHE_ONLY")
    return cache, {"mode": mode, "fetched": fetched, "failed": failed}


def rolling4(series: dict[str, dict], key: str) -> dict[str, float]:
    """4분기 합(또는 평균) — 10-K 계절성을 제거한다."""
    quarters = sorted(series)
    out: dict[str, float] = {}
    for i in range(3, len(quarters)):
        window = [series[q].get(key) for q in quarters[i - 3:i + 1]]
        if any(v is None for v in window):
            continue
        out[quarters[i]] = sum(window) / (4 if key == "sic_n" else 1)
    return out


def combine(cache: dict, terms: list[str], key: str) -> dict[str, dict]:
    """여러 용어를 합산한 단일 시계열."""
    out: dict[str, dict] = {}
    for term in terms:
        for quarter, rec in (cache.get(term) or {}).items():
            slot = out.setdefault(quarter, {"hits": 0, "sic_n": 0})
            slot["hits"] += rec.get("hits", 0)
            # SIC 다양성은 합산이 아니라 최대치를 쓴다 (버킷 상한 30 때문)
            slot["sic_n"] = max(slot["sic_n"], rec.get("sic_n", 0))
    return out
