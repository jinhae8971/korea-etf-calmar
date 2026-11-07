# -*- coding: utf-8 -*-
"""
KOSPI 엘리어트 파동 판정 — "5파 상승 후 ABC 조정" 시나리오를 주봉으로 매일 갱신

- 데이터: kospi_levels 의 로더 재사용 (Yahoo ^KS11 10년 주봉 + 네이버 확정 일봉으로 마지막 봉 보정)
- 파동: 주봉 고/저 지그재그 피벗 → 최근 고점(5파)과 그 앞 저점(0파)을 앵커로 1~4파를 규칙 기반 탐색
        (2파 < 0파 이탈 금지, 4파-1파 비중첩, 3파 최단 금지) → 고점 이후 A·B·C 추적
- 판정: C파 진행률(C/A), 0→5 되돌림, EMA50/200(주봉), RSI 다이버전스, MACD 히스토그램
- 산출: data/latest.json 의 message → Brief Hub(@spytltgld_strategy_bot)가 평일 08:00 KST에 발송

이 스크립트는 발송하지 않는다.

사용:
  python kospi_wave/kospi_wave.py            # 오늘(KST) 데이터가 이미 있으면 종료(멱등)
  python kospi_wave/kospi_wave.py --force    # 다시 계산
  python kospi_wave/kospi_wave.py --dry      # 파일을 쓰지 않고 본문만 출력
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "kospi_levels"))
import kospi_levels as K  # noqa: E402

KST = timezone(timedelta(hours=9))
OUT_PATH = os.path.join(HERE, "data", "latest.json")
HIST_PATH = os.path.join(HERE, "data", "history.json")
WEEKDAY_KO = "월화수목금토일"

ZZ_PCT = 0.06          # 지그재그 피벗 최소 반전폭(주봉)
PEAK_WIN = 78          # 5파 고점 탐색 창(주) ≈ 1.5년
BASE_WIN = 104         # 0파 저점 탐색 창(고점 이전, 주)
NEW_HIGH_TOL = 0.03    # 고점 3% 이내면 '조정 시작 전'으로 간주
FIBS = (0.382, 0.5, 0.618, 0.786)


# ============================================================
# 데이터
# ============================================================
def fetch(now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    weekly, daily = K.fetch_ohlc(now)                 # 3년 주봉(보정됨) + 확정 일봉
    try:                                              # EMA200(주) 정합을 위해 10년 주봉
        w10 = K._naive_dates(K._download("10y", "1wk", 260))
        weekly = K.patch_weekly_last(w10, daily)
    except Exception as e:  # pragma: no cover - 네트워크
        print(f"[fetch] 10년 주봉 실패 → 3년 주봉 사용(EMA200 근사): {e}")
    return weekly, daily


# ============================================================
# 지그재그 피벗
# ============================================================
def zigzag(df: pd.DataFrame, pct: float) -> list[dict]:
    """주봉 High/Low 기반 지그재그. 반환: [{i, t('H'|'L'), p, date}] — 마지막 피벗은 미확정일 수 있음."""
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    piv: list[dict] = []
    trend = 0                                  # +1 상승 추적, -1 하락 추적
    ext_i, ext_p = 0, hi[0]
    lo_i, lo_p, hi_i, hi_p = 0, lo[0], 0, hi[0]
    for i in range(1, len(df)):
        if trend == 0:
            if hi[i] > hi_p:
                hi_i, hi_p = i, hi[i]
            if lo[i] < lo_p:
                lo_i, lo_p = i, lo[i]
            if hi_p >= lo_p * (1 + pct) and hi_i > lo_i:
                piv.append({"i": lo_i, "t": "L", "p": lo_p}); trend, ext_i, ext_p = 1, hi_i, hi_p
            elif lo_p <= hi_p * (1 - pct) and lo_i > hi_i:
                piv.append({"i": hi_i, "t": "H", "p": hi_p}); trend, ext_i, ext_p = -1, lo_i, lo_p
            continue
        if trend == 1:
            if hi[i] >= ext_p:
                ext_i, ext_p = i, hi[i]
            elif lo[i] <= ext_p * (1 - pct):
                piv.append({"i": ext_i, "t": "H", "p": ext_p}); trend, ext_i, ext_p = -1, i, lo[i]
        else:
            if lo[i] <= ext_p:
                ext_i, ext_p = i, lo[i]
            elif hi[i] >= ext_p * (1 + pct):
                piv.append({"i": ext_i, "t": "L", "p": ext_p}); trend, ext_i, ext_p = 1, i, hi[i]
    if trend:
        piv.append({"i": ext_i, "t": "H" if trend == 1 else "L", "p": ext_p, "open": True})
    for p in piv:
        p["date"] = df.index[p["i"]].strftime("%Y-%m-%d")
    return piv


# ============================================================
# 상승 5파 카운트
# ============================================================
def count_impulse(df: pd.DataFrame, piv: list[dict]) -> dict | None:
    """최근 PEAK_WIN 주 최고가를 5파, 그 앞 BASE_WIN 주 최저가를 0파로 두고 1~4파를 규칙 기반 탐색."""
    n = len(df)
    hi, lo = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    s = max(0, n - PEAK_WIN)
    i5 = s + int(hi[s:].argmax())
    b = max(0, i5 - BASE_WIN)
    i0 = b + int(lo[b:i5 + 1].argmin())
    if i5 - i0 < 8:
        return None
    p0, p5 = lo[i0], hi[i5]
    H = [p for p in piv if p["t"] == "H" and i0 < p["i"] < i5]
    L = [p for p in piv if p["t"] == "L" and i0 < p["i"] < i5]

    best, best_sc = None, -1e9
    for h1, h3 in itertools.combinations(H, 2):
        for l2 in L:
            if not (h1["i"] < l2["i"] < h3["i"]):
                continue
            for l4 in L:
                if not (h3["i"] < l4["i"] < i5):
                    continue
                w1, w3, w5 = h1["p"] - p0, h3["p"] - l2["p"], p5 - l4["p"]
                # 규칙: 2파 0파 이탈 금지 / 3파 1파 고점 돌파 / 4파-1파 비중첩 / 3파 최단 금지 / 5파 3파 고점 돌파
                if l2["p"] <= p0 or h3["p"] <= h1["p"] or l4["p"] <= h1["p"] or p5 <= h3["p"]:
                    continue
                if w3 < min(w1, w5):
                    continue
                # 구간 내 극값이어야 함 (1·3파는 각 구간 최고, 2·4파는 최저)
                if hi[i0:l2["i"] + 1].max() > h1["p"] or lo[h1["i"]:h3["i"] + 1].min() < l2["p"] \
                        or hi[l2["i"]:l4["i"] + 1].max() > h3["p"] or lo[h3["i"]:i5 + 1].min() < l4["p"]:
                    continue
                # 점수: 2·4파는 같은 등급에서 가장 두드러진 조정이어야 함(하락률 %) — 4파 가중
                dep2 = (h1["p"] / l2["p"] - 1) * 100
                dep4 = (h3["p"] / l4["p"] - 1) * 100
                sc = 2.0 * dep4 + dep2
                sc += 5.0 * (w3 >= max(w1, w5))                      # 3파 최장(가장 흔함)
                sc -= 10.0 * (w1 > max(w3, w5))                      # 1파 최장(드묾)
                if sc > best_sc:
                    best_sc = sc
                    best = {"1": h1, "2": l2, "3": h3, "4": l4}
    out = {"0": {"i": i0, "p": p0, "date": df.index[i0].strftime("%Y-%m-%d")},
           "5": {"i": i5, "p": p5, "date": df.index[i5].strftime("%Y-%m-%d")},
           "valid": best is not None}
    if best:
        out.update({k: {"i": v["i"], "p": v["p"], "date": v["date"]} for k, v in best.items()})
    return out


# ============================================================
# 고점 이후 ABC
# ============================================================
def track_abc(df: pd.DataFrame, imp: dict, pct: float) -> dict:
    """5파 고점 이후 주봉에서 A(첫 저점) · B(반등 고점) · C(이후 진행) 추적."""
    i5, p5 = imp["5"]["i"], imp["5"]["p"]
    sub = df.iloc[i5:]
    hi, lo = sub["High"].to_numpy(float), sub["Low"].to_numpy(float)
    d = lambda k: sub.index[k].strftime("%Y-%m-%d")
    res: dict = {"stage": None}
    if len(sub) < 2 or lo.min() > p5 * (1 - NEW_HIGH_TOL):
        res["stage"] = "IMPULSE"                      # 아직 의미 있는 조정 없음
        return res
    # A: 고점 이후 처음으로 pct 이상 반등이 나오기 전까지의 최저점
    a_k, a_p, b_k = 0, lo[0], None
    for k in range(1, len(sub)):
        if lo[k] < a_p:
            a_k, a_p = k, lo[k]
        elif hi[k] >= a_p * (1 + pct) and k > a_k:
            b_k = k
            break
    res["A"] = {"p": a_p, "date": d(a_k), "k": a_k}
    if b_k is None:
        res["stage"] = "A"
        return res
    # B: A 이후 pct 이상 하락이 나오기 전까지의 최고점
    b_p, c_k = hi[b_k], None
    for k in range(b_k + 1, len(sub)):
        if hi[k] > b_p:
            b_k, b_p = k, hi[k]
        elif lo[k] <= b_p * (1 - pct):
            c_k = k
            break
    res["B"] = {"p": b_p, "date": d(b_k), "k": b_k}
    if b_p > p5:
        res["stage"] = "NEW_HIGH"                     # B가 고점 돌파 → 조정 끝 / 5파 연장
        return res
    if c_k is None:
        res["stage"] = "B"
        return res
    c_rel = int(lo[b_k:].argmin())
    res["C"] = {"p": float(lo[b_k + c_rel]), "date": d(b_k + c_rel), "k": b_k + c_rel}
    res["stage"] = "C"
    return res


# ============================================================
# 판정
# ============================================================
# ============================================================
# 과거 유사 사례 (급등 → 조정) — 라이브러리는 analogs.json (tools/build_analogs.py로 1회 생성)
# ============================================================
ANALOG_PATH = os.path.join(HERE, "analogs.json")
ANALOG_TOP = 8
CHART_PATH = os.path.join(HERE, "data", "analog_chart.png")
CHART_REL = "kospi_wave/data/analog_chart.png"           # 허브가 레포 루트 기준으로 읽는 경로
EN = {"코스피": "KOSPI", "코스닥": "KOSDAQ", "닛케이": "Nikkei", "대만": "TAIEX", "상해": "Shanghai", "선전": "Shenzhen",
      "항셍": "Hang Seng", "나스닥": "Nasdaq", "SOX": "SOX", "S&P500": "S&P500", "다우": "Dow", "센섹스": "Sensex",
      "보베스파": "Bovespa", "인니": "Jakarta", "아르헨": "Merval", "튀르키예": "BIST100"}
FIRST_LOW_WIN = 60          # 1차 바닥 = 고점 후 60주 내 최저 종가


def _shape(seg, n: int = 40):
    import numpy as np
    x = np.log(np.asarray(seg, float)); x = (x - x[0]) / (x[-1] - x[0])
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


def find_analogs(df: pd.DataFrame, imp: dict, lib: dict | None = None) -> dict | None:
    """현재 상승 모양 + 고점 이후 경로를 과거 에피소드와 비교 → 상위 사례와 1차 바닥 시점·깊이 분포."""
    import numpy as np
    lib = lib if lib is not None else load(ANALOG_PATH)
    if not lib or not lib.get("episodes"):
        return None
    c = df["Close"].to_numpy(float)
    i5 = imp["5"]["i"]
    # 종가 기준 앵커: 5파 주의 종가 고점, 0파 구간 최저 종가
    i0 = imp["0"]["i"] + int(np.argmin(c[imp["0"]["i"]:i5 + 1]))
    pk = i5 + int(np.argmax(c[i5:min(len(c), i5 + 3)]))
    n_post = len(c) - 1 - pk
    if n_post < 2 or pk - i0 < 10:
        return None
    L = np.log(c[pk] / c[i0])
    cs = _shape(c[i0:pk + 1])
    cpost = np.log(c[pk:] / c[pk]) / L
    dd_now = c[-1] / c[pk] - 1
    rows = []
    for e in lib["episodes"]:
        post = np.asarray(e["post"], float) / 100
        if len(post) <= n_post:
            continue
        Lh = np.log(e["x"])
        pp = np.log(post[:n_post + 1]) / Lh
        sc = (np.sqrt(np.mean((np.asarray(e["shape"]) - cs) ** 2))
              + 1.5 * np.sqrt(np.mean((pp - cpost) ** 2))
              + 0.5 * abs(Lh - L) + 0.5 * abs(np.log(e["rise_w"] / (pk - i0))))
        w = post[:FIRST_LOW_WIN + 1]
        k1 = int(np.argmin(w))
        nh = np.where(post[1:] > 1.0)[0]
        rows.append({"label": f"{e['name']} {e['peak'][:4]}", "label_en": f"{EN.get(e['name'], e['name'])} {e['peak'][:4]}",
                     "path": [round(float(x * 100), 1) for x in post[:FIRST_LOW_WIN + 21]],
                     "score": float(sc), "dd_same": float(post[n_post] - 1),
                     "low_w": k1, "low_dd": float(w[k1] - 1), "min_dd": float(post.min() - 1),
                     "newhigh_w": int(nh[0] + 1) if len(nh) else None})
    rows.sort(key=lambda r: r["score"])
    fast = [r for r in rows if r["dd_same"] <= 0.6 * dd_now]          # 같은 시점에 비슷하게 급락한 사례 우선
    top = (fast if len(fast) >= 5 else rows)[:ANALOG_TOP]
    lw = sorted(r["low_w"] for r in top)
    ld = sorted(r["low_dd"] for r in top)
    peak_date = df.index[pk]
    med_w = float(np.median(lw))
    deep = [r for r in top if r["newhigh_w"] is None or r["newhigh_w"] > 156]   # 3년 내 고점 미회복 = 장기 약세
    return {
        "cur_path": [round(float(x / c[pk] * 100), 1) for x in c[pk:]],
        "peak_close": float(c[pk]), "peak_date": peak_date.strftime("%Y-%m-%d"), "weeks_since": n_post,
        "dd_now": float(dd_now), "top": top, "filtered": len(fast) >= 5,
        "low_w_range": [lw[0], lw[-1]], "low_w_med": med_w,
        "low_date_range": [(peak_date + pd.Timedelta(weeks=lw[0])).strftime("%Y-%m-%d"),
                           (peak_date + pd.Timedelta(weeks=lw[-1])).strftime("%Y-%m-%d")],
        "low_date_med": (peak_date + pd.Timedelta(weeks=med_w)).strftime("%Y-%m-%d"),
        "low_dd_med": float(np.median(ld)), "low_dd_range": [ld[0], ld[-1]],
        "low_level_med": float(c[pk] * (1 + np.median(ld))),
        "deep_n": len(deep), "deep_names": [r["label"] for r in deep],
    }


def render_analog_chart(an: dict, path: str = CHART_PATH, n_lines: int = 6) -> str | None:
    """현재 코스피 vs 유사 사례 경로(고점=100) 오버레이 PNG. 한글 폰트가 없는 러너를 고려해 영문 라벨."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # pragma: no cover
        print(f"[chart] matplotlib 없음: {e}")
        return None
    top = an["top"][:n_lines]
    fig, ax = plt.subplots(figsize=(8, 5), dpi=110)
    pal = ["#eb6834", "#1baf7a", "#eda100", "#6250d6", "#e34948", "#888780"]
    for r, col, ls in zip(top, pal, ["--", ":", "--", ":", "--", ":"]):
        ax.plot(range(len(r["path"])), r["path"], ls, color=col, lw=1.4, label=r["label_en"])
    cur = an["cur_path"]
    ax.plot(range(len(cur)), cur, color="#2a78d6", lw=3, label="KOSPI now", zorder=5)
    ax.plot(len(cur) - 1, cur[-1], "o", color="#2a78d6", ms=7, zorder=6)
    a, b = an["low_w_range"]
    ax.axvspan(a, b, color="#888780", alpha=0.12, lw=0)
    ax.text((a + b) / 2, 0.02, f"1st-low range {a}-{b}w", transform=ax.get_xaxis_transform(),
            ha="center", fontsize=8, color="#444441")
    lvl = 100 * (1 + an["low_dd_med"])
    ax.axhline(lvl, color="#444441", lw=1, ls="-.")
    ax.axvline(an["low_w_med"], color="#444441", lw=1, ls="-.")
    ax.text(an["low_w_med"] + 0.8, 97, f"median 1st low: week {an['low_w_med']:.0f} ({an['low_date_med'][:7]})",
            fontsize=8, color="#444441")
    ax.text(1, lvl + 1.2, f"median depth {an['low_dd_med'] * 100:.0f}% = {an['low_level_med']:,.0f}", fontsize=8, color="#444441")
    ax.set_xlim(0, FIRST_LOW_WIN + 20)
    ax.set_ylim(max(20, min(min(min(r["path"]) for r in top), min(cur)) - 5), 102)
    ax.set_xlabel(f"weeks after peak ({an['peak_date']}, close {an['peak_close']:,.0f})")
    ax.set_ylabel("% of peak")
    ax.set_title(f"KOSPI correction vs past analogs  (week {an['weeks_since']}, {an['dd_now'] * 100:.0f}%)", fontsize=11)
    ax.grid(axis="y", color="#e1e0d9", lw=0.8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(fontsize=8, frameon=False, ncol=2, loc="lower left")
    fig.tight_layout()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path)
    plt.close(fig)
    return path


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def keep_previous_count(df: pd.DataFrame, imp: dict, prev: dict | None) -> dict:
    """같은 0·5파 앵커면 전날 1~4파 라벨을 유지 — 피벗 미세 변화로 카운트가 날마다 흔들리지 않게."""
    pi = (prev or {}).get("impulse") or {}
    if not (pi.get("valid") and pi.get("0", {}).get("date") == imp["0"]["date"]
            and pi.get("5", {}).get("date") == imp["5"]["date"]):
        return imp
    idx = {d.strftime("%Y-%m-%d"): i for i, d in enumerate(df.index)}
    out = dict(imp)
    for k in ("1", "2", "3", "4"):
        d = pi[k]["date"]
        if d not in idx:
            return imp
        col = "High" if k in ("1", "3") else "Low"
        out[k] = {"i": idx[d], "p": float(df[col].iloc[idx[d]]), "date": d}
    out["valid"], out["kept"] = True, True
    return out


def analyze(df: pd.DataFrame, daily: pd.DataFrame, prev_latest: dict | None = None) -> dict:
    close = df["Close"]
    price = float(daily["Close"].iloc[-1])
    prev = float(daily["Close"].iloc[-2])
    piv = zigzag(df, ZZ_PCT)
    imp = count_impulse(df, piv)
    if imp is None:
        raise RuntimeError("상승 파동 앵커를 찾지 못함")
    imp = keep_previous_count(df, imp, prev_latest)
    abc = track_abc(df, imp, ZZ_PCT)

    e50, e200 = float(ema(close, 50).iloc[-1]), float(ema(close, 200).iloc[-1])
    r = K.rsi(close, 14)
    rsi_now, rsi_sma = float(r.iloc[-1]), float(r.rolling(14).mean().iloc[-1])
    _, _, hist = K.macd(close, 12, 26, 9)
    h0, h1, h2 = (float(hist.iloc[-k]) for k in (1, 2, 3))
    hist_state = "확대" if abs(h0) > abs(h1) else "축소"
    hist_turn = h0 < 0 and h0 > h1 > h2                # 음(-)의 히스토그램 2주 연속 축소
    st_line, st_dir = K.supertrend(df, 20, 4.0)
    st_now, st_up = float(st_line.iloc[-1]), int(st_dir.iloc[-1]) == 1

    p0, p5 = imp["0"]["p"], imp["5"]["p"]
    rise = p5 - p0
    retr_now = (p5 - price) / rise                     # 종가 기준 되돌림
    fib = {f: p5 - rise * f for f in FIBS}

    out = {
        "price": price, "prev": prev, "chg": price - prev, "chg_pct": (price / prev - 1) * 100,
        "close_date": daily.index[-1].strftime("%Y-%m-%d"),
        "impulse": imp, "abc": abc, "fib": {str(k): v for k, v in fib.items()},
        "retr_now": retr_now, "ema50": e50, "ema200": e200,
        "rsi": rsi_now, "rsi_sma": rsi_sma, "macd_hist": h0, "hist_state": hist_state,
        "hist_turn": hist_turn, "st": st_now, "st_up": st_up,
    }

    stage = abc["stage"]
    if stage == "C":
        A, B, C = abc["A"], abc["B"], abc["C"]
        a_len = p5 - A["p"]
        c_len = B["p"] - C["p"]
        out["c_ratio"] = c_len / a_len if a_len else 0.0
        out["c_targets"] = {"0.618": B["p"] - 0.618 * a_len, "1.0": B["p"] - a_len,
                            "1.618": B["p"] - 1.618 * a_len}
        # RSI 다이버전스: C 저점 가격 < A 저점인데 RSI(C) > RSI(A)
        rsi_a = float(r.iloc[imp["5"]["i"] + A["k"]])
        rsi_c = float(r.iloc[imp["5"]["i"] + C["k"]])
        out["rsi_a"], out["rsi_c"] = rsi_a, rsi_c
        out["bull_div"] = C["p"] < A["p"] and rsi_c > rsi_a + 2
        out["rebound"] = (price - C["p"]) / c_len if c_len else 0.0
        out["a_weeks"] = A["k"]                                     # 5파 고점 → A 저점
        out["c_weeks"] = len(df) - 1 - (imp["5"]["i"] + B["k"])     # B 고점 → 이번 주
        out["hist_shrink1"] = abs(h0) < abs(h1)
    out["phase"], out["phase_label"], out["tone"] = judge(out)
    if abc["stage"] not in ("IMPULSE", "NEW_HIGH"):
        try:
            out["analog"] = find_analogs(df, imp)
        except Exception as e:  # pragma: no cover - 라이브러리 손상 등은 본 판정을 막지 않음
            print(f"[analog] 실패: {e}")
            out["analog"] = None
    if abc["stage"] == "C":
        out["score_items"] = score_items(out)
        out["score"] = round(sum(it["pts"] for it in out["score_items"]))
    return out


# ============================================================
# C파 완성도 점수 (0~100) + 지표 신호등
# ============================================================
def lerp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    """x0→y0, x1→y1 선형, 구간 밖은 끝값 고정."""
    if x1 == x0:
        return y1
    t = min(max((x - x0) / (x1 - x0), 0.0), 1.0)
    return y0 + (y1 - y0) * t


def light(pts: float, mx: float) -> str:
    r = pts / mx if mx else 0
    return "🟢" if r >= 0.8 else ("🟡" if r >= 0.3 else "🔴")


SCORE_GRADES = ((80, "완성·반등 확인"), (60, "완성 유력"), (40, "완성 접근"), (0, "진행 중"))


def grade(score: float) -> str:
    return next(g for th, g in SCORE_GRADES if score >= th)


def score_items(o: dict) -> list[dict]:
    """가격 40 · 시간 10 · 모멘텀 30 · 반등 확인 20 = 100. 각 항목 신호등 = 배점 대비 80%↑ 🟢 / 30%↑ 🟡 / 그 외 🔴."""
    imp, abc, fib = o["impulse"], o["abc"], o["fib"]
    A, B, C = abc["A"], abc["B"], abc["C"]
    price, cr, rt = o["price"], o["c_ratio"], o["retr_now"]
    ct = o["c_targets"]
    it: list[dict] = []

    def add(group, mx, pts, text):
        it.append({"group": group, "max": mx, "pts": round(pts, 1), "light": light(pts, mx), "text": text})

    # --- 가격 (40) ---
    pts = lerp(cr, 0.382, 0.618, 0, 10) + (lerp(cr, 0.618, 1.0, 0, 5) if cr > 0.618 else 0)
    add("가격", 15, pts, f"C/A {cr:.2f}배 · 0.618={K.fmt(ct['0.618'])}")
    pts = lerp(rt, 0.382, 0.5, 0, 8) + (lerp(rt, 0.5, 0.618, 0, 7) if rt > 0.5 else 0)
    add("가격", 15, pts, f"되돌림 {rt * 100:.0f}% · 61.8%={K.fmt(float(fib['0.618']))}")
    if imp.get("valid"):
        w4 = imp["4"]["p"]
        gap = price / w4 - 1
        add("가격", 10, lerp(gap, 0.10, 0.03, 0, 10), f"4파 저점 {K.fmt(w4)} ({gap * 100:+.1f}%)")
    else:
        add("가격", 10, 0, "4파 저점 미확정")

    # --- 시간 (10) ---
    tr = o["c_weeks"] / o["a_weeks"] if o["a_weeks"] else 0
    add("시간", 10, lerp(tr, 0.382, 1.0, 0, 10), f"C {o['c_weeks']}주 / A {o['a_weeks']}주 ({tr:.1f}배)")

    # --- 모멘텀 (30) ---
    add("모멘텀", 10, lerp(o["rsi"], 45, 30, 0, 10), f"RSI {o['rsi']:.1f} (30 이하 만점)")
    add("모멘텀", 10, 10 if o["bull_div"] else 0,
        f"RSI 다이버전스 {'있음' if o['bull_div'] else '없음'} (A {o['rsi_a']:.0f}→C {o['rsi_c']:.0f})")
    pts = 10 if o["hist_turn"] else (5 if o["hist_shrink1"] else 0)
    st = "2주 연속 축소" if o["hist_turn"] else ("축소 시작" if o["hist_shrink1"] else "확대")
    add("모멘텀", 10, pts, f"MACD 히스토 {o['macd_hist']:+,.0f} {st}")

    # --- 반등 확인 (20) ---
    rb = o["rebound"]
    add("반등", 10, lerp(rb, 0.0, 0.382, 0, 10),
        f"C저점 대비 반등 {rb * 100:.0f}% (38.2%={K.fmt(C['p'] + 0.382 * (B['p'] - C['p']))})")
    e50 = o["ema50"]
    add("반등", 10, 10 if price > e50 else lerp(price / e50 - 1, -0.10, 0.0, 0, 5),
        f"EMA50 {K.fmt(e50)} ({(price / e50 - 1) * 100:+.1f}%)")
    return it


def judge(o: dict) -> tuple[str, str, str]:
    """(코드, 한 줄 판정, 색) — 색: red/amber/green/none"""
    stage = o["abc"]["stage"]
    price, e50, e200 = o["price"], o["ema50"], o["ema200"]
    if stage == "IMPULSE":
        return "IMPULSE", "5파 진행 중 — 고점권, 조정 미개시", "none"
    if stage == "NEW_HIGH":
        return "NEW_HIGH", "B파가 고점 돌파 — 조정 종료 또는 5파 연장", "green"
    if stage == "A":
        return "A", "A파 하락 진행 — 반등(B) 대기", "red"
    if stage == "B":
        return "B", "B파 반등 진행 — C파 하락 대비", "amber"
    # C 단계
    if price < e200:
        return "C_EXT", "주봉 종가 EMA200 이탈 — 단순 ABC보다 큰 조정(1-2-3) 경계", "red"
    if o["c_ratio"] > 1.618:
        return "C_EXT", "C가 A의 1.618배 초과 — 확장 조정 경계", "red"
    if price > o["abc"]["B"]["p"]:
        return "DONE", "B 고점 회복 — ABC 조정 종료 확정", "green"
    if price > e50 or (o["rebound"] >= 0.382 and o["hist_turn"]):
        return "C_END", "C파 종료 신호 — 반등 확인 단계", "green"
    in_zone = o["c_ratio"] >= 0.618 or o["retr_now"] >= 0.5
    if in_zone and (o["bull_div"] or o["hist_turn"]):
        return "C_ZONE_SIG", "C파 완성 구간 + 바닥 신호 출현", "amber"
    if in_zone:
        return "C_ZONE", "C파 진행 — 완성 후보 구간 진입, 바닥 신호 대기", "amber"
    return "C", "C파 하락 진행 — 완성 구간 전", "red"


# ============================================================
# 메시지
# ============================================================
f = K.fmt
DOT = {"red": "🔴", "amber": "🟡", "green": "🟢", "none": ""}


def sd(date: str) -> str:
    return f"{date[2:4]}/{date[5:7]}/{date[8:10]}"


def pct(a: float, b: float) -> str:
    return f"{(a / b - 1) * 100:+.1f}%"


def build_message(o: dict, prev: dict | None) -> str:
    imp, abc = o["impulse"], o["abc"]
    dd = datetime.strptime(o["close_date"], "%Y-%m-%d")
    arrow = "▲" if o["chg"] >= 0 else "▼"
    L = [f"<b>KOSPI 엘리어트 파동</b>  {dd:%m/%d}({WEEKDAY_KO[dd.weekday()]}) 종가 기준",
         "<i>주봉 · 기본 시나리오 5파 상승 → ABC 조정</i>", "",
         f"<b>{f(o['price'])}</b>  {arrow} {o['chg']:+,.0f} ({o['chg_pct']:+.2f}%)",
         f"{DOT[o['tone']]} <b>{o['phase_label']}</b>".strip()]

    # 전일 대비 — 판정이 바뀌었을 때만 강조
    if prev and prev.get("phase") and prev["phase"] != o["phase"]:
        L.append(f"   └ 판정 변경: {prev.get('phase_label', prev['phase'])} → 현재")

    L += ["", "〈파동 카운트〉",
          f"0  {f(imp['0']['p'])}  <i>{sd(imp['0']['date'])}</i>"]
    if imp["valid"]:
        L.append("  ".join(f"{k} {f(imp[k]['p'])}" for k in ("1", "2", "3", "4")))
    else:
        L.append("<i>1~4파 내부 카운트 미확정 (규칙 충족 조합 없음)</i>")
    L.append(f"5  {f(imp['5']['p'])}  <i>{sd(imp['5']['date'])} 고점</i>")
    for k in ("A", "B", "C"):
        if k in abc:
            tail = "  <i>진행 중</i>" if (k == "C" and o["phase"] not in ("DONE",)) else ""
            L.append(f"{k}  {f(abc[k]['p'])}  <i>{sd(abc[k]['date'])}</i>{tail}")

    fib = o["fib"]
    if abc["stage"] == "C":
        sc = o["score"]
        bar = "▰" * round(sc / 10) + "▱" * (10 - round(sc / 10))
        diff = ""
        if prev and isinstance(prev.get("score"), (int, float)):
            dlt = sc - prev["score"]
            diff = f"  <i>전일 {dlt:+d}</i>" if dlt else "  <i>전일 동일</i>"
        L += ["", f"〈C파 완성도〉 <b>{sc}/100</b> · {grade(sc)}{diff}", bar]
        for g in ("가격", "시간", "모멘텀", "반등"):
            grp = [x for x in o["score_items"] if x["group"] == g]
            got, mx = sum(x["pts"] for x in grp), sum(x["max"] for x in grp)
            L.append(f"<b>{g}</b> {got:.0f}/{mx}")
            for x in grp:
                L.append(f"{x['light']} {x['text']}")
        # 반대 시나리오 위험 — 점수와 별개
        d200 = o["price"] / o["ema200"] - 1
        lt = "🔴" if d200 < 0 else ("🟡" if d200 < 0.05 else "🟢")
        L += ["<b>위험</b>", f"{lt} EMA200 {f(o['ema200'])} ({d200 * 100:+.1f}%) · 이탈 시 큰 조정"]
    else:
        L += ["", "〈위치〉",
              f"· 0→5 상승분 되돌림 {o['retr_now'] * 100:.0f}% (종가)",
              f"   38.2% {f(fib['0.382'])} · 50% {f(fib['0.5'])} · 61.8% {f(fib['0.618'])}",
              f"· RSI {o['rsi']:.1f} · MACD 히스토 {o['macd_hist']:+,.0f} {o['hist_state']}",
              f"· EMA50 {f(o['ema50'])} · EMA200 {f(o['ema200'])}",
              "<i>C파 진입 후 완성도 점수가 표시됩니다</i>"]

    an = o.get("analog")
    if an:
        ym = lambda d: f"{d[2:4]}/{d[5:7]}"
        rem = max(0, round(an["low_w_med"] - an["weeks_since"]))
        lvl, cur = an["low_level_med"], o["price"]
        L += ["", "〈과거 유사 사례 · 조정 기간〉",
              "유사도 상위: " + " · ".join(r["label"] for r in an["top"][:3]),
              f"· 1차 바닥 시점 고점 후 {an['low_w_range'][0]}~{an['low_w_range'][1]}주 (중앙 {an['low_w_med']:.0f}주)",
              f"   → {ym(an['low_date_range'][0])}~{ym(an['low_date_range'][1])}, 중앙 {ym(an['low_date_med'])}",
              f"· 현재 {an['weeks_since']}주차 · 남은 기간 약 {rem}주",
              f"· 1차 바닥 낙폭 중앙 {an['low_dd_med'] * 100:.0f}% → 약 {f(lvl)} (현재 대비 {pct(lvl, cur)})"]
        lt = "🔴" if an["deep_n"] > len(an["top"]) / 2 else ("🟡" if an["deep_n"] else "🟢")
        L.append(f"{lt} 3년 내 고점 미회복 {an['deep_n']}/{len(an['top'])}건"
                 + (f" ({', '.join(an['deep_names'][:3])})" if an["deep_n"] else ""))
        if an["dd_now"] < min(r["dd_same"] for r in an["top"]):
            L.append(f"<i>같은 주차 기준 이번 하락({an['dd_now'] * 100:.0f}%)이 비교 사례 중 가장 빠름</i>")

    L += ["", "〈트리거〉"]
    if abc["stage"] == "C":
        L.append(f"· C 종료 확인: 주봉 종가 EMA50 {f(o['ema50'])} 회복")
        L.append(f"· 조정 종료 확정: B 고점 {f(abc['B']['p'])} 돌파")
        sup = sorted({round(fib["0.618"]), round(o["c_targets"]["0.618"]), round(o["ema200"])}, reverse=True)
        sup = [s for s in sup if s < o["price"]] or sup
        L.append("· 아래 지지 후보: " + " · ".join(f(s) for s in sup))
    elif abc["stage"] in ("A", "B"):
        L.append(f"· 되돌림 기준선: 50% {f(fib['0.5'])} · 61.8% {f(fib['0.618'])}")
        L.append(f"· 고점 {f(imp['5']['p'])} 재돌파 시 조정 시나리오 무효")
    else:
        L.append(f"· 고점 {f(imp['5']['p'])} 대비 {pct(o['price'], imp['5']['p'])}")

    L += ["", "<i>※ 신호등: 배점 80%↑ 🟢 · 30%↑ 🟡 · 미만 🔴. 파동 카운트는 해석이 갈릴 수 있는 참고용 분석.</i>"]
    return "\n".join(L)


# ============================================================
# 입출력
# ============================================================
def load(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def slim(o: dict) -> dict:
    keep = ("price", "chg", "chg_pct", "close_date", "phase", "phase_label", "tone", "retr_now",
            "ema50", "ema200", "rsi", "macd_hist", "c_ratio", "bull_div", "hist_turn", "score")
    return {k: o[k] for k in keep if k in o}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    prev_latest = load(OUT_PATH)
    if not a.force and not a.dry and prev_latest and str(prev_latest.get("as_of_kst", ""))[:10] == today \
            and prev_latest.get("message"):
        print(f"[skip] 오늘({today}) 데이터 있음")
        return 0

    weekly, daily = fetch(now)
    o = analyze(weekly, daily, prev_latest)
    hist = load(HIST_PATH) or []
    prev = next((h for h in reversed(hist) if h.get("close_date") != o["close_date"]), None)
    msg = build_message(o, prev)
    if a.dry:
        print(msg)
        return 0

    photos = []
    if o.get("analog") and render_analog_chart(o["analog"]):
        photos = [CHART_REL]
    payload = {"as_of_kst": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"), "source": K.DAILY_SOURCE,
               **{k: v for k, v in o.items()}, "message": msg,
               "photos": photos, "photo_caption": "KOSPI 조정 경로 vs 과거 유사 사례 (고점=100)"}
    write(OUT_PATH, payload)
    hist = [h for h in hist if h.get("close_date") != o["close_date"]] + [slim(o)]
    write(HIST_PATH, hist[-260:])
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
