"""미국 시장 브레드스 근사 — 트레이딩뷰의 S5TH·S5FI·NYSE 등락(ZBT) 대신 대형주 유니버스로 계산한다.

  pct200 = 200일선 위 종목 비율(%)   ≈ S5TH
  pct50  =  50일선 위 종목 비율(%)   ≈ S5FI
  adv_ratio = 상승 종목 / (상승 + 하락) → 10일 EMA로 ZBT 판정 (대형주 근사라 원본 ZBT보다 자주 발생할 수 있음)

유니버스: us_close/engine/usclose/universe.py 의 LARGE(대형주 97종, 2026-11-24 기준)를 복사해 고정.
저장: data/breadth_tails.json(종목별 최근 종가 280개) · data/breadth_series.json(일자별 집계).
첫 실행은 6년치를 받아 전체 시계열을 만들고(부트스트랩), 이후에는 최근 45일만 받아 마지막 20일을 다시 계산한다.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from sources import HERE, log, nasdaq_hist, us_session_closed

UNIVERSE = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "AMAT", "AMD", "AMGN", "AMT", "AMZN", "ANET", "AVGO", "AXP",
    "BA", "BAC", "BKNG", "BLK", "BRK-B", "C", "CAT", "CEG", "CMCSA", "COP", "COST", "CRM", "CSCO", "CVX", "DE",
    "DHR", "DIS", "GE", "GEV", "GILD", "GOOGL", "GS", "HD", "HON", "HOOD", "IBM", "INTC", "INTU", "ISRG", "JNJ",
    "JPM", "KLAC", "KO", "LIN", "LLY", "LMT", "LOW", "LRCX", "MA", "MCD", "MDLZ", "META", "MRK", "MS", "MSFT",
    "MU", "NEE", "NFLX", "NKE", "NOW", "NVDA", "ORCL", "PANW", "PEP", "PFE", "PG", "PLD", "PLTR", "PM", "QCOM",
    "RTX", "SBUX", "SCHW", "SNPS", "SO", "SPCX", "SPGI", "T", "TJX", "TMO", "TMUS", "TSLA", "TXN", "UBER", "UNH",
    "UNP", "V", "VRTX", "VST", "VZ", "WFC", "WMT", "XOM",
]
NASDAQ_SYM = {"BRK-B": "BRK.B"}
TAILS_PATH = os.path.join(HERE, "data", "breadth_tails.json")
SERIES_PATH = os.path.join(HERE, "data", "breadth_series.json")
TAIL_KEEP = 280
RECOMPUTE_LAST = 20
MIN_COVERAGE = 0.6            # 200일선 계산 가능한 종목이 유니버스의 60% 미만인 날은 판정에 쓰지 않음


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)


def _fetch_closes(sym: str, days: int, now: datetime) -> list[list]:
    bars = nasdaq_hist(NASDAQ_SYM.get(sym, sym), "stocks", days)
    return [[b["d"], round(b["c"], 4)] for b in bars if us_session_closed(b["d"], now)]


def _fetch_all(days: int, now: datetime, budget_sec: int) -> tuple[dict, list]:
    t0, got, failed = time.time(), {}, []

    def job(sym):
        if time.time() - t0 > budget_sec:
            return sym, None, "budget"
        try:
            return sym, _fetch_closes(sym, days, now), None
        except Exception as e:  # noqa: BLE001
            return sym, None, str(e)[-60:]

    with ThreadPoolExecutor(max_workers=4) as ex:
        for sym, rows, err in ex.map(job, UNIVERSE):
            if rows:
                got[sym] = rows
            else:
                failed.append(f"{sym}:{err}")
    return got, failed


def compute(closes: dict, only_dates: set | None = None) -> dict:
    """{sym: [[d, c], ...]} → {d: [pct200, pct50, adv, dec, n200, n50]}"""
    agg: dict = {}
    for rows in closes.values():
        cs = [c for _, c in rows]
        s200 = s50 = 0.0
        for i, (d, c) in enumerate(rows):
            s200 += c
            s50 += c
            if i >= 200:
                s200 -= cs[i - 200]
            if i >= 50:
                s50 -= cs[i - 50]
            if only_dates is not None and d not in only_dates:
                continue
            a = agg.setdefault(d, [0, 0, 0, 0, 0, 0])      # a200, a50, adv, dec, n200, n50
            if i >= 199:
                a[4] += 1
                a[0] += c > s200 / 200
            if i >= 49:
                a[5] += 1
                a[1] += c > s50 / 50
            if i >= 1:
                a[2] += c > cs[i - 1]
                a[3] += c < cs[i - 1]
    out = {}
    for d, (a200, a50, adv, dec, n200, n50) in agg.items():
        out[d] = [round(a200 / n200 * 100, 2) if n200 else None, round(a50 / n50 * 100, 2) if n50 else None,
                  adv, dec, n200, n50]
    return out


def _pack_tails(tails: dict) -> dict:
    """{sym: [[d, c], ...]} → 날짜 공통 축 + 종목별 종가 배열 (용량 1/3)."""
    dates = sorted({d for rows in tails.values() for d, _ in rows})
    idx = {d: i for i, d in enumerate(dates)}
    closes = {}
    for s, rows in tails.items():
        arr = [None] * len(dates)
        for d, c in rows:
            arr[idx[d]] = c
        closes[s] = arr
    return {"dates": dates, "closes": closes}


def _unpack_tails(packed: dict) -> dict:
    dates = packed.get("dates") or []
    return {s: [[d, c] for d, c in zip(dates, arr) if c is not None] for s, arr in (packed.get("closes") or {}).items()}


def update(now: datetime, budget_sec: int = 600) -> dict:
    """브레드스 시계열 갱신. 반환: {"rows": [[d, pct200, pct50, adv, dec, n200, n50], ...], "failed": [...], "mode": ...}"""
    tails = _unpack_tails(_load(TAILS_PATH, {}))
    series = _load(SERIES_PATH, {}).get("rows", [])
    by_date = {r[0]: r[1:] for r in series}
    if len(series) < 300 or len(tails) < len(UNIVERSE) * MIN_COVERAGE:
        mode = "bootstrap"
        got, failed = _fetch_all(365 * 6, now, budget_sec)
        by_date.update(compute(got))
        tails = {s: rows[-TAIL_KEEP:] for s, rows in got.items()}
    else:
        mode = "incremental"
        got, failed = _fetch_all(45, now, budget_sec)
        for s, rows in got.items():
            old = {d: c for d, c in tails.get(s, [])}
            old.update({d: c for d, c in rows})
            tails[s] = [[d, old[d]] for d in sorted(old)][-TAIL_KEEP:]
        recent = sorted({d for rows in tails.values() for d, _ in rows[-RECOMPUTE_LAST:]})[-RECOMPUTE_LAST:]
        by_date.update(compute(tails, set(recent)))
    rows = [[d] + by_date[d] for d in sorted(by_date)]
    _save(TAILS_PATH, {"updated": now.isoformat(timespec="minutes"), "universe": len(UNIVERSE), **_pack_tails(tails)})
    _save(SERIES_PATH, {"updated": now.isoformat(timespec="minutes"), "universe": len(UNIVERSE), "rows": rows})
    log(f"[breadth] {mode}: 수신 {len(got)}/{len(UNIVERSE)} · 시계열 {len(rows)}일 · 마지막 {rows[-1][0] if rows else '-'}"
        + (f" · 실패 {len(failed)}" if failed else ""))
    return {"rows": rows, "failed": failed, "mode": mode, "received": len(got)}


def align(rows: list, dates: list) -> dict:
    """브레드스 일자 시계열을 자산 일봉 날짜에 맞춘다. 비는 날은 직전 값 유지(트레이딩뷰 gaps_off와 동일)."""
    need = int(len(UNIVERSE) * MIN_COVERAGE)
    by = {}
    for d, p200, p50, adv, dec, n200, n50 in rows:
        if n200 >= need and p200 is not None:
            by[d] = (p200, p50, adv / (adv + dec) if (adv + dec) else None)
    out = {"pct200": [], "pct50": [], "adv_ratio": []}
    last = (None, None, None)
    for d in dates:
        if d in by:
            last = by[d]
        out["pct200"].append(last[0])
        out["pct50"].append(last[1])
        out["adv_ratio"].append(last[2])
    out["last_date"] = max(by) if by else None
    return out
