#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
내러티브 레이더 — 워크포워드 백테스트
====================================
검증 대상 두 가지
  A) 코인 점수: 부합도 × 자금반응 점수가 앞선 BTC 대비 초과수익을 설명했는가
  B) 내러티브 로테이션: 30일 상대강도 상위 내러티브가 이후에도 상위였는가(지속성)

**가장 중요한 한계 — 사후선택 편향(먼저 읽을 것)**
universe.json 의 44종목은 2026-08-21 에 고정됐다. 즉 이 목록을 만든 시점에
"어떤 프로젝트가 살아남아 상장을 유지했는지" 를 이미 알고 있었다. 그 목록으로 과거를
되돌려 돌리면 결과가 실제보다 좋게 나온다. 이 백테스트는 그래서 **점수 공식의 상한선**을
재는 것이지, 실제로 얻을 수 있었던 성과가 아니다. 판정 라벨에도 이 사실을 박아둔다
(POSITIVE 등급을 주지 않고 BIASED_* 를 쓴다).

데이터
  - 1순위: Binance 현물 일봉(data-api.binance.vision) — 러너 IP에서 정상, 무인증
  - 2순위: CoinGecko market_chart(일별) — 바이낸스 미상장분. 무료 티어 429가 잦아 간격·백오프 필수
  - 벤치마크: BTCUSDT
외부 파이썬 의존성 없음(stdlib only).
"""

import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UNIVERSE = os.path.join(BASE_DIR, "universe.json")

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
HORIZONS = [7, 14, 30]
MIN_BARS = 60
LOOKBACK_DAYS = 270
TOP_N = 5              # 브리프가 노출하는 '부합도 상위' 개수와 맞춘다
BOOT_ITERS = 2000
BLOCK = 5
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


# ------------------------------------------------------------------ 공통
def median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def robust_z(vals, clip=3.0):
    if not vals:
        return []
    med = median(vals)
    mad = median([abs(v - med) for v in vals])
    scale = mad * 1.4826
    if scale <= 1e-12:
        rng = (max(vals) - min(vals)) or 1.0
        return [clamp((v - med) / rng * 2.0, -clip, clip) for v in vals]
    return [clamp((v - med) / scale, -clip, clip) for v in vals]


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return 0.0

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        rk = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = math.sqrt(sum((v - mx) ** 2 for v in rx) * sum((v - my) ** 2 for v in ry))
    return (num / den) if den > 0 else 0.0


def block_bootstrap_ci(vals, iters=BOOT_ITERS, block=BLOCK, seed=11):
    if len(vals) < block * 2:
        return None, None
    rnd = random.Random(seed)
    n, nb = len(vals), max(1, len(vals) // block)
    means = []
    for _ in range(iters):
        acc = []
        for _ in range(nb):
            st = rnd.randrange(0, n - block + 1)
            acc.extend(vals[st:st + block])
        means.append(sum(acc) / len(acc))
    means.sort()
    return means[int(len(means) * 0.025)], means[int(len(means) * 0.975)]


def summarize(vals):
    if not vals:
        return None
    lo, hi = block_bootstrap_ci(vals)
    return {"n": len(vals), "mean": sum(vals) / len(vals), "median": median(vals),
            "pct_positive": sum(1 for v in vals if v > 0) / len(vals),
            "ci_low": lo, "ci_high": hi}


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


# ------------------------------------------------------------------ 수집
def http_json(url, tries=4, timeout=30):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            wait = [8, 20, 45, 90][min(i, 3)]
            if i < tries - 1:
                time.sleep(wait + random.uniform(0, 2))
        except Exception as e:                        # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep([5, 12, 30, 60][min(i, 3)])
    raise RuntimeError("%s → %s" % (url.split("?")[0], last))


def binance_daily(symbol, limit=400):
    rows = http_json("https://data-api.binance.vision/api/v3/klines"
                     "?symbol=%sUSDT&interval=1d&limit=%d" % (symbol, limit), tries=2)
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for r in rows:
        d = datetime.fromtimestamp(int(r[0]) / 1000, UTC).strftime("%Y-%m-%d")
        out[d] = {"c": float(r[4]), "qv": float(r[7])}
    return out


def gecko_daily(coin_id, days=365):
    j = http_json("https://api.coingecko.com/api/v3/coins/%s/market_chart"
                  "?vs_currency=usd&days=%d&interval=daily" % (coin_id, days))
    prices = j.get("prices") or []
    vols = {int(v[0]): float(v[1]) for v in (j.get("total_volumes") or [])}
    out = {}
    for ts, p in prices:
        d = datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")
        out[d] = {"c": float(p), "qv": vols.get(int(ts), 0.0)}
    return out or None


def load_universe():
    u = json.load(open(UNIVERSE, encoding="utf-8"))
    coins = []
    for code, nar in u["narratives"].items():
        for m in nar["members"]:
            coins.append({"code": code, "narrative": nar["name"], "symbol": m["symbol"],
                          "id": m["id"], "fit": float(m.get("fit", 1.0))})
    return u, coins


def fetch_all(coins):
    series, src, missing = {}, {}, []
    for c in coins:
        s = None
        try:
            s = binance_daily(c["symbol"])
            if s:
                src[c["symbol"]] = "binance"
        except Exception:                             # noqa: BLE001
            s = None
        if not s:
            try:
                s = gecko_daily(c["id"])
                if s:
                    src[c["symbol"]] = "coingecko"
                time.sleep(8)                         # 무료 티어 429 회피
            except Exception as e:                    # noqa: BLE001
                print("[warn] %s 수집 실패: %s" % (c["symbol"], str(e)[:60]))
                s = None
        if s:
            series[c["symbol"]] = s
        else:
            missing.append(c["symbol"])
        time.sleep(0.1)
    return series, src, missing


# ------------------------------------------------------------------ 점수 재현
def rel_return(series, btc, dates, i, n):
    """BTC 대비 상대수익 — 이 시스템의 모든 지표는 BTC 기준이다."""
    if i - n < 0:
        return None
    d0, d1 = dates[i - n], dates[i]
    a, b = series.get(d0), series.get(d1)
    p0, p1 = btc.get(d0), btc.get(d1)
    if not (a and b and p0 and p1) or a["c"] <= 0 or p0["c"] <= 0:
        return None
    return (b["c"] / a["c"]) - (p1["c"] / p0["c"])


def score_at(series, btc, coins, dates, i):
    rows = []
    for c in coins:
        s = series.get(c["symbol"])
        if not s or dates[i] not in s:
            continue
        hist = [d for d in dates[:i + 1] if d in s]
        if len(hist) < MIN_BARS:
            continue
        rs30, rs7 = rel_return(s, btc, dates, i, 30), rel_return(s, btc, dates, i, 7)
        if rs30 is None or rs7 is None:
            continue
        v = [s[d]["qv"] for d in hist[-30:] if s[d]["qv"] > 0]
        if len(v) < 10:
            continue
        turn = (sum(v[-7:]) / len(v[-7:])) / (sum(v) / len(v))   # 회전율 대용(거래대금 7일/30일)
        rows.append({**c, "rs30": rs30, "rs7": rs7, "turn": turn})
    if len(rows) < 15:
        return []
    z30 = robust_z([r["rs30"] for r in rows])
    z7 = robust_z([r["rs7"] for r in rows])
    zt = robust_z([math.log1p(max(r["turn"], 0)) for r in rows])
    for k, r in enumerate(rows):
        flow = 0.45 * z30[k] + 0.30 * z7[k] + 0.25 * zt[k]
        r["flow"] = flow
        r["score"] = flow * (0.55 + 0.45 * r["fit"])       # 라이브와 동일: 곱이 아닌 가중혼합
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def run(series, btc, coins):
    dates = sorted(set(btc) & {d for s in series.values() for d in s})
    first = max(MIN_BARS, len(dates) - LOOKBACK_DAYS)
    last = len(dates) - max(HORIZONS) - 1
    coin_res = {h: {"excess": [], "ic": [], "top": [], "uni": []} for h in HORIZONS}
    nar_res = {h: [] for h in HORIZONS}
    scanned = 0

    for i in range(first, last + 1):
        rows = score_at(series, btc, coins, dates, i)
        if not rows:
            continue
        scanned += 1
        # 내러티브 순위(브리프와 동일: 구성종목 rs30 중앙값)
        by_nar = {}
        for r in rows:
            by_nar.setdefault(r["code"], []).append(r)
        nar_rank = sorted(((k, median([x["rs30"] for x in v])) for k, v in by_nar.items()
                           if len(v) >= 2), key=lambda x: x[1], reverse=True)

        for h in HORIZONS:
            fwd = {}
            for r in rows:
                v = rel_return(series[r["symbol"]], btc, dates, i + h, h)
                if v is not None:
                    fwd[r["symbol"]] = v
            elig = [r for r in rows if r["symbol"] in fwd]
            if len(elig) < 15:
                continue
            uni = median([fwd[r["symbol"]] for r in elig])
            top = median([fwd[r["symbol"]] for r in elig[:TOP_N]])
            coin_res[h]["excess"].append(top - uni)
            coin_res[h]["top"].append(top)
            coin_res[h]["uni"].append(uni)
            coin_res[h]["ic"].append(spearman([r["score"] for r in elig],
                                              [fwd[r["symbol"]] for r in elig]))
            if len(nar_rank) >= 4:
                def nar_fwd(code):
                    vs = [fwd[x["symbol"]] for x in by_nar[code] if x["symbol"] in fwd]
                    return median(vs) if vs else None
                tops = [nar_fwd(c) for c, _ in nar_rank[:2]]
                bots = [nar_fwd(c) for c, _ in nar_rank[-2:]]
                tops = [v for v in tops if v is not None]
                bots = [v for v in bots if v is not None]
                if tops and bots:
                    nar_res[h].append(median(tops) - median(bots))
    return dates, coin_res, nar_res, scanned


def verdict(coin_res):
    """사후선택 편향이 있는 유니버스이므로 POSITIVE 라벨을 주지 않는다."""
    best, best_ci = None, None
    for h in HORIZONS:
        s = summarize(coin_res[h]["excess"])
        if s and s["ci_low"] is not None and (best_ci is None or s["ci_low"] > best_ci):
            best, best_ci = h, s["ci_low"]
    if best is None:
        return "UNKNOWN", "표본 부족 — 판정 불가.", None
    ex = summarize(coin_res[best]["excess"])
    ic = summarize(coin_res[best]["ic"])
    top = summarize(coin_res[best]["top"])
    if ex["ci_low"] > 0:
        return ("BIASED_POSITIVE",
                "%d일 기준 상위%d의 BTC 대비 초과수익 %+.1f%%p(95%% 하단 %+.1f%%p), 순위상관 %.3f. "
                "다만 유니버스가 사후에 고정된 목록이라 이 수치는 상한선이며 실현 가능한 성과가 아니다. "
                "상위%d의 절대 상대수익은 %+.1f%%p."
                % (best, TOP_N, ex["mean"] * 100, ex["ci_low"] * 100, ic["mean"], TOP_N,
                   top["mean"] * 100), best)
    if ex["mean"] > 0:
        return ("INCONCLUSIVE",
                "%d일 기준 초과수익 평균 %+.1f%%p이나 신뢰구간이 0을 포함(%+.1f~%+.1f%%p). "
                "사후선택 편향이 들어간 유니버스에서조차 유의하지 않다."
                % (best, ex["mean"] * 100, (ex["ci_low"] or 0) * 100, (ex["ci_high"] or 0) * 100), best)
    return ("NEGATIVE",
            "%d일 기준 초과수익 평균 %+.1f%%p, 순위상관 %.3f — 편향된 유니버스에서도 우위가 없다. "
            "부합도 순위를 매매 근거로 쓰지 말 것."
            % (best, ex["mean"] * 100, ic["mean"]), best)


def main():
    u, coins = load_universe()
    print("[bt] 유니버스 %d종목 / %d내러티브 (frozen %s)"
          % (len(coins), len(u["narratives"]), u.get("frozen_at")))
    btc = binance_daily("BTC")
    if not btc:
        sys.exit("BTC 벤치마크 수집 실패 — 중단")
    series, src, missing = fetch_all(coins)
    print("[bt] 수집 %d/%d (binance %d, coingecko %d) 누락: %s"
          % (len(series), len(coins), sum(1 for v in src.values() if v == "binance"),
             sum(1 for v in src.values() if v == "coingecko"), ",".join(missing) or "없음"))
    if len(series) < len(coins) * 0.6:
        sys.exit("커버리지 %d%% — 백테스트 신뢰 불가, 중단" % round(100 * len(series) / len(coins)))

    dates, coin_res, nar_res, scanned = run(series, btc, coins)
    v, note, hz = verdict(coin_res)
    out = {
        "schema": "narrative-radar-backtest@1",
        "as_of_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "window": {"first": dates[max(MIN_BARS, len(dates) - LOOKBACK_DAYS)], "last": dates[-1],
                   "rebalance_days": scanned, "coverage": round(len(series) / len(coins), 3),
                   "missing": missing, "sources": src},
        "universe_frozen_at": u.get("frozen_at"),
        "coin": {str(h): {"excess": summarize(coin_res[h]["excess"]),
                          "ic": summarize(coin_res[h]["ic"]),
                          "top": summarize(coin_res[h]["top"]),
                          "uni": summarize(coin_res[h]["uni"])} for h in HORIZONS},
        "narrative_rotation": {str(h): summarize(nar_res[h]) for h in HORIZONS},
        "verdict": v, "verdict_note": note, "verdict_horizon": hz,
        "limits": [
            "유니버스 44종목이 2026-08-21 에 사후 고정됐다 — 살아남은 종목만 들어 있어 결과가 실제보다 좋게 나온다. 이 수치는 공식의 상한선이지 실현 가능한 성과가 아니다.",
            "회전율은 시총 이력이 없어 '거래대금 7일/30일 비율'로 대체했다. 라이브 점수(거래대금/시총)와 완전히 같지 않다.",
            "모든 수익률은 BTC 대비 상대값이다. 상대우위가 있어도 절대수익은 마이너스일 수 있다.",
            "바이낸스 미상장분은 CoinGecko 일별 종가로 메웠고, 둘 다 없는 종목은 아예 빠졌다(누락 목록 참조).",
            "체결비용·슬리피지를 반영하지 않았다.",
            "관측 구간은 약 9개월, 사실상 하나의 시장국면이다.",
        ],
    }
    write_json(os.path.join(DATA_DIR, "backtest.json"), out)
    print("[bt] 리밸런스 %d일 · 판정 %s" % (scanned, v))
    print("     %s" % note)
    return out


if __name__ == "__main__":
    main()
