#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""크립토 고점신호 모니터 — 김치프리미엄 / 알트시즌 / 공포탐욕 / MVRV

설계 원칙(내러티브 레이더와 동일):
  * 관측기이지 예측기가 아니다. 각 지표는 "지금 어느 구간에 있는가"의 서술이며
    고점 시점이나 미래 수익률을 주장하지 않는다.
  * 수집 실패를 "이상 없음"으로 보고하지 않는다 → data_status 로 명시하고
    커버리지 미달이면 DEGRADED 로 표기, 릴레이가 발송을 중단한다.
  * 임계값은 코드에 상수로 고정하고 변경 이력을 남긴다(사후 조정 방지).
  * 외부 파이썬 의존성 0 (stdlib only).
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
UA = "top-signal-monitor/1.0 (+github actions)"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ── 임계값 (frozen 2026-09-05) ─────────────────────────────────────────
# 변경 시 THRESHOLDS_FROZEN_AT 을 갱신하고 사유를 커밋 메시지에 남길 것.
THRESHOLDS_FROZEN_AT = "2026-09-05"
TH = {
    "kimchi":   {"warn": 3.0,  "hot": 5.0,  "reverse": -2.0},
    "altseason": {"warn": 60,  "hot": 75},
    "fng":      {"warn": 75,   "hot": 80},
    "mvrv":     {"warn": 3.0,  "hot": 3.7},
    "mvrv_z":   {"warn": 3.5,  "hot": 5.0},
}
STABLE_OR_WRAPPED = {
    "tether", "usd-coin", "dai", "first-digital-usd", "ethena-usde", "usds",
    "binance-usd", "true-usd", "paypal-usd", "wrapped-bitcoin", "staked-ether",
    "wrapped-steth", "weth", "wrapped-eeth", "coinbase-wrapped-btc", "susds",
    "binance-staked-sol", "jito-staked-sol", "wbeth", "rocket-pool-eth",
    "usdt0", "blackrock-usd-institutional-digital-liquidity-fund",
}


def http_json(url, tries=3, timeout=25):
    """단순 GET + 지수 백오프. 실패는 예외로 올린다(조용한 성공 위장 금지)."""
    ctx = ssl.create_default_context()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last = e
            wait = [3, 9, 20][min(i, 2)]
            print("[fetch] %s -> %s: %s (%ds 후 재시도)"
                  % (url.split("?")[0], type(e).__name__, e, wait))
            if i < tries - 1:
                time.sleep(wait)
    raise RuntimeError("%s 실패: %s" % (url.split("?")[0], last))


# ── 개별 지표 수집 ────────────────────────────────────────────────────
def fetch_kimchi():
    """김치프리미엄 = 업비트 KRW-BTC 대 (해외 BTC/USD x USD/KRW).

    바이낸스는 GitHub 러너 IP 에서 HTTP 451(지역 제한)을 반환하므로 쓰지 않는다.
    해외 기준가는 코인베이스, 환율은 프랑크푸르터(ECB)를 1순위로 둔다.
    """
    up = http_json("https://api.upbit.com/v1/ticker?markets=KRW-BTC")
    krw = float(up[0]["trade_price"])

    usd = None
    for url, pick in (
        ("https://api.coinbase.com/v2/prices/BTC-USD/spot",
         lambda d: float(d["data"]["amount"])),
        ("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
         lambda d: float(d["bitcoin"]["usd"])),
    ):
        try:
            usd = pick(http_json(url, tries=2))
            break
        except Exception as e:  # noqa: BLE001
            print("[kimchi] 해외 시세 폴백: %s" % e)
    if usd is None:
        raise RuntimeError("해외 BTC/USD 확보 실패")

    fx = None
    for url, pick in (
        ("https://api.frankfurter.dev/v1/latest?base=USD&symbols=KRW",
         lambda d: float(d["rates"]["KRW"])),
        ("https://open.er-api.com/v6/latest/USD",
         lambda d: float(d["rates"]["KRW"])),
    ):
        try:
            fx = pick(http_json(url, tries=2))
            break
        except Exception as e:  # noqa: BLE001
            print("[kimchi] 환율 폴백: %s" % e)
    if fx is None:
        raise RuntimeError("USD/KRW 확보 실패")

    premium = (krw / (usd * fx) - 1.0) * 100.0
    out = {"value": round(premium, 2), "krw": krw, "usd": usd, "fx": round(fx, 2)}
    out["hist"] = _kimchi_history()
    return out


def _kimchi_history():
    """1일 전·30일 전 김프를 외부 이력에서 역산한다.

    자체 history.json 은 오늘 시작이라 30일치가 없다. 업비트 일봉 + 코인베이스
    일자별 spot + 프랑크푸르터 일자별 환율로 과거 시점을 재구성한다.
    실패하면 해당 항목만 None 으로 두고 나머지는 그대로 보고한다.
    """
    out = {}
    try:
        candles = http_json("https://api.upbit.com/v1/candles/days"
                            "?market=KRW-BTC&count=32", tries=2)
    except Exception as e:  # noqa: BLE001
        print("[kimchi] 일봉 실패: %s" % e)
        return out
    by_date = {}
    for c in candles:
        by_date[c["candle_date_time_kst"][:10]] = float(c["trade_price"])

    today = datetime.now(KST).date()
    for tag, days in (("d1", 1), ("d30", 30)):
        day = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        krw = by_date.get(day)
        if krw is None:
            continue
        try:
            usd = float(http_json(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot?date=%s" % day,
                tries=2)["data"]["amount"])
            fx = float(http_json(
                "https://api.frankfurter.dev/v1/%s?base=USD&symbols=KRW" % day,
                tries=2)["rates"]["KRW"])
        except Exception as e:  # noqa: BLE001
            print("[kimchi] %s 역산 실패: %s" % (tag, e))
            continue
        out[tag] = round((krw / (usd * fx) - 1.0) * 100.0, 2)
    return out


def fetch_altseason():
    """알트시즌 지수(자체 산출, 30일 기준).

    공식 지수(blockchaincenter)는 90일 기준이나 스크래핑 대상이라 쓰지 않는다.
    코인게코 시총 상위에서 스테이블·랩드·LST 를 제외한 알트 50종 중
    BTC 30일 수익률을 초과한 비율(%)로 정의한다. 기준이 다르므로 공식 지수와
    수치가 어긋날 수 있고, 메시지에도 그렇게 표기한다.
    """
    rows = http_json("https://api.coingecko.com/api/v3/coins/markets"
                     "?vs_currency=usd&order=market_cap_desc&per_page=80&page=1"
                     "&price_change_percentage=30d")
    btc = next((r for r in rows if r.get("id") == "bitcoin"), None)
    if not btc or btc.get("price_change_percentage_30d_in_currency") is None:
        raise RuntimeError("BTC 30일 수익률 없음")
    base = float(btc["price_change_percentage_30d_in_currency"])

    alts = []
    for r in rows:
        if r.get("id") == "bitcoin" or r.get("id") in STABLE_OR_WRAPPED:
            continue
        chg = r.get("price_change_percentage_30d_in_currency")
        if chg is None:
            continue
        alts.append((r.get("symbol", "").upper(), float(chg)))
        if len(alts) >= 50:
            break
    if len(alts) < 30:
        raise RuntimeError("알트 표본 부족: %d" % len(alts))

    beat = [s for s, c in alts if c > base]
    idx = round(100.0 * len(beat) / len(alts))
    top = sorted(alts, key=lambda x: -x[1])[:3]
    return {"value": idx, "sample": len(alts), "btc_30d": round(base, 1),
            "leaders": [{"sym": s, "chg": round(c, 1)} for s, c in top]}


def fetch_fng():
    d = http_json("https://api.alternative.me/fng/?limit=31")
    rows = d.get("data") or []
    if not rows:
        raise RuntimeError("공포탐욕지수 응답 비어 있음")
    cur = int(rows[0]["value"])
    hist = {}
    if len(rows) > 1:
        hist["d1"] = int(rows[1]["value"])
    if len(rows) > 30:
        hist["d30"] = int(rows[30]["value"])
    return {"value": cur, "label": rows[0].get("value_classification"), "hist": hist}


def fetch_mvrv():
    m = http_json("https://bitcoin-data.com/v1/mvrv/last")
    out = {"value": round(float(m["mvrv"]), 2), "as_of": m.get("d")}
    try:
        z = http_json("https://bitcoin-data.com/v1/mvrv-zscore/last", tries=2)
        out["zscore"] = round(float(z["mvrvZscore"]), 2)
    except Exception as e:  # noqa: BLE001
        print("[mvrv] z-score 실패(무시): %s" % e)
        out["zscore"] = None
    return out


# ── 변동폭 ────────────────────────────────────────────────────────────
# 네 지표 모두 "값이 오르면 과열 방향"이라 상승=🔺(적신호) / 하락=🔻 로 통일한다.
ARROW = {"up": "🔺", "down": "🔻", "flat": "▬"}
UNIT = {"kimchi": "%p", "altseason": "", "fng": "", "mvrv": ""}


def _arrow(delta, eps):
    if delta is None:
        return None
    if abs(delta) < eps:
        return "flat"
    return "up" if delta > 0 else "down"


def build_deltas(key, cur_value, ext_hist, own_hist, today):
    """1일·30일 변동폭. 자체 이력을 1순위, 외부 역산(kimchi·fng)을 폴백으로 둔다."""
    eps = {"kimchi": 0.05, "altseason": 1, "fng": 1, "mvrv": 0.01}[key]
    fmt = {"kimchi": "%+.2f", "altseason": "%+.0f", "fng": "%+.0f", "mvrv": "%+.2f"}[key]
    by_date = {}
    for row in own_hist or []:
        for sig in row.get("signals", []):
            if sig.get("key") == key and sig.get("value") is not None:
                by_date[row.get("as_of")] = sig["value"]
    out = {}
    for tag, days in (("d1", 1), ("d30", 30)):
        day = (today - timedelta(days=days)).strftime("%Y-%m-%d")
        base = by_date.get(day)
        if base is None:
            base = (ext_hist or {}).get(tag)
        if base is None or cur_value is None:
            out[tag] = None
            continue
        d = cur_value - base
        out[tag] = {"delta": round(d, 2), "dir": _arrow(d, eps),
                    "text": fmt % d + UNIT[key]}
    return out


def render_delta_line(deltas):
    if not deltas:
        return ""
    parts = []
    for tag, label in (("d1", "1일"), ("d30", "30일")):
        d = deltas.get(tag)
        if not d:
            parts.append("%s —" % label)
        else:
            parts.append("%s %s%s" % (label, ARROW[d["dir"]], d["text"]))
    return "  ·  ".join(parts)


# ── 판정 ──────────────────────────────────────────────────────────────
def grade(name, value):
    """지표별 3단계 판정 → (level, 설명). level: 0 평온 / 1 주의 / 2 과열"""
    t = TH[name]
    if name == "kimchi":
        if value >= t["hot"]:
            return 2, "국내 수요 과열 구간"
        if value >= t["warn"]:
            return 1, "국내 프리미엄 확대"
        if value <= t["reverse"]:
            return 0, "역프리미엄 — 국내 수요 위축"
        return 0, "정상 범위"
    if name == "altseason":
        if value >= t["hot"]:
            return 2, "알트시즌 — 자금이 알트로 광범위 확산"
        if value >= t["warn"]:
            return 1, "알트 우위 확대"
        return 0, "비트코인 우위"
    if name == "fng":
        if value >= t["hot"]:
            return 2, "극단적 탐욕"
        if value >= t["warn"]:
            return 1, "탐욕 구간"
        return 0, "탐욕 임계 미만"
    if name == "mvrv":
        if value >= t["hot"]:
            return 2, "역사적 고점권 밸류에이션"
        if value >= t["warn"]:
            return 1, "미실현이익 누적 확대"
        return 0, "평균 회귀 구간"
    if name == "mvrv_z":
        if value >= t["hot"]:
            return 2, "역사적 고점권"
        if value >= t["warn"]:
            return 1, "과열 진입"
        return 0, "정상 범위"
    raise KeyError(name)


def compose(signals):
    """지표 level 합산 → 종합 국면. 최대 8점(4지표 x 2)."""
    scored = [s for s in signals if s.get("level") is not None]
    if not scored:
        return {"score": None, "max": None, "phase": "판정 불가"}
    score = sum(s["level"] for s in scored)
    mx = 2 * len(scored)
    ratio = score / mx
    if ratio >= 0.75:
        phase = "고점 경계"
    elif ratio >= 0.5:
        phase = "과열 확산"
    elif ratio >= 0.25:
        phase = "주의"
    else:
        phase = "평온"
    return {"score": score, "max": mx, "phase": phase}


# ── 렌더링 ────────────────────────────────────────────────────────────
DOT = {0: "🟢", 1: "🟠", 2: "🔴"}


def render_message(payload):
    p = payload
    L = ["🧭 <b>크립토 고점신호</b>  %s KST" % p["as_of_kst"],
         "국면: <b>%s</b>  (과열도 %s/%s)" % (p["phase"]["phase"],
                                              p["phase"]["score"], p["phase"]["max"]),
         ""]
    for s in p["signals"]:
        if s.get("level") is None:
            L.append("⚪ <b>%s</b> — 수집 실패" % s["label"])
            continue
        L.append("%s <b>%s</b> %s" % (DOT[s["level"]], s["label"], s["display"]))
        line = render_delta_line(s.get("deltas"))
        if line:
            L.append("     %s" % line)
        L.append("     <i>%s</i>%s" % (s["note"], s.get("extra", "")))
    if p.get("changes"):
        L.append("")
        L.append("⚡ <b>변화</b>")
        for c in p["changes"]:
            L.append("· %s" % c)
    L.append("")
    if p["data_status"] != "OK":
        L.append("⚠️ 데이터 상태 <b>%s</b> — 일부 지표가 빠졌습니다." % p["data_status"])
    L.append("<i>관측 리포트입니다. 각 지표는 현재 구간의 서술이며 "
             "고점 시점이나 수익률을 주장하지 않습니다.</i>")
    return "\n".join(L)


def render_dashboard(payload):
    def cell(d):
        if not d:
            return '<td style="text-align:right;color:#aaa">—</td>'
        c = {"up": "#c62828", "down": "#1565c0", "flat": "#777"}[d["dir"]]
        a = {"up": "▲", "down": "▼", "flat": "▬"}[d["dir"]]
        return ('<td style="text-align:right;color:%s;white-space:nowrap">%s %s</td>'
                % (c, a, d["text"]))

    rows = ['<tr><th style="text-align:left">지표</th><th></th>'
            '<th style="text-align:right">현재</th>'
            '<th style="text-align:right">1일</th>'
            '<th style="text-align:right">30일</th>'
            '<th style="text-align:left">판정</th></tr>']
    for s in payload["signals"]:
        lv = s.get("level")
        color = {0: "#2e7d32", 1: "#ef6c00", 2: "#c62828"}.get(lv, "#777")
        dl = s.get("deltas") or {}
        rows.append(
            '<tr><td><b>%s</b></td><td style="color:%s">%s</td>'
            '<td style="text-align:right"><b>%s</b></td>%s%s<td>%s</td></tr>'
            % (s["label"], color, DOT.get(lv, "⚪"), s["display"],
               cell(dl.get("d1")), cell(dl.get("d30")), s["note"]))
    return """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>크립토 고점신호</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
margin:0;padding:18px;background:#fafafa;color:#222;max-width:760px}
h1{font-size:19px;margin:0 0 2px}.sub{color:#666;font-size:13px;margin-bottom:14px}
.phase{display:inline-block;padding:6px 12px;border-radius:8px;background:#eef;
font-weight:700;margin-bottom:14px}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
td,th{padding:9px 10px;border-bottom:1px solid #eee;font-size:14px;vertical-align:top}
.note{color:#777;font-size:12px;margin-top:14px;line-height:1.6}
</style>
<h1>크립토 고점신호</h1>
<div class="sub">%s KST · 데이터 상태 %s</div>
<div class="phase">%s · 과열도 %s/%s</div>
<table>%s</table>
<p class="note">관측 리포트입니다. 각 지표는 현재 구간의 서술이며 고점 시점이나
수익률을 주장하지 않습니다.<br>알트시즌 지수는 코인게코 시총 상위 알트 50종의
30일 수익률을 BTC와 비교해 자체 산출한 값으로, 90일 기준의 공식 지수와 다릅니다.<br>
임계값 고정일 %s</p>
</html>""" % (payload["as_of_kst"], payload["data_status"], payload["phase"]["phase"],
              payload["phase"]["score"], payload["phase"]["max"], "".join(rows),
              THRESHOLDS_FROZEN_AT)


# ── 변화 탐지 ─────────────────────────────────────────────────────────
def detect_changes(cur, prev):
    if not prev:
        return []
    out = []
    if prev.get("phase", {}).get("phase") != cur["phase"]["phase"]:
        out.append("국면 %s → <b>%s</b>"
                   % (prev.get("phase", {}).get("phase"), cur["phase"]["phase"]))
    pmap = {s["key"]: s for s in prev.get("signals", [])}
    for s in cur["signals"]:
        q = pmap.get(s["key"])
        if not q or s.get("level") is None or q.get("level") is None:
            continue
        if s["level"] > q["level"]:
            out.append("%s 단계 상승 (%s → %s)" % (s["label"], DOT[q["level"]], DOT[s["level"]]))
        elif s["level"] < q["level"]:
            out.append("%s 단계 하락 (%s → %s)" % (s["label"], DOT[q["level"]], DOT[s["level"]]))
    k = next((s for s in cur["signals"] if s["key"] == "kimchi"), None)
    kq = pmap.get("kimchi")
    if k and kq and k.get("value") is not None and kq.get("value") is not None:
        d = k["value"] - kq["value"]
        if abs(d) >= 1.0:
            out.append("김치프리미엄 %+.2f%%p 급변 (%.2f%% → %.2f%%)"
                       % (d, kq["value"], k["value"]))
    return out


# ── 메인 ──────────────────────────────────────────────────────────────
def collect(own_hist=None):
    signals, failed = [], []
    today = datetime.now(KST).date()

    def add(key, label, fn, fmt, level_from):
        try:
            d = fn()
        except Exception as e:  # noqa: BLE001
            print("[collect] %s 실패: %s" % (key, e))
            failed.append(key)
            signals.append({"key": key, "label": label, "level": None,
                            "value": None, "display": "—", "note": "수집 실패"})
            return
        lv, note = level_from(d)
        signals.append({"key": key, "label": label, "level": lv, "value": d["value"],
                        "display": fmt(d), "note": note, "raw": d,
                        "deltas": build_deltas(key, d["value"], d.get("hist"),
                                               own_hist, today),
                        "extra": d.get("_extra", "")})

    add("kimchi", "김치프리미엄", fetch_kimchi,
        lambda d: "%+.2f%%" % d["value"],
        lambda d: grade("kimchi", d["value"]))

    add("altseason", "알트시즌 지수", fetch_altseason,
        lambda d: "%d / 100" % d["value"],
        lambda d: grade("altseason", d["value"]))

    add("fng", "공포탐욕지수", fetch_fng,
        lambda d: "%d (%s)" % (d["value"], d["label"]),
        lambda d: grade("fng", d["value"]))

    def mvrv_level(d):
        lv1, n1 = grade("mvrv", d["value"])
        if d.get("zscore") is None:
            return lv1, n1
        lv2, n2 = grade("mvrv_z", d["zscore"])
        return max(lv1, lv2), (n1 if lv1 >= lv2 else n2)

    add("mvrv", "MVRV", fetch_mvrv,
        lambda d: ("%.2f" % d["value"]) + ("" if d.get("zscore") is None
                                           else " · Z %.2f" % d["zscore"]),
        mvrv_level)

    ok = len(signals) - len(failed)
    status = "OK" if ok == len(signals) else ("DEGRADED" if ok >= 2 else "FAIL")
    return signals, status, failed


def main():
    now = datetime.now(KST)
    os.makedirs(DATA, exist_ok=True)
    hist_path = os.path.join(DATA, "history.json")
    hist = []
    if os.path.exists(hist_path):
        try:
            hist = json.load(open(hist_path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            hist = []
    signals, status, failed = collect(own_hist=hist)
    payload = {
        "as_of": now.strftime("%Y-%m-%d"),
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "data_status": status,
        "failed": failed,
        "thresholds_frozen_at": THRESHOLDS_FROZEN_AT,
        "signals": signals,
        "phase": compose(signals),
    }

    prev = hist[-1] if hist else None
    payload["changes"] = detect_changes(payload, prev)
    payload["message"] = render_message(payload)

    if status == "FAIL":
        print("[main] 수집 대부분 실패 — 스냅샷은 남기되 판정 불가로 표기")

    json.dump(payload, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    slim = {k: payload[k] for k in ("as_of", "as_of_kst", "data_status", "phase")}
    slim["signals"] = [{"key": s["key"], "level": s["level"], "value": s["value"]}
                       for s in signals]
    if not hist or hist[-1].get("as_of") != slim["as_of"]:
        hist.append(slim)
    else:
        hist[-1] = slim
    hist = hist[-400:]
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False)

    docs = os.path.join(os.path.dirname(HERE), "docs", "top-signal")
    os.makedirs(docs, exist_ok=True)
    open(os.path.join(docs, "index.html"), "w", encoding="utf-8").write(
        render_dashboard(payload))

    print(payload["message"])
    return 0 if status != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
