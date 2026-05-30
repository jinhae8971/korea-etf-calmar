#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S&P500 구성종목 칼마비율(최근 1년 수익률 ÷ |MDD|) 상위 10 → 텔레그램 발송.
- 구성종목: datahub S&P500 constituents CSV
- 심볼 해석: 네이버 autoComplete → reutersCode (NYSE/NASDAQ/.K 등 자동)
- 일별 종가: 네이버 해외 차트 API (실데이터 직접 수집), USD 기준
- 윈도우: 실행 시점 기준 최근 6개월(롤링 6M)
- 주간 변동: snapshot_history_sp500.json 에 누적, ~7일 전 스냅샷과 비교
"""
import os, sys, json, csv, io, datetime, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0"
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "snapshot_history_sp500.json")
SP500_CSV = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def load_config() -> dict:
    cfg = {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if not cfg.get(k):
                    cfg[k] = v
    return cfg


def send_telegram(messages, token, chat_id):
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for msg in messages:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": msg,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(base, data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def get_sp500_list():
    txt = fetch(SP500_CSV).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(txt)))
    return [(r["Symbol"].strip(), r["Security"].strip()) for r in rows if r.get("Symbol")]


def resolve_symbol(sym):
    try:
        url = f"https://m.stock.naver.com/front-api/search/autoComplete?query={urllib.parse.quote(sym)}&target=stock"
        items = json.loads(fetch(url))["result"]["items"]
        for it in items:
            if it.get("code") == sym and it.get("nationCode") == "USA" and it.get("category") == "stock":
                return it.get("reutersCode")
        for it in items:
            if it.get("nationCode") == "USA" and it.get("category") == "stock":
                return it.get("reutersCode")
    except Exception:
        return None
    return None


def get_prices(reuters, start, end):
    url = (f"https://api.stock.naver.com/chart/foreign/item/{reuters}/day"
           f"?startDateTime={start}&endDateTime={end}")
    try:
        arr = json.loads(fetch(url))
        out = []
        for r in arr:
            d = str(r.get("localDate", ""))
            c = r.get("closePrice")
            if len(d) == 8 and c is not None:
                out.append((d, float(c)))
        return out
    except Exception:
        return None


def compute_top10():
    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")
    window_start = today - datetime.timedelta(days=182)  # 최근 6개월(롤링 6M) 윈도우
    start = (window_start - datetime.timedelta(days=15)).strftime("%Y%m%d")

    stocks = get_sp500_list()
    name_map = dict(stocks)
    syms = [s for s, _ in stocks]

    reuters_map = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for sym, rc in zip(syms, ex.map(resolve_symbol, syms)):
            if rc:
                reuters_map[sym] = rc

    def fetch_one(sym):
        return sym, get_prices(reuters_map[sym], start, today_str)
    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for sym, prices in ex.map(fetch_one, list(reuters_map.keys())):
            results[sym] = prices

    def pdate(s):
        return datetime.datetime.strptime(s, "%Y%m%d").date()

    rows = []
    for sym, prices in results.items():
        name = name_map.get(sym, sym)
        if not prices or len(prices) < 2:
            continue
        prices = sorted(prices, key=lambda x: x[0])
        if pdate(prices[0][0]) > window_start + datetime.timedelta(days=14):
            continue
        window = [p for p in prices if pdate(p[0]) >= window_start]
        before = [p for p in prices if pdate(p[0]) < window_start]
        if not window:
            continue
        if before:
            base, base_date = before[-1][1], before[-1][0]
        else:
            base, base_date = window[0][1], window[0][0]
        if base <= 0:
            continue
        last, last_date = window[-1][1], window[-1][0]
        ret = last / base - 1
        _span_days = (pdate(last_date) - pdate(base_date)).days
        ann_ret = (1 + ret) ** (365.0 / _span_days) - 1 if _span_days > 0 else ret
        curve = [base] + [p[1] for p in window]
        peak, mdd = curve[0], 0.0
        for v in curve:
            if v > peak:
                peak = v
            dd = v / peak - 1
            if dd < mdd:
                mdd = dd
        calmar = None if abs(mdd) < 1e-9 else ann_ret / abs(mdd)
        if ret > 0 and calmar is not None:
            rows.append({
                "code": sym, "name": name,
                "ret": ret, "mdd": mdd, "calmar": calmar,
                "base_date": base_date, "last_date": last_date,
            })
    rows.sort(key=lambda r: r["calmar"], reverse=True)
    return rows[:10], today_str


def load_history():
    if os.path.exists(HIST_PATH):
        try:
            return json.load(open(HIST_PATH, "r", encoding="utf-8"))
        except Exception:
            return []
    return []


def weekly_diff(history, today_str, top10):
    if not history:
        return None
    today = datetime.datetime.strptime(today_str, "%Y%m%d").date()
    target = today - datetime.timedelta(days=7)
    past = [h for h in history if h["date"] != today_str]
    if not past:
        return None
    def keyf(h):
        d = datetime.datetime.strptime(h["date"], "%Y%m%d").date()
        return abs((d - target).days)
    ref = min(past, key=keyf)
    prev_names = {x["name"] for x in ref["top10"]}
    cur_names = {x["name"] for x in top10}
    entered = [n for n in (x["name"] for x in top10) if n not in prev_names]
    exited = [n for n in (x["name"] for x in ref["top10"]) if n not in cur_names]
    return {"ref_date": ref["date"], "entered": entered, "exited": exited}


def save_history(history, today_str, top10):
    history = [h for h in history if h["date"] != today_str]
    history.append({
        "date": today_str,
        "top10": [{"code": r["code"], "name": r["name"],
                   "calmar": round(r["calmar"], 2)} for r in top10],
    })
    history = sorted(history, key=lambda h: h["date"])[-40:]
    json.dump(history, open(HIST_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def build_message(top10, today_str, diff):
    base_date = top10[0]["base_date"]
    last_date = top10[0]["last_date"]
    def fmt_d(s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    lines = ["<b>🇺🇸 S&amp;P500 칼마비율 TOP 10</b>",
             f"<i>최근 1년 기준: {fmt_d(base_date)} → {fmt_d(last_date)} (USD)</i>", ""]
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(top10, 1):
        tag = medal.get(i, f"{i}.")
        lines.append(
            f"{tag} <b>{r['name']}</b> ({r['code']})\n"
            f"    칼마 <b>{r['calmar']:.2f}</b> · 6M {r['ret']*100:+.1f}% · MDD {r['mdd']*100:.1f}%")
    lines.append("")
    if diff is None:
        lines.append("ℹ️ 지난주 비교 기준 스냅샷이 아직 없어요. 다음 주부터 변동을 알려드릴게요.")
    else:
        parts = []
        if diff["entered"]:
            parts.append("🆕 신규 진입: " + ", ".join(diff["entered"]))
        if diff["exited"]:
            parts.append("🔻 이탈: " + ", ".join(diff["exited"]))
        if not parts:
            parts.append(f"지난주({fmt_d(diff['ref_date'])}) 대비 TOP10 구성 변동 없음")
        lines.append("<b>지난주 대비</b>")
        lines.extend(parts)
    return "\n".join(lines)


def main():
    cfg = load_config()
    top10, today_str = compute_top10()
    if not top10:
        raise RuntimeError("산출된 종목이 없습니다. 데이터 수집 확인 필요")
    history = load_history()
    diff = weekly_diff(history, today_str, top10)
    msg = build_message(top10, today_str, diff)
    print(msg)
    if "--dry-run" not in sys.argv:
        if not (cfg["telegram_token"] and cfg["telegram_chat_id"]):
            raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 가 설정되지 않았습니다.")
        send_telegram([msg], cfg["telegram_token"], cfg["telegram_chat_id"])
        print("[telegram] sent.")
    save_history(history, today_str, top10)


if __name__ == "__main__":
    main()
