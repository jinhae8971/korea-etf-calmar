#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chainvol.py — 로빈후드 체인 **전체** DEX 거래량 추이 (DefiLlama)

왜 별도 소스인가
  GeckoTerminal에서 우리가 세는 값은 "거래대금 상위 200풀의 합"이다.
  체인 전체가 아니다. 실측 대조: 같은 시점에 우리 집계 $744.7M vs 체인 전체 $1,033.9M.
  체인 전체 추이를 보려면 전 프로토콜을 커버하는 집계원이 필요하다.

DefiLlama `/overview/dexs/Robinhood Chain`
  · totalDataChart = 일별 거래량 시계열 (메인넷 2026-07-01부터 65일치 확보 확인)
  · total24h / total7d / total30d / change_1d
  · protocols = 프로토콜별 24h 내역 (Uniswap V3 $322M, V2 $65M 등)

이 값들은 재조회 가능하므로 캐시는 표시 속도용이며, 실패 시 직전 캐시로 폴백한다.
"""

import json
import os
import time
import urllib.request

LLAMA = "https://api.llama.fi/overview/dexs/Robinhood%20Chain?excludeTotalDataChartBreakdown=true"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _fnum(v, default=0.0):
    try:
        f = float(v)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def fetch(timeout=35, tries=3, log=print):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(LLAMA, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            log("[chainvol] 재시도 %d/%d: %s" % (i + 1, tries, exc))
            time.sleep([5, 12, 25][min(i, 2)])
    raise RuntimeError("DefiLlama 조회 실패: %s" % last)


def summarize(payload, days=45, top_protocols=6):
    chart = payload.get("totalDataChart") or []
    series = []
    for row in chart:
        if not row or len(row) < 2:
            continue
        series.append({"ts": int(row[0]), "v": _fnum(row[1])})
    series.sort(key=lambda x: x["ts"])
    recent = series[-days:]

    protos = []
    for p in (payload.get("protocols") or []):
        v = _fnum(p.get("total24h"))
        if v > 0:
            protos.append({"name": p.get("name") or "?", "v24": v})
    protos.sort(key=lambda p: -p["v24"])

    total24 = _fnum(payload.get("total24h"))
    total7d = _fnum(payload.get("total7d"))
    prev24 = _fnum(payload.get("total48hto24h"))
    avg7 = (total7d / 7.0) if total7d else 0.0

    return {
        "updated_at": time.time(),
        "total24h": total24,
        "prev24h": prev24,
        "change_1d_pct": round(payload.get("change_1d"), 2) if payload.get("change_1d") is not None
                         else (round((total24 - prev24) / prev24 * 100.0, 2) if prev24 else None),
        "total7d": total7d,
        "avg7d": avg7,
        "vs_avg7d_pct": round((total24 - avg7) / avg7 * 100.0, 1) if avg7 else None,
        "total30d": _fnum(payload.get("total30d")),
        "days": len(series),
        "series": recent,
        "peak": max((s["v"] for s in series), default=0.0),
        "peak_ts": max(series, key=lambda s: s["v"])["ts"] if series else None,
        "protocols": protos[:top_protocols],
        "protocol_total": sum(p["v24"] for p in protos),
    }


def load_or_fetch(cache_path, max_age_hours=3, log=print):
    """캐시가 신선하면 재사용, 아니면 갱신. 실패 시 캐시 폴백(없으면 None)."""
    cached = None
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        pass
    if cached and (time.time() - cached.get("updated_at", 0)) < max_age_hours * 3600:
        log("[chainvol] 캐시 사용 (일별 %d일)" % len(cached.get("series") or []))
        return cached
    try:
        summary = summarize(fetch(log=log))
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, separators=(",", ":"))
        log("[chainvol] 갱신 — 24h $%.1fM (전일 %s%%), 일별 %d일"
            % (summary["total24h"] / 1e6,
               summary.get("change_1d_pct"), len(summary["series"])))
        return summary
    except Exception as exc:
        log("[chainvol] 실패: %s — %s" % (exc, "캐시 폴백" if cached else "데이터 없음"))
        return cached
