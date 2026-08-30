#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
security.py — GoPlus 토큰 보안 검증 (Robinhood Chain, chain_id 4663)

설계 메모
  · GoPlus 무료 엔드포인트는 contract_addresses에 여러 주소를 넣어도 **1건만 반환**한다(실측).
    따라서 단건 조회 + 캐시 + 로테이션으로 예산을 관리한다.
  · 실행당 refresh_per_run개만 갱신하므로 상위권 캐시는 2~3회 실행이면 채워진다.
  · **미색인(unverified)도 정보다.** 신생·소형 토큰일수록 색인이 안 되며, 그 자체가 위험 신호다.
    "안전"으로 해석하지 않는다.
  · LP 락 비율은 이 체인에서 전 종목 0%로 관측된다 — 락커 컨트랙트를 GoPlus가 인식하지 못할
    가능성이 있으므로 "락 없음"이 아니라 **"확인 불가"**로 표기한다. 단정 금지.
"""

import json
import os
import time
import urllib.request

GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/4663"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"


def _fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_one(address, timeout=25):
    url = "%s?contract_addresses=%s" % (GOPLUS, address.lower())
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 1:
        raise RuntimeError("GoPlus code=%s msg=%s" % (payload.get("code"), payload.get("message")))
    return (payload.get("result") or {}).get(address.lower())


def summarize(raw):
    """GoPlus 원본 → 판정에 쓰는 최소 필드로 축약."""
    if not raw:
        return {"indexed": False}
    holders = raw.get("holders") or []
    lp = raw.get("lp_holders") or []
    top10 = sum(_fnum(h.get("percent")) for h in holders[:10]) * 100.0
    lp_locked = sum(_fnum(h.get("percent")) for h in lp if str(h.get("is_locked")) == "1") * 100.0
    owner = (raw.get("owner_address") or "").strip()
    zero = owner.lower() in ("", "0x0000000000000000000000000000000000000000")
    return {
        "indexed": True,
        "honeypot": raw.get("is_honeypot"),          # "0"/"1"/None(미판정)
        "buy_tax": _fnum(raw.get("buy_tax"), None) if raw.get("buy_tax") not in (None, "") else None,
        "sell_tax": _fnum(raw.get("sell_tax"), None) if raw.get("sell_tax") not in (None, "") else None,
        "mintable": raw.get("is_mintable"),
        "pausable": raw.get("transfer_pausable"),
        "cannot_sell_all": raw.get("cannot_sell_all"),
        "owner_renounced": zero,
        "owner": owner[:10] if owner else None,
        "open_source": raw.get("is_open_source"),
        "proxy": raw.get("is_proxy"),
        "holder_count": int(_fnum(raw.get("holder_count"))) or None,
        "top10_pct": round(top10, 2) if holders else None,
        "lp_locked_pct": round(lp_locked, 2) if lp else None,
        "lp_holder_count": int(_fnum(raw.get("lp_holder_count"))) or None,
    }


def load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def refresh(addresses, cache_path, refresh_per_run=12, ttl_hours=18, sleep_sec=1.6, log=print):
    """
    캐시를 읽어 만료·미조회 주소를 우선순위 순으로 refresh_per_run개만 갱신한다.
    addresses는 시총 순위 순으로 전달할 것 — 상위 종목이 먼저 채워진다.
    """
    cache = load_cache(cache_path)
    now = time.time()
    stale = []
    for addr in addresses:
        entry = cache.get(addr.lower())
        if entry is None or (now - entry.get("fetched_at", 0)) > ttl_hours * 3600:
            stale.append(addr.lower())

    done, failed = 0, 0
    for addr in stale[:refresh_per_run]:
        try:
            summary = summarize(fetch_one(addr))
            summary["fetched_at"] = now
            cache[addr] = summary
            done += 1
        except Exception as exc:
            failed += 1
            log("[security] %s 조회 실패: %s" % (addr[:10], exc))
        time.sleep(sleep_sec)

    log("[security] 갱신 %d건 / 실패 %d건 / 대기 %d건 / 캐시 %d건"
        % (done, failed, max(0, len(stale) - refresh_per_run), len(cache)))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"))
    return cache


def flags_for(summary, cfg):
    """보안 요약 → 리스크 플래그 목록. 확실한 것만 단정한다."""
    out = []
    if not summary:
        return out
    if not summary.get("indexed"):
        out.append({"code": "UNVERIFIED", "detail": "보안 스캐너 미색인 — 검증 불가(신생·소형일수록 흔함)"})
        return out
    if str(summary.get("honeypot")) == "1":
        out.append({"code": "HONEYPOT", "detail": "허니팟 판정 — 매도 불가 가능성"})
    elif summary.get("honeypot") is None:
        out.append({"code": "HP_UNKNOWN", "detail": "허니팟 판정 불가"})
    if str(summary.get("cannot_sell_all")) == "1":
        out.append({"code": "CANNOT_SELL_ALL", "detail": "전량 매도 제한 코드 감지"})
    for key, code, word in (("buy_tax", "BUY_TAX", "매수세"), ("sell_tax", "SELL_TAX", "매도세")):
        tax = summary.get(key)
        if tax is not None and tax * 100 >= cfg["tax_alert_pct"]:
            out.append({"code": code, "detail": "%s %.0f%%" % (word, tax * 100)})
    if str(summary.get("mintable")) == "1":
        out.append({"code": "MINTABLE", "detail": "추가 발행 가능"})
    if str(summary.get("pausable")) == "1":
        out.append({"code": "PAUSABLE", "detail": "전송 정지 권한 존재"})
    if summary.get("owner_renounced") is False:
        out.append({"code": "OWNER_ACTIVE", "detail": "오너 권한 미소각 (%s…)" % (summary.get("owner") or "?")})
    if str(summary.get("open_source")) == "0":
        out.append({"code": "CLOSED_SOURCE", "detail": "컨트랙트 미검증(소스 비공개)"})
    top10 = summary.get("top10_pct")
    if top10 is not None and top10 >= cfg["holder_concentration_pct"]:
        out.append({"code": "CONCENTRATED", "detail": "상위10 홀더 %.0f%%" % top10})
    return out
