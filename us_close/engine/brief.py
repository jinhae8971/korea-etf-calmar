#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQQ·SPY daily chart brief engine.

Fetches Yahoo Finance chart data and computes the same indicators as the user's
TradingView layout (SuperTrend 10/3, two MAs, RSI 14 + SMA 14 signal,
MACD 12/26/9) on 4H (extended hours, 04:00 ET anchored), 1D and 1W.

Commands
  python3 brief.py                 snapshot for QQQ, SPY -> JSON on stdout, state saved
  python3 brief.py --next-run      next delivery time (07:40 KST after each NYSE session)
  python3 brief.py --record JSON   store the published probabilities for the session
  python3 brief.py --calibrate     MA / indicator candidates vs. reference chart values
"""
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
CONFIG = os.path.join(HERE, "config.json")
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
SYMBOLS = ["QQQ", "SPY", "AAPL"]  # AAPL added 2026-10-23 at the user's request
RUN_HH, RUN_MM = 7, 40  # KST delivery time

# NYSE full-day holidays (2026-2027)
NYSE_HOLIDAYS = {dt.date(*d) for d in [
    (2026, 1, 1), (2026, 1, 19), (2026, 2, 16), (2026, 4, 3), (2026, 5, 25),
    (2026, 6, 19), (2026, 7, 3), (2026, 9, 7), (2026, 11, 26), (2026, 12, 25),
    (2027, 1, 1), (2027, 1, 18), (2027, 2, 15), (2027, 3, 26), (2027, 5, 31),
    (2027, 6, 18), (2027, 7, 5), (2027, 9, 6), (2027, 11, 25), (2027, 12, 24),
]}

# Calibrated 2026-10-17 against the user's TradingView screenshots (QQQ/SPY,
# 4H/1D/1W, 2026-10-16 close): EMA50/EMA120, RSI/MACD exact to 0.01,
# SuperTrend(ATR 20 RMA, x4, hl2) within 0.03%, 4H = regular session bars.
DEFAULT_CONFIG = {
    "ma": {  # [type, length] for light-blue (mid) and navy (long) lines
        "4h": [["ema", 50], ["ema", 120]],
        "1d": [["ema", 50], ["ema", 120]],
        "1w": [["ema", 50], ["ema", 120]],
    },
    "supertrend": [20, 4.0],
    "rsi": [14, 14],
    "macd": [12, 26, 9],
    "four_hour_session": "rth",
}

# Reference values read from the user's charts (2026-10-16 close) for --selftest.
REFERENCE = {
    "QQQ": {"4h": {"ema50": 734.73, "ema120": 725.08, "st": ["down", 738.09], "rsi": [27.44, 43.90], "macd": [None, -1.33, -4.76]},
            "1d": {"ema50": 727.94, "ema120": 706.73, "st": ["down", 758.22], "rsi": [35.00, 57.67], "macd": [1.52, 6.17, -4.64]},
            "1w": {"ema50": 669.91, "ema120": 591.13, "st": ["up", 649.61], "rsi": [51.80, 58.63], "macd": [19.60, 21.84, -2.24]}},
    "SPY": {"4h": {"ema50": 769.80, "ema120": 763.64, "st": ["up", 762.34], "rsi": [38.23, 51.85], "macd": [-0.6565, 1.04, -1.70]},
            "1d": {"ema50": 765.71, "ema120": 748.95, "st": ["up", 752.48], "rsi": [43.79, 54.37], "macd": [1.70, 2.50, -0.7953]},
            "1w": {"ema50": 719.63, "ema120": 652.11, "st": ["up", 705.44], "rsi": [57.15, 62.48], "macd": [16.30, 18.25, -1.96]}},
}


# ----------------------------------------------------------------- data
def yahoo(symbol, interval, rng, prepost=False, tries=5):
    params = {"interval": interval, "range": rng,
              "includePrePost": "true" if prepost else "false",
              "events": "div,splits"}
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for i in range(tries):
        url = (f"https://{hosts[i % 2]}/v8/finance/chart/{symbol}?"
               + urllib.parse.urlencode(params))
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(ET)
            df = pd.DataFrame({k: q[k] for k in
                               ["open", "high", "low", "close", "volume"]}, index=idx)
            df = df.dropna(subset=["open", "high", "low", "close"])
            df = df[~df.index.duplicated(keep="last")].sort_index()
            return df, res["meta"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"yahoo fetch failed {symbol} {interval} {rng}: {last}")


def agg(g):
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                         "low": g["low"].min(), "close": g["close"].last(),
                         "volume": g["volume"].sum()})


def daily_bars(symbol):
    df, meta = yahoo(symbol, "1d", "10y")
    df.index = pd.DatetimeIndex([t.date() for t in df.index])
    df = df[~df.index.duplicated(keep="last")]
    return df, meta


def weekly_from_daily(d):
    w = agg(d.groupby(d.index.to_period("W-SUN")))
    w.index = w.index.start_time  # Monday label like TradingView
    return w


def bucket_eth_4h(df):
    """4H buckets of the extended session: 04:00, 08:00, 12:00, 16:00 ET."""
    t = df.index
    mins = t.hour * 60 + t.minute
    k = np.floor((mins - 240) / 240).astype(int)
    start = t.normalize() + pd.to_timedelta(240 + 240 * k, unit="m")
    return agg(df.groupby(start))


def bucket_rth_4h(df):
    """4H buckets of the regular session: 09:30-13:30, 13:30-16:00 ET."""
    t = df.index
    mins = t.hour * 60 + t.minute
    df = df[(mins >= 570) & (mins < 960)]
    t = df.index
    mins = t.hour * 60 + t.minute
    off = np.where(mins < 810, 570, 810)
    start = t.normalize() + pd.to_timedelta(off, unit="m")
    return agg(df.groupby(start))


def four_hour_bars(symbol, mode="rth"):
    prepost = mode == "eth"
    h1, _ = yahoo(symbol, "1h", "730d", prepost=prepost)
    m30, _ = yahoo(symbol, "30m", "60d", prepost=prepost)
    cut = m30.index[0].normalize() + pd.Timedelta(days=1)
    base = pd.concat([h1[h1.index < cut], m30[m30.index >= cut]])
    fn = bucket_eth_4h if mode == "eth" else bucket_rth_4h
    return fn(base)


# ------------------------------------------------------------ indicators
def sma(s, n):
    return s.rolling(n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rma(s, n):
    """Wilder smoothing as in Pine ta.rma (seeded with an SMA)."""
    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    a = 1.0 / n
    seed, prev = [], None
    for i, x in enumerate(v):
        if np.isnan(x):
            continue
        if prev is None:
            seed.append(x)
            if len(seed) == n:
                prev = float(np.mean(seed))
                out[i] = prev
        else:
            prev = a * x + (1 - a) * prev
            out[i] = prev
    return pd.Series(out, index=s.index)


def ma(s, kind, n):
    return sma(s, n) if kind == "sma" else ema(s, n)


def rsi(close, n=14):
    ch = close.diff()
    up = rma(ch.clip(lower=0), n)
    dn = rma((-ch).clip(lower=0), n)
    r = 100 - 100 / (1 + up / dn)
    r = r.where(dn != 0, 100.0)
    return r


def macd(close, f=12, s=26, sig=9):
    m = ema(close, f) - ema(close, s)
    sg = ema(m, sig)
    return m, sg, m - sg


def supertrend(df, n=20, factor=4.0):
    """ATR trailing SuperTrend (hl2, Wilder ATR). direction -1 = up (green), +1 = down (red)."""
    h, l, c = (df[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l,
                  np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))))
    atr = rma(pd.Series(tr, index=df.index), n).to_numpy()
    src = (h + l) / 2
    up_b = src - factor * atr
    dn_b = src + factor * atr
    N = len(c)
    UP = np.full(N, np.nan)
    DN = np.full(N, np.nan)
    trend = np.full(N, np.nan)
    for i in range(N):
        if np.isnan(atr[i]):
            continue
        if i == 0 or np.isnan(UP[i - 1]):
            UP[i], DN[i], trend[i] = up_b[i], dn_b[i], 1
            continue
        UP[i] = max(up_b[i], UP[i - 1]) if c[i - 1] > UP[i - 1] else up_b[i]
        DN[i] = min(dn_b[i], DN[i - 1]) if c[i - 1] < DN[i - 1] else dn_b[i]
        t = trend[i - 1]
        if t == -1 and c[i] > DN[i - 1]:
            t = 1
        elif t == 1 and c[i] < UP[i - 1]:
            t = -1
        trend[i] = t
    st = np.where(trend == 1, UP, DN)
    d = np.where(np.isnan(trend), np.nan, np.where(trend == 1, -1, 1))
    return (pd.Series(st, index=df.index), pd.Series(d, index=df.index),
            pd.Series(atr, index=df.index))


# --------------------------------------------------------------- levels
def swing_levels(df, win, lookback):
    x = df.iloc[-lookback:]
    hi, lo = x["high"].to_numpy(), x["low"].to_numpy()
    idx = x.index
    out = []
    for i in range(win, len(x) - win):
        if hi[i] == hi[i - win:i + win + 1].max():
            out.append(("high", float(hi[i]), str(idx[i])[:16]))
        if lo[i] == lo[i - win:i + win + 1].min():
            out.append(("low", float(lo[i]), str(idx[i])[:16]))
    return out


def cluster(levels, price, tol=0.004, band=0.10):
    pts = sorted([lv for lv in levels if abs(lv[1] / price - 1) <= band],
                 key=lambda z: z[1])
    merged = []
    for lv in pts:
        if merged and abs(lv[1] / merged[-1]["level"] - 1) <= tol:
            m = merged[-1]
            m["n"] += 1
            m["tags"].append(lv[0] + "@" + lv[2])
            m["level"] = round((m["level"] * (m["n"] - 1) + lv[1]) / m["n"], 2)
        else:
            merged.append({"level": round(lv[1], 2), "n": 1,
                           "tags": [lv[0] + "@" + lv[2]]})
    sup = [m for m in merged if m["level"] < price][-4:][::-1]
    res = [m for m in merged if m["level"] > price][:4]
    return sup, res


# ------------------------------------------------------------- analysis
def r2(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)


def tf_snapshot(df, cfg):
    c = df["close"]
    (k1, n1), (k2, n2) = cfg["ma"]
    m1, m2 = ma(c, k1, n1), ma(c, k2, n2)
    st, d, atr = supertrend(df, *cfg["supertrend"])
    rv = rsi(c, cfg["rsi"][0])
    rs = sma(rv, cfg["rsi"][1])
    mc, sg, hs = macd(c, *cfg["macd"])
    last = df.index[-1]
    dirs = d.dropna()
    flip_idx = dirs[dirs != dirs.iloc[-1]].index
    # 0 = flipped on the latest bar
    since = (len(dirs) - 2 - dirs.index.get_loc(flip_idx[-1])) if len(flip_idx) else None
    hsign = np.sign(hs.dropna())
    hflip = hsign[hsign != hsign.iloc[-1]].index
    hsince = (len(hsign) - 2 - hsign.index.get_loc(hflip[-1])) if len(hflip) else None
    vol20 = df["volume"].iloc[-21:-1].mean()
    return {
        "bar": str(last)[:16],
        "o": r2(df["open"].iloc[-1]), "h": r2(df["high"].iloc[-1]),
        "l": r2(df["low"].iloc[-1]), "c": r2(c.iloc[-1]),
        "prev_c": r2(c.iloc[-2]),
        "ma_mid": {"type": f"{k1}{n1}", "v": r2(m1.iloc[-1]), "prev": r2(m1.iloc[-2])},
        "ma_long": {"type": f"{k2}{n2}", "v": r2(m2.iloc[-1]), "prev": r2(m2.iloc[-2])},
        "supertrend": {"dir": "up" if d.iloc[-1] == -1 else "down",
                       "line": r2(st.iloc[-1]), "prev_line": r2(st.iloc[-2]),
                       "bars_since_flip": since, "atr": r2(atr.iloc[-1])},
        "rsi": {"v": r2(rv.iloc[-1]), "ma": r2(rs.iloc[-1]), "prev": r2(rv.iloc[-2])},
        "macd": {"macd": r2(mc.iloc[-1]), "signal": r2(sg.iloc[-1]),
                 "hist": r2(hs.iloc[-1]), "hist_prev": r2(hs.iloc[-2]),
                 "hist_bars_same_sign": hsince},
        "volume": {"last": int(df["volume"].iloc[-1] or 0),
                   "avg20": int(vol20) if not math.isnan(vol20) else None},
    }


def changes(cur, prev):
    out = []
    if not prev:
        return out
    for sym in cur["symbols"]:
        for tf in ("4h", "1d", "1w"):
            a = cur["symbols"][sym].get(tf)
            b = prev.get("symbols", {}).get(sym, {}).get(tf)
            if not a or not b:
                continue
            tag = f"{sym} {tf}"
            if a["supertrend"]["dir"] != b["supertrend"]["dir"]:
                out.append(f"{tag} SuperTrend {b['supertrend']['dir']}->{a['supertrend']['dir']} "
                           f"(line {a['supertrend']['line']})")
            for key, nm in (("ma_mid", "mid MA"), ("ma_long", "long MA")):
                pa = a["c"] > a[key]["v"]
                pb = b["c"] > b[key]["v"]
                if pa != pb:
                    out.append(f"{tag} close {'above' if pa else 'below'} {nm} {a[key]['v']}")
            if (a["macd"]["hist"] > 0) != (b["macd"]["hist"] > 0):
                out.append(f"{tag} MACD {'golden' if a['macd']['hist'] > 0 else 'dead'} cross")
            if (a["macd"]["macd"] > 0) != (b["macd"]["macd"] > 0):
                out.append(f"{tag} MACD line crossed {'above' if a['macd']['macd'] > 0 else 'below'} 0 "
                           f"({b['macd']['macd']}->{a['macd']['macd']})")
            ha, hb = a["macd"]["hist"], b["macd"]["hist"]
            hb_prev = b["macd"].get("hist_prev")
            # skip when the same (in-progress weekly) bar is being compared with itself
            if (hb_prev is not None and a["bar"] != b["bar"]
                    and (hb - hb_prev) * (ha - hb) < 0 and abs(ha - hb) >= 0.02):
                out.append(f"{tag} MACD hist turned {'up' if ha > hb else 'down'} "
                           f"({hb_prev}->{hb}->{ha})")
            for lvl in (30, 50, 70):
                if (a["rsi"]["v"] >= lvl) != (b["rsi"]["v"] >= lvl):
                    out.append(f"{tag} RSI crossed {lvl} ({b['rsi']['v']}->{a['rsi']['v']})")
    return out


def load_config():
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg
    return DEFAULT_CONFIG


def latest_state(prefix, before):
    if not os.path.isdir(STATE):
        return None
    files = sorted(f for f in os.listdir(STATE)
                   if f.startswith(prefix) and f[len(prefix):len(prefix) + 10] < before)
    if not files:
        return None
    with open(os.path.join(STATE, files[-1])) as f:
        return json.load(f)


def snapshot():
    cfg = load_config()
    out = {"generated_kst": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
           "symbols": {}}
    session = None
    for sym in SYMBOLS:
        d, meta = daily_bars(sym)
        w = weekly_from_daily(d)
        h4 = four_hour_bars(sym, cfg.get("four_hour_session", "rth"))
        session = str(d.index[-1])[:10]
        s = {
            "4h": tf_snapshot(h4, {**cfg, "ma": cfg["ma"]["4h"]}),
            "1d": tf_snapshot(d, {**cfg, "ma": cfg["ma"]["1d"]}),
            "1w": tf_snapshot(w, {**cfg, "ma": cfg["ma"]["1w"]}),
        }
        price = float(d["close"].iloc[-1])
        ath_i = d["high"].idxmax()
        s["price"] = r2(price)
        s["chg_pct"] = r2((price / d["close"].iloc[-2] - 1) * 100)
        s["ath"] = {"v": r2(d["high"].max()), "date": str(ath_i)[:10],
                    "dd_pct": r2((price / d["high"].max() - 1) * 100)}
        lv = (swing_levels(d, 5, 130) + swing_levels(h4, 6, 160)
              + swing_levels(w, 3, 80))
        sup, res = cluster(lv, price)
        s["swing_support"], s["swing_resistance"] = sup, res
        s["post_market_last"] = r2(meta.get("postMarketPrice")) if meta.get("postMarketPrice") else None
        out["symbols"][sym] = s
    out["session"] = session
    exp = expected_session(dt.datetime.now(KST))
    out["expected_session"] = str(exp) if exp else None
    out["stale"] = bool(exp and session < str(exp))
    prev = latest_state("snap_", session)
    out["changes_vs_prev"] = changes(out, prev)
    out["prev_session"] = prev["session"] if prev else None
    pp = latest_state("probs_", session)
    out["prev_probs"] = pp
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, f"snap_{session}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return out


# --------------------------------------------------------------- schedule
def is_trading_day(d):
    return d.weekday() < 5 and d not in NYSE_HOLIDAYS


def expected_session(now_kst):
    """Most recent NYSE session that has closed (16:00 ET) before now."""
    now_et = now_kst.astimezone(ET)
    d = now_et.date()
    if now_et.hour < 16 or not is_trading_day(d):
        d -= dt.timedelta(days=1)
    for _ in range(10):
        if is_trading_day(d):
            return d
        d -= dt.timedelta(days=1)
    return None


def next_run(now_kst=None):
    now_kst = now_kst or dt.datetime.now(KST)
    for i in range(0, 20):
        day = now_kst.date() + dt.timedelta(days=i)
        cand = dt.datetime(day.year, day.month, day.day, RUN_HH, RUN_MM, tzinfo=KST)
        if cand <= now_kst + dt.timedelta(minutes=10):
            continue
        if is_trading_day(day - dt.timedelta(days=1)):
            return cand
    raise RuntimeError("no trading day found")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--next-run"]:
        n = next_run()
        print(json.dumps({"kst": n.strftime("%Y-%m-%d %H:%M (%a) KST"),
                          "utc": n.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:00Z"),
                          "us_session": str(n.date() - dt.timedelta(days=1))}))
        return
    if args[:1] == ["--record"]:
        payload = json.loads(args[1])
        session = payload.pop("session")
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, f"probs_{session}.json"), "w") as f:
            json.dump({"session": session, **payload}, f, ensure_ascii=False, indent=1)
        print("recorded", session)
        return
    if args[:1] == ["--selftest"]:
        sys.exit(selftest())
    out = snapshot()
    if args[:1] == ["--json"]:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(digest(out))


def digest(s):
    """Compact text view of a snapshot for the analyst (keeps the chat light)."""
    L = [f"session {s['session']} (expected {s['expected_session']}, stale={s['stale']}) "
         f"generated {s['generated_kst']} KST"]
    for sym, v in s["symbols"].items():
        L.append(f"\n{sym} {v['price']} ({v['chg_pct']:+.2f}%) | ATH {v['ath']['v']} "
                 f"({v['ath']['date']}) {v['ath']['dd_pct']:+.2f}%")
        for tf in ("4h", "1d", "1w"):
            t = v[tf]
            st, m, r, vo = t["supertrend"], t["macd"], t["rsi"], t["volume"]
            vr = f"x{vo['last'] / vo['avg20']:.2f}" if vo["avg20"] else "n/a"
            L.append(
                f" {tf} {t['bar']} O{t['o']} H{t['h']} L{t['l']} C{t['c']} | "
                f"ST {st['dir']} {st['line']} (flip {st['bars_since_flip']} bars ago, prev {st['prev_line']}) | "
                f"EMA50 {t['ma_mid']['v']} EMA120 {t['ma_long']['v']} | "
                f"RSI {r['v']} (ma {r['ma']}, prev {r['prev']}) | "
                f"MACD {m['macd']}/{m['signal']} hist {m['hist']} (prev {m['hist_prev']}, "
                f"same sign {m['hist_bars_same_sign']} bars) | vol {vr}")
        L.append(" support: " + ", ".join(f"{x['level']}({x['n']})" for x in v["swing_support"]))
        L.append(" resist : " + ", ".join(f"{x['level']}({x['n']})" for x in v["swing_resistance"]))
    L.append(f"\nchanges vs {s['prev_session']}: " + ("; ".join(s["changes_vs_prev"]) or "none"))
    L.append("prev probs: " + json.dumps(s["prev_probs"], ensure_ascii=False))
    return "\n".join(L)


def selftest():
    """Recompute 2026-10-16 values and compare with the user's chart readings."""
    cfg = load_config()
    worst, bad = 0.0, []
    for sym, tfs in REFERENCE.items():
        d, _ = daily_bars(sym)
        d = d[d.index <= pd.Timestamp("2026-10-16")]
        h4 = four_hour_bars(sym, cfg.get("four_hour_session", "rth"))
        h4 = h4[h4.index <= pd.Timestamp("2026-10-16 15:59", tz=ET)]
        frames = {"4h": h4, "1d": d, "1w": weekly_from_daily(d)}
        for tf, ref in tfs.items():
            s = tf_snapshot(frames[tf], {**cfg, "ma": cfg["ma"][tf]})
            got = {"ema50": s["ma_mid"]["v"], "ema120": s["ma_long"]["v"],
                   "st": s["supertrend"]["line"], "rsi": s["rsi"]["v"],
                   "rsi_ma": s["rsi"]["ma"], "sig": s["macd"]["signal"],
                   "hist": s["macd"]["hist"]}
            exp = {"ema50": ref["ema50"], "ema120": ref["ema120"], "st": ref["st"][1],
                   "rsi": ref["rsi"][0], "rsi_ma": ref["rsi"][1],
                   "sig": ref["macd"][1], "hist": ref["macd"][2]}
            if s["supertrend"]["dir"] != ref["st"][0]:
                bad.append(f"{sym} {tf} supertrend dir {s['supertrend']['dir']} != {ref['st'][0]}")
            for k, v in exp.items():
                diff = abs(got[k] - v)
                worst = max(worst, diff / max(abs(v), 1.0))
                if diff > max(0.05, abs(v) * 0.001):
                    bad.append(f"{sym} {tf} {k} {got[k]} vs ref {v}")
    print(json.dumps({"ok": not bad, "worst_rel_err": round(worst, 5), "issues": bad},
                     ensure_ascii=False))
    return 0 if not bad else 1


if __name__ == "__main__":
    main()
