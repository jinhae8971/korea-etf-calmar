#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill.py — OHLCV로 과거 시총·순위를 소급 재구성

문제
  자체 스냅샷만 쓰면 24시간 순위 변동은 하루가 지나야 나온다. 가동 첫날은 눈이 없다.

해법
  GeckoTerminal 시간봉 OHLCV(풀 단위)를 받아 **가격 × 공급량**으로 시총 시계열을 만들고,
  같은 시각대의 횡단면을 세워 과거 순위를 소급 계산한다.

한계 (반드시 명시할 것)
  · 공급량은 현재값을 과거에 그대로 적용한다. 추가발행·소각이 있었다면 과거 시총이 왜곡된다.
    → mintable 토큰은 backfill 신뢰도가 낮다.
  · 가격은 그 토큰의 최대 유동성 풀 하나만 쓴다(멀티풀 가중 아님).
  · 그 시점에 아직 존재하지 않던 토큰은 자연히 빠지므로, 소급 순위의 모집단은
    현재 유니버스로 한정된다 — 당시 실제 순위와 완전히 같지 않다.
  이 스냅샷들은 source="ohlcv_backfill"로 표시해 실측 스냅샷과 구분한다.

운영 메모
  백필은 보조 기능이다. 재시도를 짧게 잡고 전체 시간 상한을 둬서
  본 파이프라인(수집·판정·발송)을 절대 붙잡지 않게 한다.
  실측: 429 백오프가 길면 30종 수집이 30분을 넘겨 러너 타임아웃을 유발했다.
"""

import json
import random
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.geckoterminal.com/api/v2"
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
RETRY_WAIT = [6, 15, 30]


def _get(url, timeout=25, tries=3, log=print):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json;version=20230302"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            wait = RETRY_WAIT[min(i, len(RETRY_WAIT) - 1)] + random.uniform(0, 2)
            log("[backfill] 재시도 %d/%d (%s) — %.0fs 대기" % (i + 1, tries, exc, wait))
            time.sleep(wait)
    raise RuntimeError("OHLCV fetch 실패: %s (%s)" % (url, last))


def fetch_ohlcv(pool_address, hours=96, timeout=25, log=print):
    """시간봉 종가 시계열 → [(unix_ts, close), ...] 오래된 것부터."""
    url = "%s/networks/robinhood/pools/%s/ohlcv/hour?aggregate=1&limit=%d" % (
        API, pool_address, min(hours, 1000))
    payload = _get(url, timeout, log=log)
    rows = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    out = [(int(r[0]), float(r[4])) for r in rows if r and r[4] is not None]
    out.sort(key=lambda x: x[0])
    return out


def build_snapshots(rows, hours_back=72, step_hours=6, sleep_sec=4.0, max_tokens=24,
                    log=print, deadline_sec=480.0):
    """
    rows: 현재 유니버스(주소·심볼·시총·가격·최대유동성 풀 주소 포함)
    반환: 과거 시각별 합성 스냅샷 리스트(오래된 것부터)
    """
    series = {}
    started = time.time()
    for row in rows[:max_tokens]:
        if time.time() - started > deadline_sec:
            log("[backfill] 시간 상한 %.0fs 도달 — %d종까지만 수집" % (deadline_sec, len(series)))
            break
        pool = row.get("top_pool_address")
        price_now = row.get("price") or 0.0
        mcap_now = row.get("mcap") or 0.0
        if not pool or price_now <= 0 or mcap_now <= 0:
            continue
        supply_equiv = mcap_now / price_now  # 유통(또는 총) 수량 등가 — 현재값 고정
        try:
            candles = fetch_ohlcv(pool, hours=hours_back + 6, log=log)
        except Exception as exc:
            log("[backfill] %s OHLCV 실패: %s" % (row["symbol"], exc))
            time.sleep(sleep_sec)
            continue
        if len(candles) < 4:
            log("[backfill] %s 캔들 부족(%d) — 신생 토큰" % (row["symbol"], len(candles)))
            time.sleep(sleep_sec)
            continue
        series[row["address"]] = {
            "symbol": row["symbol"],
            "supply": supply_equiv,
            "candles": candles,
        }
        log("[backfill] %-12s 캔들 %d개" % (row["symbol"], len(candles)))
        time.sleep(sleep_sec)

    if not series:
        return []

    now = datetime.now(KST)
    snapshots = []
    for hours_ago in range(hours_back, 0, -step_hours):
        target = now - timedelta(hours=hours_ago)
        target_ts = int(target.timestamp())
        priced = {}
        for addr, s in series.items():
            best = None
            for ts, close in s["candles"]:
                if ts <= target_ts and (best is None or ts > best[0]):
                    best = (ts, close)
            # 6시간 이상 벌어진 캔들은 신뢰하지 않는다(거래 공백)
            if best and (target_ts - best[0]) <= 6 * 3600 and best[1] > 0:
                priced[addr] = best[1] * s["supply"]
        if len(priced) < 5:
            continue
        ordered = sorted(priced.items(), key=lambda kv: -kv[1])
        snapshots.append({
            "ts": target.isoformat(),
            "source": "ohlcv_backfill",
            "rank": {addr: i + 1 for i, (addr, _) in enumerate(ordered)},
            "mcap": {addr: round(mc, 2) for addr, mc in ordered},
            "liq": {},
            "symbol": {addr: series[addr]["symbol"] for addr, _ in ordered},
        })
    log("[backfill] 합성 스냅샷 %d개 (토큰 %d종)" % (len(snapshots), len(series)))
    return snapshots


def merge(history, synthetic):
    """
    기존 이력과 합성 스냅샷 병합.
    실측 스냅샷이 우선 — 같은 시각대(±3시간)에 실측이 있으면 합성은 버린다.
    """
    def parse(ts):
        try:
            return datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None

    real = [(parse(s.get("ts")), s) for s in history if s.get("source") != "ohlcv_backfill"]
    real = [(t, s) for t, s in real if t]
    kept = []
    for snap in synthetic:
        t = parse(snap["ts"])
        if t is None:
            continue
        if any(abs((t - rt).total_seconds()) < 3 * 3600 for rt, _ in real):
            continue
        kept.append(snap)

    merged = [s for s in history] + kept
    merged.sort(key=lambda s: s.get("ts") or "")
    seen, out = {}, []
    for snap in merged:
        key = snap.get("ts")
        if key in seen:
            if seen[key].get("source") == "ohlcv_backfill" and snap.get("source") != "ohlcv_backfill":
                out[out.index(seen[key])] = snap
                seen[key] = snap
            continue
        seen[key] = snap
        out.append(snap)
    return out
