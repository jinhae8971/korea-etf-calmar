# -*- coding: utf-8 -*-
"""
CHAINCTX — 종목 특성별 감시 규칙이 필요로 하는 체인 맥락 관측치.

종목 하나만 들여다보면 알 수 없는 것들이 있다.
  · 런치패드는 **점유율과 발행 속도**가 매출보다 먼저 움직인다.
  · 밈은 **체인 전체 관심 중 내 몫**이 시총보다 먼저 움직인다.
  · 소형 프로토콜은 **동종 배수 분포** 없이는 10배가 비싼지 알 수 없다.

호출 3회(DefiLlama 2 + GeckoTerminal 2페이지)로 전부 얻는다. 실패해도
개별 항목만 None 이 되고 감시 자체는 계속된다 — 맥락은 보조지 본체가 아니다.
"""

import difflib
import json
import re
import time
import urllib.request
from datetime import datetime, timezone

UA = {"User-Agent": "hood-radar-chainctx/2.3"}
GT = "https://api.geckoterminal.com/api/v2"
LLAMA = "https://api.llama.fi"


def _fnum(v, default=0.0):
    try:
        f = float(v)
        return default if f != f or f in (float("inf"), float("-inf")) else f
    except (TypeError, ValueError):
        return default


def _get(url, timeout=40, tries=2, log=print):
    for i in range(tries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as exc:                       # noqa: BLE001
            if i == tries - 1:
                log("[chainctx] %s 실패: %s" % (url.split("?")[0].rsplit("/", 1)[-1], exc))
            else:
                time.sleep(3)
    return None


def _norm(sym):
    return re.sub(r"[^A-Z0-9]", "", (sym or "").upper())


# ------------------------------------------------------------------ 수집
def launchpad_landscape(network="robinhood", log=print):
    """
    런치패드 카테고리 내 점유율(24h 수수료 기준) + 배수 산정을 위한 매출 분포.
    본 브리프와 같은 규칙(카테고리 내 상대 점유율)을 시간별 실행에서도 쓰기 위함이다.
    """
    ov = _get("%s/overview/fees/%s?excludeTotalDataChart=true"
              "&excludeTotalDataChartBreakdown=true&dataType=dailyRevenue"
              % (LLAMA, network), log=log)
    if not ov:
        return None
    protos = ov.get("protocols") or []
    lp = [p for p in protos if (p.get("category") or "") == "Launchpad"]
    tot = sum(_fnum(p.get("total24h")) for p in lp)
    shares = {}
    if tot > 0:
        for p in lp:
            shares[p.get("slug") or p.get("name")] = round(
                _fnum(p.get("total24h")) / tot * 100.0, 1)
    ranked = sorted(shares.items(), key=lambda kv: -kv[1])
    return {
        "shares": shares,
        "leader": ranked[0][0] if ranked else None,
        "runner_up": ranked[1][0] if len(ranked) > 1 else None,
        "runner_up_share": ranked[1][1] if len(ranked) > 1 else None,
        "category_fee24": round(tot, 2),
        "chain_fee24": _fnum(ov.get("total24h")),
        "n": len(lp),
    }


def dex_volume(network="robinhood", log=print):
    """체인 전체 DEX 거래대금 — 밈의 '관심 점유율' 분모."""
    ov = _get("%s/overview/dexs/%s?excludeTotalDataChart=true"
              "&excludeTotalDataChartBreakdown=true" % (LLAMA, network), log=log)
    if not ov:
        return None
    return {"total24h": _fnum(ov.get("total24h")), "total7d": _fnum(ov.get("total7d"))}


def new_pool_pulse(network="robinhood", pages=2, watch_symbols=(), watch_addrs=(),
                   log=print):
    """
    신규 풀 발행 속도(분당) + 카피캣 후보.

    이 체인은 하루 만 개 넘는 풀이 생겨 24시간 카운트는 불가능하다. 대신 최근
    N개의 생성시각 폭으로 **순간 발행 속도**를 재는 편이 정확하고 싸다 —
    런치패드 매출의 선행지표는 총량이 아니라 속도다.
    """
    seen, newest, oldest = [], None, None
    for page in range(1, pages + 1):
        d = _get("%s/networks/%s/new_pools?page=%d" % (GT, network, page),
                 timeout=30, log=log)
        if not d:
            break
        for x in d.get("data", []):
            a = x.get("attributes") or {}
            seen.append(a)
        time.sleep(0.5)
    if len(seen) < 10:
        return None

    def _ts(a):
        try:
            return datetime.strptime(a.get("pool_created_at", ""),
                                     "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    stamps = [t for t in (_ts(a) for a in seen) if t]
    rate = None
    if len(stamps) >= 10:
        newest, oldest = max(stamps), min(stamps)
        span_min = (newest - oldest).total_seconds() / 60.0
        if span_min > 0.5:
            rate = round(len(stamps) / span_min, 1)

    # 카피캣 — 유사 심볼의 신규 풀. 이 체인에서 실제로 관측되는 위험이다.
    watch = {_norm(s) for s in watch_symbols if s}
    addrs = {a.lower() for a in watch_addrs if a}
    copycats = []
    for a in seen:
        name = a.get("name") or ""
        base = _norm(name.split("/")[0])
        if not base or len(base) < 4 or base in watch:
            continue
        for w in watch:
            if len(w) < 4:
                continue
            ratio = difflib.SequenceMatcher(None, base, w).ratio()
            # 짧은 심볼일수록 우연한 유사도가 쉽게 나온다(PORN vs PONS = 0.75).
            # 포함관계이거나 매우 높은 유사도일 때만 카피캣으로 본다.
            contains = (w in base or base in w) and abs(len(base) - len(w)) <= 3
            if not (contains or ratio >= 0.85):
                continue
            if True:
                copycats.append({"target": w, "name": name, "ratio": round(ratio, 2),
                                 "created": a.get("pool_created_at")})
                break
    # 같은 이름의 신규 LP는 중복 보고하지 않는다
    dedup, keys = [], set()
    for c in copycats:
        k = (c["target"], _norm(c["name"].split("/")[0]))
        if k not in keys:
            keys.add(k)
            dedup.append(c)
    return {"rate_per_min": rate, "sampled": len(seen),
            "window_min": round((newest - oldest).total_seconds() / 60.0, 1)
            if newest and oldest else None,
            "copycats": dedup[:6], "addr_filter": len(addrs)}


def collect(cfg, symbols=(), addresses=(), log=print):
    """세 관측치를 한 번에. 개별 실패는 None 으로 남기고 진행한다."""
    net = cfg.get("network", "robinhood")
    started = time.time()
    ctx = {
        "launchpad": launchpad_landscape(net, log=log),
        "dex": dex_volume(net, log=log),
        "pulse": new_pool_pulse(net, watch_symbols=symbols, watch_addrs=addresses, log=log),
    }
    ctx["elapsed_sec"] = round(time.time() - started, 1)
    return ctx
