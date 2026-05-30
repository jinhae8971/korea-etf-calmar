#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
미국 상장 ETF 칼마비율(최근 1년 수익률 ÷ |MDD|) 상위 10 → 텔레그램 발송.
- 유니버스: 주요 유동 미국 ETF 큐레이션 세트(레버리지·인버스·채권형 제외)
- 심볼 해석: 네이버 autoComplete → reutersCode
- 일별 종가: 네이버 해외 차트 API (실데이터 직접 수집), USD 기준
- 윈도우: 실행 시점 기준 최근 6개월(롤링 6M)
- 주간 변동: snapshot_history_us_etf.json 에 누적, ~7일 전 스냅샷과 비교
"""
import os, sys, json, re, datetime, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0"
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "snapshot_history_us_etf.json")

UNIVERSE = """
SPY IVV VOO VTI QQQ QQQM DIA IWB IWM IWN IWO IWD IWF VUG VTV VO VB MDY RSP SCHX SCHB SCHG SCHD SPLG SPYG SPYV OEF IJH IJR ITOT MGK MGV
XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC VGT VHT VFH VDE VIS VCR VDC VPU VAW VNQ IYR SMH SOXX IGV SKYY FDN IBB XBI KRE KBE ITA XAR JETS TAN ICLN LIT XME XOP OIH XRT KIE IYT
ARKK ARKW ARKG ARKQ ARKF BOTZ ROBO AIQ IRBO FINX BUG CIBR ESPO HERO DRIV IDRV NLR URA URNM COPX GDX GDXJ SIL REMX ITB XHB PAVE GRID FAN QCLN PBW
VYM VIG DGRO HDV DVY NOBL SDY SPHD MOAT QUAL MTUM USMV SPLV VLUE COWZ FNDX RPV RPG
VEA VWO IEFA IEMG EFA EEM VXUS ACWI VEU VGK EWJ EWZ EWY EWT EWG EWU EWC EWA EWH INDA MCHI FXI KWEB ASHR EWW EZU FEZ ILF VPL AAXJ
GLD IAU SLV GLDM PPLT PALL DBC PDBC USO UNG DBA GSG CPER
IBIT FBTC ETHA
""".split()

# 안전망: 유니버스에 혹시 포함되어도 제외할 키워드(레버리지/인버스/채권/현금성)
EXCL = ["leverage", "leveraged", "inverse", "ultrapro", "ultrashort", "2x", "3x", "-1x",
        "bull", "bear", "bond", "treasury", "t-bill", "money market", "floating rate",
        "municipal", "aggregate bond", "fixed income"]


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
        with urllib.request.urlopen(urllib.request.Request(base, data=data), timeout=20) as r:
            r.read()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def resolve(sym):
    try:
        url = f"https://m.stock.naver.com/front-api/search/autoComplete?query={urllib.parse.quote(sym)}&target=stock"
        items = json.loads(fetch(url))["result"]["items"]
        for it in items:
            if it.get("code") == sym and it.get("nationCode") == "USA":
                return it.get("reutersCode"), it.get("name") or sym
        for it in items:
            if it.get("nationCode") == "USA":
                return it.get("reutersCode"), it.get("name") or sym
    except Exception:
        return None, None
    return None, None


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

    syms = sorted(set(UNIVERSE))

    resolved = {}
    names = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for sym, (rc, nm) in zip(syms, ex.map(resolve, syms)):
            if rc and not any(k in (nm or "").lower() for k in EXCL):
                resolved[sym] = rc
                names[sym] = nm

    def fetch_one(sym):
        return sym, get_prices(resolved[sym], start, today_str)
    results = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for sym, prices in ex.map(fetch_one, list(resolved.keys())):
            results[sym] = prices

    def pdate(s):
        return datetime.datetime.strptime(s, "%Y%m%d").date()

    rows = []
    for sym, prices in results.items():
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
                "code": sym, "name": names.get(sym, sym),
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
    prev = {x["name"] for x in ref["top10"]}
    cur = {x["name"] for x in top10}
    entered = [n for n in (x["name"] for x in top10) if n not in prev]
    exited = [n for n in (x["name"] for x in ref["top10"]) if n not in cur]
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
    lines = ["<b>🇺🇸 미국 ETF 칼마비율 TOP 10</b>",
             f"<i>최근 1년 기준: {fmt_d(base_date)} → {fmt_d(last_date)} (USD)</i>", ""]
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(top10, 1):
        tag = medal.get(i, f"{i}.")
        lines.append(
            f"{tag} <b>{r['code']}</b> {r['name']}\n"
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
