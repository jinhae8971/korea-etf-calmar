# -*- coding: utf-8 -*-
"""US Earnings Desk — builds the dashboard's data files (JSON {rows, meta}).

python3 build_desk.py            -> desk/*.json
Sources
  company figures : NVIDIA 8-K press releases (Q4 FY26, Q1/Q2/Q3 FY27) and Q3 FY27 CFO commentary (sec.gov)
  consensus       : LSEG via Reuters (revenue, Q4 guide, gross margin), EPS consensus 2.47 (FactSet/LSEG reports)
  EPS history     : Nasdaq.com API (Zacks) /api/company/<sym>/earnings-surprise, /api/analyst/<sym>/earnings-forecast
  calendar        : Nasdaq.com API /api/calendar/earnings?date=YYYY-MM-DD (past rows carry EPS and % surprise)
  prices          : Yahoo Finance chart API v8 (daily closes, extended-hours 5-minute bars)
Amounts are in 억 달러 (1억 달러 = $100M) so the page shows the stored number as is.
"""
import datetime as dt
import json
import os
import time
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "desk")
os.makedirs(OUT, exist_ok=True)
CODE = open(os.path.abspath(__file__), encoding="utf-8").read()
BUILT = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
NAS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*",
       "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
TODAY_ET = dt.date(2026, 11, 18)          # US session date of the build (ET)
PAST_FROM, FUT_TO = dt.date(2026, 10, 19), dt.date(2026, 12, 4)
REACT_MIN_CAP, CAL_MIN_CAP = 200e9, 50e9


def r1(x, nd=1):
    return None if x is None else float(Decimal(str(x)).quantize(Decimal(1).scaleb(-nd), rounding=ROUND_HALF_UP))


def eok(b):  # $B -> 억 달러, half-up integer
    return int(Decimal(str(b * 10)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def getj(url, headers=None):
    for i in range(3):
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"}), timeout=30))
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(2 + i * 3)


def yahoo_daily(sym, rng="6mo"):
    r = getj(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d")["chart"]["result"][0]
    out = {}
    for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]):
        if c is not None:
            out[dt.datetime.fromtimestamp(t + r["meta"].get("gmtoffset", -18000), dt.timezone.utc).date()] = c
    return out


def yahoo_ext(sym):
    r = getj(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1d&interval=5m&includePrePost=true")["chart"]["result"][0]
    pts = [(t, c) for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
    return r["meta"]["regularMarketPrice"], pts[-1][1], dt.datetime.fromtimestamp(pts[-1][0], dt.timezone.utc)


def money(s):
    s = (s or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def write(name, rows, meta):
    meta = meta + [{"label": "Built", "value": BUILT}, {"label": "build_desk.py", "value": CODE, "type": "code", "lang": "python"}]
    json.dump({"rows": rows, "meta": meta}, open(os.path.join(OUT, name), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(name, len(rows))


WD = "월화수목금토일"


def kst_label(d, time_kind):
    """Earnings time in Korea: pre-market (ET 7-9am) -> same day 21~23시 KST, after-hours (ET 4-5pm) -> next day 06~07시 KST."""
    if time_kind == "장전":
        return f"{d.month}/{d.day}({WD[d.weekday()]}) 밤"
    if time_kind == "장후":
        n = d + dt.timedelta(days=1)
        return f"{n.month}/{n.day}({WD[n.weekday()]}) 새벽"
    return f"{d.month}/{d.day}({WD[d.weekday()]}) 중"


# --------------------------------------------------------------------------------------------- calendar
def calendar_rows():
    past, fut = [], []
    d = PAST_FROM
    while d <= FUT_TO:
        if d.weekday() < 5:
            rows = (getj(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", NAS).get("data") or {}).get("rows") or []
            for x in rows:
                cap = money(x.get("marketCap"))
                if not cap:
                    continue
                x = dict(x, _date=d, _cap=cap)
                (past if d < TODAY_ET else fut).append(x)
            time.sleep(0.4)
        d += dt.timedelta(days=1)
    return past, fut


def dedupe(rows):
    seen, out = set(), []
    for x in sorted(rows, key=lambda r: (r["_date"], -r["_cap"])):
        key = (x["name"].strip().lower(), x["_date"])
        if key in seen or x["symbol"] in ("GOOG",):
            continue
        seen.add(key); out.append(x)
    return out


TIME = {"time-pre-market": "장전", "time-after-hours": "장후"}


def build_calendar(fut):
    rows = []
    for x in dedupe([r for r in fut if r["_cap"] >= CAL_MIN_CAP]):
        tk = TIME.get(x.get("time"), "미정")
        rows.append({"date": x["_date"].isoformat(), "kst": kst_label(x["_date"], tk), "time": tk, "symbol": x["symbol"],
                     "name": x["name"].strip(), "mcap_eok": eok(x["_cap"] / 1e9), "eps_forecast": money(x.get("epsForecast")),
                     "n_ests": int(x["noOfEsts"]) if (x.get("noOfEsts") or "").isdigit() else None,
                     "last_year_eps": money(x.get("lastYearEPS"))})
    write("calendar.json", rows, [
        {"label": "Source", "value": "Nasdaq.com earnings calendar API, " + f"{TODAY_ET.isoformat()} ~ {FUT_TO.isoformat()} (ET)"},
        {"label": "Filter", "value": f"market cap >= ${CAL_MIN_CAP/1e9:.0f}B; duplicate share classes removed"},
        {"label": "Korean time", "value": "장전 = ET 오전 발표 → 한국 같은 날 밤, 장후 = ET 오후 4시 이후 → 한국 다음 날 새벽"}])


def build_reactions(past, extra):
    rows = []
    cache = {}
    for x in dedupe([r for r in past if r["_cap"] >= REACT_MIN_CAP]):
        sym, d = x["symbol"], x["_date"]
        ysym = sym.replace(".", "-")
        try:
            px = cache.get(ysym) or yahoo_daily(ysym, "6mo"); cache[ysym] = px
        except Exception:  # noqa: BLE001
            px = {}
        days = sorted(px)
        before = [k for k in days if k < d]; after = [k for k in days if k > d]
        move = None
        if before and after:
            move = r1((px[after[0]] / px[before[-1]] - 1) * 100)
        rows.append({"date": d.isoformat(), "symbol": sym, "name": x["name"].strip(), "mcap_eok": eok(x["_cap"] / 1e9),
                     "eps": money(x.get("eps")), "eps_forecast": money(x.get("epsForecast")), "surprise_pct": r1(money(x.get("surprise"))),
                     "move_2d_pct": move, "move_note": "" if move is not None else "다음 거래일 반영 전"})
    for e in extra:  # company-reported figures for a same-day reporter the calendar has not filled yet (e.g. NVDA)
        hit = next((r for r in rows if r["symbol"] == e["symbol"] and r["date"] == e["date"]), None)
        if hit is None:
            rows.append(e)
        else:
            for k, v in e.items():
                if v is not None and (hit.get(k) in (None, "") or k == "move_note"):
                    hit[k] = v
    rows.sort(key=lambda r: (r["date"], r["mcap_eok"]), reverse=True)
    write("reactions.json", rows, [
        {"label": "Source", "value": f"Nasdaq.com earnings calendar API (EPS, consensus, % surprise; Zacks basis), {PAST_FROM.isoformat()} ~ {TODAY_ET.isoformat()}"},
        {"label": "Price move", "value": "Yahoo Finance 일봉: 발표일 전 거래일 종가 → 발표일 다음 거래일 종가 (장전·장후 발표 모두 반영되는 2거래일 구간)"},
        {"label": "Filter", "value": f"market cap >= ${REACT_MIN_CAP/1e9:.0f}B"}])


# --------------------------------------------------------------------------------------------- NVIDIA (first company)
NVDA_Q = [  # fq, period end (YYYY-MM), revenue $B, data center $B, non-GAAP GM %, non-GAAP EPS, guidance midpoint given a quarter earlier ($B)
    ("4Q FY25", "2025-01", 39.331, None, 73.5, 0.89, None),
    ("1Q FY26", "2025-04", 44.062, None, 60.8, 0.78, None),
    ("2Q FY26", "2025-07", 46.743, None, 72.5, 1.01, None),
    ("3Q FY26", "2025-10", 57.006, None, 73.5, 1.27, None),
    ("4Q FY26", "2026-01", 68.127, 62.3, 75.1, 1.59, None),
    ("1Q FY27", "2026-04", 81.615, 75.2, 75.0, 1.87, 78.0),
    ("2Q FY27", "2026-07", 96.221, 89.0, 75.0, 2.22, 91.0),
    ("3Q FY27", "2026-10", 115.8, 107.6, 74.9, 2.68, 108.0),
]
NVDA_GUIDE = ("4Q FY27", "2027-01", 130.0, None, 71.5, None, None)  # guidance midpoint; GM 71-72% midpoint


def nvda():
    T = "NVDA"
    q_rows = []
    allq = NVDA_Q + [NVDA_GUIDE]
    for i, (fq, pe, rev, dc, gm, eps, gmid) in enumerate(allq):
        yoy = None
        if i >= 4:
            yoy = r1((rev / allq[i - 4][2] - 1) * 100, 0)
        q_rows.append({"ticker": T, "fq": fq, "period_end": pe, "kind": "guide" if fq == NVDA_GUIDE[0] else "actual",
                       "revenue_eok": eok(rev), "yoy_pct": yoy, "dc_eok": eok(dc) if dc else None,
                       "gm_pct": gm, "eps": eps, "guide_mid_eok": eok(gmid) if gmid else None})
    write("quarters.json", q_rows, [
        {"label": "NVIDIA Q3 FY27 8-K", "value": "https://www.sec.gov/Archives/edgar/data/0001045810/000104581026000088/q3fy27pr.htm"},
        {"label": "NVIDIA Q2 FY27 8-K", "value": "https://www.sec.gov/Archives/edgar/data/0001045810/000104581026000073/q2fy27pr.htm"},
        {"label": "NVIDIA Q1 FY27 8-K", "value": "https://www.sec.gov/Archives/edgar/data/0001045810/000104581026000051/q1fy27pr.htm"},
        {"label": "NVIDIA Q4 FY26 8-K", "value": "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000019/q4fy26pr.htm"},
        {"label": "Notes", "value": "총이익률·EPS는 조정(non-GAAP), 최신 보도자료의 값. 4Q FY27 행은 회사 가이던스 중간값(매출 1,300억 ±3%, 총이익률 71~72%). guide_mid_eok는 직전 분기에 회사가 낸 그 분기 매출 가이던스 중간값. 데이터센터는 회사가 밝힌 분기만."}])

    sc = [  # n, key, label, actual, estimate, unit, diff kind, note
        (1, "revenue", "매출", eok(115.8), eok(109.11), "억 달러", "pct", "작년 같은 분기보다 +103%"),
        (2, "eps", "주당순이익(조정)", 2.68, 2.47, "달러", "pct", "작년 같은 분기 1.27달러"),
        (3, "guide", "다음 분기 매출 가이던스", eok(130.0), eok(125.25), "억 달러", "pct", "±3% · 14주 분기 · 중국 데이터센터 매출 0 가정"),
        (4, "gm", "총이익률(조정)", 74.9, 74.1, "%", "pp", "다음 분기 전망 71~72% (예상 71.7%)"),
        (5, "dc", "데이터센터 매출", eok(107.6), None, "억 달러", "yoy", "전년 대비 +110% · 매출의 93%"),
    ]
    sc_rows = []
    for n, k, lab, a, e, unit, kind, note in sc:
        diff = None
        if e is not None:
            diff = r1((a / e - 1) * 100) if kind == "pct" else r1(a - e)
        sc_rows.append({"ticker": T, "n": n, "metric": k, "label": lab, "actual": a, "estimate": e, "unit": unit,
                        "diff": diff, "diff_kind": kind, "note": note})
    write("scorecard.json", sc_rows, [
        {"label": "Actuals", "value": "NVIDIA Q3 FY27 8-K press release and CFO commentary"},
        {"label": "Revenue / guidance / GM consensus", "value": "LSEG via Reuters: revenue $109.11B, Q4 revenue $125.25B, adjusted GM 74.1%, Q4 GM 71.7% (IBTimes, AOL/Reuters 2026-11-17)"},
        {"label": "EPS consensus", "value": "$2.47 (Kiplinger live blog, Constellation Research; FactSet $2.48, Zacks $2.49)"},
        {"label": "diff", "value": "pct = actual / estimate - 1 (%), pp = actual - estimate (%p)"}])

    close, ah, ah_at = yahoo_ext(T)
    d2 = yahoo_daily(T, "2y")
    days = sorted(d2)
    hi_day = max(days, key=lambda k: d2[k])
    prev_year = [k for k in days if k < dt.date(2026, 1, 1)][-1]
    rep = [{"ticker": T, "name": "엔비디아", "name_en": "NVIDIA", "quarter": "3Q FY27", "period": "2026년 8~10월",
            "report_date": "2026-11-17", "report_time": "장후", "report_kst": "11/18(수) 새벽",
            "close": r1(close, 2), "ext_price": r1(ah, 2), "ext_pct": r1((ah / close - 1) * 100),
            "ext_at": ah_at.strftime("%Y-%m-%dT%H:%MZ"), "high": r1(d2[hi_day], 2), "high_date": hi_day.isoformat(),
            "from_high_pct": r1((close / d2[hi_day] - 1) * 100), "ytd_pct": r1((close / d2[prev_year] - 1) * 100),
            "next_report": "2027년 2월 하순(예상)"}]
    write("report.json", rep, [
        {"label": "Prices", "value": "Yahoo Finance: regular close 2026-11-17, latest extended-hours 5-minute bar, 2-year daily closes"},
        {"label": "Timing", "value": "11/17 ET 장 마감 후(16:20) 발표 = 한국 11/18 06:20"}])

    px = yahoo_daily(T, "6mo")
    write("price.json", [{"ticker": T, "date": k.isoformat(), "close": r1(v, 2)} for k, v in sorted(px.items())],
          [{"label": "Source", "value": "Yahoo Finance chart API v8, daily closes, 6 months"}])

    es = getj(f"https://api.nasdaq.com/api/company/{T}/earnings-surprise", NAS)["data"]["earningsSurpriseTable"]["rows"]
    fc = getj(f"https://api.nasdaq.com/api/analyst/{T}/earnings-forecast", NAS)["data"]["quarterlyForecast"]["rows"]
    eps_rows = []
    for x in reversed(es):
        eps_rows.append({"ticker": T, "fq_end": x["fiscalQtrEnd"], "date_reported": dt.datetime.strptime(x["dateReported"], "%m/%d/%Y").date().isoformat(),
                         "eps": float(x["eps"]), "consensus": float(x["consensusForecast"]), "surprise_pct": r1(float(x["percentageSurprise"])),
                         "basis": "Zacks"})
    cur = next((x for x in fc if x["fiscalEnd"] == "Oct 2026"), None)
    if cur and not any(r["fq_end"] == "Oct 2026" for r in eps_rows):
        eps_rows.append({"ticker": T, "fq_end": "Oct 2026", "date_reported": "2026-11-17", "eps": 2.68, "consensus": cur["consensusEPSForecast"],
                         "surprise_pct": r1((2.68 / cur["consensusEPSForecast"] - 1) * 100), "basis": "회사 발표 조정 EPS vs Zacks 컨센서스"})
    write("eps.json", eps_rows, [
        {"label": "Source", "value": "Nasdaq.com API (Zacks): /api/company/NVDA/earnings-surprise; latest quarter = NVIDIA non-GAAP EPS vs Zacks consensus from /api/analyst/NVDA/earnings-forecast"}])
    extra = [{"date": "2026-11-17", "symbol": T, "name": "NVIDIA Corporation", "mcap_eok": None, "eps": 2.68,
              "eps_forecast": cur["consensusEPSForecast"] if cur else None,
              "surprise_pct": r1((2.68 / cur["consensusEPSForecast"] - 1) * 100) if cur else None,
              "move_2d_pct": None, "move_note": f"시간외 {r1((ah / close - 1) * 100):+.1f}%"}]
    return extra


NOTES_NVDA = [
    ("headline", "CEO 사임 이후 첫 실적에서 매출과 다음 분기 전망이 모두 예상을 넘었어요. 'AI 수요가 꺾였다'는 걱정을 숫자로 반박한 분기예요.", None, None),
    ("easy", "만드는 대로 팔려요. 회사는 수요가 공급보다 많다고 했고, 부품 확보 약정을 2,790억 달러에서 3,900억 달러로 늘렸어요.", None, None),
    ("easy", "신제품 베라 루빈이 회사 역사상 가장 빠른 속도로 출하되고 있어요. 내년(FY28) 매출 성장 전망도 70%에서 75%로 올렸어요.", None, None),
    ("easy", "번 돈은 주주에게 돌려줘요. 3분기에만 281억 달러를 환원했고, 자사주 매입 한도를 1,500억 달러 늘렸어요.", None, None),
    ("risk", "4분기 총이익률이 74.9%에서 71~72%로 내려가요. 시장 예상(71.7%) 수준이라 충격은 크지 않아요.", None, None),
    ("risk", "CEO 공백: 이사회는 젠슨 황의 복귀는 없다고 밝혔고, 외부 헤드헌팅사와 후임을 찾고 있어요.", None, None),
    ("risk", "중국: 4분기 전망은 중국 데이터센터 매출 0을 가정했어요(3분기 중국향 호퍼 24억 달러). 12/3 상원 증언 요청도 남아 있어요.", None, None),
    ("next", "새 CEO 선임 발표", None, None),
    ("next", "12/3 젠슨 황, 12/10 러트닉 상무장관 상원 증언 요청", None, None),
    ("next", "다음 실적(2027년 2월 하순 예상): 총이익률 71~72% 달성 여부", None, None),
    ("event", "고점", "2026-10-06", None),
    ("event", "AI 데이터센터 둔화 공포", "2026-11-05", None),
    ("event", "CEO 사임 발표", "2026-11-12", None),
    ("event", "실적 발표", "2026-11-17", None),
    ("title", "엔비디아 3분기 실적 총정리: 매출 1,158억 달러, 예상보다 6% 더 벌었다", None, None),
    ("title", "CEO 공백에도 '역대급' 실적… 엔비디아가 숫자로 답한 것과 남은 숙제", None, None),
    ("title", "고점 대비 35% 빠진 엔비디아, 실적 발표 후 시간외에서 오른 이유", None, None),
    ("blog", "엔비디아가 11월 17일(미국 시간) 장 마감 후 2027 회계연도 3분기(2026년 8~10월) 실적을 발표했어요. 젠슨 황 CEO가 물러난 뒤 처음 나온 실적이라 관심이 컸는데요, 결론부터 말하면 매출과 다음 분기 전망 모두 시장 예상을 넘었습니다.", None, None),
    ("blog", "## 숫자로 보는 3분기", None, None),
    ("blog", "매출은 1,158억 달러로 1년 전(570억 달러)의 두 배가 넘었고, 시장 예상(약 1,091억 달러)보다 6.1% 많았어요. 조정 주당순이익(EPS)도 2.68달러로 예상(2.47달러)을 8.5% 웃돌았습니다. 매출의 93%인 1,076억 달러가 AI 서버용 칩을 파는 데이터센터 부문에서 나왔어요.", None, None),
    ("blog", "## 다음 분기 전망도 예상 이상", None, None),
    ("blog", "회사는 4분기(2026년 11월~2027년 1월) 매출을 1,300억 달러(±3%)로 제시했어요. 시장이 기대한 1,253억 달러보다 높은 숫자예요. 다만 이번 4분기는 평소보다 한 주 긴 14주짜리이고, 중국 데이터센터 매출은 0으로 가정했다는 점을 함께 봐야 해요.", None, None),
    ("blog", "## 컨퍼런스콜에서 나온 이야기", None, None),
    ("blog", "임시 CEO를 맡은 콜레트 크레스 CFO는 수요가 공급보다 많다고 강조했어요. 부품 확보 약정이 한 분기 만에 2,790억 달러에서 3,900억 달러로 늘었고, 신제품 베라 루빈은 회사 역사상 가장 빠른 속도로 출하되고 있다고 했습니다. 내년(FY28) 매출 성장률 전망도 70%에서 75%로 올렸어요.", None, None),
    ("blog", "## 남은 숙제 세 가지", None, None),
    ("blog", "첫째, 4분기 총이익률이 74.9%에서 71~72%로 내려갈 전망이에요. 시장도 71.7%를 예상하고 있었기 때문에 놀라운 숫자는 아니에요. 둘째, 리더십 공백이에요. 이사회는 젠슨 황의 복귀는 없다고 밝혔고 새 CEO를 찾고 있어요. 셋째, 중국 수출 규제와 12월 상원 증언 요청 같은 정책 변수가 남아 있습니다.", None, None),
    ("blog", "## 주가는 어디에 있나", None, None),
    ("blog", "엔비디아 주가는 10월 고점(239.24달러)에서 실적 직전 155.46달러까지 약 35% 떨어진 상태였어요. 실적 발표 후 시간외 거래에서는 4~5% 올랐습니다. 'AI 투자 둔화'와 'CEO 리스크'로 빠졌던 주가가 실적으로 얼마나 회복할지가 관전 포인트예요.", None, None),
    ("blog", "## 한 줄 정리", None, None),
    ("blog", "숫자는 여전히 강하고, 남은 숙제는 리더십과 마진이에요. 다음으로 볼 것은 새 CEO 선임, 4분기 총이익률, 중국 규제 흐름입니다.", None, None),
    ("blog", "이 글은 공개 자료를 정리한 것이며 투자 권유가 아닙니다.", None, None),
    ("tags", "#엔비디아 #NVDA #엔비디아실적 #AI반도체 #베라루빈 #데이터센터 #미국주식 #실적발표", None, None),
    ("img", "성적표(핵심 숫자 6개)", None, "https://jinhae8971.github.io/korea-etf-calmar/earnings/NVDA/2026-11-17/01_scorecard.png"),
    ("img", "분기 매출과 총이익률", None, "https://jinhae8971.github.io/korea-etf-calmar/earnings/NVDA/2026-11-17/02_trend.png"),
    ("img", "주가 위치(최근 6개월)", None, "https://jinhae8971.github.io/korea-etf-calmar/earnings/NVDA/2026-11-17/03_price.png"),
]


def notes():
    rows, cnt = [], {}
    for sec, text, date, url in NOTES_NVDA:
        cnt[sec] = cnt.get(sec, 0) + 1
        rows.append({"ticker": "NVDA", "section": sec, "n": cnt[sec], "text": text, "date": date, "url": url})
    write("notes.json", rows, [
        {"label": "What this is", "value": "블로그 작성용 문안: 한 줄 요약, 쉽게 풀기, 리스크, 다음 체크포인트, 주가 차트 이벤트, 제목 후보, 본문 초안(## = 소제목), 해시태그, 차트 이미지 주소"},
        {"label": "Based on", "value": "NVIDIA 8-K·CFO 코멘터리, Reuters/LSEG(IBTimes·AOL), Kiplinger·Constellation Research 컨퍼런스콜 정리, TheStreet(상원 증언 요청), Newsquawk(FY28 75%)"}])


if __name__ == "__main__":
    extra = nvda()
    notes()
    past, fut = calendar_rows()
    build_reactions(past, extra)
    build_calendar(fut)
