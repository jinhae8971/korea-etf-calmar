#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BTC 매크로 레이더 — 비트코인 거시 환경 변화 모니터

지표 5종 (모두 공개 원천, 키 불필요, stdlib only):
  1. 글로벌 유동성 (US+유로존 M2 프록시, USD 환산)   ← Fed H.6 / ECB BSI / Frankfurter
  2. 미국 10년물 실질금리 (TIPS)                      ← Treasury.gov / DBnomics(FED/TIPS)
  3. 달러 인덱스 (DXY)                                 ← Yahoo chart v8 / DBnomics(FED/H10 broad)
  4. 현물 BTC ETF 순유입 (5일·20일 누적)               ← bitcoin-data.com
  5. 나스닥100 동조화 (30일 상관 x 20일 추세)          ← CoinGecko / Yahoo chart v8

설계 원칙(고점신호·내러티브 레이더와 동일):
  * 관측기이지 예측기가 아니다. 각 지표는 "BTC 입장에서 지금 순풍/중립/역풍 중 어디인가"의
    서술이며 방향이나 수익률을 주장하지 않는다.
  * 수집 실패를 "이상 없음"으로 보고하지 않는다 → data_status 로 명시(OK/DEGRADED/FAIL).
  * 임계값은 코드 상수로 고정하고 변경 이력을 남긴다(사후 조정 방지).
  * 1일·30일 전 대비 변동은 원천의 자체 시계열에서 재구성해 첫 실행부터 채워진다.
  * 운영성 메시지 최소화 — 본문은 "변화" 섹션이 핵심이며 릴레이는 그대로 전달만 한다.
"""

import csv
import io
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone, date

KST = timezone(timedelta(hours=9))
UA = "btc-macro-radar/1.0 (+github actions)"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ── 임계값 (frozen 2026-09-06) ─────────────────────────────────────────
# 변경 시 THRESHOLDS_FROZEN_AT 을 갱신하고 사유를 커밋 메시지에 남길 것.
THRESHOLDS_FROZEN_AT = "2026-09-06"
TH = {
    # 실질금리(%): 낮을수록 무수익 자산에 유리
    "real_yield": {"good": 1.50, "bad": 2.00},
    # DXY 레벨: 100 하회가 변곡점, 105 상회는 달러 강세 역풍
    "dxy": {"good": 100.0, "bad": 105.0},
    # M2 프록시 YoY(%)와 3개월 모멘텀(%p)
    "m2": {"yoy_good": 3.0, "yoy_bad": 0.0, "mom_flat": 0.3},
    # ETF 순유입: 20일 누적 부호 + 5일 누적 부호
    "etf": {"strong_btc": 20000},        # 20일 누적 2만 BTC 이상이면 '강한 순유입' 표기
    # 나스닥 동조화: 30일 상관 0.5 이상이면 하이베타 국면으로 간주
    "corr": {"coupled": 0.50, "ndx_bad": -3.0, "ndx_good": 2.0},
}

# 순풍/중립/역풍 (BTC 입장). level 0=순풍, 1=중립, 2=역풍
DOT = {0: "🟢", 1: "🟡", 2: "🔴"}
WORD = {0: "순풍", 1: "중립", 2: "역풍"}


# ── HTTP ──────────────────────────────────────────────────────────────
def http_get(url, tries=3, timeout=30, accept="*/*"):
    ctx = ssl.create_default_context()
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            wait = [3, 9, 20][min(i, 2)]
            print("[fetch] %s -> %s: %s (%ds 후 재시도)"
                  % (url.split("?")[0], type(e).__name__, e, wait))
            if i < tries - 1:
                time.sleep(wait)
    raise RuntimeError("%s 실패: %s" % (url.split("?")[0], last))


def http_json(url, **kw):
    return json.loads(http_get(url, accept="application/json", **kw))


def first_ok(candidates, tag):
    """(이름, callable) 목록을 순서대로 시도해 첫 성공 결과를 반환. 전부 실패면 예외."""
    errs = []
    for name, fn in candidates:
        try:
            out = fn()
            print("[%s] 원천 = %s" % (tag, name))
            return out, name
        except Exception as e:  # noqa: BLE001
            print("[%s] %s 실패: %s" % (tag, name, e))
            errs.append("%s: %s" % (name, e))
    raise RuntimeError("; ".join(errs))


# ── 시계열 유틸 ───────────────────────────────────────────────────────
def series_from_dbnomics(code, n=800):
    j = http_json("https://api.db.nomics.world/v22/series/%s?observations=1&format=json" % code)
    d = j["series"]["docs"][0]
    out = []
    for p, v in zip(d["period"], d["value"]):
        if v is None or v == "NA":
            continue
        out.append((str(p)[:10], float(v)))
    return out[-n:]


def value_on_or_before(series, day):
    """series = [(YYYY-MM-DD, v)] 정렬. day 이전 최근 관측값."""
    best = None
    for d, v in series:
        if d <= day:
            best = (d, v)
        else:
            break
    return best


def delta_pack(series, today, unit="", pct=False, dec=2):
    """1일·30일 전 대비 변동을 표준 포맷으로. series 는 오름차순 (date, value)."""
    if not series:
        return {}
    cur_d, cur = series[-1]
    out = {}
    for tag, days in (("d1", 1), ("d30", 30)):
        ref = (datetime.strptime(cur_d, "%Y-%m-%d").date() - timedelta(days=days)).isoformat()
        hit = value_on_or_before(series[:-1], ref)
        if not hit:
            continue
        _, prev = hit
        diff = cur - prev
        if pct:
            diff = (cur / prev - 1.0) * 100.0 if prev else 0.0
            text = "%+.*f%%" % (dec, diff)
        else:
            text = "%+.*f%s" % (dec, diff, unit)
        out[tag] = {"dir": "up" if diff > 0 else ("down" if diff < 0 else "flat"),
                    "text": text, "value": round(diff, 4)}
    return out


def render_delta_line(deltas):
    if not deltas:
        return ""
    arrow = {"up": "▲", "down": "▼", "flat": "▬"}
    parts = []
    for tag, label in (("d1", "1일"), ("d30", "30일")):
        d = deltas.get(tag)
        if d:
            parts.append("%s %s%s" % (label, arrow[d["dir"]], d["text"]))
    return "  ·  ".join(parts)


# ── 1. 글로벌 유동성 (M2 프록시) ──────────────────────────────────────
def _us_m2_fed_ddp():
    url = ("https://www.federalreserve.gov/datadownload/Output.aspx?rel=H6"
           "&series=798e2796917702a5f8423426ba7e6b42&lastobs=30&from=&to="
           "&filetype=csv&label=include&layout=seriescolumn")
    rows = list(csv.reader(io.StringIO(http_get(url))))
    hdr_i = next(i for i, r in enumerate(rows) if r and r[0].startswith("Time Period"))
    col = rows[hdr_i].index("M2_N.M")
    out = []
    for r in rows[hdr_i + 1:]:
        if len(r) > col and re.match(r"^\d{4}-\d{2}$", r[0]):
            try:
                out.append((r[0], float(r[col]) * 1e9))
            except ValueError:
                pass
    if len(out) < 14:
        raise RuntimeError("행 부족 %d" % len(out))
    return out  # USD, 월, NSA


def _us_m2_dbnomics():
    s = series_from_dbnomics("FED/H6_H6_M2/M2_N.M", n=30)
    return [(d[:7], v * 1e9) for d, v in s]


def _ea_m2_ecb():
    url = ("https://data-api.ecb.europa.eu/service/data/BSI/"
           "M.U2.Y.V.M20.X.1.U2.2300.Z01.E?format=csvdata&lastNObservations=30")
    rows = list(csv.DictReader(io.StringIO(http_get(url))))
    out = [(r["TIME_PERIOD"], float(r["OBS_VALUE"]) * 1e6) for r in rows if r.get("OBS_VALUE")]
    if len(out) < 14:
        raise RuntimeError("행 부족 %d" % len(out))
    return out  # EUR


def _ea_m2_dbnomics():
    s = series_from_dbnomics("ECB/BSI/M.U2.Y.V.M20.X.1.U2.2300.Z01.E", n=30)
    return [(d[:7], v * 1e6) for d, v in s]


def _eurusd_monthly():
    today = date.today()
    start = (today - timedelta(days=430)).isoformat()
    j = http_json("https://api.frankfurter.dev/v1/%s..%s?base=EUR&symbols=USD"
                  % (start, today.isoformat()))
    by_m = {}
    for d in sorted(j["rates"]):
        by_m[d[:7]] = float(j["rates"][d]["USD"])   # 월말(마지막 관측) 환율
    return by_m


def fetch_m2():
    us, us_src = first_ok([("fed-ddp", _us_m2_fed_ddp), ("dbnomics", _us_m2_dbnomics)], "m2-us")
    ea, ea_src = first_ok([("ecb", _ea_m2_ecb), ("dbnomics", _ea_m2_dbnomics)], "m2-ea")
    fx = _eurusd_monthly()
    usm, eam = dict(us), dict(ea)
    months = sorted(set(usm) & set(eam))
    if not months:
        raise RuntimeError("공통 월 없음")
    # 환율은 해당 월, 없으면 가장 최근 월
    fx_months = sorted(fx)

    def fx_for(m):
        hit = [k for k in fx_months if k <= m]
        return fx[hit[-1]] if hit else fx[fx_months[0]]

    usd_total = [(m, usm[m] + eam[m] * fx_for(m)) for m in months]
    # 고정환율(최신 월 환율) 기준 — FX 착시 제거용
    fx_last = fx_for(months[-1])
    cc_total = [(m, usm[m] + eam[m] * fx_last) for m in months]

    def yoy(s, k):
        if len(s) < 13 + k:
            return None
        return (s[-1 - k][1] / s[-13 - k][1] - 1.0) * 100.0

    yoy_now, yoy_3m = yoy(usd_total, 0), yoy(usd_total, 3)
    yoy_cc = yoy(cc_total, 0)
    if yoy_now is None:
        raise RuntimeError("13개월 미만")
    mom = (yoy_now - yoy_3m) if yoy_3m is not None else None
    # 대시보드용 최근 24개월 YoY 시계열
    yoy_series = []
    for k in range(min(24, len(usd_total) - 13), -1, -1):
        v = yoy(usd_total, k)
        if v is not None:
            yoy_series.append((usd_total[-1 - k][0] + "-01", round(v, 2)))
    return {
        "value": round(yoy_now, 2), "yoy_3m_ago": None if yoy_3m is None else round(yoy_3m, 2),
        "momentum": None if mom is None else round(mom, 2),
        "yoy_const_fx": None if yoy_cc is None else round(yoy_cc, 2),
        "as_of": months[-1], "total_usd_tn": round(usd_total[-1][1] / 1e12, 2),
        "sources": {"us": us_src, "ea": ea_src, "fx": "frankfurter"},
        "series": yoy_series,
        "deltas": _m2_deltas(yoy_series),
    }


def _m2_deltas(yoy_series):
    """월 단위 지표라 1일 변동은 없음 — 전월·3개월 전 YoY 대비 변동(%p)을 d30 슬롯에 담는다."""
    out = {}
    if len(yoy_series) >= 2:
        d = yoy_series[-1][1] - yoy_series[-2][1]
        out["d30"] = {"dir": "up" if d > 0 else ("down" if d < 0 else "flat"),
                      "text": "%+.2f%%p(전월)" % d, "value": round(d, 4)}
    return out


def grade_m2(d):
    t = TH["m2"]
    y, m = d["value"], d["momentum"]
    rising = m is not None and m > t["mom_flat"]
    falling = m is not None and m < -t["mom_flat"]
    cc = "" if d.get("yoy_const_fx") is None else " · 고정환율 YoY %.1f%%" % d["yoy_const_fx"]
    if y >= t["yoy_good"] and not falling:
        return 0, "확장 국면 — YoY %.1f%%, 3개월 모멘텀 %s%s" % (y, _pp(m), cc)
    if y < t["yoy_bad"] or (falling and y < t["yoy_good"]):
        return 2, "수축·둔화 — YoY %.1f%%, 3개월 모멘텀 %s%s" % (y, _pp(m), cc)
    if rising:
        return 0, "바닥 통과 후 반등 — YoY %.1f%%, 모멘텀 %s%s" % (y, _pp(m), cc)
    if falling:
        return 1, "확장세 둔화 — YoY %.1f%%, 모멘텀 %s%s" % (y, _pp(m), cc)
    return 1, "완만한 확장 — YoY %.1f%%, 모멘텀 %s%s" % (y, _pp(m), cc)


def _pp(x):
    return "n/a" if x is None else "%+.2f%%p" % x


# ── 2. 미국 10년물 실질금리 ──────────────────────────────────────────
def _real_yield_treasury():
    year = date.today().year
    out = []
    for y in (year - 1, year):
        x = http_get("https://home.treasury.gov/resource-center/data-chart-center/"
                     "interest-rates/pages/xml?data=daily_treasury_real_yield_curve"
                     "&field_tdr_date_value=%d" % y)
        for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
            d = re.search(r"<d:NEW_DATE[^>]*>(\d{4}-\d{2}-\d{2})", e)
            v = re.search(r"<d:TC_10YEAR[^>]*>([-\d.]+)<", e)
            if d and v:
                out.append((d.group(1), float(v.group(1))))
    out.sort()
    if len(out) < 40:
        raise RuntimeError("관측 부족 %d" % len(out))
    return out


def _real_yield_dbnomics():
    return series_from_dbnomics("FED/TIPS/TIPSPY10", n=400)


def _real_yield_fred():
    rows = list(csv.reader(io.StringIO(http_get(
        "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10"))))
    out = [(r[0], float(r[1])) for r in rows[1:] if len(r) > 1 and r[1] not in (".", "")]
    return out[-400:]


def fetch_real_yield():
    s, src = first_ok([("treasury", _real_yield_treasury), ("dbnomics", _real_yield_dbnomics),
                       ("fred", _real_yield_fred)], "real-yield")
    return {"value": round(s[-1][1], 2), "as_of": s[-1][0], "source": src,
            "series": s[-260:], "deltas": delta_pack(s, None, unit="%p")}


def grade_real_yield(d):
    t = TH["real_yield"]
    v = d["value"]
    d30 = (d.get("deltas") or {}).get("d30", {}).get("value")
    trend = "" if d30 is None else (" · 30일 %s" % ("하락" if d30 < -0.05 else "상승" if d30 > 0.05 else "보합"))
    if v < t["good"]:
        return 0, "실질금리 낮음(<%.1f%%) — 무수익 자산 밸류에이션 우호%s" % (t["good"], trend)
    if v >= t["bad"]:
        return 2, "실질금리 높음(≥%.1f%%) — 단기 밸류에이션 부담%s" % (t["bad"], trend)
    return 1, "중간 구간 — 추가 하락 시 랠리 트리거%s" % trend


# ── 3. 달러 인덱스 ────────────────────────────────────────────────────
def _yahoo_close(symbol, rng="1y"):
    last = None
    for host in ("query1", "query2"):
        try:
            j = http_json("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d"
                          % (host, urllib.request.quote(symbol), rng), tries=2)
            r = j["chart"]["result"][0]
            ts, cl = r["timestamp"], r["indicators"]["quote"][0]["close"]
            out = [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), float(c))
                   for t, c in zip(ts, cl) if c is not None]
            if len(out) < 30:
                raise RuntimeError("관측 부족 %d" % len(out))
            return out
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError("yahoo %s: %s" % (symbol, last))


def fetch_dxy():
    def yahoo():
        return {"series": _yahoo_close("DX-Y.NYB"), "kind": "DXY"}

    def broad():
        return {"series": series_from_dbnomics("FED/H10/JRXWTFB_N.B", n=400), "kind": "BROAD"}

    d, src = first_ok([("yahoo", yahoo), ("dbnomics-broad", broad)], "dxy")
    s = d["series"]
    return {"value": round(s[-1][1], 2), "as_of": s[-1][0], "source": src, "kind": d["kind"],
            "series": s[-260:], "deltas": delta_pack(s, None, pct=True, dec=2)}


def grade_dxy(d):
    t = TH["dxy"]
    v = d["value"]
    d30 = (d.get("deltas") or {}).get("d30", {}).get("value")
    trend = "" if d30 is None else (" · 30일 %s" % ("약세" if d30 < -1 else "강세" if d30 > 1 else "박스권"))
    if d["kind"] != "DXY":
        # 광의 달러지수(스케일 다름) — 레벨 판정 불가, 추세만
        if d30 is None:
            return 1, "광의 달러지수 대체 관측(레벨 판정 생략)"
        return (0 if d30 < -1 else 2 if d30 > 1 else 1), "광의 달러지수 대체 관측%s" % trend
    if v < t["good"]:
        return 0, "100 하회 — 달러 약세, 위험자산 우호%s" % trend
    if v >= t["bad"]:
        return 2, "105 상회 — 달러 강세, 달러 유동성 경색 주의%s" % trend
    return 1, "100~105 박스권 — 100 하회 여부가 변곡점%s" % trend


# ── 4. 현물 ETF 순유입 ───────────────────────────────────────────────
def fetch_etf_flow(btc_price=None):
    j = http_json("https://bitcoin-data.com/v1/etf-flow-btc")
    s = sorted(((x["d"], float(x["etfFlow"])) for x in j if x.get("etfFlow") not in (None, "")),
               key=lambda p: p[0])
    if len(s) < 25:
        raise RuntimeError("관측 부족 %d" % len(s))
    last5 = s[-5:]
    last20 = s[-20:]
    prev20 = s[-40:-20]
    sum5 = sum(v for _, v in last5)
    sum20 = sum(v for _, v in last20)
    sum20_prev = sum(v for _, v in prev20) if len(prev20) == 20 else None
    streak = 0
    sign = 1 if s[-1][1] > 0 else -1
    for _, v in reversed(s):
        if (v > 0) == (sign > 0) and v != 0:
            streak += 1
        else:
            break
    usd = None
    if btc_price:
        usd = {"d1": s[-1][1] * btc_price / 1e6, "s5": sum5 * btc_price / 1e6,
               "s20": sum20 * btc_price / 1e6}
    return {"value": round(sum20, 0), "sum5": round(sum5, 0), "last": round(s[-1][1], 0),
            "last_date": s[-1][0], "sum20_prev": None if sum20_prev is None else round(sum20_prev, 0),
            "streak": streak * sign, "usd_mn": usd, "series": s[-120:],
            "source": "bitcoin-data.com"}


def grade_etf(d):
    s20, s5 = d["value"], d["sum5"]
    stk = d["streak"]
    stk_txt = ("%d일 연속 %s" % (abs(stk), "유입" if stk > 0 else "유출")) if abs(stk) >= 3 else ""
    if s20 > 0 and s5 > 0:
        strong = s20 >= TH["etf"]["strong_btc"]
        return 0, "20일·5일 모두 순유입%s — 기관 하방 지지%s" % (" (강함)" if strong else "", (" · " + stk_txt) if stk_txt else "")
    if s20 < 0 and s5 < 0:
        return 2, "20일·5일 모두 순유출 — 기관 매도 우위%s" % ((" · " + stk_txt) if stk_txt else "")
    if s20 > 0 >= s5:
        return 1, "중기 순유입이나 최근 5일 유출 전환 — 관망%s" % ((" · " + stk_txt) if stk_txt else "")
    return 1, "중기 순유출이나 최근 5일 유입 전환 — 저점 매수 여부 확인%s" % ((" · " + stk_txt) if stk_txt else "")


# ── 5. 나스닥 동조화 ─────────────────────────────────────────────────
def fetch_btc_series():
    j = http_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                  "?vs_currency=usd&days=120&interval=daily")
    out = {}
    for t, p in j["prices"]:
        out[datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")] = float(p)
    return sorted(out.items())


def _pearson(a, b):
    n = len(a)
    if n < 10:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = math.sqrt(sum((x - ma) ** 2 for x in a))
    vb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)


def fetch_coupling(btc=None):
    btc = btc or fetch_btc_series()
    ndx = _yahoo_close("^NDX", rng="6mo")
    bd, nd = dict(btc), dict(ndx)
    days = sorted(set(bd) & set(nd))
    if len(days) < 35:
        raise RuntimeError("공통 거래일 부족 %d" % len(days))
    rb = [bd[days[i]] / bd[days[i - 1]] - 1 for i in range(1, len(days))]
    rn = [nd[days[i]] / nd[days[i - 1]] - 1 for i in range(1, len(days))]
    corr30 = _pearson(rb[-30:], rn[-30:])
    corr30_prev = _pearson(rb[-60:-30], rn[-60:-30]) if len(rb) >= 60 else None
    ndx20 = (nd[days[-1]] / nd[days[-21]] - 1) * 100 if len(days) > 21 else None
    btc20 = (bd[days[-1]] / bd[days[-21]] - 1) * 100 if len(days) > 21 else None
    if corr30 is None:
        raise RuntimeError("상관 계산 불가")
    return {"value": round(corr30, 2), "corr_prev": None if corr30_prev is None else round(corr30_prev, 2),
            "ndx_20d": None if ndx20 is None else round(ndx20, 2),
            "btc_20d": None if btc20 is None else round(btc20, 2),
            "as_of": days[-1], "btc_price": bd[days[-1]], "source": "coingecko+yahoo"}


def grade_coupling(d):
    t = TH["corr"]
    c, n = d["value"], d["ndx_20d"]
    if c < t["coupled"]:
        return 1, "동조화 낮음(상관 %.2f) — 디지털 금 성격 우세, 기술주와 독립 움직임" % c
    if n is None:
        return 1, "하이베타 국면(상관 %.2f)" % c
    if n <= t["ndx_bad"]:
        return 2, "하이베타 국면(상관 %.2f) + 나스닥 20일 %+.1f%% — 기술주 조정 동반 압력" % (c, n)
    if n >= t["ndx_good"]:
        return 0, "하이베타 국면(상관 %.2f) + 나스닥 20일 %+.1f%% — 위험선호 동반 상승" % (c, n)
    return 1, "하이베타 국면(상관 %.2f), 나스닥 20일 %+.1f%% 횡보" % (c, n)


# ── 합성 ──────────────────────────────────────────────────────────────
def compose(signals):
    scored = [s for s in signals if s.get("level") is not None]
    if not scored:
        return {"score": None, "max": None, "phase": "판정 불가"}
    # 순풍 점수: 순풍 2점, 중립 1점, 역풍 0점
    score = sum(2 - s["level"] for s in scored)
    mx = 2 * len(scored)
    ratio = score / mx
    if ratio >= 0.75:
        phase = "우호"
    elif ratio >= 0.55:
        phase = "완만한 우호"
    elif ratio >= 0.40:
        phase = "중립·과도기"
    else:
        phase = "역풍"
    return {"score": score, "max": mx, "phase": phase}


def detect_changes(cur, prev):
    """전일 스냅샷과 비교해 사람이 읽을 변화만 뽑는다. 첫 실행이면 빈 목록."""
    out = []
    if not prev:
        return out
    pp = prev.get("phase", {}).get("phase")
    if pp and pp != cur["phase"]["phase"]:
        out.append("매크로 국면 %s → <b>%s</b>" % (pp, cur["phase"]["phase"]))
    pmap = {s["key"]: s for s in prev.get("signals", [])}
    for s in cur["signals"]:
        q = pmap.get(s["key"])
        if not q or s.get("level") is None or q.get("level") is None:
            continue
        if s["level"] != q["level"]:
            out.append("%s %s → %s (%s)" % (s["label"], DOT[q["level"]], DOT[s["level"]], WORD[s["level"]]))
    # 레벨 임계값 통과 이벤트
    def crossed(key, th, label_fmt):
        a, b = pmap.get(key, {}).get("value"), next((s["value"] for s in cur["signals"] if s["key"] == key), None)
        if a is None or b is None:
            return
        if (a < th) != (b < th):
            out.append(label_fmt % (a, b))
    crossed("dxy", TH["dxy"]["good"], "DXY 100선 통과 (%.2f → %.2f)")
    crossed("real_yield", TH["real_yield"]["bad"], "실질금리 2.0%% 경계 통과 (%.2f → %.2f)")
    crossed("real_yield", TH["real_yield"]["good"], "실질금리 1.5%% 경계 통과 (%.2f → %.2f)")
    # ETF 5일 누적 부호 반전
    a = pmap.get("etf", {}).get("sum5")
    b = next((s.get("sum5") for s in cur["signals"] if s["key"] == "etf"), None)
    if a is not None and b is not None and (a > 0) != (b > 0) and a != 0 and b != 0:
        out.append("ETF 5일 누적 %s 전환 (%+.0f → %+.0f BTC)" % ("순유입" if b > 0 else "순유출", a, b))
    return out


# ── 렌더링 ────────────────────────────────────────────────────────────
def render_message(p):
    L = ["🌐 <b>BTC 매크로 레이더</b>  %s KST" % p["as_of_kst"],
         "국면: <b>%s</b>  (순풍도 %s/%s)" % (p["phase"]["phase"], p["phase"]["score"], p["phase"]["max"]),
         ""]
    for s in p["signals"]:
        if s.get("level") is None:
            L.append("⚪ <b>%s</b> — 수집 실패" % s["label"])
            continue
        L.append("%s <b>%s</b> %s" % (DOT[s["level"]], s["label"], s["display"]))
        line = render_delta_line(s.get("deltas"))
        if line:
            L.append("     %s" % line)
        L.append("     <i>%s</i>" % s["note"])
    L.append("")
    if p.get("changes"):
        L.append("⚡ <b>변화</b>")
        for c in p["changes"]:
            L.append("· %s" % c)
    elif p.get("first_run"):
        L.append("⚡ 첫 관측 — 내일부터 전일 대비 변화를 표시합니다")
    else:
        L.append("⚡ 변화 없음 — 전일과 동일 국면")
    L.append("")
    if p["data_status"] != "OK":
        L.append("⚠️ 데이터 상태 <b>%s</b> — 일부 지표가 빠졌습니다." % p["data_status"])
    L.append("<i>관측 리포트입니다. 순풍/역풍은 현재 구간의 서술이며 방향이나 수익률을 "
             "주장하지 않습니다. 유동성은 US+유로존 M2(USD 환산) 프록시입니다.</i>")
    return "\n".join(L)


def _spark(series, w=220, h=44, color="#1565c0"):
    vals = [v for _, v in series]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(vals):
        x = i * (w - 4) / (len(vals) - 1) + 2
        y = h - 2 - (v - lo) * (h - 4) / rng
        pts.append("%.1f,%.1f" % (x, y))
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d"><polyline fill="none" stroke="%s" '
            'stroke-width="1.6" points="%s"/></svg>' % (w, h, w, h, color, " ".join(pts)))


def render_dashboard(p):
    def cell(d):
        if not d:
            return '<td class="r" style="color:#aaa">—</td>'
        c = {"up": "#c62828", "down": "#1565c0", "flat": "#777"}[d["dir"]]
        a = {"up": "▲", "down": "▼", "flat": "▬"}[d["dir"]]
        return '<td class="r" style="color:%s;white-space:nowrap">%s %s</td>' % (c, a, d["text"])

    rows = ['<tr><th style="text-align:left">지표</th><th></th><th class="r">현재</th>'
            '<th class="r">1일</th><th class="r">30일</th><th style="text-align:left">판정</th>'
            '<th>추이</th></tr>']
    for s in p["signals"]:
        lv = s.get("level")
        color = {0: "#2e7d32", 1: "#ef6c00", 2: "#c62828"}.get(lv, "#777")
        dl = s.get("deltas") or {}
        spark = _spark(s.get("series") or [], color=color) if s.get("series") else ""
        rows.append('<tr><td><b>%s</b><div class="src">%s</div></td><td style="color:%s">%s</td>'
                    '<td class="r"><b>%s</b></td>%s%s<td>%s</td><td>%s</td></tr>'
                    % (s["label"], s.get("source_txt", ""), color, DOT.get(lv, "⚪"), s["display"],
                       cell(dl.get("d1")), cell(dl.get("d30")), s["note"], spark))
    changes = "".join("<li>%s</li>" % c for c in p.get("changes", [])) or "<li>변화 없음</li>"
    return """<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BTC 매크로 레이더</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
margin:0;padding:18px;background:#fafafa;color:#222;max-width:920px}
h1{font-size:19px;margin:0 0 2px}.sub{color:#666;font-size:13px;margin-bottom:14px}
.phase{display:inline-block;padding:6px 12px;border-radius:8px;background:#eef;
font-weight:700;margin-bottom:14px}
table{width:100%%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}
td,th{padding:9px 10px;border-bottom:1px solid #eee;font-size:14px;vertical-align:top}
td.r,th.r{text-align:right}.src{color:#999;font-size:11px}
h2{font-size:15px;margin:18px 0 6px}ul{margin:0;padding-left:18px;font-size:14px}
.note{color:#777;font-size:12px;margin-top:14px;line-height:1.6}
</style>
<h1>🌐 BTC 매크로 레이더</h1>
<div class="sub">%s KST · 데이터 상태 %s</div>
<div class="phase">%s · 순풍도 %s/%s</div>
<table>%s</table>
<h2>⚡ 변화(전일 대비)</h2><ul>%s</ul>
<p class="note">관측 리포트입니다. 🟢순풍 🟡중립 🔴역풍은 BTC 입장에서 본 현재 구간의 서술이며
방향이나 수익률을 주장하지 않습니다.<br>
글로벌 유동성은 미국 M2(Fed H.6)와 유로존 M2(ECB)를 USD 환산해 합산한 프록시이며 일본·중국은
공개 원천 지연이 커서 제외했습니다. 30일 변동은 각 원천의 자체 시계열에서 산출합니다.<br>
임계값 고정일 %s</p>
</html>""" % (p["as_of_kst"], p["data_status"], p["phase"]["phase"], p["phase"]["score"],
              p["phase"]["max"], "".join(rows), changes, THRESHOLDS_FROZEN_AT)


# ── 수집 ──────────────────────────────────────────────────────────────
def collect():
    signals, failed = [], []

    def add(key, label, fn, fmt, grader, source_txt=""):
        try:
            d = fn()
        except Exception as e:  # noqa: BLE001
            print("[collect] %s 실패: %s" % (key, e))
            failed.append(key)
            signals.append({"key": key, "label": label, "level": None, "value": None,
                            "display": "—", "note": "수집 실패", "source_txt": source_txt})
            return
        lv, note = grader(d)
        entry = {"key": key, "label": label, "level": lv, "value": d["value"],
                 "display": fmt(d), "note": note, "deltas": d.get("deltas") or {},
                 "series": [(a, round(b, 4)) for a, b in (d.get("series") or [])][-260:],
                 "source_txt": source_txt or str(d.get("source", ""))}
        for k in ("sum5", "sum20_prev", "streak", "usd_mn", "as_of", "momentum",
                  "yoy_const_fx", "ndx_20d", "btc_20d", "corr_prev", "kind"):
            if k in d:
                entry[k] = d[k]
        signals.append(entry)

    add("m2", "글로벌 유동성(M2 프록시)", fetch_m2,
        lambda d: "YoY %+.2f%%  (%s, $%.1fT)" % (d["value"], d["as_of"], d["total_usd_tn"]),
        grade_m2, "Fed H.6 + ECB BSI, USD 환산")

    add("real_yield", "미10Y 실질금리", fetch_real_yield,
        lambda d: "%.2f%%" % d["value"], grade_real_yield)

    add("dxy", "달러 인덱스", fetch_dxy,
        lambda d: "%.2f" % d["value"] if d["kind"] == "DXY" else "%.2f (광의)" % d["value"],
        grade_dxy)

    btc = None
    try:
        btc = fetch_btc_series()
    except Exception as e:  # noqa: BLE001
        print("[collect] BTC 시세 실패: %s" % e)
    price = btc[-1][1] if btc else None

    def fmt_etf(d):
        u = d.get("usd_mn") or {}
        base = "20일 %+.0f BTC · 5일 %+.0f BTC" % (d["value"], d["sum5"])
        if u:
            base += "  (≈ $%+.0fM / $%+.0fM)" % (u["s20"], u["s5"])
        return base
    add("etf", "현물 ETF 순유입", lambda: fetch_etf_flow(price), fmt_etf, grade_etf,
        "bitcoin-data.com (BTC 단위)")

    add("coupling", "나스닥 동조화", lambda: fetch_coupling(btc),
        lambda d: "30일 상관 %.2f · NDX 20일 %+.1f%% · BTC 20일 %+.1f%%"
                  % (d["value"], d["ndx_20d"] or 0.0, d["btc_20d"] or 0.0),
        grade_coupling, "CoinGecko + Yahoo ^NDX")

    ok = len(signals) - len(failed)
    status = "OK" if ok == len(signals) else ("DEGRADED" if ok >= 3 else "FAIL")
    return signals, status, failed, price


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
    signals, status, failed, price = collect()
    payload = {
        "as_of": now.strftime("%Y-%m-%d"),
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),
        "data_status": status,
        "failed": failed,
        "btc_price": None if price is None else round(price, 2),
        "thresholds_frozen_at": THRESHOLDS_FROZEN_AT,
        "signals": signals,
        "phase": compose(signals),
    }
    # 전일 비교 대상: 오늘 것이 이미 있으면(재실행) 그 앞 것을 쓴다
    prev = None
    for h in reversed(hist):
        if h.get("as_of") != payload["as_of"]:
            prev = h
            break
    payload["first_run"] = prev is None
    payload["changes"] = detect_changes(payload, prev)
    payload["message"] = render_message(payload)

    if status == "FAIL":
        print("[main] 수집 대부분 실패 — 스냅샷은 남기되 판정 불가로 표기")

    json.dump(payload, open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    slim = {k: payload[k] for k in ("as_of", "as_of_kst", "data_status", "phase", "btc_price")}
    slim["signals"] = [{"key": s["key"], "level": s["level"], "value": s["value"],
                        "sum5": s.get("sum5")} for s in signals]
    if not hist or hist[-1].get("as_of") != slim["as_of"]:
        hist.append(slim)
    else:
        hist[-1] = slim
    hist = hist[-400:]
    json.dump(hist, open(hist_path, "w", encoding="utf-8"), ensure_ascii=False)

    docs = os.path.join(os.path.dirname(HERE), "docs", "btc-macro")
    os.makedirs(docs, exist_ok=True)
    open(os.path.join(docs, "index.html"), "w", encoding="utf-8").write(render_dashboard(payload))

    print(payload["message"])
    return 0 if status != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
