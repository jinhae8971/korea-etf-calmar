"""가격 시계열 수집 — 3중 소스 폴백 + 레포 캐시 병합.

소스 우선순위
  1) Stooq CSV      : 러너 친화적, 미국/한국 모두 커버
  2) Yahoo chart v8 : query1 -> query2 이중화 (러너에서 429 가능)
  3) Nasdaq API     : 미국 종목 한정

캐시(data/prices.json)는 레포에 커밋된다. 모든 소스가 실패해도
캐시가 있으면 시스템은 DEGRADED 상태로 계속 계산한다.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import date, datetime, timedelta

from .http_client import FetchError, fetch, fetch_json, looks_like_challenge

Series = dict[str, float]  # {"YYYY-MM-DD": close}


# --------------------------------------------------------------------------- #
# 심볼 변환
# --------------------------------------------------------------------------- #
def to_stooq(symbol: str) -> str:
    if symbol.endswith(".KS"):
        return symbol[:-3].lower() + ".kr"
    if symbol.startswith("^"):
        return "^" + symbol[1:].lower()
    return symbol.lower() + ".us"


def is_us(symbol: str) -> bool:
    return "." not in symbol and not symbol.startswith("^")


# --------------------------------------------------------------------------- #
# 개별 소스
# --------------------------------------------------------------------------- #
def _from_stooq(symbol: str) -> Series:
    raw = fetch(f"https://stooq.com/q/d/l/?s={to_stooq(symbol)}&i=d")
    if looks_like_challenge(raw):
        raise FetchError("stooq challenge page")
    out: Series = {}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
    for row in reader:
        try:
            out[row["Date"]] = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
    if len(out) < 30:
        raise FetchError("stooq returned too few rows")
    return out


def _from_yahoo(symbol: str) -> Series:
    last: Exception | None = None
    for host in ("query1", "query2"):
        url = (
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=8y&interval=1d"
        )
        try:
            payload = fetch_json(url)
            result = payload["chart"]["result"][0]
            stamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            out: Series = {}
            for ts, close in zip(stamps, closes):
                if close is None:
                    continue
                out[datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")] = float(close)
            if len(out) < 30:
                raise FetchError("yahoo returned too few rows")
            return out
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise FetchError(f"yahoo failed: {last}")


def _from_nasdaq(symbol: str) -> Series:
    if not is_us(symbol):
        raise FetchError("nasdaq: US symbols only")
    today = date.today()
    start = today - timedelta(days=760)
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/historical"
        f"?assetclass=stocks&fromdate={start:%Y-%m-%d}&todate={today:%Y-%m-%d}&limit=600"
    )
    payload = fetch_json(url, headers={"Accept": "application/json"})
    rows = (payload.get("data") or {}).get("tradesTable", {}).get("rows") or []
    out: Series = {}
    for row in rows:
        try:
            stamp = datetime.strptime(row["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            out[stamp] = float(str(row["close"]).replace("$", "").replace(",", ""))
        except (KeyError, ValueError):
            continue
    if len(out) < 30:
        raise FetchError("nasdaq returned too few rows")
    return out


SOURCES = (("stooq", _from_stooq), ("yahoo", _from_yahoo), ("nasdaq", _from_nasdaq))


# --------------------------------------------------------------------------- #
# 캐시 병합 수집
# --------------------------------------------------------------------------- #
def load_cache(path: str) -> dict:
    if not os.path.exists(path):
        return {"series": {}, "updated": None}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data.setdefault("series", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"series": {}, "updated": None}


def save_cache(path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def trim(series: Series, keep: int) -> Series:
    keys = sorted(series)[-keep:]
    return {k: series[k] for k in keys}


def collect(symbols: list[str], cache_path: str, keep_days: int = 420) -> tuple[dict[str, Series], dict]:
    """심볼별 종가 시계열을 수집하고 캐시와 병합한다.

    반환: (series_map, status)
    status.mode = OK | DEGRADED | CACHE_ONLY
    """
    cache = load_cache(cache_path)
    cached: dict[str, Series] = cache.get("series", {})
    out: dict[str, Series] = {}
    fresh, stale, failed = [], [], []
    source_hits: dict[str, int] = {}

    for symbol in symbols:
        merged: Series = dict(cached.get(symbol, {}))
        got = False
        for name, fetcher in SOURCES:
            try:
                merged.update(fetcher(symbol))
                source_hits[name] = source_hits.get(name, 0) + 1
                got = True
                break
            except Exception:  # noqa: BLE001, PERF203
                continue
        if merged:
            out[symbol] = trim(merged, keep_days)
            (fresh if got else stale).append(symbol)
        else:
            failed.append(symbol)

    # 시계열 내용이 동일하면 updated 타임스탬프도 갱신하지 않는다.
    # 그래야 워크플로우의 "변경 없음 → 커밋 생략" 가드가 실제로 발동한다.
    if cache.get("series") != out:
        cache["series"] = out
        cache["updated"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        save_cache(cache_path, cache)

    if not fresh:
        mode = "CACHE_ONLY" if out else "FAILED"
    elif stale or failed:
        mode = "DEGRADED"
    else:
        mode = "OK"

    status = {
        "mode": mode,
        "fresh": len(fresh),
        "from_cache": len(stale),
        "failed": failed,
        "sources": source_hits,
    }
    return out, status
