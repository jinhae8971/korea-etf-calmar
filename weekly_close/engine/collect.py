# -*- coding: utf-8 -*-
"""Weekly close report: collect stock + crypto data for the week just ended.

python3 collect.py            -> out/<week>/data.json + digest on stdout
<week> = the Friday (NY session date) that closes the stock week.
Crypto window = the 7 UTC daily candles ending on the latest closed candle (Sat UTC close = Sun 09:00 KST).
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# (label, yahoo symbol, kind)  kind: idx = % change, yld = bp change, lvl = point change
INDICES = [
    ("S&P 500", "^GSPC", "idx"), ("나스닥100", "^NDX", "idx"), ("다우", "^DJI", "idx"),
    ("러셀2000", "^RUT", "idx"), ("필라델피아 반도체", "^SOX", "idx"),
    ("코스피", "^KS11", "idx"), ("코스닥", "^KQ11", "idx"), ("닛케이225", "^N225", "idx"),
    ("상하이종합", "000001.SS", "idx"), ("항셍", "^HSI", "idx"), ("유로스톡스600", "^STOXX", "idx"),
]
MACRO = [
    ("VIX", "^VIX", "lvl"), ("미 2년물", "2YY=F", "yld"), ("미 10년물", "^TNX", "yld"),
    ("달러인덱스", "DX-Y.NYB", "idx"), ("원/달러", "USDKRW=X", "idx"), ("WTI", "CL=F", "idx"),
    ("금", "GC=F", "idx"), ("구리", "HG=F", "idx"),
]
SECTORS = [
    ("XLK", "정보기술"), ("XLC", "커뮤니케이션"), ("XLY", "경기소비재"), ("XLP", "필수소비재"),
    ("XLE", "에너지"), ("XLF", "금융"), ("XLV", "헬스케어"), ("XLI", "산업재"),
    ("XLB", "소재"), ("XLRE", "부동산"), ("XLU", "유틸리티"),
]
THEMES = [
    ("SMH", "반도체"), ("IGV", "소프트웨어"), ("KRE", "지역은행"), ("XBI", "바이오"),
    ("ITB", "주택건설"), ("GDX", "금광"), ("ARKK", "혁신성장"), ("IBIT", "비트코인 ETF"),
    ("RSP", "S&P 동일가중"), ("EWY", "한국(EWY)"),
]
US_LARGE = ("AAPL MSFT NVDA AVGO ORCL CRM AMD ADBE CSCO IBM QCOM TXN NOW AMAT MU LRCX KLAC PANW INTC PLTR ANET "
            "GOOGL META NFLX TMUS DIS VZ AMZN TSLA HD MCD LOW BKNG NKE SBUX BRK-B JPM V MA BAC WFC GS MS AXP BLK "
            "LLY UNH JNJ ABBV MRK TMO ABT ISRG PFE AMGN WMT PG COST KO PEP PM XOM CVX COP GE CAT RTX BA HON UNP "
            "LMT DE LIN NEE").split()
KR_LARGE = [("005930.KS", "삼성전자"), ("000660.KS", "SK하이닉스"), ("373220.KS", "LG에너지솔루션"),
            ("207940.KS", "삼성바이오로직스"), ("005380.KS", "현대차"), ("000270.KS", "기아"),
            ("012450.KS", "한화에어로스페이스"), ("329180.KS", "HD현대중공업"), ("105560.KS", "KB금융"),
            ("035420.KS", "NAVER"), ("034020.KS", "두산에너빌리티"), ("068270.KS", "셀트리온"),
            ("035720.KS", "카카오"), ("042660.KS", "한화오션"), ("005490.KS", "POSCO홀딩스")]
PATH = [("SPY", "S&P 500"), ("QQQ", "나스닥100"), ("SOXX", "반도체"), ("^KS11", "코스피")]
STABLES = {"usdt", "usdc", "dai", "usds", "usde", "fdusd", "pyusd", "usd1", "tusd", "usdd", "susde", "susds",
           "bfusd", "usdf", "rlusd", "usdg", "usdtb", "usd0", "buidl", "usyc", "ousg", "usdy", "frax", "gho", "xaut", "paxg", "eursafo", "eurc", "eurs", "eurcv"}
WRAPPED = ("wrapped", "staked", "bridged", "restaked", "binance-peg", "liquid staking", "coinbase wrapped", "lombard",
           "kelp", "ether.fi staked", "jito staked", "binance staked", "renzo", "mantle staked", "tether gold", "pax gold")


def http_json(url, tries=4, timeout=25, headers=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def yahoo_daily(sym, rng="2y"):
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last = None
    for i in range(5):
        try:
            d = http_json(f"https://{hosts[i % 2]}/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range={rng}&events=div,splits", tries=1)
            res = d["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", 0)
            idx = [dt.datetime.fromtimestamp(t + off, dt.timezone.utc).date() for t in res["timestamp"]]
            s = pd.Series(res["indicators"]["quote"][0]["close"], index=pd.DatetimeIndex(idx)).dropna()
            s = s[~s.index.duplicated(keep="last")].sort_index()
            # unadjusted split guard (same idea as us_close adjust_splits)
            for j in range(len(s) - 1, 0, -1):
                r = s.iloc[j - 1] / s.iloc[j]
                if r > 1.8 or r < 0.55:
                    k = min([2, 3, 4, 5, 8, 10, 15, 20, 25, 50, .5, 1 / 3, .25, .2, .1], key=lambda x: abs(r / x - 1))
                    if abs(r / k - 1) < 0.2:
                        s.iloc[:j] = s.iloc[:j] / k
                        print(f"[split-adjust] {sym} {s.index[j].date()} ratio {k:g}", file=sys.stderr)
            return s
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"yahoo {sym}: {last}")


def fetch_many(symbols):
    out, err = {}, {}

    def one(s):
        try:
            return s, yahoo_daily(s), None
        except Exception as e:  # noqa: BLE001
            return s, None, str(e)

    with ThreadPoolExecutor(6) as ex:
        for s, ser, e in ex.map(one, symbols):
            if ser is None or len(ser) < 30:
                err[s] = e or "short history"
            else:
                out[s] = ser
    return out, err


def pct(a, b):
    return None if (a is None or b is None or not b) else round(float((a / b - 1) * 100), 2)


def metrics(s, week_end, kind="idx"):
    """Weekly numbers for a daily close series. Week = Mon..Fri containing week_end (a Friday)."""
    s = s[s.index <= pd.Timestamp(week_end)]
    mon = pd.Timestamp(week_end) - pd.Timedelta(days=4)
    wk = s[s.index >= mon]
    prev = s[s.index < mon]
    if wk.empty or prev.empty:
        return None
    c, p = float(wk.iloc[-1]), float(prev.iloc[-1])
    p4 = s[s.index < mon - pd.Timedelta(days=21)]
    ytd0 = s[s.index < pd.Timestamp(dt.date(week_end.year, 1, 1))]
    hi = float(s.iloc[-252:].max())
    prev_mon = mon - pd.Timedelta(days=7)
    pp = s[s.index < prev_mon]
    if kind == "yld":
        f = lambda a, b: None if b is None else round((a - b) * 100, 1)  # noqa: E731  bp
    elif kind == "lvl":
        f = lambda a, b: None if b is None else round(a - b, 2)  # noqa: E731
    else:
        f = pct
    return {"close": round(c, 4), "w1": f(c, p), "w4": f(c, float(p4.iloc[-1]) if len(p4) else None),
            "ytd": f(c, float(ytd0.iloc[-1]) if len(ytd0) else None),
            "prev_w1": f(p, float(pp.iloc[-1]) if len(pp) else None),
            "from_high": pct(c, hi), "last_date": wk.index[-1].date().isoformat(), "sessions": int(len(wk)),
            "week_hi": round(float(wk.max()), 4), "week_lo": round(float(wk.min()), 4)}


def streak(s, week_end):
    """Consecutive up/down weeks ending at week_end (positive = up weeks)."""
    s = s[s.index <= pd.Timestamp(week_end)]
    w = s.resample("W-FRI").last().dropna()
    ch = w.pct_change().dropna()
    n, sign = 0, None
    for v in reversed(ch.values):
        sg = 1 if v > 0 else -1
        if sign is None:
            sign = sg
        if sg != sign:
            break
        n += 1
    return (sign or 0) * n


def collect_stock(now):
    # week end = most recent Friday whose NY session has closed
    d = now.astimezone(dt.timezone.utc).date()
    while d.weekday() != 4 or (d == now.astimezone(dt.timezone.utc).date() and now.astimezone(dt.timezone.utc).hour < 21):
        d -= dt.timedelta(days=1)
    week_end = d
    syms = [s for _, s, _ in INDICES + MACRO] + [s for s, _ in SECTORS + THEMES] + US_LARGE + [s for s, _ in KR_LARGE] + [s for s, _ in PATH]
    data, err = fetch_many(list(dict.fromkeys(syms)))

    def rows(defs):
        out = []
        for lab, sym, kind in defs:
            if sym in data:
                m = metrics(data[sym], week_end, kind)
                if m:
                    out.append({"label": lab, "sym": sym, "kind": kind, **m})
        return out

    idx, macro = rows(INDICES), rows(MACRO)
    sectors = rows([(lab, s, "idx") for s, lab in SECTORS])
    themes = rows([(lab, s, "idx") for s, lab in THEMES])
    us = rows([(s, s, "idx") for s in US_LARGE])
    kr = rows([(lab, s, "idx") for s, lab in KR_LARGE])
    us_sorted = sorted(us, key=lambda r: -r["w1"])
    up = sum(1 for r in us if r["w1"] > 0)
    paths = {}
    for sym, lab in PATH:
        if sym in data:
            s = data[sym][data[sym].index <= pd.Timestamp(week_end)].iloc[-11:]
            base = float(s.iloc[-6]) if len(s) >= 6 else float(s.iloc[0])
            paths[lab] = [{"d": i.date().isoformat(), "v": round((float(v) / base - 1) * 100, 2)} for i, v in s.iloc[-6:].items()]
    streaks = {lab: streak(data[sym], week_end) for lab, sym, _ in INDICES if sym in data}
    spx = next((r for r in idx if r["sym"] == "^GSPC"), None)
    stale = (spx is None) or spx["last_date"] != week_end.isoformat()
    return {"week_end": week_end.isoformat(), "week_start": (week_end - dt.timedelta(days=4)).isoformat(),
            "stale": stale, "indices": idx, "macro": macro, "sectors": sectors, "themes": themes,
            "us_top": us_sorted[:8], "us_bottom": us_sorted[-8:][::-1], "kr": sorted(kr, key=lambda r: -r["w1"]),
            "breadth": {"up": up, "n": len(us), "pct": round(up / max(1, len(us)) * 100)},
            "paths": paths, "streaks": streaks, "errors": err}


def collect_crypto(now):
    out = {"errors": {}}
    # --- majors daily candles (Coinbase, UTC days)
    def candles(prod):
        raw = http_json(f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=86400")
        df = pd.DataFrame(raw, columns=["t", "low", "high", "open", "close", "vol"]).sort_values("t")
        df.index = pd.to_datetime(df["t"], unit="s", utc=True)
        today = pd.Timestamp(now.astimezone(dt.timezone.utc).date(), tz="UTC")
        return df[df.index < today]                       # closed candles only

    majors = []
    paths = {}
    last_day = None
    for prod, lab in (("BTC-USD", "BTC"), ("ETH-USD", "ETH"), ("SOL-USD", "SOL"), ("XRP-USD", "XRP"), ("DOGE-USD", "DOGE")):
        try:
            df = candles(prod)
            c = df["close"]
            last_day = c.index[-1].date()
            wk = df.iloc[-7:]
            majors.append({"label": lab, "close": float(c.iloc[-1]), "w1": pct(c.iloc[-1], c.iloc[-8]),
                           "w4": pct(c.iloc[-1], c.iloc[-29]), "prev_w1": pct(c.iloc[-8], c.iloc[-15]),
                           "week_hi": float(wk["high"].max()), "week_lo": float(wk["low"].min()),
                           "from_high": pct(c.iloc[-1], float(df["high"].iloc[-300:].max())),
                           "hi_300d": float(df["high"].iloc[-300:].max())})
            paths[lab] = [{"d": i.date().isoformat(), "v": round((float(v) / float(c.iloc[-8]) - 1) * 100, 2)}
                          for i, v in c.iloc[-8:].items()]
            if lab == "BTC":
                out["btc_daily"] = [{"d": i.date().isoformat(), "c": float(v)} for i, v in c.iloc[-60:].items()]
            if lab == "ETH":
                eth = c
            if lab == "BTC":
                btc = c
        except Exception as e:  # noqa: BLE001
            out["errors"][prod] = str(e)
    try:
        r = (eth / btc).dropna()
        out["ethbtc"] = {"close": round(float(r.iloc[-1]), 5), "w1": pct(float(r.iloc[-1]), float(r.iloc[-8]))}
    except Exception as e:  # noqa: BLE001
        out["errors"]["ethbtc"] = str(e)
    out["majors"], out["paths"] = majors, paths
    out["week_end"] = last_day.isoformat() if last_day else None
    out["week_start"] = (last_day - dt.timedelta(days=6)).isoformat() if last_day else None

    # --- top-100 movers + market totals (CoinGecko: 2 calls)
    try:
        mk = http_json("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=120&page=1&price_change_percentage=7d,30d")
        coins = []
        for c in mk:
            sym, name = (c.get("symbol") or "").lower(), (c.get("name") or "").lower()
            if sym in STABLES or any(w in name for w in WRAPPED) or c.get("price_change_percentage_7d_in_currency") is None:
                continue
            if abs((c.get("current_price") or 0) - 1) < 0.02 and abs(c.get("price_change_percentage_7d_in_currency") or 0) < 1.0:
                continue                                    # unlisted stablecoin
            if abs(c.get("price_change_percentage_7d_in_currency") or 0) < 1.5 and abs(c.get("price_change_percentage_30d_in_currency") or 0) < 3.0:
                continue                                    # tokenized funds / pegged assets (USTB, JAAA, FIGR_HELOC ...)
            coins.append({"sym": sym.upper(), "name": c.get("name"), "rank": c.get("market_cap_rank"),
                          "mcap": c.get("market_cap"), "price": c.get("current_price"),
                          "w1": round(c["price_change_percentage_7d_in_currency"], 1),
                          "m1": round(c.get("price_change_percentage_30d_in_currency") or 0, 1),
                          "ath_chg": round(c.get("ath_change_percentage") or 0, 1)})
        coins = coins[:100]
        srt = sorted(coins, key=lambda r: -r["w1"])
        out["top100"] = {"n": len(coins), "up": sum(1 for c in coins if c["w1"] > 0),
                         "median_w1": round(float(pd.Series([c["w1"] for c in coins]).median()), 1),
                         "beat_btc": None, "top": srt[:8], "bottom": srt[-8:][::-1]}
        b = next((c for c in coins if c["sym"] == "BTC"), None)
        if b:
            out["top100"]["beat_btc"] = sum(1 for c in coins if c["w1"] > b["w1"])
            out["top100"]["btc_w1_cg"] = b["w1"]
        out["leaders"] = [c for c in coins if c["rank"] and c["rank"] <= 15]
    except Exception as e:  # noqa: BLE001
        out["errors"]["coingecko_markets"] = str(e)
    time.sleep(2)
    try:
        g = http_json("https://api.coingecko.com/api/v3/global")["data"]
        out["global"] = {"mcap": g["total_market_cap"]["usd"], "vol": g["total_volume"]["usd"],
                         "btc_dom": round(g["market_cap_percentage"]["btc"], 2),
                         "eth_dom": round(g["market_cap_percentage"].get("eth", 0), 2)}
    except Exception as e:  # noqa: BLE001
        out["errors"]["coingecko_global"] = str(e)

    # --- fear & greed (alternative.me)
    try:
        fg = http_json("https://api.alternative.me/fng/?limit=31")["data"]
        ser = [{"d": dt.datetime.fromtimestamp(int(x["timestamp"]), dt.timezone.utc).date().isoformat(), "v": int(x["value"]),
                "c": x["value_classification"]} for x in fg][::-1]
        out["fng"] = {"now": ser[-1]["v"], "cls": ser[-1]["c"], "w_ago": ser[-8]["v"] if len(ser) >= 8 else None,
                      "min30": min(x["v"] for x in ser), "max30": max(x["v"] for x in ser), "series": ser}
    except Exception as e:  # noqa: BLE001
        out["errors"]["fng"] = str(e)

    # --- stablecoin supply (DefiLlama)
    try:
        st = http_json("https://stablecoins.llama.fi/stablecoincharts/all", timeout=40)
        v = [(dt.datetime.fromtimestamp(int(x["date"]), dt.timezone.utc).date().isoformat(), x["totalCirculating"]["peggedUSD"]) for x in st[-40:]]
        out["stables"] = {"now": v[-1][1], "w1_abs": v[-1][1] - v[-8][1], "w1": pct(v[-1][1], v[-8][1]),
                          "m1": pct(v[-1][1], v[-31][1]), "series": [{"d": a, "v": b} for a, b in v]}
    except Exception as e:  # noqa: BLE001
        out["errors"]["stables"] = str(e)

    # --- spot BTC ETF flows (bitcoin-data.com, BTC units)
    try:
        ef = http_json("https://bitcoin-data.com/v1/etf-flow-btc", timeout=40)
        rows = [(x["d"], float(x["etfFlow"])) for x in ef if x.get("etfFlow") not in (None, "")][-15:]
        ws, we = out["week_start"], out["week_end"]
        wk = [r for r in rows if ws <= r[0] <= we]
        pw = [r for r in rows if (dt.date.fromisoformat(ws) - dt.timedelta(days=7)).isoformat() <= r[0] < ws]
        out["etf"] = {"week_btc": round(sum(v for _, v in wk)), "prev_week_btc": round(sum(v for _, v in pw)),
                      "days": [{"d": a, "v": round(b)} for a, b in rows[-10:]], "n_days": len(wk),
                      "last": rows[-1][0] if rows else None}
    except Exception as e:  # noqa: BLE001
        out["errors"]["etf"] = str(e)
    out["stale"] = not majors or (now.astimezone(dt.timezone.utc).date() - last_day).days > 1
    return out


def digest(d):
    s, c = d["stock"], d["crypto"]
    L = [f"== STOCK week {s['week_start']}~{s['week_end']} stale={s['stale']} errors={list(s['errors'])}"]
    for r in s["indices"] + s["macro"]:
        u = {"idx": "%", "yld": "bp", "lvl": "pt"}[r["kind"]]
        L.append(f"  {r['label']:<12} {r['close']:>10,.2f}  1w {r['w1']:+}{u}  prev {r['prev_w1']}{u}  4w {r['w4']}{u}  ytd {r['ytd']}{u}  고점대비 {r['from_high']}%  ({r['last_date']}, {r['sessions']}s)")
    L.append("  streaks(주): " + ", ".join(f"{k} {v:+d}" for k, v in s["streaks"].items()))
    L.append("  sectors: " + ", ".join(f"{r['label']} {r['w1']:+}" for r in sorted(s["sectors"], key=lambda r: -r["w1"])))
    L.append("  themes : " + ", ".join(f"{r['label']} {r['w1']:+}" for r in sorted(s["themes"], key=lambda r: -r["w1"])))
    L.append("  US top : " + ", ".join(f"{r['label']} {r['w1']:+}" for r in s["us_top"]))
    L.append("  US bot : " + ", ".join(f"{r['label']} {r['w1']:+}" for r in s["us_bottom"]))
    L.append("  KR     : " + ", ".join(f"{r['label']} {r['w1']:+}" for r in s["kr"]))
    L.append(f"  breadth: {s['breadth']}")
    L.append("  paths  : " + json.dumps(s["paths"], ensure_ascii=False))
    L.append(f"== CRYPTO window {c.get('week_start')}~{c.get('week_end')} (UTC daily closes) stale={c.get('stale')} errors={c['errors']}")
    for r in c.get("majors", []):
        L.append(f"  {r['label']:<5} {r['close']:>10,.2f}  7d {r['w1']:+}%  prev7d {r['prev_w1']}%  28d {r['w4']}%  wk hi/lo {r['week_hi']:,.2f}/{r['week_lo']:,.2f}  300d고점 {r['hi_300d']:,.0f} 대비 {r['from_high']}%")
    L.append(f"  ETH/BTC {c.get('ethbtc')}  global {c.get('global')}")
    t = c.get("top100") or {}
    L.append(f"  top100: n={t.get('n')} up={t.get('up')} median {t.get('median_w1')}% beat_btc={t.get('beat_btc')} (BTC 7d CG {t.get('btc_w1_cg')})")
    L.append("  top  : " + ", ".join(f"{x['sym']} {x['w1']:+}" for x in t.get("top", [])))
    L.append("  bot  : " + ", ".join(f"{x['sym']} {x['w1']:+}" for x in t.get("bottom", [])))
    L.append("  leaders: " + ", ".join(f"{x['sym']} {x['w1']:+}/{x['m1']:+}m/ATH{x['ath_chg']}" for x in c.get("leaders", [])))
    f = c.get("fng") or {}
    L.append(f"  F&G now {f.get('now')} ({f.get('cls')}) 1w ago {f.get('w_ago')} 30d range {f.get('min30')}~{f.get('max30')}; last8 {[x['v'] for x in f.get('series', [])[-8:]]}")
    st = c.get("stables") or {}
    L.append(f"  stables ${st.get('now', 0) / 1e9:.1f}B 7d {st.get('w1')}% ({st.get('w1_abs', 0) / 1e9:+.2f}B) 30d {st.get('m1')}%")
    L.append(f"  ETF {c.get('etf')}")
    return "\n".join(L)


def main():
    now = dt.datetime.now(dt.timezone.utc)
    d = {"generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stock": collect_stock(now), "crypto": collect_crypto(now)}
    d["week"] = d["stock"]["week_end"]
    odir = os.path.join(OUT, d["week"])
    os.makedirs(odir, exist_ok=True)
    json.dump(d, open(os.path.join(odir, "data.json"), "w"), ensure_ascii=False, indent=1)
    print(digest(d))
    print(f"\nsaved {odir}/data.json")


if __name__ == "__main__":
    main()
