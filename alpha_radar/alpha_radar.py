#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Alpha Trend Radar
=========================
바이낸스 알파 마켓 상장 토큰 중 '차트 구조 + 수급'이 동시에 개선된 종목을 선별해
GitHub Pages 대시보드와 텔레그램 브리프로 보고한다.

설계 원칙 (narrative-radar / consensus-gap 에서 이어받음)
  1) 이 시스템은 예측기가 아니라 **관측기**다. 점수는 "지금 자금이 어디에 반응하고
     있고, 그 자리가 차트 구조상 어디인가"의 서술이며 미래 수익률을 주장하지 않는다.
  2) 유니버스는 고정목록이 아니라 **규칙**으로 매 실행 재구성한다. 알파 마켓은 회전이
     빨라 고정목록을 쓰면 신규 상장이 원리적으로 누락된다.
  3) 수집 실패 시 "이상 없음"을 보내지 않는다. **판정 불가**를 명시하고 실패 처리한다.
  4) 하루 반짝 뜬 종목은 알림으로 승격하지 않는다. **2일 이상 유지**된 것만 승격한다.

외부 파이썬 의존성 없음(stdlib only).
"""

import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import tracking

# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
KST = timezone(timedelta(hours=9))
UTC = timezone.utc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
THEMES_PATH = os.path.join(BASE_DIR, "themes.json")

TOKEN_LIST_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
KLINES_PATH = "/bapi/defi/v1/public/alpha-trade/klines"
HOSTS = ["https://www.binance.com", "https://www.binance.info"]
BENCH_HOSTS = ["https://data-api.binance.vision"]  # api.binance.com 은 러너/컨테이너 IP에서 451

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]

BACKOFF = [3, 8, 20, 45]
REQ_GAP = 0.15          # klines 연속 호출 간격(초)
HISTORY_KEEP_DAYS = 90
TOP_N_TELEGRAM = 5
TOP_N_DASHBOARD = 20

DASHBOARD_URL = "https://jinhae8971.github.io/korea-etf-calmar/alpha-radar/"

BADGE = {
    "POSITIVE": "과거검증에서 약한 우위 관측 (상세는 대시보드 검증 탭)",
    "RELATIVE_ONLY": "유니버스 대비 상대우위만 확인 — 절대수익은 마이너스였음. 매수신호 아님",
    "INCONCLUSIVE": "과거검증 결과 유의미한 우위 없음 (신뢰구간이 0을 포함)",
    "NEGATIVE": "과거검증에서 우위 없음 — 순위를 매매 근거로 쓰지 말 것",
    "UNKNOWN": "검증 표본 부족 — 판정 불가",
}


# ----------------------------------------------------------------------------
# 유틸
# ----------------------------------------------------------------------------
def now_utc():
    return datetime.now(UTC)


def fnum(v, default=0.0):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def robust_z(values, clip=3.0):
    """중앙값·MAD 기반 z. 크립토의 극단치가 평균·표준편차를 지배하는 문제를 피한다."""
    if not values:
        return []
    med = median(values)
    mad = median([abs(v - med) for v in values])
    scale = mad * 1.4826
    if scale <= 1e-12:
        rng = (max(values) - min(values)) or 1.0
        return [clamp((v - med) / rng * 2.0, -clip, clip) for v in values]
    return [clamp((v - med) / scale, -clip, clip) for v in values]


def http_get_json(url, timeout=25, tries=4):
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": random.choice(UA_POOL),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 451:                     # 지역 차단 — 재시도해도 동일
                raise RuntimeError("HTTP 451 지역차단(restricted location)") from e
            retry_after = e.headers.get("Retry-After") if e.headers else None
            wait = fnum(retry_after, 0) or BACKOFF[min(i, len(BACKOFF) - 1)]
            if i < tries - 1:
                time.sleep(wait + random.uniform(0, 1.5))
        except Exception as e:                    # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)] + random.uniform(0, 1.5))
    raise RuntimeError("요청 실패: %s (%r)" % (url.split("?")[0], last))


def fetch_with_hosts(path, query="", hosts=None):
    hosts = hosts or HOSTS
    errs = []
    for h in hosts:
        url = h + path + (("?" + query) if query else "")
        try:
            return http_get_json(url), h
        except Exception as e:                    # noqa: BLE001
            errs.append("%s: %s" % (h, e))
    raise RuntimeError(" | ".join(errs))


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                             # noqa: BLE001
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


SAFE_TEXT = re.compile(r"[^\w \-\.\+\:\(\)\[\]/&,'\u3000-\u9fff\uac00-\ud7a3]")


def safe_label(s, limit=18):
    """토큰 이름은 외부 입력이다. 메시지·HTML 인젝션 경로를 차단한다."""
    s = SAFE_TEXT.sub("", str(s or ""))[:limit].strip()
    return s or "?"


def esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ----------------------------------------------------------------------------
# 수집
# ----------------------------------------------------------------------------
def fetch_token_list():
    payload, host = fetch_with_hosts(TOKEN_LIST_PATH)
    if not payload.get("success") or not isinstance(payload.get("data"), list):
        raise RuntimeError("토큰 리스트 응답 이상: %s" % str(payload.get("code")))
    return payload["data"], host


def fetch_klines(alpha_id, interval="1d", limit=120, host=None):
    q = "symbol=%sUSDT&interval=%s&limit=%d" % (alpha_id, interval, limit)
    hosts = [host] + [h for h in HOSTS if h != host] if host else HOSTS
    payload, _ = fetch_with_hosts(KLINES_PATH, q, hosts=hosts)
    if not payload.get("success"):
        return []
    rows = payload.get("data") or []
    out = []
    for r in rows:
        try:
            out.append({
                "t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                "c": float(r[4]), "v": float(r[5]), "ct": int(r[6]),
                "qv": float(r[7]), "n": int(float(r[8])),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


def fetch_benchmark(symbol="BNBUSDT"):
    """벤치마크(BNB/BTC) 일봉. 실패해도 시스템은 계속 간다."""
    try:
        payload, _ = fetch_with_hosts(
            "/api/v3/klines", "symbol=%s&interval=1d&limit=35" % symbol, hosts=BENCH_HOSTS)
        closes = [float(r[4]) for r in payload]
        return closes
    except Exception:                             # noqa: BLE001
        return []


# ----------------------------------------------------------------------------
# 지표
# ----------------------------------------------------------------------------
def closed_bars(bars, now_ms=None):
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return [b for b in bars if b["ct"] <= now_ms]


def ema(xs, span):
    if not xs:
        return []
    k = 2.0 / (span + 1.0)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def pct_change(xs, n):
    if len(xs) <= n or xs[-1 - n] <= 0:
        return None
    return xs[-1] / xs[-1 - n] - 1.0


def true_ranges(bars):
    tr = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def linreg_r2(ys):
    """로그가격 선형적합의 R²와 기울기 부호. 매끄러운 추세 vs 단발 스파이크 구분용."""
    n = len(ys)
    if n < 5:
        return 0.0, 0.0
    xs = list(range(n))
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    if sxx <= 0:
        return 0.0, 0.0
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in ys)
    if syy <= 0:
        return 0.0, slope
    r2 = (sxy ** 2) / (sxx * syy)
    return clamp(r2, 0.0, 1.0), slope


def chart_features(bars):
    """일봉 종가 기준 차트 구조 지표. 미완성 봉은 이미 제거된 상태로 들어온다."""
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    qvs = [b["qv"] for b in bars]
    n = len(closes)
    f = {"bars": n}

    f["ret7"] = pct_change(closes, 7)
    f["ret30"] = pct_change(closes, 30) if n > 30 else pct_change(closes, n - 1)

    e20 = ema(closes, 20)
    e50 = ema(closes, 50) if n >= 30 else e20
    f["above_ema20"] = closes[-1] > e20[-1]
    f["ema_stack"] = e20[-1] > e50[-1]
    f["ema20_up"] = len(e20) > 5 and e20[-1] > e20[-6]
    f["struct"] = (0.5 * f["above_ema20"] + 0.3 * f["ema_stack"] + 0.2 * f["ema20_up"])

    win_h = highs[-60:] if n >= 60 else highs
    f["near_high"] = closes[-1] / max(win_h) if max(win_h) > 0 else 0.0
    win30 = highs[-30:] if n >= 30 else highs
    f["dd30"] = closes[-1] / max(win30) - 1.0 if max(win30) > 0 else 0.0
    prev20 = closes[-21:-1] if n >= 21 else closes[:-1]
    f["new_high20"] = bool(prev20) and closes[-1] >= max(prev20)

    tr = true_ranges(bars)
    px = closes[-1] or 1.0
    f["atr10"] = (sum(tr[-10:]) / len(tr[-10:]) / px) if len(tr) >= 10 else None
    # 압축도는 '돌파 직전' 구간으로 잰다. 최근 3일을 포함하면 돌파 당일의 ATR 확장이
    # 압축 신호를 스스로 지워버린다(설계 초기 실측으로 발견).
    recent = tr[-13:-3] if len(tr) >= 20 else []
    base = tr[-43:-13] if len(tr) >= 20 else []
    a_r = (sum(recent) / len(recent)) if recent else None
    a_b = (sum(base) / len(base)) if base else None
    f["squeeze"] = (a_r / a_b) if (a_r and a_b and a_b > 0) else None

    r2, slope = linreg_r2([math.log(max(c, 1e-12)) for c in closes[-30:]])
    f["r2"] = r2 if slope > 0 else 0.0

    v5 = sum(qvs[-5:]) / max(1, len(qvs[-5:]))
    v20 = sum(qvs[-20:]) / max(1, len(qvs[-20:]))
    f["volx"] = (v5 / v20) if v20 > 0 else None
    f["adv20"] = v20
    f["trades5"] = sum(b["n"] for b in bars[-5:]) / max(1, len(bars[-5:]))
    return f


def confirm_4h(rows, host, top=12):
    """상위 후보만 4시간봉으로 단기 구조를 확인한다(표시 전용 — 점수에는 넣지 않는다).
    일봉 신호가 4h에서 이미 무너져 있는 경우를 눈으로 걸러내기 위한 보조 정보."""
    for r in rows[:top]:
        r["c4h"] = None
        try:
            bars = closed_bars(fetch_klines(r["alpha_id"], "4h", 120, host))
        except Exception:                         # noqa: BLE001
            continue
        finally:
            time.sleep(REQ_GAP)
        closes = [b["c"] for b in bars]
        if len(closes) < 30:
            continue
        e20 = ema(closes, 20)
        r["c4h"] = {
            "above_ema20": closes[-1] > e20[-1],
            "high25": closes[-1] >= max(closes[-25:-1]),
            "ret24h": pct_change(closes, 6),
        }
    return rows


def snapshot_features(tok):
    mc = fnum(tok.get("marketCap"))
    liq = fnum(tok.get("liquidity"))
    vol = fnum(tok.get("volume24h"))
    fdv = fnum(tok.get("fdv"))
    circ = fnum(tok.get("circulatingSupply"))
    tot = fnum(tok.get("totalSupply"))
    return {
        "price": fnum(tok.get("price")),
        "mc": mc, "liq": liq, "vol24": vol, "fdv": fdv,
        "holders": int(fnum(tok.get("holders"))),
        "turnover": (vol / mc) if mc > 0 else 0.0,
        "liq_ratio": (liq / mc) if mc > 0 else 0.0,
        "churn": (vol / liq) if liq > 0 else 0.0,
        "float_ratio": (circ / tot) if tot > 0 else 1.0,
        "mc_fdv": (mc / fdv) if fdv > 0 else 1.0,
        "mul_point": fnum(tok.get("mulPoint"), 1.0),
        "listing_ms": int(fnum(tok.get("listingTime"))),
        "chain": str(tok.get("chainName") or "?"),
        "cex": bool(tok.get("listingCex")),
    }


# ----------------------------------------------------------------------------
# 테마
# ----------------------------------------------------------------------------
def tag_theme(name, symbol, themes):
    text = ("%s %s" % (name or "", symbol or "")).lower()
    best = None
    for th in themes:
        for kw in th["keywords"]:
            if kw in text:
                # 더 긴 키워드가 우선 (우연한 부분일치 완화)
                if best is None or len(kw) > best[1]:
                    best = (th["key"], len(kw))
    return best[0] if best else "UNTAGGED"


# ----------------------------------------------------------------------------
# 점수
# ----------------------------------------------------------------------------
def compute_scores(rows, hist_prev):
    """rows: 종목 dict 목록(feature 포함). 횡단면 z를 계산해 점수를 채운다."""
    if not rows:
        return rows, {}

    z_ret30 = robust_z([r["f"]["ret30"] or 0.0 for r in rows])
    z_ret7 = robust_z([r["f"]["ret7"] or 0.0 for r in rows])
    z_turn = robust_z([math.log1p(max(r["s"]["turnover"], 0)) for r in rows])
    z_liqr = robust_z([math.log1p(max(r["s"]["liq_ratio"], 0)) for r in rows])

    has_hist = bool(hist_prev)
    z_hol, z_liqchg = [0.0] * len(rows), [0.0] * len(rows)
    if has_hist:
        hol_chg, liq_chg = [], []
        for r in rows:
            p = hist_prev.get(r["alpha_id"]) or {}
            h0, l0 = fnum(p.get("hol")), fnum(p.get("liq"))
            hol_chg.append((r["s"]["holders"] / h0 - 1.0) if h0 > 0 else 0.0)
            liq_chg.append((r["s"]["liq"] / l0 - 1.0) if l0 > 0 else 0.0)
        z_hol = robust_z(hol_chg)
        z_liqchg = robust_z(liq_chg)
        for i, r in enumerate(rows):
            r["hol_chg"], r["liq_chg"] = hol_chg[i], liq_chg[i]

    for i, r in enumerate(rows):
        f, s = r["f"], r["s"]
        struct_c = f["struct"] * 2 - 1
        nh = clamp((f["near_high"] - 0.55) / 0.45, 0.0, 1.0) * 2 - 1
        r2c = f["r2"] * 2 - 1
        chart = (0.30 * z_ret30[i] + 0.20 * z_ret7[i] +
                 0.20 * struct_c + 0.15 * nh + 0.15 * r2c)

        volx = f["volx"] if f["volx"] is not None else 1.0
        volx_c = clamp((volx - 1.0) / 0.8, -1.0, 1.5)
        if has_hist:
            flow = (0.32 * z_turn[i] + 0.23 * volx_c + 0.15 * z_liqr[i] +
                    0.18 * z_hol[i] + 0.12 * z_liqchg[i])
        else:
            flow = (0.45 * z_turn[i] + 0.32 * volx_c + 0.23 * z_liqr[i])

        r["chart"] = chart
        r["flow"] = flow
        r["base"] = 0.60 * chart + 0.40 * flow

    # 테마 강도(구성종목 base 의 중앙값) → fit 은 0.55~1.00 가중혼합. 부호를 뒤집지 않는다.
    by_theme = {}
    for r in rows:
        by_theme.setdefault(r["theme"], []).append(r)
    theme_stat = {}
    for k, members in by_theme.items():
        bases = [m["base"] for m in members]
        rets = [(m["f"]["ret7"] or 0.0) for m in members]
        theme_stat[k] = {
            "key": k, "n": len(members),
            "median_base": median(bases),
            "median_ret7": median(rets),
            "breadth": sum(1 for m in members if m["base"] > 0) / len(members),
        }
    # UNTAGGED 는 테마가 아니라 '미매칭 잔여집합'이다. 정규화에서 빼고 중립 fit 을 준다.
    strengths = [v["median_base"] for k, v in theme_stat.items() if k != "UNTAGGED" and v["n"] >= 3]
    lo, hi = (min(strengths), max(strengths)) if strengths else (0.0, 1.0)
    span = (hi - lo) or 1.0
    for k, v in theme_stat.items():
        if k == "UNTAGGED" or v["n"] < 3:
            v["fit"] = 0.775
        else:
            v["fit"] = 0.55 + 0.45 * clamp((v["median_base"] - lo) / span, 0.0, 1.0)

    for r in rows:
        st = theme_stat.get(r["theme"], {"fit": 0.775})
        r["theme_fit"] = st["fit"]
        pen, flags = 1.0, []
        s, f = r["s"], r["f"]
        if s["churn"] >= 100:
            pen *= 0.70
            flags.append("WASH_SUSPECT")
        if s["liq"] < 500000:
            pen *= 0.80
            flags.append("THIN_LIQ")
        if s["float_ratio"] < 0.30:
            pen *= 0.85
            flags.append("LOW_FLOAT")
        if (f["ret30"] or 0) > 3.0:
            pen *= 0.70
            flags.append("OVERHEATED")
        if s["mul_point"] > 1:
            flags.append("POINT_EVENT")
        if s["cex"]:
            flags.append("CEX_LISTED")
        r["penalty"] = pen
        r["flags"] = flags
        r["score"] = r["base"] * r["theme_fit"] * pen

    rows.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows, theme_stat


# ----------------------------------------------------------------------------
# 이벤트 · 승격
# ----------------------------------------------------------------------------
def detect_events(rows, state, today):
    """하루 반짝은 승격하지 않는다. streak >= 2 만 '추세 후보'가 된다."""
    events = []
    top_ids = {r["alpha_id"] for r in rows[:10]}
    new_state = {}

    for r in rows:
        aid = r["alpha_id"]
        prev = state.get(aid, {})
        in_top = aid in top_ids
        streak = (prev.get("streak", 0) + 1) if in_top else 0
        entry = {
            "streak": streak,
            "rank": r["rank"],
            "prev_rank": prev.get("rank"),
            "first_seen": prev.get("first_seen") or (today if in_top else None),
            "last_date": today,
        }
        if not in_top:
            entry["first_seen"] = None
        new_state[aid] = entry
        r["streak"] = streak
        r["prev_rank"] = prev.get("rank")

        if in_top and prev.get("streak", 0) == 0:
            events.append({"type": "TOP10_ENTRY", "symbol": r["symbol"], "rank": r["rank"],
                           "detail": "상위 10 신규 진입 (승격 대기: 2일 유지 필요)"})
        if prev.get("rank") and r["rank"] <= 10 and prev["rank"] - r["rank"] >= 5:
            events.append({"type": "RANK_JUMP", "symbol": r["symbol"], "rank": r["rank"],
                           "detail": "%d위 → %d위" % (prev["rank"], r["rank"])})
        f = r["f"]
        sq = f["squeeze"]
        if f["new_high20"] and (f["volx"] or 0) >= 1.5 and (sq is None or sq <= 1.2):
            events.append({"type": "BREAKOUT", "symbol": r["symbol"], "rank": r["rank"],
                           "detail": "20일 신고가 + 거래대금 %.1f배%s" % (
                               f["volx"] or 0,
                               " (직전 변동성 압축 %.2f)" % sq if (sq is not None and sq < 0.8) else "")})
        if r.get("hol_chg") is not None and r.get("hol_chg", 0) >= 0.10 and r["rank"] <= 30:
            events.append({"type": "HOLDER_SURGE", "symbol": r["symbol"], "rank": r["rank"],
                           "detail": "홀더 +%.0f%%" % (r["hol_chg"] * 100)})

    for aid, prev in state.items():
        if prev.get("streak", 0) >= 2 and new_state.get(aid, {}).get("streak", 0) == 0:
            sym = next((r["symbol"] for r in rows if r["alpha_id"] == aid), aid)
            events.append({"type": "TOP10_EXIT", "symbol": sym, "rank": None,
                           "detail": "상위 10 이탈 (%d일 유지 후)" % prev.get("streak", 0)})
    return events, new_state


# ----------------------------------------------------------------------------
# 렌더링
# ----------------------------------------------------------------------------
def fmt_pct(x, digits=0):
    if x is None:
        return "n/a"
    return ("%+." + str(digits) + "f%%") % (x * 100)


def fmt_usd(x):
    if x >= 1e9:
        return "$%.1fB" % (x / 1e9)
    if x >= 1e6:
        return "$%.1fM" % (x / 1e6)
    if x >= 1e3:
        return "$%.0fK" % (x / 1e3)
    return "$%.0f" % x


FLAG_KO = {
    "WASH_SUSPECT": "회전이상", "THIN_LIQ": "유동성얕음", "LOW_FLOAT": "유통물량적음",
    "OVERHEATED": "과열", "POINT_EVENT": "포인트이벤트", "CEX_LISTED": "현물상장",
}


def render_telegram(payload):
    p = payload
    L = []
    L.append("🛰️ <b>알파 추세 레이더</b>  %s KST" % p["as_of_kst"][:16])

    if p["data_status"] != "OK":
        L.append("⚠️ 데이터 상태: <b>%s</b> — %s" % (p["data_status"], esc_html(p.get("status_note", ""))))
        if p["data_status"] == "FAILED":
            L.append("판정 불가. 이번 회차 결과를 신뢰하지 마세요.")
            return "\n".join(L)

    m = p["market"]
    L.append("유니버스 %d종목 · 커버리지 %d%% · 알파 중앙 7일 %s%s"
             % (p["universe_size"], round(p["coverage"] * 100), fmt_pct(m["median_ret7"], 1),
                (" · BNB %s" % fmt_pct(m["bnb_ret7"], 1)) if m.get("bnb_ret7") is not None else ""))
    L.append("국면: <b>%s</b>" % esc_html(m["regime"]))
    L.append("")

    promoted = [r for r in p["candidates"] if r["streak"] >= 2][:TOP_N_TELEGRAM]
    if promoted:
        L.append("🎯 <b>추세 후보 (2일 이상 유지)</b>")
        for r in promoted:
            flags = " ".join("⚠%s" % FLAG_KO.get(x, x) for x in r["flags"] if x in
                             ("WASH_SUSPECT", "THIN_LIQ", "LOW_FLOAT", "OVERHEATED"))
            L.append("%d. <b>%s</b> (%s) · %.2f점 · %d일차" %
                     (r["rank"], esc_html(r["symbol"]), r["chain"], r["score"], r["streak"]))
            L.append("   7일 %s / 30일 %s · 60일고점比 %d%% · 거래대금 %.1f배" %
                     (fmt_pct(r["ret7"]), fmt_pct(r["ret30"]), round(r["near_high"] * 100),
                      r["volx"] or 0))
            c4 = r.get("c4h")
            if c4:
                L.append("   4h구조: EMA20 %s · 최근25봉 고점 %s" %
                         ("위 ✔" if c4["above_ema20"] else "아래 ✘",
                          "갱신 ✔" if c4["high25"] else "미갱신 -"))
            L.append("   시총 %s · 유동성 %s · 회전 %.1f · 홀더 %s%s" %
                     (fmt_usd(r["mc"]), fmt_usd(r["liq"]), r["turnover"],
                      "{:,}".format(r["holders"]),
                      ("  " + flags) if flags else ""))
    else:
        L.append("🎯 <b>추세 후보</b>: 승격 조건(상위10 · 2일 유지) 충족 0건")
        top1 = p["candidates"][:3]
        if top1:
            L.append("   오늘 관찰: " + ", ".join("%s(%.2f, %d일차)" %
                     (esc_html(r["symbol"]), r["score"], r["streak"]) for r in top1))

    L.append("")
    th = p["themes"][:3]
    if th:
        L.append("🧭 <b>테마 상대강도</b> (중앙 7일)")
        L.append("   " + " · ".join("%s %s(%d)" % (esc_html(t["label"]), fmt_pct(t["median_ret7"], 0), t["n"])
                                    for t in th))

    ev = [e for e in p["events"] if e["type"] in ("BREAKOUT", "RANK_JUMP", "HOLDER_SURGE")][:4]
    if ev:
        L.append("")
        L.append("📌 <b>변화 감지</b>")
        for e in ev:
            L.append("   · %s %s — %s" % (esc_html(e["symbol"]), e["type"], esc_html(e["detail"])))

    bt = p.get("backtest_badge")
    if bt:
        L.append("")
        L.append("🔬 <b>검증</b>: %s" % esc_html(bt))

    L.append("")
    L.append("📊 <a href=\"%s\">전체 순위·백테스트 대시보드</a>" % DASHBOARD_URL)
    L.append("<i>관측 리포트입니다. 점수는 '지금 자금이 반응한 자리'의 서술이며 미래 수익률을 주장하지 않습니다. "
             "알파 마켓은 유동성이 얕고 포인트 파밍성 거래가 섞여 있어 회전이상·유동성얕음 표시를 반드시 확인하세요.</i>")
    return "\n".join(L)


def bar_svg(rows, width=760):
    if not rows:
        return ""
    h = 26 * len(rows) + 20
    mx = max(abs(r["score"]) for r in rows) or 1.0
    mid = width * 0.42
    out = ['<svg viewBox="0 0 %d %d" width="100%%" role="img">' % (width, h)]
    for i, r in enumerate(rows):
        y = 10 + i * 26
        w = abs(r["score"]) / mx * (width - mid - 120)
        x = mid if r["score"] >= 0 else mid - w
        color = "#2e9e6b" if r["score"] >= 0 else "#c1524b"
        out.append('<text x="6" y="%d" font-size="13" fill="#ddd">%d. %s</text>'
                   % (y + 14, r["rank"], esc_html(r["symbol"])))
        out.append('<rect x="%.1f" y="%d" width="%.1f" height="15" fill="%s" rx="2"/>' % (x, y + 3, w, color))
        out.append('<text x="%.1f" y="%d" font-size="12" fill="#aaa">%.2f</text>'
                   % (mid + max(w, 4) + 8 if r["score"] >= 0 else mid + 8, y + 15, r["score"]))
    out.append("</svg>")
    return "".join(out)


def _pct_cell(v, digits=1):
    if v is None:
        return "n/a"
    return ("%+." + str(digits) + "f%%") % (v * 100)


def render_backtest_section(bt):
    if not bt:
        return ("<h2>검증</h2><p class=note>백테스트가 아직 실행되지 않았습니다. "
                "주간 백테스트 워크플로우가 돌면 이 자리에 채워집니다.</p>")

    vclass = {"POSITIVE": "ok", "RELATIVE_ONLY": "warn", "INCONCLUSIVE": "warn",
              "NEGATIVE": "bad", "UNKNOWN": "warn"}.get(bt.get("verdict"), "warn")
    w = bt.get("window", {})
    rows = []
    for h in sorted(bt.get("horizons", {}), key=lambda x: int(x)):
        H = bt["horizons"][h]
        ex, en, ic, tm, um, ht = (H.get("excess"), H.get("excess_net"), H.get("ic"),
                                  H.get("top_med"), H.get("uni_med"), H.get("hit"))
        if not ex:
            continue
        sig = "유의" if (ex.get("ci_low") or 0) > 0 else "무의미"
        rows.append(
            "<tr><td>%s일</td><td class=%s>%s</td><td>[%s, %s]</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%.3f</td><td>%s</td></tr>" % (
                h, "ok" if sig == "유의" else "muted", _pct_cell(ex["mean"]),
                _pct_cell(ex.get("ci_low")), _pct_cell(ex.get("ci_high")),
                _pct_cell(en["mean"]), _pct_cell(tm["mean"]), _pct_cell(um["mean"]),
                ic["mean"], sig))

    dec = bt.get("decay", {})
    dk = sorted(dec, key=lambda x: int(x))
    dvals = [(k, dec[k]["mean"]) for k in dk if dec[k].get("mean") is not None]
    decay_svg = ""
    if dvals:
        mx = max(abs(v) for _, v in dvals) or 1.0
        W, Hh = 700, 150
        bw = W / max(1, len(dvals))
        parts = ['<svg viewBox="0 0 %d %d" width="100%%">' % (W, Hh)]
        parts.append('<line x1="0" y1="%d" x2="%d" y2="%d" stroke="#333"/>' % (Hh / 2, W, Hh / 2))
        for i, (k, v) in enumerate(dvals):
            hgt = abs(v) / mx * (Hh / 2 - 18)
            y = (Hh / 2 - hgt) if v >= 0 else Hh / 2
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                         % (i * bw + 6, y, bw - 12, hgt, "#2e9e6b" if v >= 0 else "#c1524b"))
            parts.append('<text x="%.1f" y="%d" font-size="11" fill="#8a8f98" text-anchor="middle">%sd</text>'
                         % (i * bw + bw / 2, Hh - 3, k))
            parts.append('<text x="%.1f" y="%.1f" font-size="10" fill="#aaa" text-anchor="middle">%+.1f</text>'
                         % (i * bw + bw / 2, y - 4 if v >= 0 else y + hgt + 12, v * 100))
        parts.append("</svg>")
        decay_svg = "".join(parts)

    mrows = "".join(
        "<tr><td>%s</td><td>%d</td><td class=%s>%s</td></tr>" % (
            esc_html(m["month"]), m["n"], "ok" if m["excess_mean"] > 0 else "bad",
            _pct_cell(m["excess_mean"])) for m in bt.get("monthly", []))

    pr = bt.get("promoted", {})
    prows = []
    for h in sorted(pr, key=lambda x: int(x)):
        e = pr[h].get("excess")
        a = pr[h].get("abs")
        if not e:
            continue
        prows.append("<tr><td>%s일</td><td>%d</td><td class=%s>%s</td><td>[%s, %s]</td><td>%s</td></tr>"
                     % (h, e["n_dates"], "ok" if (e.get("ci_low") or 0) > 0 else "muted",
                        _pct_cell(e["mean"]), _pct_cell(e.get("ci_low")), _pct_cell(e.get("ci_high")),
                        _pct_cell(a["mean"]) if a else "n/a"))

    bo = bt.get("breakout", {})
    borows = "".join(
        "<tr><td>%s일</td><td>%s</td><td>%d%%</td></tr>" % (
            h, _pct_cell(bo[h]["mean"]), round(bo[h]["pct_positive"] * 100))
        for h in sorted(bo, key=lambda x: int(x)) if bo.get(h))

    limits = "".join("<li>%s</li>" % esc_html(x) for x in bt.get("limits", []))

    return """<h2>검증 — 워크포워드 백테스트</h2>
<div class="verdict %(vclass)s"><b>%(verdict)s</b><br>%(note)s</div>
<div class=meta>구간 %(first)s ~ %(last)s · 리밸런스 %(nreb)d일 · 이력 보유 %(ntok)d종목(상장폐지 포함) ·
왕복비용 %(cost).1f%% 가정 · 기준일 %(as_of)s</div>
<div class=wrap><table>
<tr><th>보유</th><th>초과수익</th><th>95%% 신뢰구간</th><th>비용차감</th><th>상위10 절대</th><th>유니버스 절대</th><th>순위상관</th><th>판정</th></tr>
%(rows)s</table></div>
<p class=note>초과수익 = 상위10 중앙 수익률 − 같은 날 유니버스 중앙 수익률. 신뢰구간은 5일 블록 부트스트랩 2,000회.
<b>유니버스 절대 열을 반드시 함께 보세요</b> — 알파 토큰 전체가 시간이 갈수록 빠지기 때문에, 상대우위가 있어도 절대수익은 마이너스일 수 있습니다.</p>

<h3>신호 감쇠 — 보유기간별 초과수익(%%p)</h3>%(decay)s
<h3>승격 규칙(상위10 · 2일 유지) 효과</h3>
<div class=wrap><table><tr><th>보유</th><th>표본일</th><th>초과수익</th><th>95%% 구간</th><th>절대수익</th></tr>%(prows)s</table></div>
<h3>돌파 신호(20일 신고가 + 거래대금 1.5배) 신뢰도</h3>
<div class=wrap><table><tr><th>보유</th><th>초과수익 평균</th><th>플러스 비율</th></tr>%(borows)s</table></div>
<h3>월별 안정성 (14일 보유 초과수익)</h3>
<div class=wrap><table><tr><th>월</th><th>표본일</th><th>초과수익</th></tr>%(mrows)s</table></div>
<h3>이 검증이 말하지 않는 것</h3><ul class=note>%(limits)s</ul>
""" % {"vclass": vclass, "verdict": esc_html(bt.get("verdict", "")),
       "note": esc_html(bt.get("verdict_note", "")),
       "first": esc_html(w.get("first", "?")), "last": esc_html(w.get("last", "?")),
       "nreb": w.get("rebalance_days", 0), "ntok": w.get("tokens_with_history", 0),
       "cost": (bt.get("config", {}).get("round_trip_cost", 0.01)) * 100,
       "as_of": esc_html(bt.get("as_of_kst", "")),
       "rows": "".join(rows), "decay": decay_svg, "prows": "".join(prows),
       "borows": borows, "mrows": mrows, "limits": limits}


def render_tracking_section(tr):
    if not tr:
        return ""
    rows = []
    for h in sorted(tr.get("horizons", {}), key=lambda x: int(x)):
        H = tr["horizons"][h]
        if H.get("status") != "집계":
            rows.append("<tr><td>%s일</td><td>%d</td><td colspan=3 class=muted>표본 부족 — %d건 더 필요</td></tr>"
                        % (h, H.get("n", 0), H.get("need", 0)))
        else:
            rows.append("<tr><td>%s일</td><td>%d</td><td class=%s>%s</td><td>%s</td><td>%d%%</td></tr>"
                        % (h, H["n"], "ok" if H["excess_median"] > 0 else "bad",
                           _pct_cell(H["excess_median"]), _pct_cell(H["abs_median"]),
                           round(H["win_rate"] * 100)))
    return """<h2>라이브 추적 — 실제 승격 후보의 이후 성과</h2>
<div class=wrap><table><tr><th>보유</th><th>표본</th><th>초과수익(중앙)</th><th>절대수익(중앙)</th><th>초과 플러스 비율</th></tr>%s</table></div>
<p class=note>배포 이후 <b>실제로 텔레그램에 승격된 후보만</b> 기록합니다(사후 선택 없음). 백테스트가 검증하지 못하는
수급축(홀더·유동성 증감)까지 포함한 라이브 점수의 유일한 증거이며, 표본 20건이 쌓이기 전에는 수치를 내지 않습니다.
현재 추적 중 %d건.</p>""" % ("".join(rows), tr.get("open_entries", 0))


def render_dashboard(payload, backtest=None):
    p = payload
    rows = p["candidates"][:TOP_N_DASHBOARD]
    trs = []
    for r in rows:
        flags = ", ".join(FLAG_KO.get(x, x) for x in r["flags"]) or "-"
        trs.append(
            "<tr><td>%d</td><td><b>%s</b><br><span class=sub>%s</span></td><td>%.2f</td>"
            "<td>%s</td><td>%s</td><td>%d%%</td><td>%.1f×</td><td>%s</td><td>%s</td>"
            "<td>%.1f</td><td>%s</td><td>%s</td></tr>" % (
                r["rank"], esc_html(r["symbol"]), esc_html(r["chain"]), r["score"],
                fmt_pct(r["ret7"]), fmt_pct(r["ret30"]), round(r["near_high"] * 100),
                r["volx"] or 0, fmt_usd(r["mc"]), fmt_usd(r["liq"]), r["turnover"],
                "{:,}".format(r["holders"]), esc_html(flags)))
    theme_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%s</td><td>%.2f</td><td>%d%%</td></tr>" %
        (esc_html(t["label"]), t["n"], fmt_pct(t["median_ret7"], 1), t["median_base"],
         round(t["breadth"] * 100)) for t in p["themes"])
    ev_rows = "".join("<li><b>%s</b> · %s — %s</li>" %
                      (esc_html(e["symbol"]), e["type"], esc_html(e["detail"])) for e in p["events"][:20]) \
        or "<li>기록된 변화 없음</li>"

    badge = p.get("backtest_badge") or "검증 미실행"
    bclass = {"POSITIVE": "ok", "RELATIVE_ONLY": "warn", "INCONCLUSIVE": "warn",
              "NEGATIVE": "bad"}.get((backtest or {}).get("verdict"), "warn")

    return """<!doctype html><html lang=ko><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Binance Alpha Trend Radar</title>
<style>
body{background:#0f1115;color:#e6e6e6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:0;padding:18px}
h1{font-size:19px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px;color:#9fd0ff}
h3{font-size:13px;margin:18px 0 6px;color:#c9d4e0}
.meta{color:#8a8f98;font-size:12px;margin-bottom:10px;line-height:1.6}
table{width:100%%;border-collapse:collapse;font-size:12px}
th,td{padding:6px 5px;border-bottom:1px solid #22262e;text-align:right;white-space:nowrap}
th:nth-child(2),td:nth-child(2){text-align:left;white-space:normal}
th{color:#8a8f98;font-weight:500}.sub{color:#6e737c;font-size:11px}
.wrap{overflow-x:auto}.note{color:#8a8f98;font-size:11.5px;line-height:1.7;margin-top:8px}
.badge{display:inline-block;padding:2px 7px;border-radius:9px;font-size:11px;background:#1c2129;color:#9fd0ff}
.verdict{border-radius:8px;padding:11px 13px;font-size:12.5px;line-height:1.65;margin:6px 0 10px}
.verdict.ok{background:#12251b;border-left:3px solid #2e9e6b}
.verdict.warn{background:#2a2415;border-left:3px solid #c9a227}
.verdict.bad{background:#2a1717;border-left:3px solid #c1524b}
.ok{color:#4bbd85}.bad{color:#e0736b}.muted{color:#7b818b}
ul{padding-left:18px;font-size:12px;line-height:1.8}
.tabs{display:flex;gap:6px;margin:12px 0}
.tabs a{padding:5px 11px;border-radius:14px;background:#1a1e26;color:#9fd0ff;text-decoration:none;font-size:12px}
</style>
<h1>🛰️ Binance Alpha Trend Radar</h1>
<div class=meta>%(as_of)s KST · 유니버스 %(n)d · 커버리지 %(cov)d%% · 상태 <span class=badge>%(status)s</span> · 국면 %(regime)s</div>
<div class="verdict %(bclass)s">🔬 검증 요약 — %(badge)s</div>
<div class=tabs><a href="#today">오늘</a><a href="#verify">검증</a><a href="#live">라이브 추적</a></div>

<h2 id=today>추세 점수 상위</h2>%(svg)s
<div class=wrap><table>
<tr><th>#</th><th>종목</th><th>점수</th><th>7일</th><th>30일</th><th>60일고점比</th><th>거래대금</th><th>시총</th><th>유동성</th><th>회전</th><th>홀더</th><th>플래그</th></tr>
%(rows)s</table></div>
<h3>테마 상대강도</h3>
<div class=wrap><table><tr><th>테마</th><th>종목수</th><th>중앙 7일</th><th>중앙 점수</th><th>폭</th></tr>%(themes)s</table></div>
<h3>변화 감지</h3><ul>%(events)s</ul>

<div id=verify>%(backtest)s</div>
<div id=live>%(tracking)s</div>

<p class=note>
점수 = (0.60×차트구조 + 0.40×수급) × 테마적합(0.55~1.00) × 리스크감점.
이 시스템은 <b>관측기이지 예측기가 아닙니다.</b> 알파 마켓은 유동성이 얕고 에어드랍 포인트 파밍 거래가 섞여 있어,
거래량 기반 지표가 실수요를 과대평가할 수 있습니다(회전이상 플래그 참고).
상위 10 진입 후 <b>2일 이상 유지</b>된 종목만 텔레그램으로 승격합니다.
</p>
</html>""" % {
        "as_of": esc_html(p["as_of_kst"][:16]), "n": p["universe_size"],
        "cov": round(p["coverage"] * 100), "status": p["data_status"],
        "regime": esc_html(p["market"]["regime"]),
        "svg": bar_svg(rows[:12]), "rows": "".join(trs),
        "themes": theme_rows, "events": ev_rows,
        "badge": esc_html(badge), "bclass": bclass,
        "backtest": render_backtest_section(backtest),
        "tracking": render_tracking_section(p.get("tracking")),
    }


# ----------------------------------------------------------------------------
# 파이프라인
# ----------------------------------------------------------------------------
def regime_label(median_ret7, breadth, bnb_ret7):
    if median_ret7 is None:
        return "판정 불가"
    if median_ret7 > 0.05 and breadth > 0.55:
        base = "알파 전반 강세 (자금 유입)"
    elif median_ret7 > 0 and breadth > 0.45:
        base = "선별 강세 (종목별 차별화)"
    elif median_ret7 > -0.08:
        base = "횡보·눌림"
    else:
        base = "알파 전반 약세 (위험회피)"
    if bnb_ret7 is not None:
        base += " · BNB 대비 %s" % ("우위" if median_ret7 > bnb_ret7 else "열위")
    return base


def run(offline_payload=None):
    cfg = read_json(THEMES_PATH, {})
    themes = cfg.get("themes", [])
    theme_label = {t["key"]: t["label"] for t in themes}
    theme_label["UNTAGGED"] = "미분류"
    sr = cfg.get("screen_rule", {})
    min_liq = sr.get("min_liquidity_usd", 200000)
    min_vol = sr.get("min_volume24h_usd", 300000)
    min_mc = sr.get("min_marketcap_usd", 3000000)
    min_bars = sr.get("min_daily_bars", 20)
    new_days = sr.get("new_listing_track_days", 21)

    t_now = now_utc()
    today = t_now.astimezone(KST).strftime("%Y-%m-%d")
    status, note = "OK", ""

    tokens, host = (offline_payload or (None, None)) if offline_payload else (None, None)
    if tokens is None:
        tokens, host = fetch_token_list()

    active = [t for t in tokens if not t.get("offline") and not t.get("fullyDelisted")]
    cand = []
    new_listings = []
    for t in active:
        s = snapshot_features(t)
        if s["liq"] < min_liq or s["vol24"] < min_vol or s["mc"] < min_mc:
            continue
        age_days = (t_now.timestamp() * 1000 - s["listing_ms"]) / 86400000.0 if s["listing_ms"] else 999
        rec = {"tok": t, "s": s, "age_days": age_days}
        (new_listings if age_days < new_days else cand).append(rec)

    print("[universe] active=%d screened=%d new=%d host=%s" %
          (len(active), len(cand), len(new_listings), host))

    hist = read_json(os.path.join(DATA_DIR, "history.json"), [])
    prev_snap = {}
    if hist:
        target = (t_now - timedelta(days=7)).astimezone(KST).strftime("%Y-%m-%d")
        older = [h for h in hist if h["date"] <= target] or [hist[0]]
        prev_snap = older[-1].get("tokens", {})

    rows, fails = [], 0
    for rec in cand:
        t, s = rec["tok"], rec["s"]
        aid = t.get("alphaId")
        try:
            bars = closed_bars(fetch_klines(aid, "1d", 120, host))
        except Exception as e:                    # noqa: BLE001
            print("[warn] klines 실패 %s: %s" % (aid, e))
            bars = []
            fails += 1
        time.sleep(REQ_GAP)
        if len(bars) < min_bars:
            continue
        f = chart_features(bars)
        rows.append({
            "alpha_id": aid, "symbol": safe_label(t.get("symbol")),
            "name": safe_label(t.get("name"), 24), "chain": safe_label(s["chain"], 10),
            "theme": tag_theme(t.get("name"), t.get("symbol"), themes),
            "f": f, "s": s, "age_days": round(rec["age_days"], 1),
        })

    coverage = (len(rows) / len(cand)) if cand else 0.0
    if not rows:
        status, note = "FAILED", "차트 데이터를 한 건도 확보하지 못했습니다(지역차단 또는 API 변경 가능)."
    elif coverage < 0.70:
        status, note = "DEGRADED", "커버리지 %d%% — 일부 종목 차트 수집 실패." % round(coverage * 100)

    rows, theme_stat = compute_scores(rows, prev_snap)
    rows = confirm_4h(rows, host)

    state_path = os.path.join(DATA_DIR, "state.json")
    state_all = read_json(state_path, {})
    events, new_state = detect_events(rows, state_all.get("tokens", {}), today)

    bnb = fetch_benchmark("BNBUSDT")
    bnb_ret7 = (bnb[-1] / bnb[-8] - 1.0) if len(bnb) >= 8 else None
    med_ret7 = median([r["f"]["ret7"] or 0.0 for r in rows]) if rows else None
    breadth = (sum(1 for r in rows if (r["f"]["ret7"] or 0) > 0) / len(rows)) if rows else 0.0

    cands_out = []
    for r in rows[:TOP_N_DASHBOARD]:
        f, s = r["f"], r["s"]
        cands_out.append({
            "rank": r["rank"], "symbol": r["symbol"], "name": r["name"], "chain": r["chain"],
            "alpha_id": r["alpha_id"], "theme": r["theme"], "theme_label": theme_label.get(r["theme"], r["theme"]),
            "score": round(r["score"], 3), "chart": round(r["chart"], 3), "flow": round(r["flow"], 3),
            "theme_fit": round(r["theme_fit"], 3), "penalty": round(r["penalty"], 3),
            "streak": r["streak"], "prev_rank": r["prev_rank"], "flags": r["flags"],
            "ret7": f["ret7"], "ret30": f["ret30"], "near_high": f["near_high"],
            "volx": f["volx"], "squeeze": f["squeeze"], "r2": round(f["r2"], 2),
            "new_high20": f["new_high20"], "bars": f["bars"], "age_days": r["age_days"],
            "mc": s["mc"], "liq": s["liq"], "vol24": s["vol24"], "holders": s["holders"],
            "turnover": round(s["turnover"], 3), "churn": round(s["churn"], 1),
            "float_ratio": round(s["float_ratio"], 3), "price": s["price"],
            "hol_chg": r.get("hol_chg"), "liq_chg": r.get("liq_chg"), "c4h": r.get("c4h"),
        })

    themes_out = sorted(
        [{"key": k, "label": theme_label.get(k, k), **v}
         for k, v in theme_stat.items() if v["n"] >= 3 and k != "UNTAGGED"],
        key=lambda x: x["median_ret7"], reverse=True)

    payload = {
        "schema": "alpha-radar@1",
        "as_of_utc": t_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_kst": t_now.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
        "date": today,
        "data_status": status,
        "status_note": note,
        "source_host": host,
        "universe_size": len(rows),
        "screened": len(cand),
        "coverage": round(coverage, 3),
        "klines_fail": fails,
        "market": {
            "median_ret7": med_ret7, "breadth": round(breadth, 3), "bnb_ret7": bnb_ret7,
            "regime": regime_label(med_ret7, breadth, bnb_ret7),
        },
        "candidates": cands_out,
        "themes": themes_out,
        "events": events,
        "new_listings": sorted(
            [{"symbol": safe_label(r["tok"].get("symbol")), "chain": safe_label(r["s"]["chain"], 10),
              "age_days": round(r["age_days"], 1), "mc": r["s"]["mc"], "liq": r["s"]["liq"],
              "holders": r["s"]["holders"]} for r in new_listings],
            key=lambda x: x["mc"], reverse=True)[:10],
    }
    bt = read_json(os.path.join(DATA_DIR, "backtest.json"), None)
    if bt:
        payload["backtest"] = {
            "verdict": bt.get("verdict"), "note": bt.get("verdict_note"),
            "horizon": bt.get("verdict_horizon"), "as_of": bt.get("as_of_kst"),
            "window": bt.get("window"),
        }
        payload["backtest_badge"] = BADGE.get(bt.get("verdict"), bt.get("verdict", ""))

    # 라이브 추적 — 오늘 승격분 기록 + 만기 도래분 성과 확정
    hist_for_track = [h for h in hist if h["date"] != today] + [{
        "date": today,
        "tokens": {r["alpha_id"]: {"p": r["s"]["price"]} for r in rows}}]
    hist_for_track.sort(key=lambda h: h["date"])
    track_path = os.path.join(DATA_DIR, "track.json")
    track = tracking.load(track_path)
    promoted_rows = [r for r in rows if r.get("streak", 0) >= 2]
    track = tracking.update(track, promoted_rows, hist_for_track, today)
    write_json(track_path, track)
    payload["tracking"] = tracking.summarize(track)

    payload["message"] = render_telegram(payload)

    write_json(os.path.join(DATA_DIR, "latest.json"), payload)
    write_json(state_path, {"date": today, "tokens": new_state})

    snap = {"date": today, "tokens": {
        r["alpha_id"]: {"p": round(r["s"]["price"], 10), "mc": round(r["s"]["mc"]),
                        "liq": round(r["s"]["liq"]), "hol": r["s"]["holders"],
                        "vol": round(r["s"]["vol24"])} for r in rows}}
    hist = [h for h in hist if h["date"] != today]
    hist.append(snap)
    hist = sorted(hist, key=lambda h: h["date"])[-HISTORY_KEEP_DAYS:]
    write_json(os.path.join(DATA_DIR, "history.json"), hist)

    docs = os.environ.get("DOCS_DIR")
    if docs:
        os.makedirs(docs, exist_ok=True)
        with open(os.path.join(docs, "index.html"), "w", encoding="utf-8") as f:
            f.write(render_dashboard(payload, bt))

    print("[result] status=%s universe=%d coverage=%.2f events=%d promoted=%d" % (
        status, len(rows), coverage, len(events),
        sum(1 for c in cands_out if c["streak"] >= 2)))
    return payload


def main():
    try:
        payload = run()
    except Exception as e:                        # noqa: BLE001
        print("[fatal] %s" % e)
        # 실패를 성공으로 위장하지 않는다 — 릴레이가 옛 스냅샷을 재발송하지 않도록 종료코드로 알린다.
        sys.exit(1)
    if payload["data_status"] == "FAILED":
        sys.exit(1)


if __name__ == "__main__":
    main()
