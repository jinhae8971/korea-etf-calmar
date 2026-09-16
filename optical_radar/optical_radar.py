#!/usr/bin/env python3
"""Optical Radar — 광통신 vs 메모리 vs 나스닥 상대강도 + 광통신 과열 게이지.

매일(미국 종가 후 KST 오전) 실행:
  1. Yahoo chart v8 로 1y 일봉 수집 (query1/query2 이중화, 레포 캐시 폴백)
  2. 종목별 지표 → 동일가중 바스켓 지수 → 나스닥 대비 RS
  3. 광통신 과열 게이지(0~100) + 차트 패턴 관측
  4. data/latest.json / data/history.json / docs 대시보드 데이터 갱신
  5. 텔레그램(스톡봇) 발송 — 같은 as_of 는 재발송하지 않음(멱등)

설계 원칙: 단정 금지. 게이지는 "위치"를 보고할 뿐 천정 예측이 아니다.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
KST = timezone(timedelta(hours=9))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_BARS = 520
CACHE_STALE_DAYS = 6          # 캐시가 이보다 오래되면 해당 종목 degraded


# --------------------------------------------------------------------------- io
def log(msg: str) -> None:
    print(msg, flush=True)


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "pages_url": os.environ.get("PAGES_URL", ""),
        "dashboard_dir": os.environ.get("DASHBOARD_DIR", ""),
    }
    local = load_json(os.path.join(HERE, "config.json"), {})
    for k, v in local.items():
        if k.lower() in cfg and not cfg[k.lower()]:
            cfg[k.lower()] = v
    return cfg


# ------------------------------------------------------------------- fetching
def http_json(url: str, timeout: int = 20, retries: int = 2) -> dict:
    """러너 IP는 Yahoo 429가 잦다 — 재시도 예산을 짧게 잡고(최대 ~8s) 캐시 폴백에 맡긴다."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2 + 3 * i)
    raise RuntimeError(f"http {url}: {last}")


def yahoo_daily(symbol: str, rng: str = "2y") -> list[dict]:
    """[{d, o, h, l, c, v}] 오름차순. 결측 봉은 버린다."""
    last = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{urllib.parse.quote(symbol)}?range={rng}&interval=1d")
        try:
            j = http_json(url)
            res = j["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(ts):
                c = q["close"][i]
                if c is None or q["open"][i] is None:
                    continue
                out.append({
                    "d": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "o": float(q["open"][i]), "h": float(q["high"][i]),
                    "l": float(q["low"][i]), "c": float(c),
                    "v": float(q["volume"][i] or 0),
                })
            if len(out) < 40:
                raise RuntimeError(f"too few bars ({len(out)})")
            return out
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"yahoo {symbol}: {last}")


def yahoo_spark_closes(symbols: list[str], rng: str = "1mo") -> dict[str, list[tuple[str, float]]]:
    """종가만 주는 배치 엔드포인트 — 요청 1회로 전 종목. chart v8 이 막혔을 때 최신 종가 보강용."""
    out = {}
    if not symbols:
        return out
    q = urllib.parse.quote(",".join(symbols))
    for host in ("query1", "query2"):
        try:
            j = http_json(f"https://{host}.finance.yahoo.com/v7/finance/spark?symbols={q}&range={rng}&interval=1d")
            for r in j["spark"]["result"]:
                resp = r["response"][0]
                ts, cl = resp["timestamp"], resp["indicators"]["quote"][0]["close"]
                out[r["symbol"]] = [(datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"), float(c))
                                    for t, c in zip(ts, cl) if c is not None]
            return out
        except Exception as e:  # noqa: BLE001
            log(f"[spark] {host} 실패: {e}")
    return out


def patch_with_closes(bars: list[dict], closes: list[tuple[str, float]]) -> tuple[list[dict], int]:
    """캐시에 없는 날짜만 종가 봉으로 보강. o=h=l=c, 거래량은 최근 20일 중앙값(근사, synthetic 표시)."""
    have = {b["d"] for b in bars}
    vols = sorted(b["v"] for b in bars[-20:]) or [0.0]
    v_med = vols[len(vols) // 2]
    added = 0
    for d, c in closes:
        if d not in have and d > bars[-1]["d"]:
            bars.append({"d": d, "o": c, "h": c, "l": c, "c": c, "v": v_med, "synthetic": True})
            added += 1
    return bars[-MAX_BARS:], added


NASDAQ_MAP = {"^IXIC": ("COMP", "index"), "QQQ": ("QQQ", "etf")}


def nasdaq_daily(symbol: str, days: int = 70) -> list[dict]:
    """api.nasdaq.com 히스토리(미국 주식·ETF·지수). Yahoo 429 시 폴백."""
    if symbol.endswith(".KS"):
        raise RuntimeError("nasdaq: KR 미지원")
    sym, cls = NASDAQ_MAP.get(symbol, (symbol, "stocks"))
    to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frm = (datetime.now(timezone.utc) - timedelta(days=int(days * 1.6))).strftime("%Y-%m-%d")
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}"
           f"&fromdate={frm}&todate={to}&limit={days}")
    j = http_json(url)
    rows = (j.get("data") or {}).get("tradesTable", {}).get("rows") or []
    def num(x):
        x = str(x).replace("$", "").replace(",", "").strip()
        return float(x) if x not in ("", "N/A", "--") else None
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
            c, o, h, l = num(r["close"]), num(r["open"]), num(r["high"]), num(r["low"])
            if None in (c, o, h, l):
                continue
            out.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": num(r.get("volume")) or 0.0})
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda b: b["d"])
    if len(out) < 10:
        raise RuntimeError(f"nasdaq {symbol}: rows={len(out)}")
    return out


def naver_daily(symbol: str, count: int = 70) -> list[dict]:
    """네이버 fchart XML (국내 종목). <item data="YYYYMMDD|o|h|l|c|v"/>"""
    if not symbol.endswith(".KS"):
        raise RuntimeError("naver: KR 전용")
    code = symbol.split(".")[0]
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        xml = r.read().decode("euc-kr", "replace")
    out = []
    for m in re.finditer(r'data="(\d{8})\|([\d.]+)\|([\d.]+)\|([\d.]+)\|([\d.]+)\|(\d+)"', xml):
        d, o, h, l, c, v = m.groups()
        out.append({"d": f"{d[:4]}-{d[4:6]}-{d[6:]}", "o": float(o), "h": float(h), "l": float(l),
                    "c": float(c), "v": float(v)})
    if len(out) < 10:
        raise RuntimeError(f"naver {symbol}: rows={len(out)}")
    return out


def fetch_symbol(symbol: str, rng: str) -> tuple[list[dict], str]:
    """소스 체인: yahoo → nasdaq(미국) / naver(국내). 성공한 소스명을 함께 반환."""
    errs = []
    for name, fn in (("yahoo", lambda: yahoo_daily(symbol, rng)),
                     ("nasdaq", lambda: nasdaq_daily(symbol)),
                     ("naver", lambda: naver_daily(symbol))):
        try:
            return fn(), name
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}:{str(e)[-60:]}")
    raise RuntimeError(" / ".join(errs))


def merge_bars(old: list[dict], new: list[dict]) -> list[dict]:
    by = {b["d"]: b for b in old}
    by.update({b["d"]: b for b in new})
    bars = [by[k] for k in sorted(by)]
    return bars[-MAX_BARS:]


def fetch_universe(symbols: list[str], cache: dict, today: str) -> tuple[dict, dict]:
    """returns (bars_by_symbol, status_by_symbol). status: fresh / cache / missing"""
    bars, status = {}, {}
    # 캐시가 있으면 짧은 구간만 요청(응답 작음), 없으면 2y 전체
    pending = list(symbols)
    for attempt in range(2):
        failed = []
        for s in pending:
            rng = "3mo" if cache.get(s) else "2y"
            try:
                fresh, src = fetch_symbol(s, rng)
                bars[s] = merge_bars(cache.get(s, []), fresh)
                status[s] = "fresh" if src == "yahoo" else f"fresh:{src}"
            except Exception as e:  # noqa: BLE001
                log(f"[fetch] {s} 실패({attempt + 1}차): {e}")
                failed.append(s)
            time.sleep(1.2)
        pending = failed
        if not pending:
            break
        log(f"[fetch] {len(pending)}종목 재시도 전 25s 대기")
        time.sleep(25)
    # 실패분: 캐시 + spark 종가 보강
    spark = yahoo_spark_closes(pending) if pending else {}
    for s in pending:
        cached = [dict(b) for b in cache.get(s, [])]
        if not cached:
            status[s] = "missing"
            continue
        added = 0
        if s in spark:
            cached, added = patch_with_closes(cached, spark[s])
        bars[s] = cached
        age = (datetime.strptime(today, "%Y-%m-%d") -
               datetime.strptime(cached[-1]["d"], "%Y-%m-%d")).days
        status[s] = ("spark" if added else "cache") if age <= CACHE_STALE_DAYS else "stale"
    return bars, status


# ------------------------------------------------------------------ indicators
def sma(x: list[float], n: int) -> float | None:
    if len(x) < n:
        return None
    return sum(x[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def rsi_series(closes: list[float], n: int = 14) -> list[float | None]:
    return [rsi(closes[: i + 1], n) for i in range(len(closes))]


def atr(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b - 1.0) * 100.0


def ret_n(closes: list[float], n: int) -> float | None:
    if len(closes) <= n:
        return None
    return pct(closes[-1], closes[-1 - n])


def ret_ytd(bars: list[dict]) -> float | None:
    year = bars[-1]["d"][:4]
    prev = [b for b in bars if b["d"] < f"{year}-01-01"]
    if not prev:
        return None
    return pct(bars[-1]["c"], prev[-1]["c"])


def percentile_rank(hist: list[float], x: float) -> float | None:
    vals = [v for v in hist if v is not None and not math.isnan(v)]
    if len(vals) < 30:
        return None
    below = sum(1 for v in vals if v <= x)
    return 100.0 * below / len(vals)


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(252) * 100.0


def obv_slope(bars: list[dict], n: int = 20) -> float | None:
    """OBV n일 변화를 n일 평균거래량으로 정규화 (거래량 일수 단위)."""
    if len(bars) < n + 1:
        return None
    obv = 0.0
    series = [0.0]
    for i in range(1, len(bars)):
        if bars[i]["c"] > bars[i - 1]["c"]:
            obv += bars[i]["v"]
        elif bars[i]["c"] < bars[i - 1]["c"]:
            obv -= bars[i]["v"]
        series.append(obv)
    avg_v = sum(b["v"] for b in bars[-n:]) / n
    if avg_v == 0:
        return None
    return (series[-1] - series[-1 - n]) / avg_v


def up_volume_ratio(bars: list[dict], n: int = 10) -> float | None:
    if len(bars) < n + 1:
        return None
    up = tot = 0.0
    for i in range(len(bars) - n, len(bars)):
        v = bars[i]["v"]
        tot += v
        if bars[i]["c"] > bars[i - 1]["c"]:
            up += v
    return (up / tot * 100.0) if tot else None


def mfi(bars: list[dict], n: int = 14) -> float | None:
    if len(bars) < n + 1:
        return None
    pos = neg = 0.0
    tp_prev = None
    for b in bars[-(n + 1):]:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        if tp_prev is not None:
            flow = tp * b["v"]
            if tp > tp_prev:
                pos += flow
            elif tp < tp_prev:
                neg += flow
        tp_prev = tp
    if neg == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + pos / neg)


def vol_ratio(bars: list[dict], short: int = 5, long: int = 60) -> float | None:
    if len(bars) < long:
        return None
    s = sum(b["v"] for b in bars[-short:]) / short
    l = sum(b["v"] for b in bars[-long:]) / long
    return (s / l) if l else None


def consecutive_up(closes: list[float]) -> int:
    k = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            k += 1
        else:
            break
    return k


def dist_series(closes: list[float], n: int) -> list[float]:
    out = []
    for i in range(n, len(closes) + 1):
        m = sum(closes[i - n:i]) / n
        out.append((closes[i - 1] / m - 1.0) * 100.0)
    return out


def symbol_metrics(sym: str, bars: list[dict]) -> dict:
    c = [b["c"] for b in bars]
    d50 = dist_series(c, 50)
    d200 = dist_series(c, 200)
    hi52 = max(b["h"] for b in bars[-252:])
    a = atr(bars)
    m = {
        "symbol": sym, "as_of": bars[-1]["d"], "close": round(c[-1], 2),
        "r1d": ret_n(c, 1), "r1w": ret_n(c, 5), "r1m": ret_n(c, 21),
        "r3m": ret_n(c, 63), "ytd": ret_ytd(bars),
        "rsi": rsi(c), "vol20": realized_vol(c),
        "d50": d50[-1] if d50 else None, "d200": d200[-1] if d200 else None,
        "d50_pct": percentile_rank(d50[:-1], d50[-1]) if len(d50) > 30 else None,
        "d200_pct": percentile_rank(d200[:-1], d200[-1]) if len(d200) > 30 else None,
        "from_52w_high": pct(c[-1], hi52),
        "atr_pct": (a / c[-1] * 100.0) if a else None,
        "vol_ratio": vol_ratio(bars), "obv_slope": obv_slope(bars),
        "upvol": up_volume_ratio(bars), "mfi": mfi(bars),
        "consec_up": consecutive_up(c),
    }
    m["patterns"] = detect_patterns(bars, m)
    return m


# ---------------------------------------------------------------- patterns
def detect_patterns(bars: list[dict], m: dict) -> list[str]:
    """정량 규칙으로 잡히는 과열형 패턴만. 해석은 메시지에서 한다."""
    c = [b["c"] for b in bars]
    out = []
    # 1) 파라볼릭: 50일선 이격이 자기 1년 분포 상위 5% + 최근 10일 상승 기울기가 이전 10일보다 가파름
    if m["d50_pct"] is not None and m["d50_pct"] >= 95 and len(c) > 21:
        r_recent = c[-1] / c[-11] - 1
        r_prior = c[-11] / c[-21] - 1
        if r_recent > r_prior > 0:
            out.append("파라볼릭")
    # 2) 거래량 클라이맥스: 5/60일 거래량 1.8배↑ + 당일 진폭이 ATR 1.5배↑
    if m["vol_ratio"] and m["vol_ratio"] >= 1.8 and m["atr_pct"]:
        rng = (bars[-1]["h"] - bars[-1]["l"]) / bars[-1]["c"] * 100
        if rng >= 1.5 * m["atr_pct"]:
            out.append("거래량 클라이맥스")
    # 3) 블로우오프 후보: 위 클라이맥스 + 종가가 당일 고가 대비 하단 40% 이내 (긴 윗꼬리)
    b = bars[-1]
    if "거래량 클라이맥스" in out and (b["h"] - b["l"]) > 0:
        pos = (b["c"] - b["l"]) / (b["h"] - b["l"])
        if pos <= 0.4:
            out.append("블로우오프 후보(윗꼬리)")
    # 4) 갭업 연속: 최근 5일 중 3일 이상 시가가 전일 고가 위
    gaps = sum(1 for i in range(len(bars) - 5, len(bars)) if i > 0 and bars[i]["o"] > bars[i - 1]["h"])
    if gaps >= 3:
        out.append("갭업 연속")
    # 5) RSI 하방 다이버전스: 최근 20일 신고가인데 RSI는 직전 고점(20~60일 전)보다 낮음
    rs = rsi_series(c[-80:])
    if len(rs) >= 80 and rs[-1] is not None:
        win_c, win_r = c[-20:], rs[-20:]
        prev_c, prev_r = c[-60:-20], rs[-60:-20]
        if c[-1] >= max(win_c) and c[-1] > max(prev_c):
            pr = max(v for v in prev_r if v is not None)
            if rs[-1] < pr - 5:
                out.append("RSI 하방 다이버전스")
    # 6) 200일선 극단 이격: 1년 분포 상위 3%
    if m["d200_pct"] is not None and m["d200_pct"] >= 97:
        out.append("200일선 극단 이격")
    return out


# ------------------------------------------------------------------ baskets
def basket_index(bars_by: dict, members: list[str]) -> list[tuple[str, float]]:
    """동일가중 일간수익률 평균을 누적한 지수(100 시작). 공통 거래일만 사용."""
    sets = [set(b["d"] for b in bars_by[s]) for s in members if s in bars_by]
    if not sets:
        return []
    days = sorted(set.intersection(*sets))
    closes = {s: {b["d"]: b["c"] for b in bars_by[s]} for s in members if s in bars_by}
    idx, out = 100.0, []
    prev = None
    for d in days:
        if prev is not None:
            rets = [closes[s][d] / closes[s][prev] - 1 for s in closes]
            idx *= 1 + sum(rets) / len(rets)
        out.append((d, idx))
        prev = d
    return out


def series_ret(series: list[tuple[str, float]], n: int) -> float | None:
    if len(series) <= n:
        return None
    return pct(series[-1][1], series[-1 - n][1])


def series_ytd(series: list[tuple[str, float]]) -> float | None:
    year = series[-1][0][:4]
    prev = [v for d, v in series if d < f"{year}-01-01"]
    return pct(series[-1][1], prev[-1]) if prev else None


def rs_line(a: list[tuple[str, float]], b: list[tuple[str, float]]) -> list[tuple[str, float]]:
    bd = dict(b)
    return [(d, v / bd[d]) for d, v in a if d in bd and bd[d]]


def rs_stats(rs: list[tuple[str, float]]) -> dict:
    vals = [v for _, v in rs]
    if len(vals) < 30:
        return {"rs_1m": None, "rs_3m": None, "rs_pct_1y": None, "rs_slope20": None}
    def r(n):
        return pct(vals[-1], vals[-1 - n]) if len(vals) > n else None
    slope = None
    if len(vals) > 20:
        slope = pct(sum(vals[-5:]) / 5, sum(vals[-25:-20]) / 5)
    return {"rs_1m": r(21), "rs_3m": r(63), "rs_pct_1y": percentile_rank(vals[-252:-1], vals[-1]),
            "rs_slope20": slope}


def basket_flow(metrics: list[dict]) -> dict:
    def avg(k):
        v = [m[k] for m in metrics if m.get(k) is not None]
        return sum(v) / len(v) if v else None
    return {"vol_ratio": avg("vol_ratio"), "upvol": avg("upvol"), "mfi": avg("mfi"),
            "obv_slope": avg("obv_slope"), "rsi": avg("rsi")}


# ------------------------------------------------------------- overheat gauge
def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def overheat_gauge(metrics: list[dict], rs: dict, flow: dict, basket_idx: list | None = None) -> dict:
    """0~100. 각 요소는 0~1로 정규화 후 가중합. 근거는 components 에 그대로 남긴다."""
    n = len(metrics)
    if n == 0:
        return {"score": None, "regime": "판정불가", "components": {}}
    comps = {}
    # A. 바스켓 평균 RSI (50→0, 80→1)
    r = flow.get("rsi")
    comps["rsi"] = {"v": r, "s": clamp01((r - 50) / 30) if r is not None else 0.0, "w": 0.15}
    # B. 50일선 이격 백분위 평균 (60%→0, 100%→1)
    p50 = [m["d50_pct"] for m in metrics if m["d50_pct"] is not None]
    v = sum(p50) / len(p50) if p50 else None
    comps["d50_pct"] = {"v": v, "s": clamp01((v - 60) / 40) if v else 0.0, "w": 0.15}
    # C. 200일선 이격 백분위 평균
    p200 = [m["d200_pct"] for m in metrics if m["d200_pct"] is not None]
    v = sum(p200) / len(p200) if p200 else None
    comps["d200_pct"] = {"v": v, "s": clamp01((v - 60) / 40) if v else 0.0, "w": 0.10}
    # D. 브레드스: 52주 고점 5% 이내 비율 + RSI>70 비율
    near_hi = sum(1 for m in metrics if m["from_52w_high"] is not None and m["from_52w_high"] >= -5) / n
    rsi70 = sum(1 for m in metrics if m["rsi"] is not None and m["rsi"] >= 70) / n
    comps["breadth_near_high"] = {"v": near_hi * 100, "s": clamp01(near_hi), "w": 0.10}
    comps["breadth_rsi70"] = {"v": rsi70 * 100, "s": clamp01(rsi70 / 0.6), "w": 0.10}
    # E. 거래량 클라이맥스 (5/60일 배수 1.0→0, 2.0→1)
    vr = flow.get("vol_ratio")
    comps["vol_climax"] = {"v": vr, "s": clamp01((vr - 1.0) / 1.0) if vr else 0.0, "w": 0.10}
    # F. 1M 상대강도(vs 나스닥) 1년 백분위 (70→0, 100→1)
    rp = rs.get("rs_pct_1y")
    comps["rs_extreme"] = {"v": rp, "s": clamp01((rp - 70) / 30) if rp is not None else 0.0, "w": 0.10}
    # G. 패턴 비율: 종목당 과열형 패턴 보유 비율
    pat = sum(1 for m in metrics if m["patterns"]) / n
    comps["pattern_ratio"] = {"v": pat * 100, "s": clamp01(pat / 0.5), "w": 0.10}
    # I. 브레드스 다이버전스: 바스켓 지수는 60일 고점권(2% 이내)인데 개별 52주 고점 5% 이내 비율이 낮음 = 소수 종목이 끌고 가는 말기 상승
    bd = 0.0
    bvals = [v for _, v in (basket_idx or [])]
    if len(bvals) >= 60:
        near_top = bvals[-1] >= max(bvals[-60:]) * 0.98
        bd = clamp01(1 - near_hi / 0.6) if near_top else 0.0
    comps["breadth_divergence"] = {"v": round(bd * 100, 1), "s": bd, "w": 0.10}
    # H. 실현변동성 평균 (40%→0, 90%→1) — 변동성 팽창은 말기 신호
    vv = [m["vol20"] for m in metrics if m["vol20"] is not None]
    v = sum(vv) / len(vv) if vv else None
    comps["vol20"] = {"v": v, "s": clamp01((v - 40) / 50) if v else 0.0, "w": 0.10}

    score = sum(c["s"] * c["w"] for c in comps.values()) / sum(c["w"] for c in comps.values()) * 100
    score = round(score, 1)
    regime = ("극단과열" if score >= 75 else "과열" if score >= 50 else
              "중립" if score >= 25 else "냉각")
    for c in comps.values():
        c["s"] = round(c["s"], 3)
        if c["v"] is not None:
            c["v"] = round(c["v"], 2)
    return {"score": score, "regime": regime, "components": comps}


# ---------------------------------------------------------------- pipeline
def market_day_ceiling(now_kst: datetime) -> str:
    """미국 종가 확정 기준일. KST 07시 이전 실행이면 전전일 취급하지 않고,
    단순히 '마지막으로 마감된 미국 거래일'을 Yahoo 데이터 최종봉으로 판단하므로 여기선 라벨용."""
    return now_kst.strftime("%Y-%m-%d")


def build_snapshot(uni: dict, bars_by: dict, status: dict, run_label: str) -> dict:
    opt = [s for s in uni["optical"]["symbols"] if s in bars_by and status[s] != "stale"]
    mem = [s for s in uni["memory"]["symbols"] if s in bars_by and status[s] != "stale"]
    bench = uni["benchmark"]["price"]
    vproxy = uni["benchmark"]["volume_proxy"]

    opt_m = [symbol_metrics(s, bars_by[s]) for s in opt]
    mem_m = [symbol_metrics(s, bars_by[s]) for s in mem]
    for m in opt_m:
        m["name"] = uni["optical"]["symbols"][m["symbol"]]
    for m in mem_m:
        m["name"] = uni["memory"]["symbols"][m["symbol"]]

    opt_idx = basket_index(bars_by, opt)
    mem_idx = basket_index(bars_by, mem)
    ndq_idx = [(b["d"], b["c"]) for b in bars_by[bench]] if bench in bars_by else []
    ndq_m = symbol_metrics(bench, bars_by[bench]) if bench in bars_by else None
    qqq_m = symbol_metrics(vproxy, bars_by[vproxy]) if vproxy in bars_by else None

    def basket_row(label, idx, ms, rs, flow):
        return {
            "label": label, "n": len(ms),
            "r1d": series_ret(idx, 1), "r1w": series_ret(idx, 5), "r1m": series_ret(idx, 21),
            "r3m": series_ret(idx, 63), "ytd": series_ytd(idx) if idx else None,
            **rs, "flow": flow,
        }

    opt_rs = rs_stats(rs_line(opt_idx, ndq_idx))
    mem_rs = rs_stats(rs_line(mem_idx, ndq_idx))
    opt_flow = basket_flow(opt_m)
    mem_flow = basket_flow(mem_m)
    ndq_flow = {k: qqq_m[k] for k in ("vol_ratio", "upvol", "mfi", "obv_slope", "rsi")} if qqq_m else {}
    ndq_row = {"label": "나스닥", "n": 1,
               "r1d": ndq_m["r1d"] if ndq_m else None, "r1w": ndq_m["r1w"] if ndq_m else None,
               "r1m": ndq_m["r1m"] if ndq_m else None, "r3m": ndq_m["r3m"] if ndq_m else None,
               "ytd": ndq_m["ytd"] if ndq_m else None,
               "rs_1m": 0.0, "rs_3m": 0.0, "rs_pct_1y": None, "rs_slope20": 0.0, "flow": ndq_flow}

    gauge = overheat_gauge(opt_m, opt_rs, opt_flow, opt_idx)
    if opt_idx:
        vals = [v for _, v in opt_idx]
        gauge["basket_from_52w_high"] = pct(vals[-1], max(vals[-252:]))
        gauge["basket_from_60d_high"] = pct(vals[-1], max(vals[-60:]))
    # 광통신-메모리 페어 RS
    om_rs = rs_stats(rs_line(opt_idx, mem_idx))

    as_of = max(b["d"] for s in (opt + [bench]) for b in bars_by[s][-1:]) if opt else run_label
    fresh_n = sum(1 for s in status if status[s].startswith("fresh"))
    data_status = "OK" if fresh_n == len(status) else ("DEGRADED" if opt_m and ndq_m else "FAIL")

    top5 = sorted(opt_m, key=lambda m: (m["r1m"] if m["r1m"] is not None else -1e9), reverse=True)[:5]
    snap = {
        "as_of": as_of, "run_label": run_label, "generated_kst": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "data_status": data_status, "fetch_status": status,
        "scoreboard": [basket_row("광통신", opt_idx, opt_m, opt_rs, opt_flow),
                       basket_row("메모리", mem_idx, mem_m, mem_rs, mem_flow), ndq_row],
        "optical_vs_memory": om_rs,
        "optical": {"members": opt_m, "top5": [m["symbol"] for m in top5]},
        "memory": {"members": mem_m},
        "gauge": gauge,
        "series": {
            "optical_idx": opt_idx[-130:], "memory_idx": mem_idx[-130:], "nasdaq_idx": ndq_idx[-130:],
            "rs_opt_ndq": rs_line(opt_idx, ndq_idx)[-130:], "rs_mem_ndq": rs_line(mem_idx, ndq_idx)[-130:],
        },
    }
    return snap


def fmt(v, dec=1, sign=True, suffix="%") -> str:
    if v is None:
        return "—"
    s = f"{v:+.{dec}f}" if sign else f"{v:.{dec}f}"
    return s + suffix


def bar_of(v, lo=0, hi=100, n=10) -> str:
    if v is None:
        return "░" * n
    k = int(round(clamp01((v - lo) / (hi - lo)) * n))
    return "█" * k + "░" * (n - k)


def regime_emoji(regime: str) -> str:
    return {"극단과열": "🔴", "과열": "🟠", "중립": "🟡", "냉각": "🔵"}.get(regime, "⚪")


def rs_arrow(slope) -> str:
    if slope is None:
        return "·"
    return "↗" if slope > 1 else "↘" if slope < -1 else "→"


def build_messages(snap: dict, pages_url: str) -> list[str]:
    sb = snap["scoreboard"]
    g = snap["gauge"]
    st = snap["data_status"]
    warn = "" if st == "OK" else f"\n⚠️ 데이터 {st}: " + ", ".join(
        f"{k}={v}" for k, v in snap["fetch_status"].items() if not v.startswith("fresh")) + \
        ("\n(spark=종가만 보강, 당일 거래량·진폭은 근사)" if "spark" in snap["fetch_status"].values() else "")

    # ---- 1) 상대강도 스코어보드
    lines = [f"<b>📡 옵티컬 레이더</b> · {snap['as_of']} 美 종가{warn}", "",
             "<b>① 상대 퍼포먼스</b> (동일가중 바스켓)",
             "<pre>구분    1D     1W     1M     3M     YTD</pre>"]
    for r in sb:
        lines.append("<pre>" + f"{r['label']:<4}" + "".join(
            f"{fmt(r[k], 1):>7}" for k in ("r1d", "r1w", "r1m", "r3m")) + f"{fmt(r['ytd'], 0):>8}" + "</pre>")
    lines += ["", "<b>② 나스닥 대비 상대강도(RS)</b>"]
    for r in sb[:2]:
        lines.append(f"· {r['label']}: 1M {fmt(r['rs_1m'])} / 3M {fmt(r['rs_3m'])} "
                     f"{rs_arrow(r['rs_slope20'])} 1Y백분위 {fmt(r['rs_pct_1y'], 0, False, '')}")
    om = snap["optical_vs_memory"]
    lines.append(f"· 광통신÷메모리: 1M {fmt(om['rs_1m'])} / 3M {fmt(om['rs_3m'])} {rs_arrow(om['rs_slope20'])}")
    lines += ["", "<b>③ 수급 프록시</b> (거래량배수 5/60 · 상승거래량비 10D · MFI14)"]
    for r in sb:
        f = r["flow"]
        lines.append(f"· {r['label']}: {fmt(f.get('vol_ratio'), 2, False, 'x')} · "
                     f"{fmt(f.get('upvol'), 0, False)} · MFI {fmt(f.get('mfi'), 0, False, '')}")
    msg1 = "\n".join(lines)

    # ---- 2) 광통신 상세 + 과열 게이지
    opt = snap["optical"]["members"]
    by = {m["symbol"]: m for m in opt}
    lines = [f"<b>🔦 광통신 상세</b> ({len(opt)}종목)", "",
             "<b>TOP5 (1M 수익률)</b>",
             "<pre>종목       1D     1W     1M  RSI  50MA</pre>"]
    for s in snap["optical"]["top5"]:
        m = by[s]
        lines.append("<pre>" + f"{s:<5}" + f"{fmt(m['r1d']):>7}{fmt(m['r1w']):>7}{fmt(m['r1m']):>7}"
                     f"{fmt(m['rsi'], 0, False, ''):>5}{fmt(m['d50'], 0):>6}" + "</pre>")
    laggards = sorted(opt, key=lambda m: (m["r1m"] if m["r1m"] is not None else 1e9))[:2]
    lines.append("· 후미: " + ", ".join(f"{m['symbol']} {fmt(m['r1m'])}" for m in laggards))

    emo = regime_emoji(g["regime"])
    lines += ["", f"<b>④ 과열 게이지</b> {emo} <b>{g['regime']}</b> {g['score']}/100",
              f"<pre>{bar_of(g['score'])}</pre>"]
    c = g["components"]
    def cv(k, d=0, suf=""):
        return fmt(c[k]["v"], d, False, suf) if k in c else "—"
    dd = g.get("basket_from_52w_high")
    g20 = snap.get("gauge_20d_max")
    lines.append(f"· 바스켓 52주고점 대비 {fmt(dd)} · 게이지 20일 최고 {fmt(g20, 0, False, '')}")
    lines += [f"· RSI평균 {cv('rsi')} · 50MA이격 백분위 {cv('d50_pct')} · 200MA {cv('d200_pct')}",
              f"· 52주고점 5%내 {cv('breadth_near_high')}% · RSI70↑ {cv('breadth_rsi70')}%",
              f"· 거래량배수 {cv('vol_climax', 2, 'x')} · 실현변동성 {cv('vol20')}% · RS극단 {cv('rs_extreme')}",
              f"· 브레드스 다이버전스 {cv('breadth_divergence')}/100 (지수 고점권+소수 주도)"]
    pats = [(m["symbol"], m["patterns"]) for m in opt if m["patterns"]]
    if pats:
        lines += ["", "<b>패턴 관측</b>"]
        for s, p in pats[:8]:
            lines.append(f"· {s}: {', '.join(p)}")
    else:
        lines += ["", "패턴 관측: 과열형 패턴 없음"]
    lines += ["", "<i>게이지는 위치 보고이지 천정 예측이 아닙니다. 극단과열은 '분할 익절·신규 진입 보류' 검토 구간으로만 해석하세요.</i>"]
    if pages_url:
        lines.append(f'<a href="{pages_url}">📊 대시보드</a>')
    msg2 = "\n".join(lines)
    return [msg1, msg2]


def send_telegram(messages: list[str], token: str, chat_id: str) -> None:
    if not token or not chat_id:
        log("[telegram] 자격증명 없음 — 발송 생략")
        return
    for text in messages:
        if len(text) > 4000:
            text = text[:3990] + "\n…(생략)"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            log(f"[telegram] 발송 완료 (HTTP {r.status})")
        time.sleep(1.0)


def append_history(history: list[dict], snap: dict) -> list[dict]:
    row = {"as_of": snap["as_of"], "gauge": snap["gauge"]["score"], "regime": snap["gauge"]["regime"],
           "opt_1m": snap["scoreboard"][0]["r1m"], "mem_1m": snap["scoreboard"][1]["r1m"],
           "ndq_1m": snap["scoreboard"][2]["r1m"], "opt_rs_1m": snap["scoreboard"][0]["rs_1m"],
           "mem_rs_1m": snap["scoreboard"][1]["rs_1m"]}
    hist = [h for h in history if h.get("as_of") != row["as_of"]]
    hist.append(row)
    hist.sort(key=lambda h: h["as_of"])
    return hist[-400:]


def backfill_history(uni: dict, bars_by: dict, status: dict, days: int = 250) -> list[dict]:
    """history.json 이 비어 있을 때 캐시 일봉으로 과거 게이지를 재구성한다 (대시보드 추이용)."""
    bench = uni["benchmark"]["price"]
    if bench not in bars_by:
        return []
    dates = [b["d"] for b in bars_by[bench]][-days:]
    out = []
    for d in dates:
        trunc = {s: [b for b in bs if b["d"] <= d] for s, bs in bars_by.items()}
        trunc = {s: bs for s, bs in trunc.items() if len(bs) >= 220}
        if bench not in trunc:
            continue
        try:
            snap = build_snapshot(uni, trunc, {s: "fresh" for s in trunc}, d)
            out = append_history(out, snap)
        except Exception as e:  # noqa: BLE001
            log(f"[backfill] {d} 건너뜀: {e}")
    return out


def main(argv: list[str]) -> int:
    cfg = load_config()
    force = "--force" in argv
    dry = "--dry-run" in argv
    label = next((a for a in argv if DATE_RE.match(a)), "") or datetime.now(KST).strftime("%Y-%m-%d")

    uni = load_json(os.path.join(HERE, "universe.json"), None)
    if not uni:
        log("::error::universe.json 없음")
        return 1
    symbols = (list(uni["optical"]["symbols"]) + list(uni["memory"]["symbols"]) +
               [uni["benchmark"]["price"], uni["benchmark"]["volume_proxy"]])

    cache_path = os.path.join(DATA_DIR, "prices.json")
    cache = load_json(cache_path, {})
    bars, status = fetch_universe(symbols, cache, label)
    log("[fetch] " + ", ".join(f"{k}:{v}" for k, v in status.items()))
    if uni["benchmark"]["price"] not in bars or not any(s in bars for s in uni["optical"]["symbols"]):
        log("::error::벤치마크 또는 광통신 종목 전부 수집 실패 — 판정 불가")
        return 1

    snap = build_snapshot(uni, bars, status, label)
    latest_path = os.path.join(DATA_DIR, "latest.json")
    prev = load_json(latest_path, {})
    unchanged = prev.get("as_of") == snap["as_of"] and prev.get("data_status") == snap["data_status"]

    if not dry:
        if any(v.startswith("fresh") or v == "spark" for v in status.values()):
            save_json(cache_path, {k: v for k, v in bars.items()})
        if not unchanged:
            save_json(latest_path, snap)
            hist_path = os.path.join(DATA_DIR, "history.json")
            hist = load_json(hist_path, [])
            if len(hist) < 5:
                log("[backfill] history 부족 — 캐시 일봉으로 과거 게이지 재구성")
                hist = backfill_history(uni, bars, status)
            save_json(hist_path, append_history(hist, snap))
        # 대시보드 데이터
        if cfg["dashboard_dir"]:
            dd = os.path.join(cfg["dashboard_dir"], "data")
            save_json(os.path.join(dd, "latest.json"), load_json(latest_path, snap))
            save_json(os.path.join(dd, "history.json"), load_json(os.path.join(DATA_DIR, "history.json"), []))

    hist_now = load_json(os.path.join(DATA_DIR, "history.json"), [])
    recent = [h["gauge"] for h in hist_now[-20:] if h.get("gauge") is not None]
    snap["gauge_20d_max"] = max(recent + ([snap["gauge"]["score"]] if snap["gauge"]["score"] is not None else []), default=None)
    msgs = build_messages(snap, cfg["pages_url"])
    for m in msgs:
        log("-" * 60 + "\n" + re.sub(r"<[^>]+>", "", m))
    if dry:
        log("[dry-run] 발송 생략")
    elif unchanged and not force:
        log(f"[skip] as_of {snap['as_of']} 이미 발송됨 — 재발송 생략 (--force 로 강제)")
    else:
        send_telegram(msgs, cfg["telegram_token"], cfg["telegram_chat_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
