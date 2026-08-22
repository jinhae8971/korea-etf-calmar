#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Alpha Trend Radar — 워크포워드 백테스트
======================================
목적: 대시보드 상단에 붙일 '신빙성 근거'를 실측으로 만든다.
      점수가 실제로 앞선 수익률을 설명했는지, 아니면 설명하지 못했는지를
      **결과가 나쁘게 나와도 그대로** 보고한다.

정직성 규칙(설계에 못박음)
  1) 각 시점 t 의 점수는 t 까지의 봉만 사용한다. 미래 정보 누출 금지.
  2) 스냅샷 지표(시총·유동성·홀더)는 과거값이 존재하지 않는다 → **백테스트는
     차트 + 거래대금 축만 검증**한다. 수급 축(홀더·유동성 증감)은 라이브 추적으로만
     검증되며, 그 사실을 대시보드에 명시한다.
  3) 상장폐지 생존편향 완화 — 현재 offline/delisted 토큰의 과거 캔들도 포함하고,
     보유 중 거래가 끊긴 종목은 **마지막 체결가를 그대로 들고 있는 것**으로 처리한다.
  4) 유의성은 날짜 단위 블록 부트스트랩으로 낸다(일별 표본은 서로 겹쳐 독립이 아님).
  5) 판정 문구는 임계값 기반으로 자동 생성한다. 사람이 사후에 말을 고르지 않는다.
"""

import json
import math
import os
import random
import sys
import threading
import queue
import time
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
sys.path.insert(0, BASE_DIR)
import alpha_radar as ar  # noqa: E402

UTC = timezone.utc
HORIZONS = [3, 7, 14, 21]
DECAY = [1, 2, 3, 5, 7, 10, 14, 21]
MIN_BARS = 60
MIN_ADV = 300000.0          # 20일 평균 거래대금 하한(과거 시점에도 재현 가능한 유일한 규모 기준)
TOP_N = 10                  # 라이브 운영과 동일하게 상위 10을 본다
ROUND_TRIP_COST = 0.01      # 알파는 스프레드·슬리피지가 크다 — 왕복 1% 가정
BOOT_ITERS = 2000
BLOCK = 5                   # 블록 부트스트랩 블록 길이(일)


# ---------------------------------------------------------------- 수집
def fetch_all_klines(alpha_ids, workers=5, limit=500):
    out, lock, q, errs = {}, threading.Lock(), queue.Queue(), []
    for a in alpha_ids:
        q.put(a)

    def work():
        while True:
            try:
                aid = q.get_nowait()
            except queue.Empty:
                return
            url = ("%s%s?symbol=%sUSDT&interval=1d&limit=%d"
                   % (ar.HOSTS[0], ar.KLINES_PATH, aid, limit))
            for attempt in range(3):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": random.choice(ar.UA_POOL)})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        j = json.loads(r.read().decode("utf-8", "replace"))
                    rows = j.get("data") or []
                    if rows:
                        with lock:
                            out[aid] = [[int(x[0]), float(x[1]), float(x[2]), float(x[3]),
                                         float(x[4]), float(x[7])] for x in rows]
                    break
                except Exception as e:                       # noqa: BLE001
                    if attempt == 2:
                        with lock:
                            errs.append(str(e)[:60])
                    else:
                        time.sleep(2 + attempt * 4)
            time.sleep(0.1)

    ths = [threading.Thread(target=work) for _ in range(workers)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    return out, errs


def to_series(cache):
    series = {}
    for aid, rows in cache.items():
        s = {}
        for r in rows:
            d = datetime.fromtimestamp(r[0] / 1000, UTC).strftime("%Y-%m-%d")
            s[d] = {"o": r[1], "h": r[2], "l": r[3], "c": r[4], "qv": r[5], "t": r[0]}
        series[aid] = s
    return series


# ---------------------------------------------------------------- 점수 재현
def bars_upto(s, dates_idx, t_idx, lookback=120):
    """t 시점까지의 봉만 만든다(미래 누출 차단)."""
    out = []
    for i in range(max(0, t_idx - lookback + 1), t_idx + 1):
        b = s.get(dates_idx[i])
        if b:
            out.append({"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                        "c": b["c"], "v": 0.0, "ct": b["t"], "qv": b["qv"], "n": 0})
    return out


def score_at(series, dates, t_idx):
    """해당 날짜의 유니버스와 점수(차트축 + 거래대금 축)를 라이브와 같은 공식으로 만든다."""
    rows = []
    for aid, s in series.items():
        if dates[t_idx] not in s:
            continue
        bars = bars_upto(s, dates, t_idx)
        if len(bars) < MIN_BARS:
            continue
        f = ar.chart_features(bars)
        if (f["adv20"] or 0) < MIN_ADV:
            continue
        rows.append({"aid": aid, "f": f, "c": s[dates[t_idx]]["c"]})
    if len(rows) < 20:
        return []

    z30 = ar.robust_z([r["f"]["ret30"] or 0.0 for r in rows])
    z7 = ar.robust_z([r["f"]["ret7"] or 0.0 for r in rows])
    zadv = ar.robust_z([math.log1p(max(r["f"]["adv20"], 0)) for r in rows])
    for i, r in enumerate(rows):
        f = r["f"]
        struct = f["struct"] * 2 - 1
        nh = ar.clamp((f["near_high"] - 0.55) / 0.45, 0.0, 1.0) * 2 - 1
        r2c = f["r2"] * 2 - 1
        volx = f["volx"] if f["volx"] is not None else 1.0
        volx_c = ar.clamp((volx - 1.0) / 0.8, -1.0, 1.5)
        chart = 0.30 * z30[i] + 0.20 * z7[i] + 0.20 * struct + 0.15 * nh + 0.15 * r2c
        r["chart"] = chart
        # 라이브 점수의 수급축 중 과거 재현이 가능한 것은 거래대금뿐이다.
        r["score"] = 0.75 * chart + 0.25 * (0.6 * volx_c + 0.4 * zadv[i])
        r["breakout"] = bool(f["new_high20"] and (f["volx"] or 0) >= 1.5)
    return rows


def fwd_return(s, dates, t_idx, h):
    """미래 h일 수익률. 중간에 거래가 끊기면 마지막 체결가를 들고 있는 것으로 본다."""
    if t_idx + h >= len(dates) or dates[t_idx] not in s:
        return None
    p0 = s[dates[t_idx]]["c"]
    if p0 <= 0:
        return None
    p1 = None
    for j in range(t_idx + h, t_idx, -1):
        b = s.get(dates[j])
        if b:
            p1 = b["c"]
            break
    if p1 is None:
        return None
    return p1 / p0 - 1.0


# ---------------------------------------------------------------- 통계
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


def block_bootstrap_ci(vals, iters=BOOT_ITERS, block=BLOCK, lo=2.5, hi=97.5, seed=7):
    """일별 표본은 겹쳐서 독립이 아니다 → 블록 부트스트랩으로 평균의 신뢰구간을 낸다."""
    if len(vals) < block * 2:
        return None, None
    rnd = random.Random(seed)
    n = len(vals)
    nb = max(1, n // block)
    means = []
    for _ in range(iters):
        acc = []
        for _ in range(nb):
            st = rnd.randrange(0, n - block + 1)
            acc.extend(vals[st:st + block])
        means.append(sum(acc) / len(acc))
    means.sort()
    return means[int(len(means) * lo / 100)], means[int(len(means) * hi / 100)]


def summarize(per_date, key):
    vals = [d[key] for d in per_date if d.get(key) is not None]
    if not vals:
        return None
    lo, hi = block_bootstrap_ci(vals)
    return {
        "n_dates": len(vals),
        "mean": sum(vals) / len(vals),
        "median": ar.median(vals),
        "pct_positive": sum(1 for v in vals if v > 0) / len(vals),
        "ci_low": lo, "ci_high": hi,
    }


# ---------------------------------------------------------------- 백테스트
def run_backtest(series, start_idx_days=270, step=1):
    """단일 패스: 매일 점수를 만들고, 모든 홀딩기간의 미래수익을 한 번에 붙인다."""
    dates = sorted({d for s in series.values() for d in s})
    first = max(MIN_BARS, len(dates) - start_idx_days)
    last = len(dates) - max(HORIZONS) - 1

    per_date = {h: [] for h in HORIZONS}
    breakout = {h: [] for h in HORIZONS}
    promoted = {h: [] for h in HORIZONS}
    decay = {h: [] for h in DECAY}
    monthly = {}
    prev_top = set()
    scanned = 0

    for t_idx in range(first, last + 1, step):
        rows = score_at(series, dates, t_idx)
        if not rows:
            prev_top = set()
            continue
        scanned += 1
        rows.sort(key=lambda r: r["score"], reverse=True)
        top_ids = [r["aid"] for r in rows[:TOP_N]]
        # 라이브 운영과 동일한 승격 규칙: 어제도 상위10, 오늘도 상위10
        promo = [r for r in rows[:TOP_N] if r["aid"] in prev_top]
        prev_top = set(top_ids)

        for h in HORIZONS + [x for x in DECAY if x not in HORIZONS]:
            fw = {}
            for r in rows:
                v = fwd_return(series[r["aid"]], dates, t_idx, h)
                if v is not None:
                    fw[r["aid"]] = v
            elig = [r for r in rows if r["aid"] in fw]
            if len(elig) < 20:
                continue
            uni_med = ar.median([fw[r["aid"]] for r in elig])
            top = [r for r in elig if r["aid"] in top_ids]
            if not top:
                continue
            top_med = ar.median([fw[r["aid"]] for r in top])
            if h in decay:
                decay[h].append(top_med - uni_med)
            if h not in per_date:
                continue
            ic = spearman([r["score"] for r in elig], [fw[r["aid"]] for r in elig])
            rec = {
                "date": dates[t_idx], "n": len(elig),
                "uni_med": uni_med, "top_med": top_med,
                "excess": top_med - uni_med,
                "excess_net": (top_med - ROUND_TRIP_COST) - uni_med,
                "ic": ic,
                "hit": sum(1 for r in top if fw[r["aid"]] > uni_med) / len(top),
            }
            per_date[h].append(rec)
            if h == 14:
                mk = dates[t_idx][:7]
                monthly.setdefault(mk, []).append(rec["excess"])
            bo = [r for r in elig if r["breakout"]]
            if bo:
                breakout[h].append({"date": dates[t_idx], "n": len(bo),
                                    "excess": ar.median([fw[r["aid"]] for r in bo]) - uni_med})
            pr = [r for r in elig if r["aid"] in {x["aid"] for x in promo}]
            if pr:
                promoted[h].append({"date": dates[t_idx], "n": len(pr),
                                    "excess": ar.median([fw[r["aid"]] for r in pr]) - uni_med,
                                    "abs": ar.median([fw[r["aid"]] for r in pr])})
    decay_out = {str(h): {"mean": (sum(v) / len(v)) if v else None, "n": len(v)}
                 for h, v in decay.items()}
    monthly_out = [{"month": k, "n": len(v), "excess_mean": sum(v) / len(v)}
                   for k, v in sorted(monthly.items())]
    return dates, per_date, breakout, promoted, decay_out, monthly_out, scanned


def verdict(hz):
    """판정은 임계값으로 기계적으로 낸다. 결과가 나쁘면 나쁘다고 쓴다.
    핵심 구분: '유니버스 대비 상대우위'와 '절대수익 플러스'는 전혀 다른 이야기다."""
    best = None
    for h in ("14", "7", "21", "3"):
        s = hz.get(h)
        if s and s.get("excess") and s["excess"].get("ci_low") is not None:
            if best is None or s["excess"]["ci_low"] > hz[best]["excess"]["ci_low"]:
                best = h
    if best is None:
        return "UNKNOWN", "표본 부족 — 판정 불가.", None

    s = hz[best]
    ex, exn, ic, absr, uni = s["excess"], s["excess_net"], s["ic"], s["top_med"], s["uni_med"]
    rel_ok = ex["ci_low"] is not None and ex["ci_low"] > 0
    abs_ok = absr["mean"] > 0

    if rel_ok and abs_ok and exn["mean"] > 0:
        note = ("%s일 보유 기준 초과수익 평균 %+.1f%%p(95%% 하단 %+.1f%%p), 상위10 절대수익 %+.1f%%, "
                "순위상관 %.3f — 약한 우위가 관측됨."
                % (best, ex["mean"] * 100, ex["ci_low"] * 100, absr["mean"] * 100, ic["mean"]))
        return "POSITIVE", note, best

    if rel_ok and not abs_ok:
        note = ("%s일 기준 유니버스 대비 초과수익 %+.1f%%p(95%% 하단 %+.1f%%p)로 0을 넘지만, "
                "상위10의 절대수익은 %+.1f%%로 마이너스다. 같은 기간 알파 유니버스 중앙값이 %+.1f%%였기 때문이며, "
                "이 점수는 '덜 빠지는 쪽'을 골랐을 뿐 롱온리 매수 근거가 되지 못한다."
                % (best, ex["mean"] * 100, ex["ci_low"] * 100, absr["mean"] * 100, uni["mean"] * 100))
        return "RELATIVE_ONLY", note, best

    if ex["mean"] > 0:
        note = ("%s일 기준 초과수익 평균 %+.1f%%p이나 신뢰구간이 0을 포함(%+.1f~%+.1f%%p). "
                "표본 변동으로 설명 가능한 범위 — 우위를 주장할 수 없다."
                % (best, ex["mean"] * 100, (ex["ci_low"] or 0) * 100, (ex["ci_high"] or 0) * 100))
        return "INCONCLUSIVE", note, best

    note = ("%s일 기준 초과수익 평균 %+.1f%%p, 순위상관 %.3f — 선별이 유니버스 중앙값을 이기지 못함."
            % (best, ex["mean"] * 100, ic["mean"]))
    return "NEGATIVE", note, best


def main():
    tokens, _host = ar.fetch_token_list()
    ids = [t["alphaId"] for t in tokens if t.get("alphaId")]
    print("[backtest] 캔들 수집 %d종목 (상장폐지 포함 — 생존편향 완화)" % len(ids))
    # 캐시는 레포에 커밋하지 않는다(10MB급) — 러너 임시 디렉터리에 둔다.
    cache_path = os.environ.get("KLINES_CACHE") or os.path.join(
        tempfile.gettempdir(), "alpha_klines_cache.json")
    if os.environ.get("USE_CACHE") and os.path.exists(cache_path):
        cache = json.load(open(cache_path, encoding="utf-8"))
    else:
        cache, errs = fetch_all_klines(ids)
        print("[backtest] 확보 %d종목 / 실패 %d" % (len(cache), len(errs)))
        if len(cache) < 100:
            sys.exit("캔들 확보 %d종목 — 백테스트 신뢰 불가, 중단" % len(cache))
        try:
            json.dump(cache, open(cache_path, "w"))
        except Exception as e:                       # noqa: BLE001
            print("[backtest] 캐시 저장 생략: %s" % e)
    series = to_series(cache)

    dates, res, bo, promo, decay, monthly, scanned = run_backtest(series)
    out = {
        "schema": "alpha-radar-backtest@2",
        "as_of_kst": datetime.now(ar.KST).strftime("%Y-%m-%d %H:%M"),
        "window": {"first": dates[max(MIN_BARS, len(dates) - 270)], "last": dates[-1],
                   "rebalance_days": scanned, "tokens_with_history": len(series)},
        "config": {"top_n": TOP_N, "min_adv_usd": MIN_ADV, "min_bars": MIN_BARS,
                   "round_trip_cost": ROUND_TRIP_COST, "horizons": HORIZONS},
        "horizons": {}, "breakout": {}, "promoted": {},
        "decay": decay, "monthly": monthly,
        "series14": [{"d": r["date"], "ex": round(r["excess"], 4), "ic": round(r["ic"], 3)}
                     for r in res[14]],
    }
    for h in HORIZONS:
        out["horizons"][str(h)] = {
            "excess": summarize(res[h], "excess"),
            "excess_net": summarize(res[h], "excess_net"),
            "ic": summarize(res[h], "ic"),
            "hit": summarize(res[h], "hit"),
            "top_med": summarize(res[h], "top_med"),
            "uni_med": summarize(res[h], "uni_med"),
        }
        out["breakout"][str(h)] = summarize(bo[h], "excess")
        out["promoted"][str(h)] = {"excess": summarize(promo[h], "excess"),
                                   "abs": summarize(promo[h], "abs")}
    v, note, hz = verdict(out["horizons"])
    out["verdict"], out["verdict_note"], out["verdict_horizon"] = v, note, hz

    # 판정 등급이 바뀌면 조용히 넘어가지 않는다 — 다음 일일 브리프가 이 필드를 읽어 경고한다.
    prev = ar.read_json(os.path.join(DATA_DIR, "backtest.json"), {}) or {}
    pv = prev.get("verdict")
    today = datetime.now(ar.KST).strftime("%Y-%m-%d")
    out["prev_verdict"] = pv
    if pv and pv != v:
        out["verdict_changed"] = True
        out["verdict_changed_at"] = today
        print("[backtest] ⚠ 판정 변경: %s → %s" % (pv, v))
    else:
        out["verdict_changed"] = bool(prev.get("verdict_changed")) and \
            prev.get("verdict_changed_at") == today
        out["verdict_changed_at"] = prev.get("verdict_changed_at")
    out["limits"] = [
        "스냅샷 지표(시총·유동성·홀더)는 과거값이 없어 백테스트에 포함되지 않는다 — 검증된 것은 차트축과 거래대금축뿐이다. 수급축은 아래 라이브 추적으로만 검증된다.",
        "알파 마켓의 역사가 약 1년으로 짧고 이 구간의 시장국면은 사실상 하나다. 다른 국면에서 같은 결과가 나온다는 보장이 없다.",
        "상장폐지 종목의 과거 캔들도 포함했으나, 캔들 자체가 제공되지 않는 종목은 여전히 빠진다(부분적 생존편향 잔존).",
        "체결은 종가 기준, 왕복 비용 %.1f%%만 가정했다. 알파는 호가가 얕아 실제 슬리피지는 더 클 수 있다." % (ROUND_TRIP_COST * 100),
        "일별 관측치는 보유구간이 겹쳐 독립이 아니다 — 유의성은 5일 블록 부트스트랩(2,000회)으로 계산했다.",
        "이 백테스트는 점수의 '상대적 순위 정보'를 검증한 것이며, 어떤 종목을 사라는 권고가 아니다.",
    ]
    ar.write_json(os.path.join(DATA_DIR, "backtest.json"), out)
    print("[backtest] 리밸런스 %d일 · 판정 %s" % (scanned, v))
    print("           %s" % note)
    return out


if __name__ == "__main__":
    main()
