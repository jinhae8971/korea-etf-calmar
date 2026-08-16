"""워치리스트 스크리너 — 미국 상장 종목·ETF 를 흐름 적합도로 기계 순위화.

이 모듈은 추천하지 않는다. 사전에 정한 가중치로 점수를 계산해 줄을 세울 뿐이다.
가중치와 지표는 config/watchlist.yaml 에 사전 등록되어 있고, 결과를 보고
고치지 않는다는 것이 전제다.

종목 축 (5개):
  순도   EDGAR 자사 공시에서 로봇/피지컬AI 용어 언급 밀도 — 기계 산출
  실현   매출 YoY 지속성 (최근 4분기 중 25% 이상 통과 횟수)
  독립성 기성 자동화 바스켓과의 상관이 낮을수록 높음 (테마 고유 노출)
  생존력 영업이익률 — ① 단계 진입은 -48% 시나리오를 견뎌야 하므로 필요
  유동성 일평균 거래대금

ETF 축:
  ETF 는 보유종목을 무료로 얻을 수 없다. 대신 수익률 상관으로 노출을 역산한다.
  순수노출 = corr(ETF, 순수 피지컬AI 바스켓) - corr(ETF, 기성 자동화 바스켓)
"""

from __future__ import annotations

import json
import math
import os
import statistics
import time
import urllib.parse

from .http_client import fetch_json


# --------------------------------------------------------------------------- #
# 순도 — EDGAR 기업별 언급 밀도
# --------------------------------------------------------------------------- #
def _edgar(term: str, cik: str | None, start: str, end: str) -> int | None:
    url = ("https://efts.sec.gov/LATEST/search-index"
           f"?q=%22{urllib.parse.quote(term)}%22&startdt={start}&enddt={end}")
    if cik:
        url += f"&ciks={cik}"
    try:
        return fetch_json(url, headers={"Accept": "application/json"})["hits"]["total"]["value"]
    except Exception:  # noqa: BLE001
        return None


def purity(ciks: dict[str, str], terms: list[str], start: str, end: str,
           cache_path: str) -> tuple[dict[str, float], dict]:
    """종목별 테마 용어 언급 건수. 절대 건수는 기업 규모에 좌우되므로
    '자사 공시 중 테마 언급 비율'로 정규화한다."""
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (json.JSONDecodeError, OSError):
            cache = {}
    key = f"{start}:{end}"
    bucket = cache.setdefault(key, {})
    fetched = failed = 0

    for ticker, cik in ciks.items():
        if ticker in bucket:
            continue
        theme = 0
        ok = False
        for term in terms:
            n = _edgar(term, cik, start, end)
            time.sleep(0.3)
            if n is not None:
                theme += n
                ok = True
        total = _edgar("the", cik, start, end)  # 사실상 전체 공시 건수 대용
        time.sleep(0.3)
        if ok and total:
            bucket[ticker] = {"theme": theme, "total": total,
                              "density": round(theme / total, 4)}
            fetched += 1
        else:
            failed += 1

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    tmp = f"{cache_path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, cache_path)

    covered = sum(1 for t in ciks if t in bucket)
    if covered == len(ciks):
        mode = "OK"                      # 전부 캐시 적중도 정상이다
    elif covered:
        mode = "DEGRADED"
    else:
        mode = "FAILED"
    return ({t: v["density"] for t, v in bucket.items()},
            {"mode": mode, "covered": covered, "universe": len(ciks),
             "fetched": fetched, "failed": failed})


# --------------------------------------------------------------------------- #
# 가격 파생 지표
# --------------------------------------------------------------------------- #
def to_quarterly(daily: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for day, close in sorted(daily.items()):
        y, m = int(day[:4]), int(day[5:7])
        out[f"{y}Q{(m - 1) // 3 + 1}"] = close
    return out


def returns(series: dict[str, float], keys: list[str]) -> list[float]:
    out = []
    for a, b in zip(keys, keys[1:]):
        pa, pb = series.get(a), series.get(b)
        out.append(pb / pa - 1 if pa and pb and pa > 0 else 0.0)
    return out


def correlation(xs: list[float], ys: list[float]) -> float | None:
    n = min(len(xs), len(ys))
    if n < 8:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sx, sy = statistics.pstdev(xs), statistics.pstdev(ys)
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / (n * sx * sy)


def basket_returns(series_map: dict, tickers: list[str], keys: list[str]) -> list[float]:
    legs = [returns(to_quarterly(series_map[t]), keys) for t in tickers if series_map.get(t)]
    if not legs:
        return []
    return [statistics.median([leg[i] for leg in legs]) for i in range(len(legs[0]))]


# --------------------------------------------------------------------------- #
# 점수화
# --------------------------------------------------------------------------- #
def rank_pct(values: dict[str, float], higher_is_better: bool = True) -> dict[str, float]:
    """백분위 순위 (0~1). 값이 좋을수록 1.0 에 가깝다.

    [2026-08-16 버그 수정] 최초 구현은 내림차순 정렬 후 i/(n-1) 을 매겨
    **가장 좋은 값에 0.0 을 주고 있었다.** 실제로 영업이익률 -54.8% 인 종목이
    생존력 백분위 0.864 를 받아 상위권에 올라왔다. 아래처럼 뒤집는다.
    """
    items = [(k, v) for k, v in values.items() if v is not None and not math.isnan(v)]
    if not items:
        return {}
    items.sort(key=lambda kv: kv[1], reverse=not higher_is_better)
    n = len(items)
    return {k: (i / (n - 1) if n > 1 else 0.5) for i, (k, _) in enumerate(items)}


def median_dollar_volume(ticker: str, days: int = 60) -> float | None:
    """유동성 — 가격이 아니라 실제 거래대금으로 잰다.
    (최초 구현은 종가를 유동성 대용으로 써서 저가주가 상위에 올랐다)"""
    for host in ("query1", "query2"):
        try:
            payload = fetch_json(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
                "?range=6mo&interval=1d")
            r = payload["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            pairs = [(c, v) for c, v in zip(q.get("close") or [], q.get("volume") or [])
                     if c and v]
            if len(pairs) >= 30:
                return statistics.median(c * v for c, v in pairs[-days:])
        except Exception:  # noqa: BLE001
            continue
    return None


def score(components: dict[str, dict[str, float]], weights: dict[str, float],
          universe: list[str] | None = None, min_coverage: float = 0.6) -> dict[str, dict]:
    """components[axis][ticker] = 백분위. 결측 축은 가중치를 재분배한다.

    universe 를 주면 그 종목만 채점한다 (ETF 가 종목 순위에 섞이는 것을 막는다).
    min_coverage 미만은 근거가 부족하므로 순위에서 제외한다.
    """
    tickers = set()
    for axis in components.values():
        tickers |= set(axis)
    if universe is not None:
        tickers &= set(universe)
    out = {}
    for t in tickers:
        total = wsum = 0.0
        detail = {}
        for axis, w in weights.items():
            v = components.get(axis, {}).get(t)
            if v is None:
                continue
            total += v * w
            wsum += w
            detail[axis] = round(v, 3)
        if wsum < min_coverage:
            continue
        out[t] = {"score": round(total / wsum * 100, 1), "coverage": round(wsum, 2),
                  "detail": detail}
    return out
