# -*- coding: utf-8 -*-
"""Collect market data and compute every metric the US close briefing needs.

python3 collect.py            -> out/<session>/data.json + prints a digest for the analyst
"""
import datetime as dt
import json
import math
import os
import pickle
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import brief as B  # noqa: E402  (calibrated indicators, Yahoo fetcher, NYSE calendar)
from universe import CROSS, SECTORS, THEMES, CORE, FACTORS, LARGE, TECH_FOCUS  # noqa: E402

OUT = os.path.join(HERE, "out")
STATE = os.path.join(HERE, "state")
UA = B.UA


# ------------------------------------------------------------------ fetch
def daily(sym):
    df, meta = B.yahoo(sym, "1d", "2y")
    df.index = pd.DatetimeIndex([t.date() for t in df.index])
    df = df[~df.index.duplicated(keep="last")]
    return sym, df, meta


def fetch_all(symbols, workers=6):
    out, errors = {}, {}

    def one(s):
        try:
            return daily(s)
        except Exception as e:  # noqa: BLE001
            return s, None, str(e)

    with ThreadPoolExecutor(workers) as ex:
        for s, df, meta in ex.map(one, symbols):
            if df is None or len(df) < 30:
                errors[s] = meta if isinstance(meta, str) else "short history"
            else:
                out[s] = (df, meta)
    return out, errors


def nasdaq_mcap(sym):
    for q in (sym.replace("-", "."), sym.replace("-", "/"), sym):
        try:
            url = f"https://api.nasdaq.com/api/quote/{q}/summary?assetclass=stocks"
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
            v = d["data"]["summaryData"]["MarketCap"]["value"]
            v = float(str(v).replace(",", "").replace("$", ""))
            if v > 0:
                return v
        except Exception:  # noqa: BLE001
            continue
    return None


def shares_table(prices):
    """shares outstanding ≈ market cap / price, refreshed every 14 days (cached)."""
    os.makedirs(STATE, exist_ok=True)
    p = os.path.join(STATE, "shares.json")
    tab = json.load(open(p)) if os.path.exists(p) else {}
    today = dt.date.today().isoformat()
    stale = [s for s in LARGE if s not in tab or
             (dt.date.fromisoformat(tab[s]["asof"]) < dt.date.today() - dt.timedelta(days=14))]
    for s in stale:
        if s not in prices:
            continue
        mc = nasdaq_mcap(s)
        if mc:
            tab[s] = {"shares": mc / prices[s], "asof": today}
        time.sleep(0.3)
    json.dump(tab, open(p, "w"), indent=0)
    return {s: v["shares"] for s, v in tab.items()}


# ------------------------------------------------------------------ metrics
def ret(c, n):
    if len(c) <= n:
        return None
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1) * 100


def ytd(c):
    prev = c[c.index < pd.Timestamp(dt.date(c.index[-1].year, 1, 1))]
    return float(c.iloc[-1] / prev.iloc[-1] - 1) * 100 if len(prev) else None


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def trend(df):
    c = df["close"]
    st, d, _ = B.supertrend(df, 20, 4.0)
    rsi = B.rsi(c, 14)
    e50, e120 = B.ema(c, 50), B.ema(c, 120)
    hi52 = df["high"].iloc[-252:].max()
    return {
        "rsi": r2(rsi.iloc[-1], 1),
        "vs_ema50": r2((c.iloc[-1] / e50.iloc[-1] - 1) * 100),
        "vs_ema120": r2((c.iloc[-1] / e120.iloc[-1] - 1) * 100),
        "st_up": bool(d.iloc[-1] == -1),
        "from_52w_high": r2((c.iloc[-1] / hi52 - 1) * 100),
    }


def row(sym, df, label, kind="idx"):
    c = df["close"]
    r = {"sym": sym, "label": label, "close": r2(c.iloc[-1], 4 if c.iloc[-1] < 10 else 2), "kind": kind,
         "d1": r2(ret(c, 1)), "d5": r2(ret(c, 5)), "m1": r2(ret(c, 21)), "m3": r2(ret(c, 63)), "ytd": r2(ytd(c)),
         "last_date": str(df.index[-1].date())}
    if kind in ("yld", "lvl"):
        k = 100 if kind == "yld" else 1  # yields in bp, VIX in points
        r.update({"d1": r2((c.iloc[-1] - c.iloc[-2]) * k, 1), "d5": r2((c.iloc[-1] - c.iloc[-6]) * k, 1),
                  "m1": r2((c.iloc[-1] - c.iloc[-22]) * k, 1), "m3": None, "ytd": None})
    return r


def breadth(large):
    closes = pd.DataFrame({s: df["close"] for s, (df, _) in large.items()}).sort_index().ffill()
    sma50, sma200 = closes.rolling(50).mean(), closes.rolling(200).mean()
    above50 = (closes > sma50).sum(axis=1) / closes.notna().sum(axis=1) * 100
    above200 = (closes > sma200).sum(axis=1) / closes.notna().sum(axis=1) * 100
    chg = closes.pct_change()
    adv = int((chg.iloc[-1] > 0).sum())
    dec = int((chg.iloc[-1] < 0).sum())
    hi = closes.rolling(252, min_periods=200).max()
    lo = closes.rolling(252, min_periods=200).min()
    new_hi = int((closes.iloc[-1] >= hi.iloc[-1] * 0.999).sum())
    new_lo = int((closes.iloc[-1] <= lo.iloc[-1] * 1.001).sum())
    hist = pd.DataFrame({"above50": above50, "above200": above200}).iloc[-130:].round(1)
    return {"n": int(closes.shape[1]), "adv": adv, "dec": dec, "new_high": new_hi, "new_low": new_lo,
            "above50": r2(above50.iloc[-1], 1), "above50_prev": r2(above50.iloc[-2], 1),
            "above50_5d": r2(above50.iloc[-6], 1),
            "above200": r2(above200.iloc[-1], 1), "above200_prev": r2(above200.iloc[-2], 1),
            "hist": {"date": [str(d.date()) for d in hist.index],
                     "above50": hist["above50"].tolist(), "above200": hist["above200"].tolist()}}


def factors(px):
    out = []
    for label, a, b in FACTORS:
        if a not in px or b not in px:
            continue
        ca, cb = px[a][0]["close"], px[b][0]["close"]
        j = pd.concat([ca, cb], axis=1, keys=["a", "b"]).dropna()
        ratio = j["a"] / j["b"]
        tail = ratio.iloc[-64:]
        out.append({"label": label, "long": a, "short": b,
                    "d1": r2((ratio.iloc[-1] / ratio.iloc[-2] - 1) * 100),
                    "d5": r2((ratio.iloc[-1] / ratio.iloc[-6] - 1) * 100),
                    "m1": r2((ratio.iloc[-1] / ratio.iloc[-22] - 1) * 100),
                    "hist": {"date": [str(d.date()) for d in tail.index],
                             "idx": (tail / tail.iloc[0] * 100).round(2).tolist()}})
    return out


def main():
    t0 = time.time()
    syms = sorted({s for _, s, _ in CROSS} | {s for s, _ in SECTORS + THEMES + CORE} |
                  {x for _, a, b in FACTORS for x in (a, b)} | set(LARGE))
    px, errors = fetch_all(syms)
    spy = px["SPY"][0]
    session = str(spy.index[-1].date())
    exp = B.expected_session(dt.datetime.now(B.KST))
    sdir = os.path.join(OUT, session)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "bars.pkl"), "wb") as f:
        pickle.dump({s: v[0] for s, v in px.items()}, f)

    # align every series to the US session (FX/futures/VIX already print a partial next-day bar);
    # bitcoin trades 24h, so it keeps its latest value
    for s in list(px):
        if s != "BTC-USD":
            df, meta = px[s]
            px[s] = (df[df.index <= pd.Timestamp(session)], meta)

    def stale_sym(s):
        return str(px[s][0].index[-1].date()) < session

    cross = []
    for label, s, kind in CROSS:
        if s in px:
            r = row(s, px[s][0], label, kind)
            r["stale"] = stale_sym(s) and s not in ("BTC-USD",)
            cross.append(r)

    def etf_rows(items):
        rows = []
        for s, label in items:
            if s in px:
                r = row(s, px[s][0], label)
                r.update(trend(px[s][0]))
                rows.append(r)
        return rows

    large = {s: px[s] for s in LARGE if s in px}
    prices = {s: float(df["close"].iloc[-1]) for s, (df, _) in large.items()}
    shares = shares_table(prices)
    stocks = []
    for s, (df, _) in large.items():
        r = row(s, df, s)
        r["sector"] = LARGE[s]
        r["mcap"] = r2(prices[s] * shares[s] / 1e9, 1) if s in shares else None
        r["vol_x"] = r2(df["volume"].iloc[-1] / df["volume"].iloc[-21:-1].mean())
        stocks.append(r)
    ranked = sorted([x for x in stocks if x["d1"] is not None], key=lambda x: x["d1"])
    sec_rows = etf_rows(SECTORS)
    data = {
        "session": session, "expected_session": str(exp), "stale": session < str(exp),
        "generated_kst": dt.datetime.now(B.KST).strftime("%Y-%m-%d %H:%M"),
        "cross": cross, "core": etf_rows(CORE), "sectors": sec_rows, "themes": etf_rows(THEMES),
        "factors": factors(px), "breadth": breadth(large), "stocks": stocks,
        "movers": {"up": ranked[::-1][:6], "down": ranked[:6]},
        "errors": errors,
    }
    # calibrated technical view (also stores state/snap for day-over-day changes)
    snap = B.snapshot()
    data["tech"] = {s: snap["symbols"][s] for s in TECH_FOCUS if s in snap["symbols"]}
    data["tech_changes"] = snap["changes_vs_prev"]
    data["prev_probs"] = snap["prev_probs"]
    json.dump(data, open(os.path.join(sdir, "data.json"), "w"), ensure_ascii=False, indent=1)
    print(digest(data))
    print(f"\n[{time.time() - t0:.0f}s] out/{session}/data.json  errors={list(errors)}")


def fmt(x, unit="%"):
    return "n/a" if x is None else f"{x:+.2f}{unit}"


def digest(d):
    L = [f"session {d['session']} (expected {d['expected_session']}, stale={d['stale']})"]
    L.append("CROSS: " + " | ".join(
        f"{r['label']} {r['close']} {fmt(r['d1'], 'bp' if r['kind'] == 'yld' else ('pt' if r['kind'] == 'lvl' else '%'))}"
        + (" (stale)" if r.get("stale") else "") for r in d["cross"]))
    L.append("CORE: " + " | ".join(f"{r['sym']} {fmt(r['d1'])} 5d {fmt(r['d5'])}" for r in d["core"]))
    s = sorted(d["sectors"], key=lambda r: -r["d1"])
    L.append("SECTORS 1D: " + " | ".join(f"{r['label']} {fmt(r['d1'])}" for r in s))
    L.append("SECTORS 1M: " + " | ".join(f"{r['label']} {fmt(r['m1'])}" for r in sorted(d['sectors'], key=lambda r: -r['m1'])))
    L.append("THEMES 1D: " + " | ".join(f"{r['label']} {fmt(r['d1'])}" for r in sorted(d['themes'], key=lambda r: -r['d1'])))
    L.append("FACTORS: " + " | ".join(f"{f['label']} 1D {fmt(f['d1'])} 1M {fmt(f['m1'])}" for f in d["factors"]))
    b = d["breadth"]
    L.append(f"BREADTH(n={b['n']}): adv {b['adv']} / dec {b['dec']}, >SMA50 {b['above50']}% (prev {b['above50_prev']}, 5d {b['above50_5d']}), "
             f">SMA200 {b['above200']}%, 52w hi {b['new_high']} / lo {b['new_low']}")
    L.append("UP: " + ", ".join(f"{x['sym']} {fmt(x['d1'])} (vol x{x['vol_x']})" for x in d["movers"]["up"]))
    L.append("DOWN: " + ", ".join(f"{x['sym']} {fmt(x['d1'])} (vol x{x['vol_x']})" for x in d["movers"]["down"]))
    for s, t in d["tech"].items():
        L.append(f"TECH {s} {t['price']} ({t['chg_pct']:+.2f}%) ATH {t['ath']['v']} {t['ath']['dd_pct']:+.2f}%")
        for tf in ("4h", "1d", "1w"):
            x = t[tf]
            L.append(f"  {tf} ST {x['supertrend']['dir']} {x['supertrend']['line']} | EMA50 {x['ma_mid']['v']} EMA120 {x['ma_long']['v']} | "
                     f"RSI {x['rsi']['v']} (ma {x['rsi']['ma']}) | MACD hist {x['macd']['hist']} (prev {x['macd']['hist_prev']}) | "
                     f"vol x{(x['volume']['last'] / x['volume']['avg20']) if x['volume']['avg20'] else 0:.2f}")
        L.append("  support " + ", ".join(f"{z['level']}({z['n']})" for z in t["swing_support"]) +
                 " | resist " + ", ".join(f"{z['level']}({z['n']})" for z in t["swing_resistance"]))
    L.append("TECH changes: " + ("; ".join(d["tech_changes"]) or "none"))
    L.append("prev probs: " + json.dumps(d["prev_probs"], ensure_ascii=False))
    return "\n".join(L)


if __name__ == "__main__":
    main()
