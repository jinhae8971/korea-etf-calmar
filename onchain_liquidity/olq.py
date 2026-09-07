#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
onchain-liquidity (OLQ) — 온체인 유동성 총량 관측·변화탐지 트랙

기존 레이더들이 못 보던 축을 담당한다:
  - narrative-radar / xrs-5track : 상대강도 → 시장이 동시에 빠지면 원리적으로 침묵
  - btc-macro-radar              : M2·실질금리·DXY = 오프체인 매크로
  - crypto-top-signal            : 센티멘트·과열 = 후행

이 트랙은 "온체인에 실제로 얼마의 돈이 있는가"의 절대 수준과 방향만 본다.

설계 원칙 (기존 시스템 교훈 반영):
  1. 수집 실패 시 "이상 없음" 발송 금지 → 판정 불가 + exit 1  (crypto-monitor v2 오탐 교훈)
  2. 임계값은 코드 상수로 고정, 성과 보고 조정 금지          (narrative-radar 사후편향 교훈)
  3. 하루 반짝은 경보로 승격하지 않음 — 2일 이상 유지된 것만  (narrative-radar v4 교훈)
  4. 외부 파이썬 의존성 0 (stdlib only)                       (pip 실패로 죽는 경로 제거)
  5. TVL은 USD 표시라 가격효과를 포함 → 가격중립 보정본을 병기
"""

import json
import os
import sys
import time
import math
import random
import datetime as dt
import urllib.request
import urllib.error
import urllib.parse

# ─────────────────────────────────────────────────────────────
# 고정 상수 — 변경 시 FROZEN_AT 갱신 필수
# ─────────────────────────────────────────────────────────────
THRESHOLDS_FROZEN_AT = "2026-09-08"
SCHEMA_VERSION = "olq-1.0"

# 스테이블코인 총공급은 매우 느리게 움직인다(일 변동 0.0~0.3%).
# 따라서 임계는 작게 잡되, 연속성 게이트로 오탐을 막는다.
TH = {
    "stable_1d_crit": -0.30,     # %  1일 총공급 급감
    "stable_7d_crit": -0.80,     # %  7일 총공급 수축
    "stable_7d_warn": -0.25,     # %
    "stable_7d_exp": 1.00,       # %  확장
    "tvl_real_7d_crit": -8.0,    # %p 가격중립 TVL(실질 예치) 수축
    "tvl_real_7d_warn": -4.0,    # %p
    "tvl_real_7d_exp": 6.0,      # %p
    "dex_z_crit": -1.80,         # 30일 자기이력 로버스트 z
    "dex_z_warn": -1.20,
    "chain_shift_pct": 3.0,      # % 체인간 스테이블 잔고 7일 이동 유의 기준
    "streak_required": 2,        # 경보 승격에 필요한 연속 일수
    "coverage_min": 0.70,        # 이 미만이면 DEGRADED
}

# 가격중립 보정용 담보 가중치 (TVL 상위 체인의 네이티브 자산 비중 근사).
# Ethereum이 전체 TVL의 약 56%, Bitcoin 계열 약 5%, 나머지는 스테이블·알트 혼재.
PRICE_BASKET = [("coingecko:ethereum", 0.70), ("coingecko:bitcoin", 0.30)]

WATCH_CHAINS = ["Ethereum", "Tron", "Solana", "BSC", "Base", "Arbitrum",
                "Hyperliquid L1", "Polygon"]

KST = dt.timezone(dt.timedelta(hours=9))
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
]
BACKOFF = [4, 10, 22, 45]

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")


# ─────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────
def fetch_json(url, timeout=90, label=""):
    """Retry-After 존중 + 지수 백오프 + UA 로테이션."""
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": random.choice(UA_POOL),
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            ra = e.headers.get("Retry-After") if e.headers else None
            if ra:
                try:
                    wait = max(wait, min(int(ra), 120))
                except ValueError:
                    pass
            print("[fetch] %s HTTP %s (attempt %d) wait %ss" % (label or url, e.code, attempt + 1, wait))
        except Exception as e:  # noqa: BLE001
            last = e
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            print("[fetch] %s %s (attempt %d) wait %ss" % (label or url, type(e).__name__, attempt + 1, wait))
        if attempt < len(BACKOFF):
            time.sleep(wait + random.uniform(0, 2))
    raise RuntimeError("fetch failed: %s (%s)" % (label or url, last))


# ─────────────────────────────────────────────────────────────
# 수집
# ─────────────────────────────────────────────────────────────
def _sum_pegged(entry):
    v = entry.get("totalCirculatingUSD") or {}
    if isinstance(v, dict):
        return float(sum(x for x in v.values() if isinstance(x, (int, float))))
    return 0.0


def fetch_stable_total():
    """전체 스테이블코인 유통량 일별 시계열 → 레벨 + 1d/7d/30d."""
    raw = fetch_json("https://stablecoins.llama.fi/stablecoincharts/all", label="stable-all")
    series = []
    for d in raw:
        try:
            ts = int(d["date"])
            series.append((ts, _sum_pegged(d)))
        except (KeyError, TypeError, ValueError):
            continue
    series = [p for p in series if p[1] > 0]
    if len(series) < 31:
        raise RuntimeError("stable series too short: %d" % len(series))
    return series


def fetch_stable_assets():
    """자산별 유통량 + 전일/전주/전월 → 순발행 분해."""
    raw = fetch_json("https://stablecoins.llama.fi/stablecoins?includePrices=true", label="stable-assets")
    out = []
    for p in raw.get("peggedAssets", []):
        def g(key):
            v = p.get(key)
            return float((v or {}).get("peggedUSD") or 0) if isinstance(v, dict) else 0.0
        cur = g("circulating")
        if cur < 1e8:                     # $100M 미만 제외 (소형 %변화 폭발 방지)
            continue
        out.append({
            "symbol": p.get("symbol", "?"),
            "cur": cur,
            "d1": g("circulatingPrevDay"),
            "d7": g("circulatingPrevWeek"),
            "d30": g("circulatingPrevMonth"),
        })
    out.sort(key=lambda x: -x["cur"])
    return out


def fetch_stable_by_chain():
    """체인별 스테이블 잔고 시계열 — 자금이 어느 체인으로 이동 중인지."""
    rows = []
    for name in WATCH_CHAINS:
        try:
            raw = fetch_json(
                "https://stablecoins.llama.fi/stablecoincharts/%s" % urllib.parse.quote(name),
                timeout=60, label="stable-%s" % name)
            s = [(int(d["date"]), _sum_pegged(d)) for d in raw if _sum_pegged(d) > 0]
            if len(s) < 8:
                continue
            cur, p1, p7 = s[-1][1], s[-2][1], s[-8][1]
            rows.append({
                "chain": name, "cur": cur,
                "p1": pct(cur, p1), "p7": pct(cur, p7),
                "abs7": cur - p7,
            })
        except Exception as e:  # noqa: BLE001
            print("[chain] %s 실패: %s" % (name, e))
        time.sleep(0.4)
    rows.sort(key=lambda x: -x["cur"])
    return rows


def fetch_tvl():
    raw = fetch_json("https://api.llama.fi/v2/historicalChainTvl", label="tvl-hist")
    s = [(int(d["date"]), float(d["tvl"])) for d in raw if d.get("tvl")]
    if len(s) < 31:
        raise RuntimeError("tvl series too short: %d" % len(s))
    return s


def fetch_price_index():
    """가격중립 보정용 담보 바스켓 지수 (DefiLlama coins — CoinGecko rate limit 회피)."""
    start = int(time.time()) - 86400 * 40
    idx = {}
    for coin, w in PRICE_BASKET:
        raw = fetch_json(
            "https://coins.llama.fi/chart/%s?start=%d&span=40&period=1d&searchWidth=6h" % (coin, start),
            timeout=60, label="price-%s" % coin)
        pts = ((raw.get("coins") or {}).get(coin) or {}).get("prices") or []
        if len(pts) < 31:
            raise RuntimeError("price series too short for %s: %d" % (coin, len(pts)))
        for p in pts:
            day = dt.datetime.fromtimestamp(int(p["timestamp"]), dt.timezone.utc).strftime("%Y-%m-%d")
            idx.setdefault(day, 0.0)
        for p in pts:                      # 로그수익 가중합으로 지수화
            day = dt.datetime.fromtimestamp(int(p["timestamp"]), dt.timezone.utc).strftime("%Y-%m-%d")
            idx[day] += w * math.log(max(float(p["price"]), 1e-9))
        time.sleep(0.4)
    return {k: math.exp(v) for k, v in sorted(idx.items())}


def fetch_dex_volume():
    raw = fetch_json(
        "https://api.llama.fi/overview/dexs?excludeTotalDataChart=false"
        "&excludeTotalDataChartBreakdown=true", label="dex")
    ch = raw.get("totalDataChart") or []
    s = [(int(a), float(b)) for a, b in ch if b]
    if len(s) < 31:
        raise RuntimeError("dex series too short: %d" % len(s))
    return s


# ─────────────────────────────────────────────────────────────
# 계산
# ─────────────────────────────────────────────────────────────
def pct(cur, prev):
    if not prev:
        return 0.0
    return (cur / prev - 1.0) * 100.0


def robust_z(values, current):
    """중앙값·MAD 기반 로버스트 z. 오늘은 기준선에서 제외(look-ahead 차단).

    평균·표준편차를 쓰지 않는 이유: 거래대금은 꼬리가 두꺼워 과거 스파이크
    1회가 기준선을 부풀리고 이후 신호를 죽인다.
    """
    base = [v for v in values if v is not None]
    if len(base) < 8:
        return None
    s = sorted(base)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    dev = sorted(abs(v - med) for v in base)
    m = len(dev)
    mad = dev[m // 2] if m % 2 else (dev[m // 2 - 1] + dev[m // 2]) / 2
    scale = 1.4826 * mad
    if scale <= 0:
        return 0.0
    return max(-3.0, min(3.0, (current - med) / scale))


def day_of(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d")


def compute(stable_series, assets, chains, tvl_series, price_idx, dex_series):
    out = {}

    # ── 1. 스테이블코인 총공급 (온체인 실탄)
    cur = stable_series[-1][1]
    out["stable"] = {
        "level": cur,
        "d1": pct(cur, stable_series[-2][1]),
        "d7": pct(cur, stable_series[-8][1]),
        "d30": pct(cur, stable_series[-31][1]),
        "net1": cur - stable_series[-2][1],
        "net7": cur - stable_series[-8][1],
        "net30": cur - stable_series[-31][1],
    }
    out["stable_series"] = [(day_of(t), v) for t, v in stable_series[-60:]]

    # ── 2. 자산별 순발행 분해
    out["assets"] = [{
        "symbol": a["symbol"], "cur": a["cur"],
        "p1": pct(a["cur"], a["d1"]), "p7": pct(a["cur"], a["d7"]),
        "p30": pct(a["cur"], a["d30"]),
        "net7": a["cur"] - a["d7"] if a["d7"] else 0.0,
    } for a in assets[:8]]

    # ── 3. 체인별 이동
    out["chains"] = chains

    # ── 4. TVL 명목 + 가격중립(실질 예치)
    #
    # 명목 TVL은 USD 표시라 담보 가격이 오르면 예치 수량이 그대로여도 늘어난다.
    # 담보 바스켓 로그수익을 차감해 "실제로 예치금이 늘었는가"를 분리한다.
    # 두 시계열의 최신일이 하루씩 어긋날 수 있으므로(원천 갱신 시차) 공통일에
    # 앵커를 맞춰 같은 구간끼리만 비교한다 — 어긋난 채 계산하면 하루치 가격
    # 변동이 통째로 실질 변화로 잘못 잡힌다.
    tvl_cur = tvl_series[-1][1]
    nom7 = pct(tvl_cur, tvl_series[-8][1])
    nom30 = pct(tvl_cur, tvl_series[-31][1])

    tvl_by_day = {day_of(t): v for t, v in tvl_series}
    real7, real30, px7_chg, anchor = None, None, None, None
    common = sorted(set(tvl_by_day) & set(price_idx or {}))
    if common:
        anchor = common[-1]
        a_dt = dt.datetime.strptime(anchor, "%Y-%m-%d")

        def back(days):
            for k in range(0, 4):          # 결측일 허용 오차 3일
                key = (a_dt - dt.timedelta(days=days + k)).strftime("%Y-%m-%d")
                if key in tvl_by_day and key in price_idx:
                    return key
            return None

        for span, label in ((7, "7"), (30, "30")):
            ref = back(span)
            if not ref:
                continue
            t_chg = pct(tvl_by_day[anchor], tvl_by_day[ref])
            p_chg = pct(price_idx[anchor], price_idx[ref])
            if label == "7":
                real7, px7_chg = t_chg - p_chg, p_chg
            else:
                real30 = t_chg - p_chg

    out["tvl"] = {
        "level": tvl_cur,
        "d1": pct(tvl_cur, tvl_series[-2][1]),
        "d7": nom7, "d30": nom30,
        "real7": real7, "real30": real30,
        "px7": px7_chg, "px_anchor": anchor,
    }
    out["tvl_series"] = [(day_of(t), v) for t, v in tvl_series[-60:]]

    # ── 5. DEX 거래대금
    dex_cur = dex_series[-1][1]
    hist = [v for _, v in dex_series[-31:-1]]
    out["dex"] = {
        "level": dex_cur,
        "avg7": sum(v for _, v in dex_series[-8:-1]) / 7.0,
        "z": robust_z(hist, dex_cur),
        "d7": pct(dex_cur, dex_series[-8][1]),
    }
    out["dex_series"] = [(day_of(t), v) for t, v in dex_series[-60:]]
    return out


# ─────────────────────────────────────────────────────────────
# 판정
# ─────────────────────────────────────────────────────────────
STATES = {
    "CRITICAL": ("🚨", "급속 수축", "온체인 실탄이 빠르게 이탈 중"),
    "CONTRACT": ("🔴", "수축", "유동성이 줄고 있음"),
    "SOFTEN":   ("🟡", "둔화", "확장이 멈추고 완만히 약화"),
    "NEUTRAL":  ("⚪", "중립", "유의미한 방향성 없음"),
    "EXPAND":   ("🟢", "확장", "온체인 실탄이 유입 중"),
}
ORDER = ["CRITICAL", "CONTRACT", "SOFTEN", "NEUTRAL", "EXPAND"]


def judge(m):
    """원시 판정 — 연속성 게이트는 apply_streak()에서 별도 적용."""
    s, t, d = m["stable"], m["tvl"], m["dex"]
    reasons = []
    hits = {"CRITICAL": 0, "CONTRACT": 0, "SOFTEN": 0, "EXPAND": 0}

    if s["d7"] <= TH["stable_7d_crit"]:
        hits["CRITICAL"] += 1
        reasons.append("스테이블 총공급 7일 %.2f%% (임계 %.2f%%)" % (s["d7"], TH["stable_7d_crit"]))
    elif s["d7"] <= TH["stable_7d_warn"]:
        hits["CONTRACT"] += 1
        reasons.append("스테이블 총공급 7일 %.2f%%" % s["d7"])
    elif s["d7"] >= TH["stable_7d_exp"]:
        hits["EXPAND"] += 1
        reasons.append("스테이블 총공급 7일 +%.2f%%" % s["d7"])

    if s["d1"] <= TH["stable_1d_crit"]:
        hits["CRITICAL"] += 1
        reasons.append("스테이블 1일 %.2f%% (하루 %.1f억$ 소각)" % (s["d1"], abs(s["net1"]) / 1e8))

    if t["real7"] is not None:
        if t["real7"] <= TH["tvl_real_7d_crit"]:
            hits["CRITICAL"] += 1
            reasons.append("가격중립 TVL 7일 %.1f%%p" % t["real7"])
        elif t["real7"] <= TH["tvl_real_7d_warn"]:
            hits["CONTRACT"] += 1
            reasons.append("가격중립 TVL 7일 %.1f%%p" % t["real7"])
        elif t["real7"] >= TH["tvl_real_7d_exp"]:
            hits["EXPAND"] += 1
            reasons.append("가격중립 TVL 7일 +%.1f%%p" % t["real7"])

    if d["z"] is not None:
        if d["z"] <= TH["dex_z_crit"]:
            hits["CONTRACT"] += 1
            reasons.append("DEX 거래대금 z %.2f (30일 대비 이례적 위축)" % d["z"])
        elif d["z"] <= TH["dex_z_warn"]:
            hits["SOFTEN"] += 1
            reasons.append("DEX 거래대금 z %.2f" % d["z"])

    if hits["CRITICAL"] >= 2:
        state = "CRITICAL"
    elif hits["CRITICAL"] >= 1 or hits["CONTRACT"] >= 2:
        state = "CONTRACT"
    elif hits["CONTRACT"] >= 1 or hits["SOFTEN"] >= 1:
        state = "SOFTEN"
    elif hits["EXPAND"] >= 2:
        state = "EXPAND"
    else:
        state = "NEUTRAL"

    if not reasons:
        reasons.append("전 지표가 임계 범위 안 — 유동성 조건 변화 없음")
    return state, reasons, hits


def apply_streak(state, state_file, day=None):
    """하루 반짝은 경보로 승격하지 않는다. 2일 이상 유지된 것만.

    narrative-radar v4의 오탐 차단 원칙을 그대로 가져왔다. 크립토 온체인
    지표는 단일일 스파이크가 흔해, 연속성 게이트가 없으면 알림 피로가 된다.

    streak은 **일 단위**로만 증가한다. 같은 날 재실행(수동 트리거·재시도)에서
    증가시키면 하루 두 번 돌리는 것만으로 2일 게이트가 뚫리고, 메시지가 매번
    달라져 멱등성도 깨진다.
    """
    day = day or today_kst()
    st = load_json(state_file, {}) or {}
    prev, streak = st.get("state"), int(st.get("streak") or 0)

    if st.get("last_day") == day and prev == state:
        streak = max(streak, 1)                  # 같은 날 재실행 — 증가시키지 않음
    else:
        streak = streak + 1 if prev == state else 1
        st["first_seen"] = st.get("first_seen") if prev == state else day

    st.update({"state": state, "streak": streak, "last_day": day})
    save_json(state_file, st)

    alerting = state in ("CRITICAL", "CONTRACT")
    if alerting and streak < TH["streak_required"]:
        return "SOFTEN", streak, True     # 강등 — 관찰 단계로만 표기
    return state, streak, False


# ─────────────────────────────────────────────────────────────
# 렌더링 — 텔레그램
# ─────────────────────────────────────────────────────────────
def money(v):
    a = abs(v)
    if a >= 1e12:
        return "$%.2fT" % (v / 1e12)
    if a >= 1e9:
        return "$%.2fB" % (v / 1e9)
    if a >= 1e6:
        return "$%.0fM" % (v / 1e6)
    return "$%.0f" % v


def signed(v, unit="%", dp=2):
    return ("%+." + str(dp) + "f%s") % (v, unit)


def dot(v, warn=0.0):
    return "🟢" if v > warn else ("🔴" if v < -abs(warn) else "⚪")


def render_telegram(m, state, reasons, streak, demoted, degraded, as_of):
    emo, name, tag = STATES[state]
    L = []
    # ── 강조 헤더: 이 트랙은 채팅 상단에 고정(pin)되므로 헤더가 곧 상태판이다
    L.append("━━━━━━━━━━━━━━━━━━")
    L.append("<b>%s  온체인 유동성 : %s</b>" % (emo, name.upper()))
    L.append("<i>%s</i>" % tag)
    L.append("━━━━━━━━━━━━━━━━━━")
    if degraded:
        L.append("⚠️ <b>DEGRADED</b> — 일부 지표 수집 실패, 판정 신뢰도 낮음")
    if demoted:
        L.append("⏳ 경보 조건이 <b>1일차</b>라 관찰 단계로만 표기 (2일 연속 시 승격)")
    L.append("")

    s = m["stable"]
    L.append("<b>💵 스테이블 총공급</b>  %s" % money(s["level"]))
    L.append("  1d %s %s   7d %s %s   30d %s %s" % (
        dot(s["d1"], 0.05), signed(s["d1"]),
        dot(s["d7"], 0.10), signed(s["d7"]),
        dot(s["d30"], 0.20), signed(s["d30"])))
    L.append("  순발행 7일 %s · 30일 %s" % (money(s["net7"]), money(s["net30"])))

    t = m["tvl"]
    L.append("")
    L.append("<b>🏦 DeFi TVL</b>  %s" % money(t["level"]))
    L.append("  명목 7d %s %s   30d %s" % (dot(t["d7"], 1.0), signed(t["d7"], "%", 1), signed(t["d30"], "%", 1)))
    if t["real7"] is not None:
        L.append("  <b>가격중립 7d %s %s</b>  <i>(가격효과 %s 제거)</i>" % (
            dot(t["real7"], 2.0), signed(t["real7"], "%p", 1), signed(t["px7"] or 0, "%", 1)))

    d = m["dex"]
    L.append("")
    L.append("<b>🔁 DEX 거래대금</b>  %s" % money(d["level"]))
    L.append("  7일평균 %s · 30일 z %s" % (
        money(d["avg7"]), "n/a" if d["z"] is None else "%s %+.2f" % (dot(d["z"], 0.5), d["z"])))

    if m["chains"]:
        mv = sorted(m["chains"], key=lambda x: -abs(x["p7"]))[:4]
        L.append("")
        L.append("<b>⛓ 체인별 스테이블 7일 이동</b>")
        for c in mv:
            L.append("  %s %-14s %s  (%s)" % (
                dot(c["p7"], 0.3), c["chain"], signed(c["p7"], "%", 2), money(c["abs7"])))

    L.append("")
    L.append("<b>📌 판정 근거</b>")
    for r in reasons[:4]:
        L.append("  · %s" % r)
    L.append("")
    L.append("<i>연속 %d일 · 임계 고정 %s</i>" % (streak, THRESHOLDS_FROZEN_AT))
    L.append("<i>기준 %s (UTC 종가) · 관측기이며 예측기가 아님</i>" % as_of)
    L.append("https://jinhae8971.github.io/korea-etf-calmar/onchain-liquidity/")
    return "\n".join(L)


def render_failure(err, as_of):
    return ("━━━━━━━━━━━━━━━━━━\n"
            "<b>⚠️  온체인 유동성 : 판정 불가</b>\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "데이터 수집에 실패했습니다. <b>“이상 없음”이 아닙니다.</b>\n\n"
            "사유: <code>%s</code>\n기준 %s" % (str(err)[:300], as_of))


# ─────────────────────────────────────────────────────────────
# 렌더링 — 대시보드 (순수 SVG, 외부 라이브러리 0)
# ─────────────────────────────────────────────────────────────
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def sparkline(series, w=680, h=120, color="#4ade80"):
    if len(series) < 2:
        return "<p>데이터 부족</p>"
    vals = [v for _, v in series]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = w / (len(vals) - 1)
    pts = " ".join("%.1f,%.1f" % (i * step, h - (v - lo) / rng * (h - 16) - 8)
                   for i, v in enumerate(vals))
    area = "0,%d %s %.1f,%d" % (h, pts, w, h)
    return ("<svg viewBox='0 0 %d %d' preserveAspectRatio='none' class='spark'>"
            "<polygon points='%s' fill='%s' opacity='0.13'/>"
            "<polyline points='%s' fill='none' stroke='%s' stroke-width='2'/></svg>"
            % (w, h, area, color, pts, color))


def render_dashboard(m, state, reasons, streak, degraded, as_of):
    emo, name, tag = STATES[state]
    s, t, d = m["stable"], m["tvl"], m["dex"]
    color = {"CRITICAL": "#ef4444", "CONTRACT": "#f97316", "SOFTEN": "#eab308",
             "NEUTRAL": "#94a3b8", "EXPAND": "#22c55e"}[state]

    rows = "".join(
        "<tr><td>%s</td><td class='n'>%s</td><td class='n %s'>%s</td>"
        "<td class='n %s'>%s</td><td class='n'>%s</td></tr>" % (
            esc(a["symbol"]), money(a["cur"]),
            "up" if a["p1"] >= 0 else "dn", signed(a["p1"]),
            "up" if a["p7"] >= 0 else "dn", signed(a["p7"]), money(a["net7"]))
        for a in m["assets"])

    crows = "".join(
        "<tr><td>%s</td><td class='n'>%s</td><td class='n %s'>%s</td>"
        "<td class='n %s'>%s</td><td class='n'>%s</td></tr>" % (
            esc(c["chain"]), money(c["cur"]),
            "up" if c["p1"] >= 0 else "dn", signed(c["p1"]),
            "up" if c["p7"] >= 0 else "dn", signed(c["p7"]), money(c["abs7"]))
        for c in m["chains"])

    return """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>온체인 유동성 레이더</title><style>
*{box-sizing:border-box}body{margin:0;padding:18px;background:#0b1220;color:#e2e8f0;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif;line-height:1.55}
.wrap{max-width:860px;margin:0 auto}
.hero{border:2px solid %s;border-radius:14px;padding:20px;background:rgba(255,255,255,.03);margin-bottom:20px}
.badge{font-size:28px;font-weight:800;color:%s;letter-spacing:-.5px}
.sub{color:#94a3b8;font-size:14px;margin-top:4px}
h2{font-size:15px;margin:26px 0 10px;color:#cbd5e1;border-left:3px solid %s;padding-left:9px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.card{background:rgba(255,255,255,.04);border-radius:10px;padding:14px}
.k{font-size:12px;color:#94a3b8}.v{font-size:20px;font-weight:700;margin-top:2px}
.up{color:#22c55e}.dn{color:#ef4444}
table{width:100%%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{padding:7px 8px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left}
th{color:#94a3b8;font-weight:600;font-size:12px}.n{text-align:right;font-variant-numeric:tabular-nums}
.spark{width:100%%;height:120px;display:block;margin-top:6px}
ul{margin:8px 0;padding-left:18px;font-size:13px;color:#cbd5e1}
.foot{margin-top:28px;font-size:12px;color:#64748b;border-top:1px solid rgba(255,255,255,.08);padding-top:12px}
.warn{background:rgba(239,68,68,.14);border:1px solid #ef4444;border-radius:8px;padding:10px;font-size:13px;margin-bottom:14px}
</style></head><body><div class="wrap">
%s
<div class="hero"><div class="badge">%s 온체인 유동성 — %s</div>
<div class="sub">%s · 연속 %d일 · 기준 %s (UTC 종가)</div>
<ul>%s</ul></div>

<div class="grid">
<div class="card"><div class="k">스테이블 총공급</div><div class="v">%s</div>
<div class="k %s">7일 %s · 30일 %s</div></div>
<div class="card"><div class="k">DeFi TVL (명목)</div><div class="v">%s</div>
<div class="k %s">7일 %s · 30일 %s</div></div>
<div class="card"><div class="k">TVL 가격중립 (실질 예치)</div><div class="v %s">%s</div>
<div class="k">가격효과 %s 제거</div></div>
<div class="card"><div class="k">DEX 일 거래대금</div><div class="v">%s</div>
<div class="k">30일 z %s · 7일평균 %s</div></div>
</div>

<h2>스테이블코인 총공급 (60일)</h2>%s
<h2>DeFi TVL (60일)</h2>%s
<h2>DEX 거래대금 (60일)</h2>%s

<h2>스테이블코인 자산별 순발행</h2>
<table><thead><tr><th>자산</th><th class="n">유통량</th><th class="n">1일</th>
<th class="n">7일</th><th class="n">7일 순증</th></tr></thead><tbody>%s</tbody></table>

<h2>체인별 스테이블 잔고 이동</h2>
<table><thead><tr><th>체인</th><th class="n">잔고</th><th class="n">1일</th>
<th class="n">7일</th><th class="n">7일 순증</th></tr></thead><tbody>%s</tbody></table>

<div class="foot">
<b>이 트랙이 담당하는 축.</b> 상대강도 기반 레이더(내러티브·XRS)는 시장이 동시에 움직이면
순위가 변하지 않아 원리적으로 침묵합니다. BTC 매크로 레이더는 M2·실질금리 등 오프체인
지표만 봅니다. 이 트랙은 온체인에 실제로 존재하는 자금의 <b>절대 수준과 방향</b>만 봅니다.<br><br>
<b>한계.</b> TVL은 펀더멘털이 아니라 예치금이며 인센티브 파밍·중복계상을 포함합니다.
명목 TVL은 USD 표시라 가격 변동을 그대로 반영하므로, 담보 바스켓(ETH 70%% / BTC 30%%)
로그수익을 차감한 가격중립본을 병기합니다. 이 보정은 근사이며 알트 담보 비중은 반영하지 않습니다.
스테이블 총공급은 거래소 내부 이동을 구분하지 못합니다.
<b>관측기이며 예측기가 아닙니다</b> — 점수는 미래 수익률을 주장하지 않습니다.<br><br>
임계 고정 %s · 스키마 %s · 원천 DefiLlama
</div></div></body></html>""" % (
        color, color, color,
        '<div class="warn">⚠️ DEGRADED — 일부 지표 수집 실패로 판정 신뢰도가 낮습니다.</div>' if degraded else "",
        emo, name, tag, streak, as_of,
        "".join("<li>%s</li>" % esc(r) for r in reasons),
        money(s["level"]), "up" if s["d7"] >= 0 else "dn", signed(s["d7"]), signed(s["d30"]),
        money(t["level"]), "up" if t["d7"] >= 0 else "dn", signed(t["d7"], "%", 1), signed(t["d30"], "%", 1),
        "up" if (t["real7"] or 0) >= 0 else "dn",
        "n/a" if t["real7"] is None else signed(t["real7"], "%p", 1),
        "n/a" if t["px7"] is None else signed(t["px7"], "%", 1),
        money(d["level"]), "n/a" if d["z"] is None else "%+.2f" % d["z"], money(d["avg7"]),
        sparkline(m["stable_series"], color="#60a5fa"),
        sparkline(m["tvl_series"], color="#4ade80"),
        sparkline(m["dex_series"], color="#c084fc"),
        rows, crows, THRESHOLDS_FROZEN_AT, SCHEMA_VERSION)


# ─────────────────────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def today_kst():
    return dt.datetime.now(KST).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────
def main():
    docs = os.environ.get("OLQ_DOCS_DIR",
                          os.path.abspath(os.path.join(HERE, "..", "docs", "onchain-liquidity")))
    latest_path = os.path.join(DATA_DIR, "latest.json")
    state_path = os.path.join(DATA_DIR, "state.json")
    hist_path = os.path.join(DATA_DIR, "history.json")

    as_of = today_kst()
    parts, degraded_reasons = {}, []

    # 필수 지표 — 실패하면 전체 판정 불가 (오탐 차단 원칙)
    try:
        parts["stable"] = fetch_stable_total()
        parts["tvl"] = fetch_tvl()
    except Exception as e:  # noqa: BLE001
        msg = render_failure(e, as_of)
        save_json(latest_path, {"schema": SCHEMA_VERSION, "as_of": as_of,
                                "data_status": "FAILED", "state": "UNKNOWN",
                                "alert": True, "pin": True, "message": msg})
        print("[FATAL] 필수 지표 수집 실패: %s" % e)
        return 1

    # 보조 지표 — 실패해도 DEGRADED로 계속
    for key, fn in (("assets", fetch_stable_assets), ("chains", fetch_stable_by_chain),
                    ("price", fetch_price_index), ("dex", fetch_dex_volume)):
        try:
            parts[key] = fn()
        except Exception as e:  # noqa: BLE001
            print("[warn] %s 수집 실패: %s" % (key, e))
            degraded_reasons.append(key)
            parts[key] = [] if key in ("assets", "chains") else ({} if key == "price" else None)

    if parts["dex"] is None:
        parts["dex"] = [(int(time.time()) - 86400 * i, 0.0) for i in range(31, 0, -1)]

    coverage = 1.0 - len(degraded_reasons) / 4.0
    degraded = coverage < TH["coverage_min"]

    m = compute(parts["stable"], parts["assets"], parts["chains"],
                parts["tvl"], parts["price"], parts["dex"])
    raw_state, reasons, hits = judge(m)
    state, streak, demoted = apply_streak(raw_state, state_path)

    message = render_telegram(m, state, reasons, streak, demoted, degraded, as_of)
    html = render_dashboard(m, state, reasons, streak, degraded, as_of)

    alerting = state in ("CRITICAL", "CONTRACT")
    payload = {
        "schema": SCHEMA_VERSION, "as_of": as_of,
        "data_status": "DEGRADED" if degraded else "OK",
        "coverage": round(coverage, 3),
        "state": state, "raw_state": raw_state, "streak": streak,
        "demoted": demoted, "reasons": reasons, "hits": hits,
        "alert": alerting,
        "pin": True,                 # 이 트랙은 항상 채팅 상단 고정
        "silent": not alerting,      # 경보가 아니면 무음 발송 (알림 피로 방지)
        "metrics": {k: v for k, v in m.items() if not k.endswith("_series")},
        "message": message,
        "thresholds_frozen_at": THRESHOLDS_FROZEN_AT,
    }

    prev = load_json(latest_path, {}) or {}
    if prev.get("message") != message or prev.get("as_of") != as_of:
        save_json(latest_path, payload)
    save_text(os.path.join(docs, "index.html"), html)

    hist = load_json(hist_path, []) or []
    entry = {"as_of": as_of, "state": state,
             "stable": m["stable"]["level"], "stable_7d": round(m["stable"]["d7"], 3),
             "tvl": m["tvl"]["level"], "tvl_real_7d": m["tvl"]["real7"],
             "dex": m["dex"]["level"], "dex_z": m["dex"]["z"]}
    hist = [h for h in hist if h.get("as_of") != as_of] + [entry]
    save_json(hist_path, hist[-400:])

    print("[OK] state=%s streak=%d coverage=%.2f stable7=%.2f%% tvl_real7=%s dex_z=%s"
          % (state, streak, coverage, m["stable"]["d7"],
             m["tvl"]["real7"], m["dex"]["z"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
