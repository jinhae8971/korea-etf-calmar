#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
국내 상장 ETF 칼마비율(YTD ÷ |MDD|) 상위 10 산출 → 텔레그램 발송.
- 데이터: 네이버 금융 일별 종가 (실데이터 직접 수집)
- 주간 변동: snapshot_history.json 에 누적, ~7일 전 스냅샷과 비교
GitHub Actions Secrets → 환경변수 → config.json 폴백 순으로 인증.
"""
import os, sys, json, ast, re, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0"
HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshot_history.json")


# ---------------------------------------------------------------- config
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
    import urllib.parse
    base = f"https://api.telegram.org/bot{token}/sendMessage"
    for msg in messages:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": msg,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(base, data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()


# ---------------------------------------------------------------- fetch
def fetch(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://finance.naver.com/"})
    return urllib.request.urlopen(req, timeout=30).read()


def get_etf_list():
    raw = fetch("https://finance.naver.com/api/sise/etfItemList.nhn")
    data = json.loads(raw.decode("cp949"))
    return [(it["itemcode"], it["itemname"]) for it in data["result"]["etfItemList"]]


def get_prices(code, start, end):
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}"
           f"&requestType=1&startTime={start}&endTime={end}&timeframe=day")
    try:
        arr = ast.literal_eval(fetch(url).decode("utf-8").strip())
        out = []
        for r in arr[1:]:
            if len(r) >= 5:
                out.append((str(r[0]), float(r[4])))
        return code, out
    except Exception:
        return code, None


# ---------------------------------------------------------------- filters
EXCL_KW = ["레버리지", "인버스", "2X", "2x", "곱버스", "머니마켓", "머니", "CD금리", "KOFR",
           "금리액티브", "초단기", "단기채", "단기통안", "통안채", "단기자금", "단기국공채",
           "SOFR", "양도성예금", "단기우량"]
BOND_KW = ["전단채", "특수은행채", "금융채", "회사채", "국공채", "종합채권", "은행채", "산금채",
           "크레딧", "신종자본", "커버드본드", "우량채", "정책금융", "특수채"]
MATURITY_RE = re.compile(r"2[5-9]-\d\d")


def excluded(name):
    if any(k in name for k in EXCL_KW):
        return True
    if MATURITY_RE.search(name):
        return True
    if ("채" in name and "액티브" in name):
        return True
    if any(k in name for k in BOND_KW):
        return True
    return False


FAMILY = {
    "KODEX": "삼성자산운용", "TIGER": "미래에셋자산운용", "RISE": "KB자산운용",
    "ACE": "한국투자신탁운용", "HANARO": "NH아문디자산운용", "SOL": "신한자산운용",
    "PLUS": "한화자산운용", "KOSEF": "키움투자자산운용", "히어로즈": "키움투자자산운용",
    "TIMEFOLIO": "타임폴리오자산운용", "WON": "우리자산운용", "BNK": "BNK자산운용",
    "IBK": "IBK자산운용", "FOCUS": "브이아이자산운용", "마이티": "DB자산운용",
    "TREX": "유리자산운용", "파워": "교보악사자산운용", "마이다스": "마이다스에셋",
    "에셋플러스": "에셋플러스자산운용", "VITA": "한국투자신탁운용", "KIWOOM": "키움투자자산운용",
}


def family_of(name):
    for k, v in FAMILY.items():
        if name.upper().startswith(k.upper()) or name.startswith(k):
            return v
    return "-"


# ---------------------------------------------------------------- compute
def compute_top10():
    """실행 시점 기준 최근 6개월(롤링 6M) 윈도우로 수익률·MDD·칼마 산출."""
    today = datetime.date.today()
    today_str = today.strftime("%Y%m%d")
    window_start = today - datetime.timedelta(days=182)  # 최근 6개월(롤링 6M) 윈도우
    # 윈도우 시작 직전 종가(base)를 확보하기 위해 15일 여유를 두고 수집
    start = (window_start - datetime.timedelta(days=15)).strftime("%Y%m%d")

    etfs = get_etf_list()
    name_map = dict(etfs)
    results = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for code, prices in ex.map(lambda c: get_prices(c[0], start, today_str), etfs):
            results[code] = prices

    def pdate(s):
        return datetime.datetime.strptime(s, "%Y%m%d").date()

    rows = []
    for code, prices in results.items():
        name = name_map.get(code, code)
        if excluded(name) or not prices or len(prices) < 2:
            continue
        prices = sorted(prices, key=lambda x: x[0])
        # 6개월 미만 상장(데이터 부족) 제외: 가장 이른 종가가 윈도우 시작보다 한참 뒤면 제외
        if pdate(prices[0][0]) > window_start + datetime.timedelta(days=14):
            continue
        window = [p for p in prices if pdate(p[0]) >= window_start]
        before = [p for p in prices if pdate(p[0]) < window_start]
        if not window:
            continue
        # base = 6개월 전 직전 거래일 종가(없으면 윈도우 첫 종가)
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
                "code": code, "name": name, "family": family_of(name),
                "ret": ret, "mdd": mdd, "calmar": calmar,
                "base_date": base_date, "last_date": last_date,
            })
    rows.sort(key=lambda r: r["calmar"], reverse=True)
    return rows[:10], today_str


# ---------------------------------------------------------------- snapshot / weekly diff
def load_history():
    if os.path.exists(HIST_PATH):
        try:
            return json.load(open(HIST_PATH, "r", encoding="utf-8"))
        except Exception:
            return []
    return []


def weekly_diff(history, today_str, top10):
    """~7일 전(없으면 가장 가까운 과거) 스냅샷과 종목 진입/이탈 비교."""
    if not history:
        return None
    today = datetime.datetime.strptime(today_str, "%Y%m%d").date()
    target = today - datetime.timedelta(days=7)
    past = [h for h in history if h["date"] != today_str]
    if not past:
        return None
    # target 날짜에 가장 가까운 과거 스냅샷 선택
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
    history = sorted(history, key=lambda h: h["date"])[-40:]  # 최근 40회만 보관
    json.dump(history, open(HIST_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- message
def build_message(top10, today_str, diff):
    base_date = top10[0]["base_date"]
    last_date = top10[0]["last_date"]
    def fmt_d(s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    lines = ["<b>📊 국내 ETF 칼마비율 TOP 10</b>",
             f"<i>최근 1년 기준: {fmt_d(base_date)} → {fmt_d(last_date)}</i>", ""]
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(top10, 1):
        tag = medal.get(i, f"{i}.")
        lines.append(
            f"{tag} <b>{r['name']}</b> ({r['family']})\n"
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


# ---------------------------------------------------------------- main
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
