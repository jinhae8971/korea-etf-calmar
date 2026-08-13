#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
인컴 ETF 주간 모니터
- 대상: QQQI / BALI / JEPI / SCHD (TICKERS 환경변수로 변경 가능)
- 측정: 총수익률(주간·1M·3M·YTD·1Y), 분배금 증감, TTM 배당률 증감, AUM 증감, MDD
- 발송: 텔레그램 전략비서 채널 (주 1회)
- 누적: data/history.json (주간 스냅샷) → WoW 비교의 기준
"""
import os
import sys
import json
import time
import math
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import yfinance as yf

KST = timezone(timedelta(hours=9))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")

DEFAULT_TICKERS = ["QQQI", "BALI", "JEPI", "SCHD"]
NAMES = {
    "QQQI": "NEOS 나스닥100 하이인컴",
    "BALI": "iShares 미국 대형주 프리미엄인컴",
    "JEPI": "JPM 에퀴티 프리미엄인컴",
    "SCHD": "Schwab 미국 배당주",
    "IDVO": "Amplify CWP 해외 배당인컴",
    "SPYI": "NEOS S&P500 하이인컴",
    "DIVO": "Amplify CWP 배당인컴",
}

# 사유 자동 판별이 안 될 때 안내할 운용사 분배 공시 페이지
DIST_URL = {
    "QQQI": "https://neosfunds.com/qqqi/",
    "BALI": "https://www.ishares.com/us/products/333207/",
    "JEPI": "https://am.jpmorgan.com/us/en/asset-management/adv/products/jpmorgan-equity-premium-income-etf-etf-shares-46641q332",
    "SCHD": "https://www.schwabassetmanagement.com/products/schd",
    "IDVO": "https://amplifyetfs.com/idvo/",
    "SPYI": "https://neosfunds.com/spyi/",
    "DIVO": "https://amplifyetfs.com/divo/",
}

# 종목별 옵션 프리미엄 환경을 대표하는 변동성 지수
VOL_INDEX = {"QQQI": "^VXN", "JEPQ": "^VXN"}
DEFAULT_VOL_INDEX = "^VIX"


# ----------------------------------------------------------------------
# 설정 / 입출력
# ----------------------------------------------------------------------
def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    config_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] 읽기 실패 - 환경변수만 사용: {e}")
            data = {}
        for k, v in data.items():
            key = k.lower()
            if key in cfg and not cfg[key]:
                cfg[key] = v
    return cfg


def get_tickers() -> list:
    raw = os.environ.get("TICKERS", "").strip()
    if not raw:
        return list(DEFAULT_TICKERS)
    out = [t.strip().upper() for t in raw.replace(",", " ").split() if t.strip()]
    return out or list(DEFAULT_TICKERS)


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[load] {path} 읽기 실패: {e}")
        return default


# ----------------------------------------------------------------------
# 지표 계산
# ----------------------------------------------------------------------
def total_return(adj: pd.Series, days: int):
    """days 영업일 전 대비 총수익률(배당 재투자). 데이터 부족 시 None."""
    if len(adj) < 2:
        return None
    cutoff = adj.index[-1] - pd.Timedelta(days=days)
    win = adj[adj.index <= cutoff]
    if win.empty:
        return None
    return float(adj.iloc[-1] / win.iloc[-1] - 1.0)


def ytd_return(adj: pd.Series):
    last = adj.index[-1]
    jan1 = pd.Timestamp(year=last.year, month=1, day=1)
    prev = adj[adj.index < jan1]
    if prev.empty:
        return None
    return float(adj.iloc[-1] / prev.iloc[-1] - 1.0)


def max_drawdown(adj: pd.Series):
    if adj.empty:
        return None
    return float((adj / adj.cummax() - 1.0).min())


def fetch_aum(ticker: str):
    """AUM(순자산)은 시계열이 없으므로 매주 스냅샷을 남겨 WoW를 계산한다."""
    for attempt in range(3):
        try:
            info = yf.Ticker(ticker).info or {}
            val = info.get("totalAssets")
            if val:
                return float(val)
            return None
        except Exception as e:  # noqa: BLE001 - 외부 API는 어떤 예외든 재시도
            print(f"[aum] {ticker} 시도 {attempt + 1} 실패: {e}")
            time.sleep(2 * (attempt + 1))
    return None


def collect(ticker: str) -> dict:
    hist = None
    for attempt in range(3):
        try:
            hist = yf.Ticker(ticker).history(period="max", auto_adjust=False)
            if hist is not None and not hist.empty:
                break
        except Exception as e:  # noqa: BLE001
            print(f"[hist] {ticker} 시도 {attempt + 1} 실패: {e}")
        time.sleep(2 * (attempt + 1))

    if hist is None or hist.empty:
        return {"ticker": ticker, "ok": False}

    hist.index = pd.to_datetime(hist.index, utc=True).tz_localize(None)
    adj = hist["Adj Close"].dropna()
    px = hist["Close"].dropna()
    div = hist["Dividends"]
    div = div[div > 0]

    last_date = adj.index[-1]
    ttm_sum = float(div[div.index > last_date - pd.Timedelta(days=365)].sum())
    ttm_yield = ttm_sum / float(px.iloc[-1]) if len(px) else None

    # 최근 분배금 2건 비교 (분배금 증감)
    last_div = float(div.iloc[-1]) if len(div) >= 1 else None
    prev_div = float(div.iloc[-2]) if len(div) >= 2 else None
    div_chg = (last_div / prev_div - 1.0) if (last_div and prev_div) else None
    last_div_date = str(div.index[-1].date()) if len(div) >= 1 else None

    # 변동분배 종목(BALI 등)은 직전비 변동폭이 커 오탐이 잦다.
    # 최근 6회 평균 대비 편차를 별도 지표로 두고 경보는 이 값으로 판단한다.
    div_vs_avg = None
    if len(div) >= 4 and last_div:
        avg6 = float(div.iloc[-6:].mean())
        if avg6 > 0:
            div_vs_avg = last_div / avg6 - 1.0

    # --- 사유 판별용 보조 지표 ---
    # (a) 지급 간격: 회차 스킵/지연 여부
    interval_days = interval_median = None
    if len(div) >= 4:
        gaps = div.index.to_series().diff().dt.days.dropna()
        interval_days = int(gaps.iloc[-1])
        interval_median = int(gaps.iloc[-6:-1].median()) if len(gaps) >= 3 else None

    # (b) 직전 회차가 비정상 고액이었는지 (기저효과)
    prev_vs_avg = None
    if len(div) >= 7 and prev_div:
        base = float(div.iloc[-7:-1].mean())
        if base > 0:
            prev_vs_avg = prev_div / base - 1.0

    # (c) 분배율(분배금 ÷ 배당락일 종가) 편차
    #     절대액은 줄었는데 분배율이 그대로면 = 기준가 하락에 따른 감소
    div_rate_dev = None
    if len(div) >= 4:
        rates = []
        for dt, amt in div.iloc[-6:].items():
            near = px[px.index <= dt]
            if not near.empty and float(near.iloc[-1]) > 0:
                rates.append(float(amt) / float(near.iloc[-1]))
        if len(rates) >= 4:
            avg_rate = sum(rates[:-1]) / len(rates[:-1])
            if avg_rate > 0:
                div_rate_dev = rates[-1] / avg_rate - 1.0

    adj_1y = adj[adj.index > last_date - pd.Timedelta(days=365)]

    return {
        "ticker": ticker,
        "ok": True,
        "name": NAMES.get(ticker, ticker),
        "date": str(last_date.date()),
        "price": round(float(px.iloc[-1]), 2),
        "r_1w": total_return(adj, 7),
        "r_1m": total_return(adj, 30),
        "r_3m": total_return(adj, 91),
        "r_ytd": ytd_return(adj),
        "r_1y": total_return(adj, 365),
        "ttm_div": round(ttm_sum, 4),
        "ttm_yield": ttm_yield,
        "last_div": last_div,
        "prev_div": prev_div,
        "div_chg": div_chg,
        "div_vs_avg": div_vs_avg,
        "interval_days": interval_days,
        "interval_median": interval_median,
        "prev_vs_avg": prev_vs_avg,
        "div_rate_dev": div_rate_dev,
        "last_div_date": last_div_date,
        "aum": fetch_aum(ticker),
        "mdd_1y": max_drawdown(adj_1y),
        "mdd_all": max_drawdown(adj),
        "inception": str(adj.index[0].date()),
    }


def fetch_vol_context() -> dict:
    """옵션 프리미엄 환경 판단용. 실패해도 전체 실행을 막지 않는다."""
    out = {}
    for sym in ("^VIX", "^VXN"):
        try:
            h = yf.Ticker(sym).history(period="1y")["Close"].dropna()
            if len(h) >= 130:
                out[sym] = {
                    "m1": float(h.iloc[-21:].mean()),
                    "m6": float(h.iloc[-126:].mean()),
                    "last": float(h.iloc[-1]),
                }
        except Exception as e:  # noqa: BLE001
            print(f"[vol] {sym} 수집 실패: {e}")
    return out


def diagnose_distribution(r: dict, vol_ctx: dict) -> list:
    """분배금 추세 이탈의 사유 후보를 근거와 함께 반환한다.

    단정하지 않는다 — 관측된 정황만 제시하고, 판별 불가 시 공시 확인을 안내한다.
    """
    reasons = []

    # 1) 회차 스킵·지연
    if r.get("interval_days") and r.get("interval_median"):
        if r["interval_days"] > r["interval_median"] * 1.5:
            reasons.append(
                f"지급 간격 {r['interval_days']}일 (통상 {r['interval_median']}일) — 회차 스킵·지연 가능성"
            )

    # 2) 직전 회차 기저효과
    if r.get("prev_vs_avg") is not None and r["prev_vs_avg"] > 0.25:
        reasons.append(
            f"직전 회차가 평균 대비 {r['prev_vs_avg'] * 100:+.0f}% 고액 — 기저효과(특별분배 등)"
        )

    # 3) 분배율은 유지 → 기준가 하락에 따른 절대액 감소
    if r.get("div_rate_dev") is not None and abs(r["div_rate_dev"]) < 0.05:
        reasons.append(
            f"분배율(분배금÷기준가)은 평시 대비 {r['div_rate_dev'] * 100:+.1f}%로 유지 — "
            "정책 변경이 아닌 기준가 하락 반영"
        )

    # 3-b) 분배율 자체가 축소 → 기준가 요인이 아닌 실제 감액
    if r.get("div_rate_dev") is not None and r["div_rate_dev"] <= -0.10:
        reasons.append(
            f"분배율도 평시 대비 {r['div_rate_dev'] * 100:+.1f}% 축소 — "
            "기준가 요인이 아닌 분배 자체의 감액"
        )

    # 4) 옵션 프리미엄 환경 축소
    sym = VOL_INDEX.get(r["ticker"], DEFAULT_VOL_INDEX)
    v = vol_ctx.get(sym)
    if v and v["m6"] > 0:
        ch = v["m1"] / v["m6"] - 1.0
        if ch < -0.10:
            reasons.append(
                f"{sym} 1M평균 {v['m1']:.1f} vs 6M평균 {v['m6']:.1f} ({ch * 100:+.0f}%) — "
                "콜 프리미엄 수취 환경 축소"
            )

    # 5) 기초자산 급등 (콜 피인 → 프리미엄 재원 압박)
    if r.get("r_1m") is not None and r["r_1m"] > 0.07:
        reasons.append(f"기초자산 1M {r['r_1m'] * 100:+.1f}% 급등 — 콜 피인 구간")

    if not reasons:
        url = DIST_URL.get(r["ticker"])
        tail = f" → {url}" if url else ""
        reasons.append(f"자동 판별 불가 — 운용사 분배 공시 확인 필요{tail}")

    return reasons


# ----------------------------------------------------------------------
# 메시지 조립
# ----------------------------------------------------------------------
def pct(v, digits=1, sign=True):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    s = f"{v * 100:+.{digits}f}%" if sign else f"{v * 100:.{digits}f}%"
    return s


def money(v):
    if v is None:
        return "–"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:,.0f}"


def delta_pp(cur, prev, digits=2):
    """퍼센트포인트 증감 (배당률처럼 이미 % 단위인 값)."""
    if cur is None or prev is None:
        return None
    return cur - prev


def build_message(rows: list, prev_snap: dict, asof: str, vol_ctx: dict = None) -> str:
    vol_ctx = vol_ctx or {}
    lines = [
        "📊 <b>인컴 ETF 주간 브리프</b>",
        f"기준일 {asof} · 종가 기준",
        "",
    ]
    for r in rows:
        if not r.get("ok"):
            lines.append(f"<b>{r['ticker']}</b> ⚠️ 데이터 수집 실패")
            lines.append("")
            continue
        p = prev_snap.get(r["ticker"], {})

        # 배당률 증감 (%p)
        dy = delta_pp(
            r["ttm_yield"] * 100 if r["ttm_yield"] is not None else None,
            p.get("ttm_yield") * 100 if p.get("ttm_yield") is not None else None,
        )
        dy_txt = f" ({dy:+.2f}%p)" if dy is not None else ""

        # AUM 증감
        aum_txt = money(r["aum"])
        if r["aum"] and p.get("aum"):
            wow = r["aum"] / p["aum"] - 1.0
            aum_txt += f" ({wow * 100:+.1f}%, {'+' if r['aum'] >= p['aum'] else '-'}{money(abs(r['aum'] - p['aum']))})"

        # 분배금 증감
        if r["last_div"] is not None:
            dv = f"${r['last_div']:.4f}"
            if r["div_chg"] is not None:
                dv += f" (직전비 {r['div_chg'] * 100:+.1f}%"
                if r.get("div_vs_avg") is not None:
                    dv += f", 6회평균비 {r['div_vs_avg'] * 100:+.1f}%"
                dv += ")"
        else:
            dv = "–"

        lines.append(f"<b>{r['ticker']}</b> ${r['price']} · {r['name']}")
        lines.append(
            f"  수익 주간 {pct(r['r_1w'])} | 1M {pct(r['r_1m'])} | "
            f"YTD {pct(r['r_ytd'])} | 1Y {pct(r['r_1y'])}"
        )
        lines.append(
            f"  배당률 {pct(r['ttm_yield'], 2, sign=False)}{dy_txt} · 최근분배 {dv}"
        )
        lines.append(f"  AUM {aum_txt}")
        lines.append(f"  MDD 1Y {pct(r['mdd_1y'])} | 전체 {pct(r['mdd_all'])}")
        lines.append("")

    ok = [r for r in rows if r.get("ok")]

    # 하이라이트
    lines.append("━━━━━━━━━━")
    wk = [r for r in ok if r["r_1w"] is not None]
    if wk:
        best = max(wk, key=lambda x: x["r_1w"])
        worst = min(wk, key=lambda x: x["r_1w"])
        lines.append(f"🏆 주간 최고 {best['ticker']} {pct(best['r_1w'])}")
        lines.append(f"🔻 주간 최저 {worst['ticker']} {pct(worst['r_1w'])}")

    # 분배금 추세 감액 경보 (최근 6회 평균 대비 -15% 미만)
    cuts = [
        r for r in ok
        if r.get("div_vs_avg") is not None and r["div_vs_avg"] < -0.15
    ]
    for c in cuts:
        lines.append(
            f"⚠️ <b>{c['ticker']} 분배금 추세 이탈</b> "
            f"{c['div_vs_avg'] * 100:+.1f}% (6회평균비)"
        )
        for reason in diagnose_distribution(c, vol_ctx):
            lines.append(f"   · {reason}")

    # AUM 순유출 경보
    outs = []
    for r in ok:
        p = prev_snap.get(r["ticker"], {})
        if r["aum"] and p.get("aum"):
            ch = r["aum"] / p["aum"] - 1.0
            if ch < -0.03:
                outs.append(f"{r['ticker']} {ch * 100:+.1f}%")
    if outs:
        lines.append(f"⚠️ AUM 3%↑ 감소: {', '.join(outs)}")

    if not prev_snap:
        lines.append("ℹ️ 첫 실행 — 증감 지표는 다음 주부터 표시됩니다.")

    return "\n".join(lines)


def send_telegram(messages: list, token: str, chat_id: str):
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for msg in messages:
        r = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        r.raise_for_status()


# ----------------------------------------------------------------------
def main():
    cfg = load_config()
    tickers = get_tickers()
    now_kst = datetime.now(KST)
    print(f"[run] {now_kst.isoformat()} tickers={tickers}")

    rows = [collect(t) for t in tickers]
    ok_rows = [r for r in rows if r.get("ok")]
    if not ok_rows:
        print("[fatal] 전 종목 수집 실패")
        sys.exit(1)

    asof = ok_rows[0]["date"]

    history = load_json(HISTORY_PATH, [])
    prev_snap = {}
    if history:
        prev_snap = {r["ticker"]: r for r in history[-1].get("rows", []) if r.get("ok")}

    need_diag = any(
        r.get("ok") and r.get("div_vs_avg") is not None and r["div_vs_avg"] < -0.15
        for r in rows
    )
    vol_ctx = fetch_vol_context() if need_diag else {}

    msg = build_message(rows, prev_snap, asof, vol_ctx)
    print(msg)
    send_telegram([msg], cfg["telegram_token"], cfg["telegram_chat_id"])

    # 같은 기준일이면 덮어쓰기 — 재실행 시 중복 스냅샷 방지(멱등)
    snapshot = {"asof": asof, "run_kst": now_kst.strftime("%Y-%m-%d"), "rows": rows}
    if history and history[-1].get("asof") == asof:
        history[-1] = snapshot
    else:
        history.append(snapshot)
    history = history[-260:]  # 5년치 주간 스냅샷 유지

    save_json(HISTORY_PATH, history)
    save_json(LATEST_PATH, snapshot)
    print(f"[done] history {len(history)}건 저장")


if __name__ == "__main__":
    main()
