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
    out["phase"], out["phase_label"], out["tone"] = judge(out)
    return out


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
    L += ["", "〈위치〉",
          f"· 0→5 상승분 되돌림 {o['retr_now'] * 100:.0f}% (종가)",
          f"   38.2% {f(fib['0.382'])} · 50% {f(fib['0.5'])} · 61.8% {f(fib['0.618'])} · 78.6% {f(fib['0.786'])}"]
    if abc["stage"] == "C":
        ct = o["c_targets"]
        L.append(f"· C/A 길이 {o['c_ratio']:.2f}배 — C=0.618A {f(ct['0.618'])} · C=A {f(ct['1.0'])}")
        if imp["valid"]:
            L.append(f"· 직전 4파 저점 {f(imp['4']['p'])} ({pct(o['price'], imp['4']['p'])})")

    L += ["", "〈바닥 신호〉"]
    if abc["stage"] == "C":
        div = "<b>상승 다이버전스</b>" if o["bull_div"] else "다이버전스 없음"
        L.append(f"· RSI {o['rsi']:.1f} (A저점 {o['rsi_a']:.1f} → C저점 {o['rsi_c']:.1f}) {div}")
    else:
        L.append(f"· RSI {o['rsi']:.1f} / SMA {o['rsi_sma']:.1f}")
    turn = " · <b>2주 연속 축소(전환 초기)</b>" if o["hist_turn"] else ""
    L.append(f"· MACD 히스토 {o['macd_hist']:+,.0f} {o['hist_state']}{turn}")
    L.append(f"· EMA50 {f(o['ema50'])} ({pct(o['price'], o['ema50'])}) · EMA200 {f(o['ema200'])} ({pct(o['price'], o['ema200'])})")
    L.append(f"· 슈퍼트렌드 {f(o['st'])} ({'상승' if o['st_up'] else '하락'})")

    L += ["", "〈트리거〉"]
    if abc["stage"] == "C":
        L.append(f"· C 종료 확인: 주봉 종가 EMA50 {f(o['ema50'])} 회복")
        L.append(f"· 조정 종료 확정: B 고점 {f(abc['B']['p'])} 돌파")
        L.append(f"· 반대 시나리오: 주봉 종가 EMA200 {f(o['ema200'])} 이탈 → 더 큰 조정")
        sup = sorted({round(fib["0.618"]), round(o["c_targets"]["0.618"]), round(o["ema200"])}, reverse=True)
        sup = [s for s in sup if s < o["price"]] or sup
        L.append("· 아래 지지 후보: " + " · ".join(f(s) for s in sup))
    elif abc["stage"] in ("A", "B"):
        L.append(f"· 되돌림 기준선: 50% {f(fib['0.5'])} · 61.8% {f(fib['0.618'])}")
        L.append(f"· 고점 {f(imp['5']['p'])} 재돌파 시 조정 시나리오 무효")
    else:
        L.append(f"· 고점 {f(imp['5']['p'])} 대비 {pct(o['price'], imp['5']['p'])}")

    L += ["", "<i>※ 파동 카운트는 해석이 갈릴 수 있는 참고용 분석. 최종 판단·리스크 관리는 본인.</i>"]
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
            "ema50", "ema200", "rsi", "macd_hist", "c_ratio", "bull_div", "hist_turn")
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

    payload = {"as_of_kst": now.strftime("%Y-%m-%dT%H:%M:%S+09:00"), "source": K.DAILY_SOURCE,
               **{k: v for k, v in o.items()}, "message": msg}
    write(OUT_PATH, payload)
    hist = [h for h in hist if h.get("close_date") != o["close_date"]] + [slim(o)]
    write(HIST_PATH, hist[-260:])
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
