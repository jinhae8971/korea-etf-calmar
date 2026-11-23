"""시세 수집 — 표준 라이브러리만 사용. 확정된 일봉만 돌려준다.

미국 ETF : api.nasdaq.com 히스토리(1순위) + 직전 마감 세션 보강(info·summary) → Yahoo chart v8 폴백 → 레포 캐시
크립토   : Coinbase Exchange 일봉(1순위) → Kraken OHLC 폴백 → 레포 캐시      (바이낸스는 러너 IP에서 451)
국내 지수: 네이버 fchart(KRX 확정 종가) → 레포 캐시

봉 형식: {"d": "YYYY-MM-DD", "o", "h", "l", "c", "v"}  (+ "patched": True = 히스토리 지연분을 마감 시세로 보강한 봉)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
UTC = timezone.utc
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_BARS = 2300            # 크립토 6년(2,190일) 보관 — 2021 고점·2022 베어장까지 상태 이력을 남긴다
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data", "cache")


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 25, retries: int = 3, accept: str = "application/json") -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(1.5 + 2.5 * i)
    raise RuntimeError(f"GET {url[:90]}: {last}")


def http_json(url: str, **kw):
    return json.loads(http_get(url, **kw))


def _num(x) -> float | None:
    s = str(x).replace("$", "").replace(",", "").strip()
    if s in ("", "N/A", "--", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- 캐시
def cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{re.sub(r'[^A-Za-z0-9_-]', '_', key)}.json")


def load_cache(key: str) -> list[dict]:
    try:
        with open(cache_path(key), encoding="utf-8") as f:
            return json.load(f).get("bars", [])
    except (OSError, json.JSONDecodeError):
        return []


def save_cache(key: str, bars: list[dict]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(key) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('{"bars": [\n')
        f.write(",\n".join(json.dumps(b, separators=(",", ":")) for b in bars[-MAX_BARS:]))
        f.write("\n]}\n")
    os.replace(tmp, cache_path(key))


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """날짜 기준 병합 — 새 값이 이긴다. 단, 보강(patched) 봉은 진짜 히스토리 봉을 덮지 않는다."""
    by = {b["d"]: b for b in old}
    for b in new:
        cur = by.get(b["d"])
        if cur is not None and b.get("patched") and not cur.get("patched"):
            continue
        by[b["d"]] = b
    return [by[k] for k in sorted(by)][-MAX_BARS:]


def _valid(b: dict) -> bool:
    try:
        return all(b[k] is not None and b[k] > 0 for k in ("h", "l", "c")) and b["l"] <= b["c"] <= b["h"] * 1.0001
    except (KeyError, TypeError):
        return False


# ----------------------------------------------------------------------------- 미국 (Nasdaq → Yahoo)
def nasdaq_hist(sym: str, cls: str, days: int) -> list[dict]:
    to = datetime.now(UTC).strftime("%Y-%m-%d")
    frm = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(sym)}/historical?assetclass={cls}"
           f"&fromdate={frm}&todate={to}&limit=9999")
    j = http_json(url, timeout=40)
    rows = ((j.get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        except (KeyError, ValueError):
            continue
        b = {"d": d, "o": _num(r.get("open")), "h": _num(r.get("high")), "l": _num(r.get("low")),
             "c": _num(r.get("close")), "v": _num(r.get("volume")) or 0.0}
        if b["o"] is None:
            b["o"] = b["c"]
        if _valid(b):
            out.append(b)
    out.sort(key=lambda b: b["d"])
    if len(out) < 5:
        raise RuntimeError(f"nasdaq {sym}: rows={len(out)}")
    return out


_CLOSED_RE = re.compile(r"Closed at ([A-Z][a-z]{2} \d{1,2}, \d{4})")


def nasdaq_closed_session(sym: str, cls: str) -> dict | None:
    """히스토리가 하루 늦게 올라오는 경우가 있어, 직전 '정규장 마감' 세션 봉을 info·summary로 만든다.
    info의 primary/secondary 중 'Closed at <날짜> 4:00 PM ET' 항목이 정규장 종가다(시간외 가격 아님)."""
    info = http_json(f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(sym)}/info?assetclass={cls}", retries=2)
    data = info.get("data") or {}
    close = day = None
    for k in ("secondaryData", "primaryData"):
        blk = data.get(k) or {}
        m = _CLOSED_RE.search(str(blk.get("lastTradeTimestamp") or ""))
        if m:
            day = datetime.strptime(m.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
            close = _num(blk.get("lastSalePrice"))
            break
    if not (day and close):
        return None
    summ = http_json(f"https://api.nasdaq.com/api/quote/{urllib.parse.quote(sym)}/summary?assetclass={cls}", retries=2)
    sd = (summ.get("data") or {}).get("summaryData") or {}
    hl = str((sd.get("TodayHighLow") or {}).get("value") or "")
    parts = [_num(x) for x in hl.split("/")]
    vol = _num((sd.get("ShareVolume") or {}).get("value")) or 0.0
    if len(parts) != 2 or None in parts:
        return None
    hi, lo = max(parts), min(parts)
    if not (lo * 0.999 <= close <= hi * 1.001):       # 범위 밖이면 다른 세션 값 → 보강하지 않음
        return None
    return {"d": day, "o": close, "h": max(hi, close), "l": min(lo, close), "c": close, "v": vol, "patched": True}


def yahoo_daily(sym: str, rng: str = "5y") -> list[dict]:
    last = None
    for host in ("query1", "query2"):
        try:
            j = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                          f"?range={rng}&interval=1d", retries=2)
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(res["timestamp"]):
                b = {"d": datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d"),
                     "o": q["open"][i], "h": q["high"][i], "l": q["low"][i], "c": q["close"][i],
                     "v": float(q["volume"][i] or 0)}
                if _valid(b):
                    out.append(b)
            return out
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"yahoo {sym}: {last}")


def us_session_closed(day: str, now: datetime) -> bool:
    """미국 정규장(16:00 ET) 마감 + 여유 15분이 지났는가. ET는 서머타임을 고려해 UTC-4/-5 중 보수적으로 -5 기준."""
    close_utc = datetime.fromisoformat(day).replace(tzinfo=UTC) + timedelta(hours=21, minutes=15)
    return now.astimezone(UTC) >= close_utc


def us_bars(sym: str, cls: str, now: datetime) -> tuple[list[dict], str]:
    key = f"us_{sym}"
    cache = load_cache(key)
    deep = len(cache) < 900 or (cache and (now.date() - datetime.fromisoformat(cache[-1]["d"]).date()).days > 20)
    fresh, src, errs = [], None, []
    try:
        fresh = [b for b in nasdaq_hist(sym, cls, 365 * 6 if deep else 60) if us_session_closed(b["d"], now)]
        src = "nasdaq"
    except Exception as e:  # noqa: BLE001
        errs.append(f"nasdaq:{str(e)[-80:]}")
        try:
            fresh = [b for b in yahoo_daily(sym, "6y" if deep else "3mo") if us_session_closed(b["d"], now)]
            src = "yahoo"
        except Exception as e2:  # noqa: BLE001
            errs.append(f"yahoo:{str(e2)[-80:]}")
    bars = merge(cache, fresh)
    if src == "nasdaq":
        try:
            pb = nasdaq_closed_session(sym, cls)
            if pb and bars and pb["d"] > bars[-1]["d"]:
                bars = merge(bars, [pb])
                src = "nasdaq+close"
        except Exception as e:  # noqa: BLE001
            errs.append(f"close:{str(e)[-60:]}")
    if not bars:
        raise RuntimeError(f"{sym}: 수집 실패 ({' / '.join(errs)})")
    if src is None:
        src = "cache"
    if errs:
        log(f"[us] {sym} 경고: {' / '.join(errs)}")
    save_cache(key, bars)
    return bars, src


# ----------------------------------------------------------------------------- 크립토 (Coinbase → Kraken)
def coinbase_daily(product: str, days: int, now: datetime) -> list[dict]:
    end = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    by = {}
    pages = max(1, -(-days // 300))
    for k in range(pages):
        e = end - timedelta(days=300 * k)
        s = e - timedelta(days=299)
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=86400"
               f"&start={s.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={e.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        rows = http_json(url)
        if not rows:
            break
        for t, lo, hi, op, cl, vol in rows:             # [time, low, high, open, close, volume]
            by[t] = {"d": datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%d"),
                     "o": float(op), "h": float(hi), "l": float(lo), "c": float(cl), "v": float(vol), "_t": t}
        time.sleep(0.35)
    out = [by[t] for t in sorted(by)]
    return _complete_utc(out, now)


def kraken_daily(pair: str, now: datetime) -> list[dict]:
    j = http_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1440")
    if j.get("error"):
        raise RuntimeError(f"kraken {pair}: {j['error']}")
    key = [k for k in j["result"] if k != "last"][0]
    out = []
    for t, op, hi, lo, cl, _vw, vol, _n in j["result"][key]:
        out.append({"d": datetime.fromtimestamp(int(t), tz=UTC).strftime("%Y-%m-%d"),
                    "o": float(op), "h": float(hi), "l": float(lo), "c": float(cl), "v": float(vol), "_t": int(t)})
    return _complete_utc(out, now)


def _complete_utc(bars: list[dict], now: datetime) -> list[dict]:
    """UTC 일봉 중 아직 안 닫힌 봉(시작 + 24h > 지금)을 뺀다."""
    ts_now = now.astimezone(UTC).timestamp()
    out = []
    for b in bars:
        t = b.pop("_t", None)
        if t is None:
            t = datetime.fromisoformat(b["d"]).replace(tzinfo=UTC).timestamp()
        if t + 86400 <= ts_now and _valid(b):
            out.append(b)
    return out


def crypto_bars(coinbase: str, kraken: str, now: datetime) -> tuple[list[dict], str]:
    key = f"crypto_{coinbase}"
    cache = load_cache(key)
    deep = len(cache) < 2000
    fresh, src, errs = [], None, []
    try:
        fresh = coinbase_daily(coinbase, 2200 if deep else 40, now)
        src = "coinbase"
    except Exception as e:  # noqa: BLE001
        errs.append(f"coinbase:{str(e)[-80:]}")
        try:
            fresh = kraken_daily(kraken, now)
            src = "kraken"
        except Exception as e2:  # noqa: BLE001
            errs.append(f"kraken:{str(e2)[-80:]}")
    bars = merge(cache, fresh)
    if not bars:
        raise RuntimeError(f"{coinbase}: 수집 실패 ({' / '.join(errs)})")
    if errs:
        log(f"[crypto] {coinbase} 경고: {' / '.join(errs)}")
    save_cache(key, bars)
    return bars, src or "cache"


# ----------------------------------------------------------------------------- 국내 (네이버 fchart)
KR_CLOSE_HM = (15, 40)


def naver_daily(symbol: str, count: int, now: datetime) -> list[dict]:
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={urllib.parse.quote(symbol)}"
           f"&timeframe=day&count={count}&requestType=0")
    xml = http_get(url, accept="text/xml").decode("euc-kr", "replace")
    out = []
    for m in re.finditer(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"', xml):
        d, o, h, l, c, v = m.groups()
        b = {"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}
        if _valid(b):
            out.append(b)
    if len(out) < 5:
        raise RuntimeError(f"naver {symbol}: rows={len(out)}")
    return drop_unconfirmed_kr(out, now)


def drop_unconfirmed_kr(bars: list[dict], now: datetime) -> list[dict]:
    """KST 15:40 전이면 오늘 날짜 봉(장중·미확정)을 뺀다."""
    k = now.astimezone(KST)
    if bars and bars[-1]["d"] == k.strftime("%Y-%m-%d") and (k.hour, k.minute) < KR_CLOSE_HM:
        return bars[:-1]
    return bars


def kr_bars(symbol: str, now: datetime) -> tuple[list[dict], str]:
    key = f"kr_{symbol}"
    cache = load_cache(key)
    try:
        fresh = naver_daily(symbol, 1500 if len(cache) < 900 else 60, now)
        src = "naver"
    except Exception as e:  # noqa: BLE001
        log(f"[kr] {symbol} 경고: {e}")
        fresh, src = [], "cache"
    bars = merge(cache, fresh)
    bars = drop_unconfirmed_kr(bars, now)
    if not bars:
        raise RuntimeError(f"{symbol}: 수집 실패")
    save_cache(key, bars)
    return bars, src


def fetch_asset(a: dict, now: datetime) -> tuple[list[dict], str]:
    if a["market"] == "us":
        return us_bars(a["symbol"], a.get("assetclass", "etf"), now)
    if a["market"] == "crypto":
        return crypto_bars(a["coinbase"], a["kraken"], now)
    if a["market"] == "kr":
        return kr_bars(a["symbol"], now)
    raise ValueError(f"unknown market {a['market']}")
