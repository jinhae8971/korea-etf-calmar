#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crosscheck.py — DexScreener 교차검증 및 프로모션(부스트) 신호

왜 필요한가
  GeckoTerminal 단일 소스는 단일 장애점이다. 값이 조용히 틀려도 알 수 없다.
  DexScreener도 chainId "robinhood"를 지원하므로 상위 종목만 대조해
  **두 소스가 어긋나는지**를 감시한다(전 종목 대조는 불필요하고 비싸다).

실측 제약
  · 다중 주소 배치(/tokens/v1, /latest/dex/tokens)는 이 체인에서 500/522로 불안정 → 단건만 사용
  · /token-pairs/v1/{chain}/{addr}는 안정적으로 동작
  · 부스트(token-boosts)는 **유료 프로모션**이다. 매수 신호가 아니라 "홍보 집행 중" 주의 표시로만 쓴다.
"""

import json
import time
import urllib.request

DS = "https://api.dexscreener.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _get(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def token_metrics(address, timeout=12):
    """단일 토큰의 DexScreener 집계값(시총·유동성·거래대금)."""
    pairs = _get("%s/token-pairs/v1/robinhood/%s" % (DS, address), timeout) or []
    mine = [p for p in pairs
            if p.get("chainId") == "robinhood"
            and (p.get("baseToken") or {}).get("address", "").lower() == address.lower()]
    if not mine:
        return None
    liq = sum(_fnum((p.get("liquidity") or {}).get("usd")) for p in mine)
    vol = sum(_fnum((p.get("volume") or {}).get("h24")) for p in mine)
    mcaps = [_fnum(p.get("marketCap")) for p in mine if _fnum(p.get("marketCap")) > 0]
    return {
        "mcap": max(mcaps) if mcaps else None,
        "liq": liq,
        "v24": vol,
        "pairs": len(mine),
    }


def compare(rows, top_n=8, tolerance_pct=8.0, sleep_sec=1.0, log=print,
            deadline_sec=75.0, max_consecutive_fail=3):
    """
    상위 top_n 종목의 시총을 두 소스로 대조한다.

    보조 검증이 본체를 붙잡으면 안 되므로 **전체 시간 상한**과 **연속 실패 중단**을 둔다.
    DexScreener는 이 체인에서 응답이 수십 초씩 늘어지는 구간이 실측됐다.
    """
    out, diffs, checked, missing = {}, [], 0, 0
    started, consecutive = time.time(), 0
    for row in rows[:top_n]:
        if time.time() - started > deadline_sec:
            log("[crosscheck] 시간 상한 도달 — 중단")
            break
        if consecutive >= max_consecutive_fail:
            log("[crosscheck] 연속 %d회 실패 — 중단" % consecutive)
            break
        try:
            ds = token_metrics(row["address"])
            consecutive = 0
        except Exception as exc:
            consecutive += 1
            missing += 1
            log("[crosscheck] %s 실패: %s" % (row["symbol"], exc))
            time.sleep(sleep_sec)
            continue
        time.sleep(sleep_sec)
        if not ds or not ds.get("mcap"):
            missing += 1
            continue
        gt = row.get("mcap") or 0.0
        if gt <= 0:
            continue
        gap = (ds["mcap"] - gt) / gt * 100.0
        checked += 1
        diffs.append(abs(gap))
        out[row["address"]] = {"ds_mcap": ds["mcap"], "gap_pct": round(gap, 2),
                               "ds_liq": ds["liq"], "ds_v24": ds["v24"]}
        if abs(gap) >= tolerance_pct:
            log("[crosscheck] %s 괴리 %+.1f%% (GT $%.0f vs DS $%.0f)"
                % (row["symbol"], gap, gt, ds["mcap"]))

    worst = max(diffs) if diffs else 0.0
    median = sorted(diffs)[len(diffs) // 2] if diffs else 0.0
    summary = {
        "checked": checked, "missing": missing,
        "median_gap_pct": round(median, 2), "worst_gap_pct": round(worst, 2),
        "status": "OK" if checked >= 3 and worst < tolerance_pct else
                  ("DIVERGENT" if checked >= 3 else "INSUFFICIENT"),
    }
    log("[crosscheck] %d종 대조 · 중앙 괴리 %.2f%% · 최대 %.2f%% · 판정 %s"
        % (checked, median, worst, summary["status"]))
    return out, summary


def boosted_addresses(log=print, deadline_sec=30.0):
    """
    DexScreener 부스트·프로필 등재 토큰 주소 집합.
    프로모션 집행 신호이며 품질 보증이 아니다. 보조 신호이므로 시간 상한을 둔다.
    """
    found = {}
    started = time.time()
    for path, kind in (("/token-boosts/top/v1", "boost_top"),
                       ("/token-boosts/latest/v1", "boost_new"),
                       ("/token-profiles/latest/v1", "profile")):
        if time.time() - started > deadline_sec:
            log("[boost] 시간 상한 도달 — 중단")
            break
        try:
            for item in _get(DS + path) or []:
                if item.get("chainId") != "robinhood":
                    continue
                addr = (item.get("tokenAddress") or "").lower()
                if addr:
                    found.setdefault(addr, set()).add(kind)
        except Exception as exc:
            log("[boost] %s 실패: %s" % (path, exc))
        time.sleep(0.6)
    log("[boost] 로빈후드 체인 프로모션 등재 %d종" % len(found))
    return {a: sorted(k) for a, k in found.items()}
