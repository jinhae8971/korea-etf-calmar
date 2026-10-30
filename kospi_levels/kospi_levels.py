# -*- coding: utf-8 -*-
"""
KOSPI 매매기준점(Trading Reference Points) — 수집·계산 전용

- 데이터: Yahoo Finance ^KS11 (가격·당일 등락=일봉 / 지표·레벨=주봉, TradingView 1주 차트와 정합)
- 지표: SuperTrend(20,4), 20주선, RSI(14)+SMA(14), MACD(12,26,9), Wilder ATR(20)
- 레벨: 스윙 고/저, 피보나치(하락 되돌림=저항 / 상승 되돌림=지지), 심리 라운드 — 티어 가중 큐레이션
- 산출: 추세 레짐 + 3-시나리오 트리거(강세/기본/약세)

이 스크립트는 발송하지 않는다. 완성된 본문을 data/latest.json 의 message 에 담아 두면
Brief Hub(@spytltgld_strategy_bot)가 평일 08:00 KST 이후 기능별 별도 메시지로 보낸다.
(2026-10-24 jinhae8971/kospi-levels 레포에서 이전 — 그쪽 봇 토큰 폐기로 401 연속 실패)

사용:
  python kospi_levels/kospi_levels.py            # 오늘(KST) 데이터가 이미 있으면 그대로 종료(멱등)
  python kospi_levels/kospi_levels.py --force    # 다시 계산해 덮어쓰기
  python kospi_levels/kospi_levels.py --dry      # 파일을 쓰지 않고 본문만 출력
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

KST = timezone(timedelta(hours=9))
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "data", "latest.json")
WEEKDAY_KO = "월화수목금토일"

# ---- 파라미터 (TradingView 차트 설정과 정합 — 이전 전과 동일) ----
TICKER          = "^KS11"
LOOKBACK_PERIOD = "3y"
ST_PERIOD       = 20      # SuperTrend ATR 기간
ST_MULT         = 4.0     # SuperTrend 승수
MA_LEN          = 20      # 20주 이동평균
RSI_LEN         = 14
RSI_SMA         = 14      # RSI 스무딩 (차트의 노란선)
MACD_FAST, MACD_SLOW, MACD_SIG = 12, 26, 9
SWING_HI_WIN    = 52      # 스윙 고점 탐색(주)
UPLEG_WIN       = 104     # 상승 파동 기저 탐색(주)


# ============================================================
# 데이터 로드
# ============================================================
def sanitize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """가격이 0·음수인 불량 봉 제거 (2026-10-30: Yahoo 주봉 저가 0 → 스윙저점 0·ATR 왜곡·'주봉 종가 0 이탈' 문구)."""
    px = df[["Open", "High", "Low", "Close"]]
    bad = (px <= 0).any(axis=1)
    if bad.any():
        print(f"[fetch] 불량 봉 {int(bad.sum())}개 제거: {[i.strftime('%Y-%m-%d') for i in df.index[bad]]}")
    return df[~bad]


def _download(period: str, interval: str, min_rows: int) -> pd.DataFrame:
    import yfinance as yf

    last_err: Exception | None = None
    for attempt, wait in enumerate((0, 4, 10), start=1):
        if wait:
            time.sleep(wait)
        try:
            df = yf.download(
                TICKER, period=period, interval=interval,
                progress=False, auto_adjust=True, threads=False,
            )
            if df is not None and len(df) >= min_rows:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = sanitize_ohlc(df[["Open", "High", "Low", "Close", "Volume"]].dropna())
                if len(df) >= min_rows:
                    return df
            last_err = RuntimeError(f"행 부족 ({0 if df is None else len(df)} < {min_rows})")
        except Exception as e:  # pragma: no cover - 네트워크
            last_err = e
        print(f"[fetch] {interval} 시도 {attempt}/3 실패: {last_err}")
    raise RuntimeError(f"데이터 로드 실패({interval}): {last_err}")


# ---- 일봉 1차 소스 = 네이버 증권 지수 API (KRX 확정 종가) ----
# 2026-10-30: Yahoo ^KS11 일봉이 10/29를 통째로 누락 → 10/30 브리프가 '10/28 종가 기준'으로 나감.
# 가격·등락·주봉 마지막 봉은 네이버 확정 종가로 맞추고, Yahoo 는 폴백 + 3년 주봉 이력용으로만 쓴다.
NAVER_URL = "https://m.stock.naver.com/api/index/KOSPI/price"
CLOSE_CONFIRM_HM = (15, 40)   # 이 시각(KST) 전 실행이면 오늘 봉은 장중/미확정 → 제외
DAILY_SOURCE = "Yahoo Finance ^KS11"


def _num(x) -> float:
    return float(str(x).replace(",", ""))


def _fetch_daily_naver(pages: int = 3) -> pd.DataFrame:
    import requests

    rows = []
    for p in range(1, pages + 1):
        r = requests.get(NAVER_URL, params={"pageSize": 20, "page": p},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        for it in r.json():
            rows.append({
                "Date": pd.Timestamp(str(it["localTradedAt"])[:10]),
                "Open": _num(it["openPrice"]), "High": _num(it["highPrice"]),
                "Low": _num(it["lowPrice"]), "Close": _num(it["closePrice"]),
                "Volume": _num(it.get("accumulatedTradingVolume") or 0),
            })
    if len(rows) < 2:
        raise RuntimeError(f"네이버 일봉 행 부족 ({len(rows)})")
    return pd.DataFrame(rows).drop_duplicates("Date").set_index("Date").sort_index()


def _naive_dates(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    return df


def drop_unconfirmed(daily: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """KST 15:40 이전 실행이면 오늘 날짜 봉(장중·미확정)을 뺀다."""
    if len(daily) and (now.hour, now.minute) < CLOSE_CONFIRM_HM \
            and daily.index[-1].date() == now.date():
        return daily.iloc[:-1]
    return daily


def patch_weekly_last(weekly: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """주봉 마지막 봉을 확정 일봉으로 보정 — 이번 주 봉이 없으면 만들고, 미확정 오늘분은 일봉에서 이미 빠져 있다."""
    w = _naive_dates(weekly)
    last_d = daily.index[-1]
    wk_start = last_d - pd.Timedelta(days=last_d.weekday())
    wk = daily[daily.index >= wk_start]
    if wk.empty:
        return w
    if w.index[-1] < wk_start:
        w.loc[wk_start] = [wk["Open"].iloc[0], wk["High"].max(), wk["Low"].min(),
                           wk["Close"].iloc[-1], wk["Volume"].sum()]
        return w
    i = w.index[-1]
    w.loc[i, ["Open", "High", "Low", "Close"]] = [
        wk["Open"].iloc[0], wk["High"].max(), wk["Low"].min(), wk["Close"].iloc[-1]]
    return w


def fetch_ohlc(now: datetime | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(weekly, daily) 반환. 레벨=주봉, 가격/등락=일봉(네이버 확정 종가, 실패 시 Yahoo)."""
    global DAILY_SOURCE
    now = now or datetime.now(KST)
    weekly = _download(LOOKBACK_PERIOD, "1wk", MACD_SLOW + MACD_SIG)
    try:
        daily = _fetch_daily_naver()
        DAILY_SOURCE = "Naver 증권 KOSPI (KRX 확정 종가)"
    except Exception as e:  # pragma: no cover - 네트워크
        print(f"[fetch] 네이버 일봉 실패 → Yahoo 폴백: {e}")
        daily = _download("2mo", "1d", 2)
        DAILY_SOURCE = "Yahoo Finance ^KS11 (폴백)"
    daily = drop_unconfirmed(_naive_dates(sanitize_ohlc(daily.dropna(subset=["Close"]))), now)
    if len(daily) < 2:
        raise RuntimeError("확정 일봉 부족")
    return patch_weekly_last(weekly, daily), daily


# ============================================================
# 지표 계산
# ============================================================
def wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    dn = (-delta).clip(lower=0.0)
    rs = up.ewm(alpha=1 / period, adjust=False).mean() / \
         dn.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(series: pd.Series, fast: int, slow: int, sig: int):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def supertrend(df: pd.DataFrame, period: int, mult: float):
    """표준 SuperTrend. 반환: (trend_line, direction) dir=+1 상승/-1 하락."""
    atr = wilder_atr(df, period)
    hl2 = (df["High"] + df["Low"]) / 2
    upper = (hl2 + mult * atr).to_numpy(dtype=float)
    lower = (hl2 - mult * atr).to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    n = len(df)
    fu, fl = upper.copy(), lower.copy()
    for i in range(1, n):
        fu[i] = min(upper[i], fu[i-1]) if (close[i-1] <= fu[i-1]) else upper[i]
        fl[i] = max(lower[i], fl[i-1]) if (close[i-1] >= fl[i-1]) else lower[i]
    st = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)
    for i in range(1, n):
        if close[i] > fu[i-1]:
            direction[i] = 1
        elif close[i] < fl[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
        st[i] = fl[i] if direction[i] == 1 else fu[i]
    return pd.Series(st, index=df.index), pd.Series(direction, index=df.index)


# ============================================================
# 매매기준점(레벨) 산출
# ============================================================
def fmt(x: float) -> str:
    return f"{x:,.0f}"


def round_levels(price: float) -> list:
    """가격 인근 심리적 라운드 넘버 후보."""
    if price <= 0:
        return []
    step = 500 if price >= 4000 else 100
    base = math.floor(price / step) * step
    return sorted({base - step, base, base + step, base + 2 * step})


def compute_reference_points(df: pd.DataFrame, daily: pd.DataFrame) -> dict:
    close = df["Close"]
    # 가격·당일 등락 = 일봉 (사용자 앱 헤더와 정합)
    price = float(daily["Close"].iloc[-1])
    prev = float(daily["Close"].iloc[-2])
    chg = price - prev
    chg_pct = chg / prev * 100 if prev else 0.0

    # --- 지표 최신값 ---
    st_line, st_dir = supertrend(df, ST_PERIOD, ST_MULT)
    st_now = float(st_line.iloc[-1])
    st_up = int(st_dir.iloc[-1]) == 1

    ma = float(close.rolling(MA_LEN).mean().iloc[-1])
    r = rsi(close, RSI_LEN)
    rsi_now = float(r.iloc[-1])
    rsi_sma = float(r.rolling(RSI_SMA).mean().iloc[-1])

    m_line, m_sig, m_hist = macd(close, MACD_FAST, MACD_SLOW, MACD_SIG)
    macd_now, sig_now, hist_now = float(m_line.iloc[-1]), float(m_sig.iloc[-1]), float(m_hist.iloc[-1])
    hist_prev = float(m_hist.iloc[-2])

    atr_now = float(wilder_atr(df, ST_PERIOD).iloc[-1])

    # --- 스윙 고/저 ---
    win_hi = df.tail(SWING_HI_WIN)
    swing_high = float(win_hi["High"].max())        # 인트라주 전고점
    peak_idx = win_hi["High"].idxmax()
    post_peak = df.loc[peak_idx:]
    swing_low = float(post_peak["Low"].min()) if len(post_peak) > 1 else float(df["Low"].tail(8).min())

    upleg = df.tail(UPLEG_WIN)
    base_low = float(upleg["Low"].min())            # 상승 파동 기저

    # --- 피보나치 ---
    # (A) 하락 되돌림: 최근 고점→저점 하락폭 → 반등 시 '저항'
    drop = swing_high - swing_low
    fib_res = {
        "38.2%": swing_low + 0.382 * drop,
        "50.0%": swing_low + 0.500 * drop,
        "61.8%": swing_low + 0.618 * drop,
    }
    # (B) 상승 되돌림: 기저→고점 상승폭 → 조정 시 '지지'
    rise = swing_high - base_low
    fib_sup = {
        "23.6%": swing_high - 0.236 * rise,
        "38.2%": swing_high - 0.382 * rise,
        "50.0%": swing_high - 0.500 * rise,
    }

    # --- 저항/지지 존 취합 (티어 가중 큐레이션) ---
    # tier 0=핵심 구조, 1=기술적, 2=심리(라운드). 파동기저는 심층타깃 전용(표시 제외).
    def tier_of(label: str) -> int:
        if label in ("전고점", "직전 저점", "슈퍼트렌드"):
            return 0
        if label == "라운드":
            return 2
        return 1

    disp = [
        ("전고점", swing_high), ("직전 저점", swing_low),
        ("슈퍼트렌드", st_now), ("20주선", ma),
    ]
    for k, v in fib_res.items():
        disp.append((f"피보 {k}(반등)", v))
    for k, v in fib_sup.items():
        disp.append((f"피보 {k}(조정)", v))
    for rl in round_levels(price):
        disp.append(("라운드", rl))

    res_all, sup_all, seen = [], [], set()
    for label, lv in disp:
        if not (lv > 0):            # NaN·음수 방어
            continue
        key = round(lv / 60) * 60          # ~60pt 근접 병합
        if key in seen:
            continue
        seen.add(key)
        rec = (label, lv, tier_of(label))
        if lv > price * 1.003:
            res_all.append(rec)
        elif lv < price * 0.997:
            sup_all.append(rec)

    def curate(items, downside: bool):
        if not items:
            return []
        items = sorted(items, key=lambda x: abs(x[1] - price))
        nearest, rest = items[0], items[1:]
        rest.sort(key=lambda x: (x[2], abs(x[1] - price)))  # 티어 → 거리
        out, keys = [], set()
        for label, lv, _t in [nearest] + rest:
            k = round(lv / 60) * 60
            if k in keys:
                continue
            keys.add(k)
            out.append((label, lv))
            if len(out) >= 4:
                break
        out.sort(key=lambda x: x[1], reverse=downside)
        return out

    resistances = curate(res_all, downside=False)
    supports = curate(sup_all, downside=True)

    # --- 추세 레짐 판정 ---
    macd_state = "정배열" if macd_now > sig_now else "역배열"
    hist_state = "확대" if hist_now > hist_prev else "축소"
    rsi_div = ""
    # 약세 다이버전스 간이 탐지: 주봉 신고가 & RSI 고점 하락
    wk_close = float(close.iloc[-1])
    last12 = df.tail(12)
    if wk_close >= float(last12["Close"].max()) * 0.999 and rsi_now < float(r.tail(26).max()) - 3:
        rsi_div = "약세 다이버전스"

    if st_up and macd_now > sig_now and price > ma:
        regime, regime_tag = "상승추세 유지", "🟢"
    elif (not st_up) or (price < ma and macd_now < sig_now):
        regime, regime_tag = "추세 훼손/전환 경계", "🔴"
    else:
        regime, regime_tag = "변동성 조정(중립)", "🟡"

    # 과확장도 (슈퍼트렌드 대비 이격)
    ext_pct = (price - st_now) / st_now * 100 if st_now else 0.0

    # --- 시나리오 트리거 (구조적 레벨 기반) ---
    key_res = fib_res["61.8%"]   # 낙폭 61.8% 회복 = 반등 성공 상단
    key_sup = swing_low          # 직전(급락) 저점 = 방어 1선
    trend_line = st_now          # 슈퍼트렌드 = 추세 최종선
    if trend_line < key_sup:
        # 상승 구조: 직전 저점 아래에 추세선이 받친다 → 추세선 테스트, 이탈 시 그 아래 심리선(1,000단위)
        deep_target = math.floor((trend_line - 1) / 1000) * 1000
        bear_action = (f"슈퍼트렌드 {fmt(trend_line)} 테스트, 이마저 이탈 시 {fmt(deep_target)} 심리선까지 개방"
                       " · 현물 코어 방어선 점검")
    else:
        # 추세선이 직전 저점보다 위(하락 추세면 가격 위의 저항) → 직전 저점 아래 심리선이 다음 목표 (2026-10-24 수정:
        # 기존 문구는 하락장에서 위쪽 추세선을 하방 '테스트' 레벨로 안내했다)
        deep_target = math.floor((key_sup - 1) / 1000) * 1000
        st_note = (f"슈퍼트렌드 {fmt(trend_line)}는 위쪽 저항(하락 추세)" if not st_up
                   else f"슈퍼트렌드 {fmt(trend_line)} 하회가 먼저 — 추세 하락 전환")
        bear_action = f"{fmt(deep_target)} 심리선까지 개방 · {st_note} · 현물 코어 방어선 점검"

    scenarios = {
        "bull": {
            "title": "강세 — 추세 재점화",
            "trigger": f"주봉 종가 {fmt(key_res)} 상향 돌파 + 거래량 동반",
            "action": f"낙폭 대부분 회복, 전고점 {fmt(swing_high)} 재도전 · 파라볼릭 재점화라 트레일링 스탑 타이트",
        },
        "base": {
            "title": "기본 — 변동성 레인지",
            "trigger": f"{fmt(key_sup)} ~ {fmt(key_res)} 등락",
            "action": "저항권({0}) 근접 시 레버리지 비중축소, 추격매수 금지 · lower-high 여부 관찰".format(fmt(key_res)),
        },
        "bear": {
            "title": "약세 — 추세 붕괴",
            "trigger": f"주봉 종가 {fmt(key_sup)} 이탈",
            "action": bear_action,
        },
    }

    core = [price, chg, st_now, ma, rsi_now, macd_now, sig_now, atr_now, swing_high, swing_low]
    if not all(math.isfinite(v) for v in core) or price <= 0 or swing_low <= 0:
        raise ValueError(f"지표 계산 결과에 비정상 값이 있습니다: {core}")

    return {
        "close_date": daily.index[-1].strftime("%Y-%m-%d"),
        "week_bar": df.index[-1].strftime("%Y-%m-%d"),
        "price": price, "chg": chg, "chg_pct": chg_pct,
        "st": st_now, "st_up": st_up, "ext_pct": ext_pct,
        "ma": ma,
        "rsi": rsi_now, "rsi_sma": rsi_sma, "rsi_div": rsi_div,
        "macd": macd_now, "macd_sig": sig_now, "macd_hist": hist_now,
        "macd_state": macd_state, "hist_state": hist_state,
        "atr": atr_now,
        "swing_high": swing_high, "swing_low": swing_low, "base_low": base_low,
        "key_res": key_res, "deep_target": deep_target,
        "resistances": [[label, lv] for label, lv in resistances],
        "supports": [[label, lv] for label, lv in supports],
        "regime": regime, "regime_tag": regime_tag,
        "scenarios": scenarios,
    }


# ============================================================
# 본문 (텔레그램 HTML) — 장식 이모지 없이, 색 표시는 레짐 판정 한 곳만
# ============================================================
def build_message(d: dict) -> str:
    arrow = "▲" if d["chg"] >= 0 else "▼"
    sign = "+" if d["chg"] >= 0 else ""
    e = d["ext_pct"]   # 슈퍼트렌드 대비 이격 — 음수(추세선 아래)도 크기를 표시 (2026-10-24: -31%가 '정상'으로 나오던 것 수정)
    ext_word = ("과확장" if e > 12 else "확장" if e > 6 else
                "하방 과이격" if e < -12 else "하방 이격" if e < -6 else "정상")
    cd = datetime.strptime(d["close_date"], "%Y-%m-%d")

    L = []
    L.append(f"<b>KOSPI 매매기준점</b>  {cd:%m/%d}({WEEKDAY_KO[cd.weekday()]}) 종가 기준")
    L.append("<i>가격 일봉 · 지표와 레벨 주봉</i>")
    L.append("")
    L.append(f"<b>{fmt(d['price'])}</b>  {arrow} {sign}{fmt(d['chg'])} ({sign}{d['chg_pct']:.2f}%)")
    L.append(f"{d['regime_tag']} <b>{d['regime']}</b>")
    L.append("")

    L.append("〈지표〉")
    L.append(f"· RSI(14) <b>{d['rsi']:.1f}</b> / SMA {d['rsi_sma']:.1f}"
             + (f"  ⚠️ {d['rsi_div']}" if d["rsi_div"] else ""))
    L.append(f"· MACD {d['macd_state']} ({fmt(d['macd'])} vs {fmt(d['macd_sig'])}) · 히스토 {d['hist_state']}")
    L.append(f"· 슈퍼트렌드 {fmt(d['st'])} ({'상승' if d['st_up'] else '하락'}) · 이격 {d['ext_pct']:+.1f}% ({ext_word})")
    L.append(f"· 20주선 {fmt(d['ma'])} · ATR {fmt(d['atr'])}")
    L.append("")

    L.append("〈저항 ▲〉")
    if d["resistances"]:
        for label, lv in d["resistances"][:4]:
            gap = (lv - d["price"]) / d["price"] * 100
            L.append(f"  {fmt(lv)}  <i>{label} (+{gap:.1f}%)</i>")
    else:
        L.append("  (전고점 상단 · 미상)")
    L.append("")

    L.append("〈지지 ▼〉")
    if d["supports"]:
        for label, lv in d["supports"][:4]:
            gap = (d["price"] - lv) / d["price"] * 100
            L.append(f"  {fmt(lv)}  <i>{label} (-{gap:.1f}%)</i>")
    else:
        L.append("  (파동 기저 하단)")
    L.append("")

    L.append("〈시나리오 &amp; 트리거〉")
    for key in ("bull", "base", "bear"):
        s = d["scenarios"][key]
        L.append(f"<b>{s['title']}</b>")
        L.append(f"   ▷ 조건: {s['trigger']}")
        L.append(f"   ▷ 대응: {s['action']}")
    L.append("")
    L.append("<i>※ 레버리지(KORU/SOXL)는 고변동 레인지에서 변동성 잠식 큼 — 저항권 비중축소 활용.</i>")
    L.append("<i>※ 참고용 분석. 최종 판단·리스크 관리는 본인.</i>")
    return "\n".join(L)


# ============================================================
# 저장 (멱등)
# ============================================================
def already_collected(path: str, now: datetime) -> bool:
    """오늘(KST) 날짜로 계산된 latest.json 이 이미 있으면 True."""
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    return str(prev.get("as_of_kst", ""))[:10] == now.strftime("%Y-%m-%d") and bool(prev.get("message"))


def build_payload(d: dict, now: datetime) -> dict:
    return {
        "as_of_kst": now.strftime("%Y-%m-%d %H:%M"),   # Brief Hub fresh_key
        "data_status": "OK",
        "source": f"일봉 {DAILY_SOURCE} · 주봉 이력 Yahoo Finance {TICKER}",
        **{k: v for k, v in d.items() if k != "regime_tag"},
        "message": build_message(d),
    }


def write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="오늘 데이터가 있어도 다시 계산")
    ap.add_argument("--dry", action="store_true", help="파일을 쓰지 않고 본문만 출력")
    ap.add_argument("--out", default=OUT_PATH)
    a = ap.parse_args(argv)

    now = datetime.now(KST)
    if not (a.force or a.dry) and already_collected(a.out, now):
        print(f"[skip] {now:%Y-%m-%d} 데이터가 이미 있습니다 — 재계산 생략 (--force 로 갱신)")
        return 0

    weekly, daily = fetch_ohlc(now)
    d = compute_reference_points(weekly, daily)
    payload = build_payload(d, now)

    if a.dry:
        print("=== DRY (파일 미기록) ===\n")
        print(payload["message"])
        return 0

    write_json(a.out, payload)
    print(f"[ok] {payload['close_date']} 종가 {fmt(payload['price'])} · {payload['regime']} → {os.path.relpath(a.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
