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

    items, unresolved = [], []
    for e in ents:
        m = market.get(e["address"])
        row = _from_meme_rows(rows, e["address"])
        item = {
            "symbol": e["symbol"], "address": e["address"], "track": e["track"],
            "slugs": e["slugs"], "note": e["note"],
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
                item["lp_share"] = _shares_from_payload(protocol_payload, e["slugs"])
        items.append(item)

    order = {e["symbol"]: i for i, e in enumerate(ents)}
    items.sort(key=lambda x: order.get(x["symbol"], 99))
    return {
        "as_of_epoch": int(time.time()),
        "as_of_kst": now_kst().strftime("%Y-%m-%d %H:%M"),
        "items": items,
        "unresolved": unresolved,
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


# ------------------------------------------------------------------ 판정
def evaluate(state, history, cfg, now_epoch=None):
    """
    보유 종목 임계는 순위 트랙보다 타이트하다. 다만 **없는 신호는 만들지 않는다** —
    기준 스냅샷이 없으면 그 항목은 조용히 건너뛴다.
    """
    now_epoch = int(now_epoch or state.get("as_of_epoch") or time.time())
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
                it["liq_mcap_pct"] < _fnum(cfg.get("wl_liq_mcap_min_pct"), 3.0):
            add("LIQ_THIN", sym, "유동성/시총 %.2f%% — 시총 대비 풀이 얇습니다"
                % it["liq_mcap_pct"], 8.0, "분할 매도 아니면 슬리피지 큽니다")
        if r6:
            dl = _pct(it.get("liq"), (r6.get("liq") or {}).get(sym))
            if dl is not None and dl <= _fnum(cfg.get("wl_liq_drain_pct"), -25.0):
                add("LIQ_DRAIN", sym, "6h 유동성 %+.0f%% ($%s)" % (dl, _h(it.get("liq"))),
                    10.0, "가격보다 먼저 빠지는 자리입니다")

        # 3) 매출 — 근거가 살아 있는가 (프로토콜 연동 종목만)
        if it.get("rev24") is not None and it.get("burst_pct") is not None:
            if it["burst_pct"] <= _fnum(cfg.get("wl_rev_collapse_pct"), -50.0):
                add("REV_COLLAPSE", sym, "24h 매출이 7일 평균 대비 %+.0f%% ($%s)"
                    % (it["burst_pct"], _h(it["rev24"])), 9.5,
                    "배수가 싸 보여도 분모가 무너지는 중입니다")
            elif it["burst_pct"] >= _fnum(cfg.get("wl_rev_surge_pct"), 100.0):
                add("REV_SURGE", sym, "24h 매출이 7일 평균 대비 %+.0f%% ($%s)"
                    % (it["burst_pct"], _h(it["rev24"])), 6.0)

        # 4) 점유율 — 경쟁에서 밀리는가
        if r24 and it.get("lp_share") is not None:
            prev = (r24.get("lp_share") or {}).get(sym)
            if prev is not None:
                dpp = it["lp_share"] - _fnum(prev)
                if dpp <= -_fnum(cfg.get("wl_share_drop_pp"), 8.0):
                    add("SHARE_LOSS", sym, "런치패드 점유율 %.0f%%→%.0f%% (%+.0f%%p)"
                        % (_fnum(prev), it["lp_share"], dpp), 9.0,
                        "매출 하락보다 먼저 오는 신호입니다")

        # 5) 배수 — 리레이팅/디레이팅
        if r24 and it.get("pf") is not None:
            prev = (r24.get("pf") or {}).get(sym)
            dp = _pct(it["pf"], prev)
            if dp is not None and abs(dp) >= _fnum(cfg.get("wl_pf_move_pct"), 25.0):
                add("PF_MOVE", sym, "매출배수 %.1f→%.1f배 (%+.0f%%)"
                    % (_fnum(prev), it["pf"], dp), 6.5,
                    "분자(FDV)와 분모(매출) 중 무엇이 움직였는지 확인")

        # 6) 가격·거래대금 — 마지막에 확인하는 것
        for ref, hours, key in ((r6, 6, "wl_price_drop_6h_pct"), (r24, 24, "wl_price_drop_24h_pct")):
            if not ref:
                continue
            dp = _pct(it.get("price"), (ref.get("px") or {}).get(sym))
            if dp is None:
                continue
            thr = _fnum(cfg.get(key), -15.0 if hours == 6 else -25.0)
            if dp <= thr:
                add("PRICE_DROP", sym, "%dh 가격 %+.1f%%" % (hours, dp), 7.5)
            elif dp >= abs(thr) * 1.5:
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
                if abs(d) >= int(_fnum(cfg.get("wl_rank_move"), 5)):
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
        "PRICE_SPIKE": "▲", "VOL_DRY": "🌵", "RANK_MOVE": "🔀", "UNRESOLVED": "❓"}


def render_telegram(state, alerts, cfg):
    """본 브리프에 붙는 상시 섹션 — 경보가 없어도 상태는 항상 보여준다."""
    if not state or not state.get("items"):
        return []
    lines = ["📌 <b>보유 종목 정밀 감시</b>"]
    for it in state["items"]:
        sym = _esc(it["symbol"])
        if not it.get("resolved"):
            lines.append("· <b>%s</b> ❓ 데이터 없음 — %s" % (sym, _esc(it.get("reason", ""))))
            continue
        head = "· <b>%s</b> $%s · 시총 $%s" % (sym, _fmt_px(it.get("price")), _h(it.get("mcap")))
        if it.get("pf") is not None:
            head += " · 배수 %.1f배" % it["pf"]
        lines.append(head)
        sub = ["유동성 $%s" % _h(it.get("liq"))]
        if it.get("liq_mcap_pct") is not None:
            sub.append("시총대비 %.1f%%" % it["liq_mcap_pct"])
        if it.get("turnover_pct") is not None:
            sub.append("회전 %.0f%%" % it["turnover_pct"])
        if it.get("rev24"):
            sub.append("24h매출 $%s" % _h(it["rev24"]))
        if it.get("burst_pct") is not None:
            b = it["burst_pct"]
            sub.append("7일평균비 %s" % ("%+.0f%%" % b if abs(b) >= 1 else "보합"))
        if it.get("lp_share") is not None:
            sub.append("점유율 %.0f%%" % it["lp_share"])
        if it.get("rank"):
            sub.append("시총 %d위" % it["rank"])
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
