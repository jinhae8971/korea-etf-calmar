# -*- coding: utf-8 -*-
"""
WATCHLIST — 보유 종목 정밀 감시 트랙 (HOOD RADAR v2.2)

순위 시스템은 "무엇이 뜨는가"를 본다. 보유 종목 감시는 다른 질문을 본다:
**내가 지금 나올 수 있는가, 그리고 근거가 무너지고 있는가.**

따라서 이 모듈의 1순위 지표는 시총이나 배수가 아니라 **유동성과 매출**이다.
가격은 마지막에 움직인다.

두 트랙을 가로지른다 —
  · 프로토콜 연동 종목(INDEX, PONS): DefiLlama 매출 + 배수 + 점유율
  · 밈 트랙 종목(CASHCAT): GeckoTerminal 시총·유동성·거래량
심볼이 아니라 **컨트랙트 주소로 고정(pin)** 한다. 이 체인은 카피캣이 실재하며
(CASHBIRD·CASH* 유사 심볼 관측), 심볼 매칭은 조용히 다른 토큰을 감시하게 만든다.

단독 실행(시간별 정밀 감시): python watchlist.py
  → 임계 위반이 있을 때만 data/watchlist_alert.json 의 send=true 로 발송 신호.
본 브리프(6시간) 안에서는 render_telegram() 으로 상시 섹션을 붙인다.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import chainctx

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
UA = {"User-Agent": "hood-radar-watchlist/2.2"}
GT = "https://api.geckoterminal.com/api/v2"
LLAMA = "https://api.llama.fi"


# ------------------------------------------------------------------ 공통
def _fnum(v, default=0.0):
    try:
        f = float(v)
        return default if f != f or f in (float("inf"), float("-inf")) else f
    except (TypeError, ValueError):
        return default


def _get(url, timeout=30, tries=3, sleep=(2, 6, 14), log=print):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except Exception as exc:                      # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(sleep[min(i, len(sleep) - 1)])
    log("[watchlist] GET 실패: %s (%s)" % (url.split("?")[0], last))
    return None


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def now_kst():
    return datetime.now(timezone(timedelta(hours=9)))


def entries(cfg):
    """설정된 보유 종목. 주소는 소문자로 정규화해 비교 실수를 없앤다."""
    out = []
    for e in cfg.get("watchlist") or []:
        addr = (e.get("address") or "").strip().lower()
        if not addr.startswith("0x"):
            continue
        out.append({
            "symbol": (e.get("symbol") or "?").upper(),
            "address": addr,
            "slugs": [s for s in (e.get("slugs") or []) if s],
            "track": e.get("track") or ("protocol" if e.get("slugs") else "meme"),
            "profile": e.get("profile") or ("meme" if not e.get("slugs") else "protocol"),
            "thresholds": e.get("thresholds") or {},
            "note": e.get("note") or "",
        })
    return out


# ------------------------------------------------------------------ 수집
def fetch_market(addresses, network="robinhood", log=print):
    """GeckoTerminal 다중 토큰 — 1회 호출로 가격·FDV·유동성·거래량."""
    if not addresses:
        return {}
    url = "%s/networks/%s/tokens/multi/%s" % (GT, network, ",".join(addresses))
    data = _get(url, timeout=30, log=log)
    out = {}
    for item in (data or {}).get("data", []):
        a = item.get("attributes") or {}
        addr = (a.get("address") or "").lower()
        if not addr:
            continue
        out[addr] = {
            "name": a.get("name"),
            "symbol": (a.get("symbol") or "").upper(),
            "price": _fnum(a.get("price_usd")),
            "fdv": _fnum(a.get("fdv_usd")),
            "mcap": _fnum(a.get("market_cap_usd")) or _fnum(a.get("fdv_usd")),
            "liq": _fnum(a.get("total_reserve_in_usd")),
            "vol24": _fnum((a.get("volume_usd") or {}).get("h24")),
        }
    return out


def _chart_tail(chart, days):
    """일별 시계열의 최근 N일 합계 — 마지막 점은 미완성일 수 있어 별도로 다룬다."""
    vals = [_fnum(p[1]) for p in (chart or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
    if not vals:
        return 0.0
    return sum(vals[-days:])


def fetch_protocol(slugs, log=print):
    """
    DefiLlama 매출/수수료 요약. 한 종목이 여러 프로토콜(Pons V1+V2)일 수 있으므로 합산한다.
    분모(FDV)는 시장에서, 분자(매출)는 여기서 — 출처를 섞지 않는다.
    """
    agg = {"rev24": 0.0, "rev7": 0.0, "rev30": 0.0, "fee24": 0.0, "fee7": 0.0,
           "fee30": 0.0, "parts": [], "ok": False}
    for slug in slugs:
        for kind, pre in (("dailyRevenue", "rev"), ("dailyFees", "fee")):
            s = _get("%s/summary/fees/%s?dataType=%s" % (LLAMA, slug, kind),
                     timeout=40, log=log)
            if not s:
                continue
            agg["ok"] = True
            agg[pre + "24"] += _fnum(s.get("total24h"))
            agg[pre + "30"] += _fnum(s.get("total30d"))
            chart = s.get("totalDataChart")
            seven = _fnum(s.get("total7d")) or _chart_tail(chart, 7)
            agg[pre + "7"] += seven
            if pre == "rev":
                agg["parts"].append({"slug": slug, "rev24": _fnum(s.get("total24h")),
                                     "rev30": _fnum(s.get("total30d"))})
            time.sleep(0.4)
    return agg


def _shares_from_payload(protocol_payload, slugs):
    """본 브리프 실행에서 이미 계산된 런치패드 점유율을 재사용한다(재호출 금지)."""
    if not protocol_payload:
        return None
    lp = (protocol_payload.get("shares") or {}).get("launchpad_24h") or {}
    tot = 0.0
    hit = False
    for key, val in lp.items():
        if key in slugs:                       # 키는 DefiLlama 슬러그 — 접두어 매칭은 오탐을 만든다
            tot += _fnum(val)
            hit = True
    return round(tot, 1) if hit else None


def _from_meme_rows(rows, address):
    for i, r in enumerate(rows or []):
        if (r.get("address") or "").lower() == address:
            r = dict(r)
            r["_rank"] = i + 1
            return r
    return None


def _from_protocol_payload(payload, address):
    if not payload:
        return None
    for item in payload.get("native") or []:
        if (item.get("address") or "").lower() == address:
            return item
    return None


def thr(cfg, item, key, default):
    """종목별 임계 재정의를 허용한다 — 회전율 높은 런치패드 토큰과 밈에
    같은 유동성 비율을 요구하면 경보가 상시 켜져 무시당한다."""
    ov = (item.get("thresholds") or {}).get(key)
    return _fnum(ov, _fnum(cfg.get(key), default)) if ov is not None \
        else _fnum(cfg.get(key), default)


def apply_profile(item, ctx):
    """종목 특성별 관측치를 붙인다. 맥락이 없으면 조용히 건너뛴다."""
    if not ctx:
        return item
    lp = ctx.get("launchpad") or {}
    dex = ctx.get("dex") or {}
    pulse = ctx.get("pulse") or {}

    if item.get("profile") == "launchpad" and lp.get("shares"):
        tot = sum(_fnum(v) for k, v in lp["shares"].items() if k in (item.get("slugs") or []))
        item["lp_share"] = round(tot, 1)
        item["lp_leader"] = lp.get("leader")
        item["rival"] = lp.get("runner_up")
        item["rival_share"] = lp.get("runner_up_share")
        item["issuance_rate"] = pulse.get("rate_per_min")

    if item.get("profile") == "meme" and _fnum(dex.get("total24h")) > 0:
        item["attn_share_pct"] = round(
            _fnum(item.get("vol24")) / _fnum(dex["total24h"]) * 100.0, 3)

    hits = [c for c in (pulse.get("copycats") or [])
            if c.get("target") == _norm_sym(item["symbol"])]
    if hits:
        item["copycats"] = hits
    return item


def _norm_sym(s):
    import re as _re
    return _re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def peer_pf_median(protocol_payload):
    """동종 배수 분포 — 소형 프로토콜의 10배가 비싼 값인지 판단할 유일한 기준."""
    if not protocol_payload:
        return None
    vals = sorted(_fnum(i.get("pf")) for i in (protocol_payload.get("native") or [])
                  if _fnum(i.get("pf")) > 0)
    if len(vals) < 4:
        return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0, 2)


# ------------------------------------------------------------------ 상태 구성
def build(cfg, rows=None, protocol_payload=None, log=print):
    """
    보유 종목 현재 상태. rows/protocol_payload 가 주어지면(본 브리프 안) 재사용하고,
    없으면(시간별 단독 실행) 직접 최소한만 호출한다.
    """
    started = time.time()
    ents = entries(cfg)
    if not ents:
        return {"as_of_epoch": int(time.time()), "items": [], "unresolved": []}

    market = fetch_market([e["address"] for e in ents],
                          network=cfg.get("network", "robinhood"), log=log)
    try:
        ctx = chainctx.collect(cfg, symbols=[e["symbol"] for e in ents],
                               addresses=[e["address"] for e in ents], log=log)
    except Exception as exc:                       # noqa: BLE001
        log("[watchlist] 체인 맥락 수집 실패(기본 규칙만 적용): %s" % exc)
        ctx = None

    items, unresolved = [], []
    for e in ents:
        m = market.get(e["address"])
        row = _from_meme_rows(rows, e["address"])
        item = {
            "symbol": e["symbol"], "address": e["address"], "track": e["track"],
            "slugs": e["slugs"], "note": e["note"],
            "profile": e["profile"], "thresholds": e["thresholds"],
            "name": (m or {}).get("name"), "resolved": bool(m),
            "price": (m or {}).get("price"), "fdv": (m or {}).get("fdv"),
            "mcap": (m or {}).get("mcap"), "liq": (m or {}).get("liq"),
            "vol24": (m or {}).get("vol24"),
            "rank": (row or {}).get("_rank"),
            "flags": [f.get("code") for f in (row or {}).get("flags", []) if f.get("code")],
        }
        if not m:
            # 조용히 빠지는 것이 가장 위험하다 — 명시적으로 남긴다
            unresolved.append(e["symbol"])
            item["reason"] = "시장 데이터 응답 없음(상장 폐지·풀 소멸·소스 장애 구분 불가)"
            items.append(item)
            continue

        # 심볼 불일치 = 주소 고정이 없었다면 다른 토큰을 봤을 상황
        if m.get("symbol") and e["symbol"] not in (m["symbol"], (m.get("name") or "").upper()):
            item["symbol_note"] = "피드 심볼 %s" % m["symbol"]

        item["liq_mcap_pct"] = round(item["liq"] / item["mcap"] * 100.0, 2) \
            if item["mcap"] > 0 and item["liq"] else None
        item["turnover_pct"] = round(item["vol24"] / item["mcap"] * 100.0, 1) \
            if item["mcap"] > 0 and item["vol24"] else None

        if e["slugs"]:
            proto = _from_protocol_payload(protocol_payload, e["address"])
            if proto and proto.get("fee30"):     # 본 브리프가 이미 부른 값 — 재호출하지 않는다
                agg = {k: _fnum(proto.get(k)) for k in
                       ("rev24", "rev7", "rev30", "fee24", "fee7", "fee30")}
                agg.update({"ok": True, "parts": []})
            else:
                agg = fetch_protocol(e["slugs"], log=log)
            if agg.get("ok"):
                base30 = agg["rev30"] or agg["fee30"]
                base_ann = base30 * 365.0 / 30.0
                item["basis"] = "REV" if agg["rev30"] > 0 else "FEES"
                item["rev24"] = agg["rev24"] or agg["fee24"]
                item["rev30"] = base30
                item["pf"] = round(item["fdv"] / base_ann, 2) if base_ann > 0 and item["fdv"] else None
                d7 = (agg["fee7"] or agg["rev7"]) / 7.0
                cur24 = agg["fee24"] or agg["rev24"]
                item["burst_pct"] = round((cur24 - d7) / d7 * 100.0, 1) if d7 > 0 else None
                if protocol_payload:
                    item["lp_share"] = _shares_from_payload(protocol_payload, e["slugs"])
        item = apply_profile(item, ctx)
        if item.get("profile") == "index" and item.get("pf"):
            med = peer_pf_median(protocol_payload)
            if med:
                item["pf_peer_median"] = med
                item["pf_premium_x"] = round(item["pf"] / med, 2)
        items.append(item)

    ctx_summary = None
    if ctx:
        ctx_summary = {"launchpad_leader": (ctx.get("launchpad") or {}).get("leader"),
                       "issuance_rate": (ctx.get("pulse") or {}).get("rate_per_min"),
                       "chain_dex_24h": (ctx.get("dex") or {}).get("total24h")}
    order = {e["symbol"]: i for i, e in enumerate(ents)}
    items.sort(key=lambda x: order.get(x["symbol"], 99))
    return {
        "as_of_epoch": int(time.time()),
        "as_of_kst": now_kst().strftime("%Y-%m-%d %H:%M"),
        "items": items,
        "unresolved": unresolved,
        "context": ctx_summary,
        "elapsed_sec": round(time.time() - started, 1),
    }


def snapshot(state, epoch=None):
    """이력은 판정에 필요한 값만 — 파일이 커지면 시간별 실행이 느려진다."""
    return {
        "epoch": int(epoch or state.get("as_of_epoch") or time.time()),
        "px": {i["symbol"]: i.get("price") for i in state["items"] if i.get("resolved")},
        "mcap": {i["symbol"]: i.get("mcap") for i in state["items"] if i.get("resolved")},
        "liq": {i["symbol"]: i.get("liq") for i in state["items"] if i.get("resolved")},
        "vol24": {i["symbol"]: i.get("vol24") for i in state["items"] if i.get("resolved")},
        "rev24": {i["symbol"]: i.get("rev24") for i in state["items"] if i.get("rev24")},
        "pf": {i["symbol"]: i.get("pf") for i in state["items"] if i.get("pf")},
        "lp_share": {i["symbol"]: i.get("lp_share") for i in state["items"] if i.get("lp_share")},
        "rank": {i["symbol"]: i.get("rank") for i in state["items"] if i.get("rank")},
        "rival_share": {i["symbol"]: i.get("rival_share")
                        for i in state["items"] if i.get("rival_share") is not None},
        "issuance": {i["symbol"]: i.get("issuance_rate")
                     for i in state["items"] if i.get("issuance_rate")},
        "attn": {i["symbol"]: i.get("attn_share_pct")
                 for i in state["items"] if i.get("attn_share_pct") is not None},
    }


def _ref(history, hours, now_epoch, tol=0.6):
    target = now_epoch - hours * 3600
    best, gap = None, None
    for snap in history:
        ts = snap.get("epoch")
        if ts is None or ts > now_epoch:
            continue
        g = abs(ts - target)
        if gap is None or g < gap:
            best, gap = snap, g
    if best is None or gap > hours * 3600 * tol:
        return None
    return best


def _pct(cur, prev):
    if prev in (None, 0) or cur is None:
        return None
    return (cur - prev) / abs(prev) * 100.0


# ------------------------------------------------------------------ 변화량
def _prev_ref(history, now_epoch, max_hours=12.0):
    """직전 관측 = now 이전 가장 최근 스냅샷. 너무 오래된 것은 '직전'이 아니다."""
    best = None
    for snap in history:
        ts = snap.get("epoch")
        if ts is None or ts >= now_epoch:
            continue
        if best is None or ts > best["epoch"]:
            best = snap
    if best is None or now_epoch - best["epoch"] > max_hours * 3600:
        return None
    return best


def _snap_val(snap, key, sym):
    if not snap:
        return None
    return (snap.get(key) or {}).get(sym)


def _snap_ratio_pct(snap, num_key, den_key, sym):
    n, d = _snap_val(snap, num_key, sym), _snap_val(snap, den_key, sym)
    if n is None or not d:
        return None
    return _fnum(n) / _fnum(d) * 100.0


def _pp(cur, prev):
    if cur is None or prev is None:
        return None
    return float(cur) - float(prev)


def _kst_hm(epoch):
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M")


def annotate_deltas(state, history, now_epoch=None):
    """
    각 종목에 delta 블록을 붙인다 — 직전 관측(prev) 및 전일(day, 24h±) 대비.
    값이 없으면 None. 렌더러는 이 블록만 보고 화살표를 그린다.
    """
    if not state or not state.get("items"):
        return state
    now_epoch = int(now_epoch or state.get("as_of_epoch") or time.time())
    prev = _prev_ref(history or [], now_epoch)
    day = _ref(history or [], 24, now_epoch)
    if day is not None and prev is not None and day["epoch"] == prev["epoch"]:
        day = None                                   # 같은 스냅샷을 두 번 쓰지 않는다
    state["delta_refs"] = {
        "prev_epoch": prev["epoch"] if prev else None,
        "prev_kst": _kst_hm(prev["epoch"]) if prev else None,
        "day_epoch": day["epoch"] if day else None,
        "day_kst": _kst_hm(day["epoch"]) if day else None,
    }
    for it in state["items"]:
        if not it.get("resolved"):
            continue
        sym = it["symbol"]

        def against(ref):
            if ref is None:
                return None
            cur_turn = it.get("turnover_pct")
            cur_liqm = it.get("liq_mcap_pct")
            return {
                "px": _pct(it.get("price"), _snap_val(ref, "px", sym)),
                "mcap": _pct(it.get("mcap"), _snap_val(ref, "mcap", sym)),
                "liq": _pct(it.get("liq"), _snap_val(ref, "liq", sym)),
                "vol24": _pct(it.get("vol24"), _snap_val(ref, "vol24", sym)),
                "turnover_pp": _pp(cur_turn, _snap_ratio_pct(ref, "vol24", "mcap", sym)),
                "liq_mcap_pp": _pp(cur_liqm, _snap_ratio_pct(ref, "liq", "mcap", sym)),
                "rev24": _pct(it.get("rev24"), _snap_val(ref, "rev24", sym)),
                "pf_prev": _snap_val(ref, "pf", sym),
                "lp_share_pp": _pp(it.get("lp_share"), _snap_val(ref, "lp_share", sym)),
                "rival_pp": _pp(it.get("rival_share"), _snap_val(ref, "rival_share", sym)),
                "issuance": _pct(it.get("issuance_rate"), _snap_val(ref, "issuance", sym)),
                "attn_pp": _pp(it.get("attn_share_pct"), _snap_val(ref, "attn", sym)),
                "rank_prev": _snap_val(ref, "rank", sym),
            }
        it["delta"] = {"prev": against(prev), "day": against(day)}
    return state


# ------------------------------------------------------------------ 판정
def evaluate(state, history, cfg, now_epoch=None):
    """
    보유 종목 임계는 순위 트랙보다 타이트하다. 다만 **없는 신호는 만들지 않는다** —
    기준 스냅샷이 없으면 그 항목은 조용히 건너뛴다.
    """
    now_epoch = int(now_epoch or state.get("as_of_epoch") or time.time())
    annotate_deltas(state, history, now_epoch)       # 렌더·대시보드가 쓰는 변화량
    r6 = _ref(history, 6, now_epoch)
    r24 = _ref(history, 24, now_epoch)
    alerts = []

    def add(code, sym, detail, sev, action=""):
        alerts.append({"code": code, "symbol": sym, "detail": detail,
                       "severity": sev, "action": action})

    for it in state["items"]:
        sym = it["symbol"]
        if not it.get("resolved"):
            add("UNRESOLVED", sym, it.get("reason", "데이터 없음"), 11.0,
                "풀 존재 여부를 직접 확인하세요")
            continue

        # 1) 보안 — 보유 종목에서는 즉시 최상위
        for code in it.get("flags", []):
            if code in ("HONEYPOT", "CANNOT_SELL_ALL", "MINTABLE", "PAUSABLE",
                        "SELL_TAX", "BUY_TAX", "CLOSED_SOURCE"):
                add("SECURITY", sym, "컨트랙트 경보 %s" % code, 12.0,
                    "소액 매도로 실제 체결 가능 여부 확인")

        # 2) 유동성 — 나올 수 있는가
        if it.get("liq_mcap_pct") is not None and \
                it["liq_mcap_pct"] < thr(cfg, it, "wl_liq_mcap_min_pct", 3.0):
            add("LIQ_THIN", sym, "유동성/시총 %.2f%% — 시총 대비 풀이 얇습니다"
                % it["liq_mcap_pct"], 8.0, "분할 매도 아니면 슬리피지 큽니다")
        if r6:
            dl = _pct(it.get("liq"), (r6.get("liq") or {}).get(sym))
            if dl is not None and dl <= thr(cfg, it, "wl_liq_drain_pct", -25.0):
                add("LIQ_DRAIN", sym, "6h 유동성 %+.0f%% ($%s)" % (dl, _h(it.get("liq"))),
                    10.0, "가격보다 먼저 빠지는 자리입니다")

        # 3) 매출 — 근거가 살아 있는가 (프로토콜 연동 종목만)
        noisy = _fnum(it.get("rev24")) < thr(cfg, it, "wl_rev_noise_floor_usd", 0.0)
        if it.get("rev24") is not None and it.get("burst_pct") is not None and not noisy:
            if it["burst_pct"] <= thr(cfg, it, "wl_rev_collapse_pct", -50.0):
                add("REV_COLLAPSE", sym, "24h 매출이 7일 평균 대비 %+.0f%% ($%s)"
                    % (it["burst_pct"], _h(it["rev24"])), 9.5,
                    "배수가 싸 보여도 분모가 무너지는 중입니다")
            elif it["burst_pct"] >= thr(cfg, it, "wl_rev_surge_pct", 100.0):
                add("REV_SURGE", sym, "24h 매출이 7일 평균 대비 %+.0f%% ($%s)"
                    % (it["burst_pct"], _h(it["rev24"])), 6.0)

        # 4) 점유율 — 경쟁에서 밀리는가
        if r24 and it.get("lp_share") is not None:
            prev = (r24.get("lp_share") or {}).get(sym)
            if prev is not None:
                dpp = it["lp_share"] - _fnum(prev)
                if dpp <= -thr(cfg, it, "wl_share_drop_pp", 8.0):
                    add("SHARE_LOSS", sym, "런치패드 점유율 %.0f%%→%.0f%% (%+.0f%%p)"
                        % (_fnum(prev), it["lp_share"], dpp), 9.0,
                        "매출 하락보다 먼저 오는 신호입니다")

        # 5) 배수 — 리레이팅/디레이팅
        if r24 and it.get("pf") is not None:
            prev = (r24.get("pf") or {}).get(sym)
            dp = _pct(it["pf"], prev)
            if dp is not None and abs(dp) >= thr(cfg, it, "wl_pf_move_pct", 25.0):
                add("PF_MOVE", sym, "매출배수 %.1f→%.1f배 (%+.0f%%)"
                    % (_fnum(prev), it["pf"], dp), 6.5,
                    "분자(FDV)와 분모(매출) 중 무엇이 움직였는지 확인")


        # ---------- 종목 특성별 규칙 ----------
        prof = it.get("profile")

        if prof == "launchpad":
            # 런치패드의 매출은 밈 발행 활동의 파생물이다. 발행이 먼저, 점유율이 그다음,
            # 매출은 마지막에 움직인다 — 앞의 둘을 본다.
            if r6 and it.get("lp_share") is not None:
                prev = (r6.get("lp_share") or {}).get(sym)
                if prev is not None and it["lp_share"] - _fnum(prev) <= \
                        -thr(cfg, it, "wl_share_drop_pp_6h", 10.0):
                    add("SHARE_LOSS", sym, "6h 런치패드 점유율 %.0f%%→%.0f%% (%+.0f%%p)"
                        % (_fnum(prev), it["lp_share"], it["lp_share"] - _fnum(prev)), 9.5,
                        "매출 하락보다 먼저 오는 신호입니다")
            if it.get("lp_leader") and it["lp_leader"] not in (it.get("slugs") or []):
                add("LEAD_LOST", sym, "런치패드 1위가 %s (내 점유율 %.0f%%)"
                    % (it["lp_leader"], _fnum(it.get("lp_share"))), 10.5,
                    "카테고리 주도권이 넘어간 상태입니다")
            if r24 and it.get("rival_share") is not None:
                prev = (r24.get("rival_share") or {}).get(sym)
                if prev is not None and it["rival_share"] - _fnum(prev) >= \
                        thr(cfg, it, "wl_rival_rise_pp", 10.0):
                    add("RIVAL_RISE", sym, "경쟁 런치패드 %s %.0f%%→%.0f%% (%+.0f%%p)"
                        % (it.get("rival") or "?", _fnum(prev), it["rival_share"],
                           it["rival_share"] - _fnum(prev)), 8.5,
                        "점유율을 가져가는 쪽을 확인하세요")
            if r24 and it.get("issuance_rate"):
                prev = (r24.get("issuance") or {}).get(sym)
                d = _pct(it["issuance_rate"], prev)
                if d is not None and d <= thr(cfg, it, "wl_issuance_drop_pct", -50.0):
                    add("ISSUANCE_SLOW", sym, "체인 신규 발행 %.1f→%.1f개/분 (%+.0f%%)"
                        % (_fnum(prev), it["issuance_rate"], d), 8.0,
                        "런치패드 매출의 선행 지표입니다")
                elif d is not None and d >= thr(cfg, it, "wl_issuance_surge_pct", 100.0):
                    add("ISSUANCE_SURGE", sym, "체인 신규 발행 %.1f→%.1f개/분 (%+.0f%%)"
                        % (_fnum(prev), it["issuance_rate"], d), 5.5)

        elif prof == "index":
            # 매출이 작은 종목은 %가 아니라 절대금액으로 봐야 한다 — 몇 천 달러의
            # 진폭이 ±200%를 만든다.
            floor = thr(cfg, it, "wl_index_rev_floor_usd", 3000.0)
            if it.get("rev24") is not None and 0 < _fnum(it["rev24"]) < floor:
                add("REV_FLOOR", sym, "24h 매출 $%s — 바닥권($%s 미만)"
                    % (_h(it["rev24"]), _h(floor)), 8.0,
                    "배수의 분모가 얇습니다")
            if it.get("rev24") is not None and _fnum(it["rev24"]) <= 0 < _fnum(it.get("rev30")):
                add("REV_ZERO", sym, "24시간 매출 0 (30일 $%s) — 서비스·어댑터 중단 가능성"
                    % _h(it.get("rev30")), 9.5, "이틀 연속이면 실질 중단으로 보세요")
            lfloor = thr(cfg, it, "wl_liq_floor_usd", 1_500_000.0)
            if _fnum(it.get("liq")) and _fnum(it["liq"]) < lfloor:
                add("LIQ_FLOOR", sym, "유동성 $%s — 절대 하한($%s) 미만"
                    % (_h(it["liq"]), _h(lfloor)), 9.0, "소형은 비율보다 절대금액입니다")
            if it.get("pf_premium_x") and \
                    it["pf_premium_x"] >= thr(cfg, it, "wl_pf_premium_x", 2.5):
                add("PF_PREMIUM", sym, "배수 %.1f배 — 체인 중앙값 %.1f배의 %.1f배"
                    % (it["pf"], it["pf_peer_median"], it["pf_premium_x"]), 7.0,
                    "동종 대비 프리미엄 구간입니다")

        elif prof == "meme":
            # 밈에는 매출이 없다. 근거는 오직 '체인 전체 관심 중 내 몫'이다.
            if r24 and it.get("attn_share_pct") is not None:
                prev = (r24.get("attn") or {}).get(sym)
                d = _pct(it["attn_share_pct"], prev)
                if d is not None and d <= thr(cfg, it, "wl_attn_drop_pct", -40.0):
                    add("ATTN_LOSS", sym, "체인 거래대금 점유율 %.2f%%→%.2f%% (%+.0f%%)"
                        % (_fnum(prev), it["attn_share_pct"], d), 8.5,
                        "밈의 가치는 상대적 관심입니다")
            tmin = thr(cfg, it, "wl_meme_turnover_min_pct", 5.0)
            if it.get("turnover_pct") is not None and it["turnover_pct"] < tmin:
                add("TURNOVER_DRY", sym, "24h 회전율 %.1f%% — 거래가 마르는 중"
                    % it["turnover_pct"], 7.5)

        # 카피캣 — 이 체인에서 실제로 관측되는 위험(유사 심볼 신규 발행)
        for c in (it.get("copycats") or [])[:3]:
            add("COPYCAT", sym, "유사 심볼 신규 풀 %s (유사도 %.2f)"
                % (c.get("name"), _fnum(c.get("ratio"))), 7.0,
                "매수 시 컨트랙트 주소를 반드시 대조하세요")

        # 6) 가격·거래대금 — 마지막에 확인하는 것
        for ref, hours, key in ((r6, 6, "wl_price_drop_6h_pct"), (r24, 24, "wl_price_drop_24h_pct")):
            if not ref:
                continue
            dp = _pct(it.get("price"), (ref.get("px") or {}).get(sym))
            if dp is None:
                continue
            lim = thr(cfg, it, key, -15.0 if hours == 6 else -25.0)
            if dp <= lim:
                add("PRICE_DROP", sym, "%dh 가격 %+.1f%%" % (hours, dp), 7.5)
            elif dp >= abs(lim) * 1.5:
                add("PRICE_SPIKE", sym, "%dh 가격 %+.1f%%" % (hours, dp), 5.0)
        if r24:
            dv = _pct(it.get("vol24"), (r24.get("vol24") or {}).get(sym))
            if dv is not None and dv <= -70.0:
                add("VOL_DRY", sym, "24h 거래대금 %+.0f%% — 관심 이탈" % dv, 6.0)

        # 7) 밈 트랙 순위
        if r24 and it.get("rank"):
            prev = (r24.get("rank") or {}).get(sym)
            if prev:
                d = int(prev) - int(it["rank"])
                if abs(d) >= int(thr(cfg, it, "wl_rank_move", 5)):
                    add("RANK_MOVE", sym, "시총 순위 %d위→%d위 (%+d)" % (prev, it["rank"], d),
                        6.5 if d < 0 else 5.5)

    alerts.sort(key=lambda a: -a["severity"])
    return alerts


def gate_alerts(alerts, sent_state, cfg, now_epoch):
    """
    같은 경보를 매시간 반복하지 않는다 — 심각도가 올라갈 때만 재발송한다.
    경보 피로가 생기면 시스템 전체가 무시당한다.
    """
    cooldown = _fnum(cfg.get("wl_cooldown_hours"), 6.0) * 3600
    keep, new_state = [], dict(sent_state or {})
    for a in alerts:
        key = "%s|%s" % (a["code"], a["symbol"])
        prev = new_state.get(key) or {}
        last = _fnum(prev.get("epoch"))
        prev_sev = _fnum(prev.get("severity"))
        if now_epoch - last < cooldown and a["severity"] <= prev_sev:
            continue
        keep.append(a)
        new_state[key] = {"epoch": now_epoch, "severity": a["severity"]}
    for key in list(new_state):
        if now_epoch - _fnum(new_state[key].get("epoch")) > 7 * 86400:
            del new_state[key]
    return keep, new_state


# ------------------------------------------------------------------ 렌더
def _h(v):
    v = _fnum(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return "%.2f%s" % (v / div, unit)
    return "%.0f" % v


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


ICON = {"SECURITY": "🛑", "LIQ_DRAIN": "🩸", "LIQ_THIN": "🪣", "REV_COLLAPSE": "🔻",
        "REV_SURGE": "🔺", "SHARE_LOSS": "📉", "PF_MOVE": "⚖️", "PRICE_DROP": "▼",
        "PRICE_SPIKE": "▲", "VOL_DRY": "🌵", "RANK_MOVE": "🔀", "UNRESOLVED": "❓",
        "LEAD_LOST": "👑", "RIVAL_RISE": "⚔️", "ISSUANCE_SLOW": "🏭",
        "ISSUANCE_SURGE": "🏭", "REV_FLOOR": "🪫", "REV_ZERO": "⛔",
        "LIQ_FLOOR": "🕳️", "PF_PREMIUM": "💸", "ATTN_LOSS": "👀",
        "TURNOVER_DRY": "🏜️", "COPYCAT": "🎭"}

PROFILE_TAG = {"launchpad": "런치패드", "index": "인덱스", "meme": "밈", "protocol": "프로토콜"}


def _arrow_pct(v, flat=0.5):
    """▲4.2%  ▼3.1%  ─(보합)  ·  None → '' """
    if v is None:
        return ""
    if abs(v) < flat:
        return "─"
    return ("▲%.1f%%" if v > 0 else "▼%.1f%%") % abs(v)


def _arrow_pp(v, flat=0.3, digits=1):
    if v is None:
        return ""
    fmt = "%%.%dfpp" % digits
    if abs(v) < flat:
        return "─"
    return ("▲" if v > 0 else "▼") + fmt % abs(v)


def _with(label, arrow):
    """'유동성 $10.03M' + '▼1.2%' → '유동성 $10.03M ▼1.2%' (변화 없으면 값만)"""
    return "%s %s" % (label, arrow) if arrow else label


def _pick_basis(it):
    """전일 기준이 있으면 전일, 없으면 직전. (기준명, 델타 블록)"""
    d = it.get("delta") or {}
    if d.get("day"):
        return "day", d["day"]
    if d.get("prev"):
        return "prev", d["prev"]
    return None, None


def _movers(state, limit=3):
    """종목·지표를 가로질러 절대 변화가 큰 순으로 — 한눈에 '무엇이 달라졌나'."""
    rows = []
    for it in state.get("items", []):
        if not it.get("resolved"):
            continue
        basis, d = _pick_basis(it)
        if not d:
            continue
        sym = it["symbol"]
        cand = [
            ("가격", d.get("px"), "%"), ("시총", d.get("mcap"), "%"),
            ("유동성", d.get("liq"), "%"), ("거래대금", d.get("vol24"), "%"),
            ("매출", d.get("rev24"), "%"), ("발행속도", d.get("issuance"), "%"),
            ("점유율", d.get("lp_share_pp"), "pp"), ("회전", d.get("turnover_pp"), "pp"),
            ("관심점유", d.get("attn_pp"), "pp"), ("2위점유", d.get("rival_pp"), "pp"),
        ]
        for name, v, unit in cand:
            if v is None:
                continue
            # pp 는 %와 스케일이 달라 3배 가중해 같은 줄에서 겨루게 한다
            score = abs(v) * (3.0 if unit == "pp" else 1.0)
            if unit == "%" and abs(v) < 3.0:
                continue
            if unit == "pp" and abs(v) < 1.0:
                continue
            rows.append((score, sym, name, v, unit))
    rows.sort(key=lambda r: -r[0])
    out = []
    for _, sym, name, v, unit in rows[:limit]:
        arrow = _arrow_pct(v) if unit == "%" else _arrow_pp(v)
        out.append("%s %s %s" % (sym, name, arrow))
    return out


def render_telegram(state, alerts, cfg):
    """
    본 브리프에 붙는 상시 섹션 — 경보가 없어도 상태는 항상 보여준다.
    수치 옆에 항상 변화 화살표를 단다: 전일(24h) 기준이 있으면 전일, 없으면 직전 관측.
    """
    if not state or not state.get("items"):
        return []
    refs = state.get("delta_refs") or {}
    basis_any = "day" if refs.get("day_kst") else ("prev" if refs.get("prev_kst") else None)
    if basis_any == "day":
        basis_note = "전일 %s 대비 · 가격은 직전 %s 병기" % (
            refs["day_kst"], refs.get("prev_kst") or "—")
    elif basis_any == "prev":
        basis_note = "직전 %s 대비 (전일 기준은 이력 축적 중)" % refs["prev_kst"]
    else:
        basis_note = "첫 관측 — 비교 기준 없음"
    lines = ["📌 <b>보유 종목 정밀 감시</b> <i>%s</i>" % _esc(basis_note)]

    movers = _movers(state)
    if movers:
        lines.append("🔎 <b>주요 변화</b> " + _esc(" · ".join(movers)))

    for it in state["items"]:
        sym = _esc(it["symbol"])
        if not it.get("resolved"):
            lines.append("· <b>%s</b> ❓ 데이터 없음 — %s" % (sym, _esc(it.get("reason", ""))))
            continue
        basis, d = _pick_basis(it)
        d = d or {}
        dp = (it.get("delta") or {}).get("prev") or {}
        tag = PROFILE_TAG.get(it.get("profile"))

        # 헤드: 가격은 직전·전일 둘 다, 시총·배수는 기준 하나
        px = "$%s" % _fmt_px(it.get("price"))
        px_bits = []
        if basis == "day":
            if dp.get("px") is not None:
                px_bits.append("직전%s" % _arrow_pct(dp["px"]))
            if d.get("px") is not None:
                px_bits.append("전일%s" % _arrow_pct(d["px"]))
        elif basis == "prev" and d.get("px") is not None:
            px_bits.append("직전%s" % _arrow_pct(d["px"]))
        if px_bits:
            px += " (%s)" % " ".join(px_bits)
        head = "· <b>%s</b>%s %s · %s" % (
            sym, " <i>[%s]</i>" % tag if tag else "", px,
            _with("시총 $%s" % _h(it.get("mcap")), _arrow_pct(d.get("mcap"))))
        if it.get("pf") is not None:
            prev_pf = d.get("pf_prev")
            if prev_pf and abs(prev_pf - it["pf"]) >= 0.05:
                head += " · 배수 %.1f→%.1f배" % (prev_pf, it["pf"])
            else:
                head += " · 배수 %.1f배" % it["pf"]
        lines.append(head)

        sub = [_with("유동성 $%s" % _h(it.get("liq")), _arrow_pct(d.get("liq")))]
        if it.get("liq_mcap_pct") is not None:
            sub.append(_with("시총대비 %.1f%%" % it["liq_mcap_pct"],
                             _arrow_pp(d.get("liq_mcap_pp"))))
        if it.get("turnover_pct") is not None:
            sub.append(_with("회전 %.0f%%" % it["turnover_pct"], _arrow_pp(d.get("turnover_pp"), digits=0)))
        if it.get("rev24"):
            sub.append(_with("24h매출 $%s" % _h(it["rev24"]), _arrow_pct(d.get("rev24"))))
        if it.get("burst_pct") is not None:
            b = it["burst_pct"]
            sub.append("7일평균비 %s" % ("%+.0f%%" % b if abs(b) >= 1 else "보합"))
        if it.get("lp_share") is not None:
            sub.append(_with("점유율 %.0f%%" % it["lp_share"], _arrow_pp(d.get("lp_share_pp"))))
        if it.get("rank"):
            rp = d.get("rank_prev")
            if rp and rp != it["rank"]:
                sub.append("시총 %d위→%d위" % (rp, it["rank"]))
            else:
                sub.append("시총 %d위" % it["rank"])
        if it.get("rival_share") is not None:
            sub.append(_with("2위 %s %.0f%%" % (it.get("rival") or "?", it["rival_share"]),
                             _arrow_pp(d.get("rival_pp"))))
        if it.get("issuance_rate"):
            sub.append(_with("발행 %.1f개/분" % it["issuance_rate"], _arrow_pct(d.get("issuance"))))
        if it.get("attn_share_pct") is not None:
            sub.append(_with("관심점유 %.2f%%" % it["attn_share_pct"],
                             _arrow_pp(d.get("attn_pp"), flat=0.1, digits=2)))
        if it.get("pf_premium_x"):
            sub.append("중앙값 대비 %.1f배" % it["pf_premium_x"])
        lines.append("  <i>%s</i>" % _esc(" · ".join(sub)))

    hot = [a for a in (alerts or []) if a["severity"] >= 6.0][:6]
    if hot:
        lines.append("")
        lines.append("🚨 <b>보유 경보</b>")
        for a in hot:
            tail = " — %s" % _esc(a["action"]) if a.get("action") else ""
            lines.append("%s <b>%s</b> %s%s" % (ICON.get(a["code"], "•"),
                                                _esc(a["symbol"]), _esc(a["detail"]), tail))
    lines.append("")
    return lines


def _fmt_px(p):
    p = _fnum(p)
    if p >= 1:
        return "%.3f" % p
    if p >= 0.001:
        return "%.4f" % p
    return "%.8f" % p


def render_alert(state, alerts, cfg, dash_url=""):
    """시간별 단독 실행에서 임계 위반이 있을 때만 나가는 메시지."""
    head = ["🚨 <b>보유 종목 경보</b> — %s KST" % state.get("as_of_kst", "")]
    for a in alerts[:8]:
        tail = "\n   <i>%s</i>" % _esc(a["action"]) if a.get("action") else ""
        head.append("%s <b>%s</b> %s%s" % (ICON.get(a["code"], "•"),
                                           _esc(a["symbol"]), _esc(a["detail"]), tail))
    head.append("")
    body = [ln for ln in render_telegram(state, [], cfg) if ln.strip()]
    head.extend(body)
    if dash_url:
        head.append('<a href="%s">대시보드 열기</a>' % dash_url)
    head.append("<i>관측 시스템입니다. 경보는 자금·매출 반응의 서술이며 매매 신호가 아닙니다.</i>")
    return "\n".join(head)


# ------------------------------------------------------------------ 단독 실행
def main():
    cfg_path = os.path.join(BASE, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not cfg.get("watchlist_enabled", True):
        print("[watchlist] 비활성")
        return 0

    hist_path = os.path.join(DATA_DIR, "watchlist_history.json")
    sent_path = os.path.join(DATA_DIR, "watchlist_sent.json")
    out_path = os.path.join(DATA_DIR, "watchlist_alert.json")
    pub_path = os.path.abspath(os.path.join(BASE, "..", "docs", "hood-radar", "watchlist.json"))

    history = read_json(hist_path, [])
    state = build(cfg, rows=None, protocol_payload=None)
    now_epoch = int(state["as_of_epoch"])
    alerts = evaluate(state, history, cfg, now_epoch)

    history.append(snapshot(state, now_epoch))
    history.sort(key=lambda s: s.get("epoch") or 0)
    history = history[-int(_fnum(cfg.get("wl_history_max"), 400)):]
    write_json(hist_path, history)

    fresh, sent_state = gate_alerts(alerts, read_json(sent_path, {}), cfg, now_epoch)
    write_json(sent_path, sent_state)

    dash = os.environ.get("HOOD_DASH_URL",
                          "https://jinhae8971.github.io/korea-etf-calmar/hood-radar/")
    out = {
        "as_of_kst": state.get("as_of_kst"),
        "as_of_epoch": now_epoch,
        "send": bool(fresh),
        "alert_id": "%d-%d" % (now_epoch, len(fresh)),
        "message": render_alert(state, fresh, cfg, dash) if fresh else "",
        "items": state["items"],
        "delta_refs": state.get("delta_refs"),
        "alerts": alerts,
    }
    write_json(out_path, out)
    write_json(pub_path, out)

    print("[watchlist] %s · 종목 %d · 경보 %d(발송 %d) · %.1fs"
          % (state.get("as_of_kst"), len(state["items"]), len(alerts), len(fresh),
             _fnum(state.get("elapsed_sec"))))
    for a in alerts[:6]:
        print("   %-13s %-9s %s" % (a["code"], a["symbol"], a["detail"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
