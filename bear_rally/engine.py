"""Bear Market Rally Detector (BMR) — 판정 엔진 (TradingView Pine v6 지표의 파이썬 이식).

외부 의존성 없음. 확정된 일봉만 받아 봉 단위로 Pine 스크립트와 같은 순서로 계산한다.

  ① 레짐 판정 → ② 반등 포착 → ③ 진위 판별 (베어랠리 vs 레짐 전환)

상태:  0 강세 · 1 베어(하락) · 2 반등 감시 · 3 베어랠리 판정 · 4 전환 후보

Pine 대비 차이 (의도된 것):
  · 주봉 값은 일봉을 월요일 시작 주로 묶어 만든 '직전 확정 주봉'을 쓴다 (= request.security(..., "W")[1] + lookahead_on).
  · 미국 브레드스는 S5TH·S5FI·NYSE 등락 대신 대형주 유니버스로 근사한다(breadth.py).
    근사치라 ZBT 단독으로 전환 후보를 띄우는 우선권은 끄고(zbt_override=False) 전환 점수 1점으로만 반영한다.
"""
from __future__ import annotations

from datetime import date, timedelta

S_BULL, S_BEAR, S_RALLY, S_FAILED, S_SHIFT = 0, 1, 2, 3, 4
STATE_KO = {0: "강세", 1: "베어", 2: "반등 감시", 3: "베어랠리 판정", 4: "전환 후보"}
STATE_EN = {0: "Bull", 1: "Bear", 2: "Rally watch", 3: "Bear rally", 4: "Shift candidate"}

DEFAULTS = {
    "ma_len": 200, "ma50_len": 50, "slope_len": 20, "dd_len": 63,
    "dd_stock": 10.0, "dd_crypto": 20.0,
    "rally_stock": 10.0, "rally_crypto": 20.0,
    "rsi_len": 14, "rsi_ceil": 60.0, "rsi_bull": 70.0, "rsi_floor": 40.0,
    "touch_tol": 1.0, "atr_len": 14, "rej_atr": 2.0, "rearm_frac": 50.0,
    "min_hits": 3, "shift_stock": 3, "shift_crypto": 3, "hold_weeks": 2,
    "zbt_near_lvl": 0.58, "zbt_mem": 40, "div_tol": 5.0,
    "zbt_lo": 0.40, "zbt_hi": 0.615, "zbt_win": 10,
    "zbt_override": False,
}


# ----------------------------------------------------------------------------- None-안전 비교 (Pine v6: na 비교 = false)
def _ok(*xs) -> bool:
    return all(x is not None for x in xs)


def lt(a, b) -> bool:
    return _ok(a, b) and a < b


def le(a, b) -> bool:
    return _ok(a, b) and a <= b


def gt(a, b) -> bool:
    return _ok(a, b) and a > b


def ge(a, b) -> bool:
    return _ok(a, b) and a >= b


# ----------------------------------------------------------------------------- 지표 (Pine 정의와 같은 초기화)
def sma(xs: list, n: int) -> list:
    out, s, cnt = [None] * len(xs), 0.0, 0
    window: list = []
    for i, x in enumerate(xs):
        window.append(x)
        if x is not None:
            s += x
            cnt += 1
        if len(window) > n:
            y = window.pop(0)
            if y is not None:
                s -= y
                cnt -= 1
        if len(window) == n and cnt == n:
            out[i] = s / n
    return out


def ema(xs: list, n: int) -> list:
    """SMA 시드 EMA (alpha = 2/(n+1)) — 첫 n개 평균으로 시작."""
    out = [None] * len(xs)
    alpha, prev, seed = 2.0 / (n + 1), None, []
    for i, x in enumerate(xs):
        if x is None:
            out[i] = prev
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = alpha * x + (1 - alpha) * prev
        out[i] = prev
    return out


def rma(xs: list, n: int) -> list:
    """Wilder 이동평균 (Pine ta.rma: 첫 값은 SMA, 이후 alpha = 1/n)."""
    out = [None] * len(xs)
    prev, seed = None, []
    for i, x in enumerate(xs):
        if x is None:
            out[i] = prev
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == n:
                prev = sum(seed) / n
                out[i] = prev
            continue
        prev = (x + (n - 1) * prev) / n
        out[i] = prev
    return out


def rsi(close: list, n: int) -> list:
    up, dn = [None], [None]
    for i in range(1, len(close)):
        ch = close[i] - close[i - 1]
        up.append(max(ch, 0.0))
        dn.append(max(-ch, 0.0))
    ru, rd = rma(up, n), rma(dn, n)
    out = [None] * len(close)
    for i in range(len(close)):
        if ru[i] is None or rd[i] is None:
            continue
        if rd[i] == 0:
            out[i] = 100.0 if ru[i] > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + ru[i] / rd[i])
    return out


def atr(high: list, low: list, close: list, n: int) -> list:
    tr = []
    for i in range(len(close)):
        if i == 0:
            tr.append(high[i] - low[i])
        else:
            pc = close[i - 1]
            tr.append(max(high[i] - low[i], abs(high[i] - pc), abs(low[i] - pc)))
    return rma(tr, n)


def rolling(xs: list, n: int, fn) -> list:
    out = [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = [x for x in xs[i - n + 1:i + 1] if x is not None]
        if len(w) == n:
            out[i] = fn(w)
    return out


def bars_since(flags: list) -> list:
    out, last = [None] * len(flags), None
    for i, f in enumerate(flags):
        if f:
            last = i
        out[i] = None if last is None else i - last
    return out


# ----------------------------------------------------------------------------- 주봉 (직전 확정 주봉 값을 일봉에 매핑)
def week_key(d: str) -> str:
    dt = date.fromisoformat(d)
    return (dt - timedelta(days=dt.weekday())).isoformat()


def weekly_frame(dates: list, close: list) -> dict:
    """일봉 → 주봉(월요일 시작). 반환값은 일봉 인덱스별 '직전 확정 주봉'의 BMSB·40주선·연속 안착 수."""
    wk_order, wk_close = [], {}
    for d, c in zip(dates, close):
        k = week_key(d)
        if k not in wk_close:
            wk_order.append(k)
        wk_close[k] = c                               # 주의 마지막 일봉 종가 = 주봉 종가
    wc = [wk_close[k] for k in wk_order]
    s20, e21, s40 = sma(wc, 20), ema(wc, 21), sma(wc, 40)
    top = [max(a, b) if _ok(a, b) else None for a, b in zip(s20, e21)]
    bot = [min(a, b) if _ok(a, b) else None for a, b in zip(s20, e21)]
    up_band, up40, nb, n40 = [], [], 0, 0
    for j in range(len(wc)):
        nb = nb + 1 if gt(wc[j], top[j]) else 0
        n40 = n40 + 1 if gt(wc[j], s40[j]) else 0
        up_band.append(nb)
        up40.append(n40)
    idx = {k: j for j, k in enumerate(wk_order)}
    cols = {"top": [], "bot": [], "s40": [], "close": [], "up_band": [], "up40": []}
    for d in dates:
        j = idx[week_key(d)] - 1                       # [1] — 직전 주
        ok = j >= 0
        cols["top"].append(top[j] if ok else None)
        cols["bot"].append(bot[j] if ok else None)
        cols["s40"].append(s40[j] if ok else None)
        cols["close"].append(wc[j] if ok else None)
        cols["up_band"].append(up_band[j] if ok else None)
        cols["up40"].append(up40[j] if ok else None)
    cols["weeks"] = {"keys": wk_order, "close": wc, "top": top, "bot": bot, "s40": s40}
    return cols


# ----------------------------------------------------------------------------- ZBT (Zweig Breadth Thrust) 상태 기계
def zbt_track(ratio: list, lo: float, hi: float, win: int, near_lvl: float) -> dict:
    """10일 EMA가 lo 미만 → win봉 안에 hi 초과 = VALID(fire), 근처(near_lvl)까지만 = 근접 실패(near)."""
    fixed, last = [], None
    for x in ratio:                                   # fixnan
        last = x if x is not None else last
        fixed.append(last)
    r = ema(fixed, 10)
    fire, near, act = [False] * len(r), [False] * len(r), [False] * len(r)
    cnt, mx, fired = -1, None, False
    for i, z in enumerate(r):
        if z is None:
            continue
        if z < lo:
            cnt, mx, fired = 0, z, False
        elif cnt >= 0:
            cnt += 1
            mx = max(mx, z)
            if not fired and z > hi:
                fire[i], fired = True, True
            if cnt >= win:
                if not fired and mx >= near_lvl:
                    near[i] = True
                cnt = -1
        act[i] = cnt >= 0
    return {"ema": r, "fire": fire, "near": near, "active": act}


# ----------------------------------------------------------------------------- 본체
def run(bars: list, mode: str, params: dict | None = None, breadth: dict | None = None) -> dict:
    """bars: [{d,o,h,l,c,v}] 오름차순 확정 일봉. mode: 'stock' | 'crypto'.
    breadth: {"pct200": [...], "pct50": [...], "adv_ratio": [...]} — bars와 같은 길이(날짜 정렬·전일값 유지 완료본) 또는 None.
    """
    p = dict(DEFAULTS)
    p.update(params or {})
    is_crypto = mode == "crypto"
    use_br = (not is_crypto) and breadth is not None
    n = len(bars)
    D = [b["d"] for b in bars]
    H = [float(b["h"]) for b in bars]
    L = [float(b["l"]) for b in bars]
    C = [float(b["c"]) for b in bars]
    V = [float(b.get("v") or 0.0) for b in bars]

    rally_min = (p["rally_crypto"] if is_crypto else p["rally_stock"]) / 100.0
    dd_min = (p["dd_crypto"] if is_crypto else p["dd_stock"]) / 100.0
    shift_min = p["shift_stock"] if use_br else p["shift_crypto"]
    shift_max = 6 if use_br else 4
    hits_max = 6 if is_crypto else (7 if use_br else 5)
    peak_len = 365 if is_crypto else 252
    tol = p["touch_tol"] / 100.0
    sl = p["slope_len"]

    ma200, ma50 = sma(C, p["ma_len"]), sma(C, p["ma50_len"])
    ma_fall = [i >= sl and lt(ma200[i], ma200[i - sl]) for i in range(n)]
    ma50_up = [i >= sl and gt(ma50[i], ma50[i - sl]) for i in range(n)]
    R = rsi(C, p["rsi_len"])
    A = atr(H, L, C, p["atr_len"])
    lo20 = rolling(L, 20, min)
    hi_dd = rolling(H, p["dd_len"], max)
    hi_peak = rolling(H, peak_len, max)
    hi_c20 = rolling(C, 20, max)
    dd = [(C[i] / hi_dd[i] - 1.0) if hi_dd[i] else None for i in range(n)]

    W = weekly_frame(D, C)
    w_line = W["top"] if is_crypto else W["s40"]
    w_up = W["up_band"] if is_crypto else W["up40"]

    br200 = br50 = [None] * n
    br50_hi20 = [None] * n
    z = {"ema": [None] * n, "fire": [False] * n, "near": [False] * n, "active": [False] * n}
    if use_br:
        br200, br50 = breadth["pct200"], breadth["pct50"]
        br50_hi20 = rolling([x if x is not None else 0.0 for x in br50], 20, max)
        z = zbt_track(breadth["adv_ratio"], p["zbt_lo"], p["zbt_hi"], p["zbt_win"], p["zbt_near_lvl"])
    since_fire, since_near = bars_since(z["fire"]), bars_since(z["near"])

    st = S_BULL
    bear_low = rally_base = rally_high = rally_rsi_mx = rsi_min_aft = None
    rsi_bull_arm = hit50 = hit200 = hit_band = hit_vw = narrow_seen = False
    peak_price, cum_pv, cum_v = None, 0.0, 0.0

    rows, events = [], []
    for i in range(n):
        c, h, l = C[i], H[i], L[i]
        # ---- 고점 앵커 AVWAP (직전 상태 기준 — Pine 실행 순서와 동일)
        hlc3 = (h + l + c) / 3.0
        if (st == S_BULL and ge(h, hi_peak[i])) or peak_price is None:
            peak_price, cum_pv, cum_v = h, hlc3 * V[i], V[i]
        else:
            cum_pv += hlc3 * V[i]
            cum_v += V[i]
        avwap = cum_pv / cum_v if cum_v > 0 else None

        zbt_recent = use_br and since_fire[i] is not None and since_fire[i] <= p["zbt_mem"]
        zbt_near_rec = use_br and since_near[i] is not None and since_near[i] <= p["zbt_mem"]
        narrow_now = (use_br and _ok(br50[i], br50_hi20[i], hi_c20[i])
                      and c >= hi_c20[i] and br50[i] <= br50_hi20[i] - p["div_tol"])

        # ---- ① 레짐 진입 조건
        base_ok = lt(c, ma200[i]) and le(dd[i], -dd_min)
        if is_crypto:
            bear_enter = base_ok and lt(W["close"][i], W["bot"][i])
        else:
            bear_enter = base_ok and (ma_fall[i] or lt(ma50[i], ma200[i]))

        ev = []
        # ---- 블록 1: 레짐 진입 · 신저점 · 반등 시작 · 반등 추적
        ev_rally = ev_new_low = False
        if st == S_BULL:
            if bear_enter:
                st = S_BEAR
                bear_low = rally_base = lo20[i]
                ev.append("bear_enter")
        else:
            bear_low = min(bear_low, l)
            if st != S_BEAR and l < rally_base:
                st, rally_base, ev_new_low = S_BEAR, l, True
                ev.append("new_low")
            if st == S_BEAR:
                rally_base = min(rally_base, l)
                if not ev_new_low and c >= rally_base * (1 + rally_min):
                    st, ev_rally = S_RALLY, True
                    rally_high, rally_rsi_mx = h, R[i]
                    rsi_bull_arm, rsi_min_aft = False, None
                    hit50 = hit200 = hit_band = hit_vw = narrow_seen = False
                    ev.append("rally")
            if st >= S_RALLY:
                rally_high = max(rally_high, h)
                if R[i] is not None:
                    rally_rsi_mx = R[i] if rally_rsi_mx is None else max(rally_rsi_mx, R[i])
                if ge(R[i], p["rsi_bull"]):
                    rsi_bull_arm, rsi_min_aft = True, R[i]
                elif rsi_bull_arm and R[i] is not None:
                    rsi_min_aft = R[i] if rsi_min_aft is None else min(rsi_min_aft, R[i])
                hit50 = hit50 or (ma50[i] is not None and h >= ma50[i] * (1 - tol))
                hit200 = hit200 or (ma200[i] is not None and h >= ma200[i] * (1 - tol))
                hit_band = hit_band or (is_crypto and W["bot"][i] is not None and h >= W["bot"][i] * (1 - tol))
                hit_vw = hit_vw or (avwap is not None and h >= avwap * (1 - tol))
                narrow_seen = narrow_seen or narrow_now

        # ---- ③ 진위 판별 근거
        in_rally = st >= S_RALLY
        turn_down = in_rally and _ok(rally_high, A[i], R[i]) and (rally_high - c) >= p["rej_atr"] * A[i] and R[i] < p["rsi_ceil"]
        fib_range = (peak_price - bear_low) if _ok(peak_price, bear_low) else None
        fib_ratio = (rally_high - bear_low) / fib_range if (in_rally and gt(fib_range, 0)) else None

        hits = {
            "rsi_ceil": in_rally and ge(rally_rsi_mx, p["rsi_ceil"]) and lt(rally_rsi_mx, p["rsi_bull"]),
            "ma50": in_rally and hit50 and lt(c, ma50[i]),
            "ma200": in_rally and hit200 and lt(c, ma200[i]),
            "band": in_rally and hit_band and lt(c, W["bot"][i]),
            "avwap": in_rally and hit_vw and lt(c, avwap),
            "fib": in_rally and ge(fib_ratio, 0.382),
            "narrow": in_rally and use_br and narrow_seen,
            "zbt_near": in_rally and zbt_near_rec,
        }
        bear_hits = sum(1 for v in hits.values() if v)

        evid = {
            "zbt": bool(zbt_recent),
            "breadth": use_br and ge(br200[i], 50),
            "rsi_shift": in_rally and rsi_bull_arm and ge(rsi_min_aft, p["rsi_floor"]),
            "weekly": ge(w_up[i], 1),
            "ma_flat": not ma_fall[i],
            "reclaim": gt(c, ma200[i]),
        }
        shift_score = sum(1 for v in evid.values() if v)
        zbt_ovr = p["zbt_override"] and zbt_recent
        shift_ok = shift_score >= shift_min or zbt_ovr

        rearm_hit = in_rally and _ok(rally_base, rally_high) and c <= rally_base + (rally_high - rally_base) * (1 - p["rearm_frac"] / 100.0)
        bull_confirm = (st == S_SHIFT and ge(w_up[i], p["hold_weeks"]) and gt(c, ma200[i])
                        and ma50_up[i] and shift_ok)

        # ---- 블록 2: 베어랠리 판정 · 전환 후보 · 강세 확정
        if in_rally and not ev_rally:
            if st == S_RALLY:
                if shift_ok:
                    st = S_SHIFT
                    ev.append("shift")
                elif turn_down and bear_hits >= p["min_hits"]:
                    st = S_FAILED
                    ev.append("bear_rally")
                elif rearm_hit:
                    st, rally_base = S_BEAR, l
                    ev.append("rearm")
            elif st == S_FAILED:
                if zbt_ovr or (shift_score >= shift_min and gt(c, ma200[i])):
                    st = S_SHIFT
                    ev.append("shift")
                elif rearm_hit:
                    st, rally_base = S_BEAR, l
                    ev.append("rearm")
            elif st == S_SHIFT:
                if bull_confirm:
                    st = S_BULL
                    ev.append("bull")
                elif not shift_ok and turn_down and lt(c, ma200[i]):
                    st = S_FAILED
                    ev.append("shift_fail")

        if use_br and z["fire"][i]:
            ev.append("zbt_fire")
        if use_br and z["near"][i]:
            ev.append("zbt_near")

        in_bear_st = st != S_BULL
        row = {
            "d": D[i], "c": c, "h": h, "l": l, "state": st,
            "ma200": ma200[i], "ma50": ma50[i], "ma_fall": ma_fall[i], "ma50_up": ma50_up[i],
            "rsi": R[i], "atr": A[i], "dd_peak": (c / peak_price - 1.0) if peak_price else None,
            "avwap": avwap, "peak": peak_price,
            "w_top": W["top"][i], "w_bot": W["bot"][i], "w_line": w_line[i], "w_close": W["close"][i], "w_up": w_up[i],
            "fib382": bear_low + 0.382 * fib_range if (in_bear_st and gt(fib_range, 0)) else None,
            "fib618": bear_low + 0.618 * fib_range if (in_bear_st and gt(fib_range, 0)) else None,
            "rally_base": rally_base if in_bear_st else None,
            "rally_high": rally_high if st >= S_RALLY else None,
            "rally_rsi_mx": rally_rsi_mx if st >= S_RALLY else None,
            "fib_ratio": fib_ratio,
            "hits": hits, "bear_hits": bear_hits, "evid": evid, "shift_score": shift_score,
            "turn_down": turn_down, "br200": br200[i], "br50": br50[i],
            "zbt": z["ema"][i], "zbt_active": z["active"][i], "zbt_since": since_fire[i],
            "zbt_recent": bool(zbt_recent), "zbt_near_rec": bool(zbt_near_rec),
        }
        rows.append(row)
        for e in ev:
            events.append({"i": i, "d": D[i], "type": e, "state": st, "c": c,
                           "hits": [k for k, v in hits.items() if v], "bear_hits": bear_hits,
                           "evid": [k for k, v in evid.items() if v], "shift_score": shift_score,
                           "fib_ratio": fib_ratio, "rally_base": rally_base,
                           "rally_pct": (c / rally_base - 1.0) if rally_base else None})

    return {
        "rows": rows, "events": events, "mode": mode, "use_breadth": use_br,
        "params": p, "shift_min": shift_min, "shift_max": shift_max, "hits_max": hits_max,
        "rally_min": rally_min, "weeks": W["weeks"],
    }
