# -*- coding: utf-8 -*-
"""Render the US close briefing: Telegram text + caption, and the full HTML report.

python3 render.py <session> <narrative.json>
  -> out/<session>/telegram.html, caption.html, report.html

narrative.json (written by the analyst each day):
{
 "headline": "...", "breadth_note": "...",
 "drivers": ["..."], "after_hours": ["..."], "calendar": ["..."],
 "tech": {"QQQ": {"short": 55, "swing": 45, "short_d": 0, "swing_d": 5,
                   "note": "...", "up": "727.9 돌파 = ...", "down": "707.6 이탈 = ..."}, ...},
 "view": "...", "summary": ["..."], "sources": [{"title": "...", "url": "..."}]
}
"""
import datetime as dt
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_URL = "https://jinhae8971.github.io/korea-etf-calmar/us-close/"
WD = "월화수목금토일"
IMG_CAPTIONS = {
    "01_cross.png": "크로스에셋 — 지수·금리·달러·원자재·비트코인",
    "02_sectors.png": "섹터·테마 히트맵 — 1일~YTD 수익률, RSI, 일봉 추세",
    "03_treemap.png": "대형주 히트맵 — 시총 크기, 1일 등락 색",
    "04_breadth.png": "브레드스·팩터 — 이동평균 위 종목 비율, 팩터 상대강도",
    "05_qqq.png": "QQQ 일봉 — EMA50·EMA120·SuperTrend·지지/저항",
    "06_spy.png": "SPY 일봉",
    "07_soxx.png": "SOXX 일봉 — 반도체 ETF",
    "08_dram.png": "DRAM 일봉 — 메모리 ETF(2026-04 상장, 주봉 이력 짧음)",
}


def e(s):
    return html.escape(str(s), quote=False)


def pct(v, nd=2):
    return "–" if v is None else f"{v:+.{nd}f}%"


def dstr(session):
    d = dt.date.fromisoformat(session)
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


def cross_map(d):
    return {r["sym"]: r for r in d["cross"]}


def num(v):
    if v >= 10000:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def k_btc(v):
    return f"{v / 1000:.1f}K"


def lead_lag(rows, n=3, key="d1"):
    s = sorted(rows, key=lambda r: -(r[key] or 0))
    up = " · ".join(f"{e(r['label'])} {r[key]:+.1f}" for r in s[:n] if r[key] > 0)
    dn = " · ".join(f"{e(r['label'])} {r[key]:+.1f}" for r in s[::-1][:n] if r[key] < 0)
    return up or "없음", dn or "없음"


def short_label(t):
    return "반등" if t["price"] < t["1d"]["ma_mid"]["v"] else "상승"


def delta(v):
    return "" if not v else f" ({v:+d})"


def key_level(x):
    """'701.3 회복 → 707.7 ...' -> '701.3 회복' (first leg only)."""
    return str(x).split("→")[0].strip()


def telegram(d, n):
    """Compact Telegram text (2026-10-31: '핵심 위주로'). Details live in the report page."""
    c = cross_map(d)
    g = lambda s: c.get(s, {})  # noqa: E731
    L = [f"<b>미증시 마감 · {dstr(d['session'])}</b>", e(n["headline"]), ""]
    L.append(f"S&amp;P {pct(g('^GSPC')['d1'])} · 나스닥100 {pct(g('^NDX')['d1'])} · 다우 {pct(g('^DJI')['d1'])} · "
             f"러셀 {pct(g('^RUT')['d1'])} · SOX {pct(g('^SOX')['d1'])}")
    L.append(f"VIX {g('^VIX')['close']:.1f} · 10년 {g('^TNX')['close']:.2f}% ({g('^TNX')['d1']:+.0f}bp) · "
             f"WTI {g('CL=F')['close']:.0f} · 금 {pct(g('GC=F')['d1'], 1)} · BTC {k_btc(g('BTC-USD')['close'])}")
    up, dn = lead_lag(d["sectors"], n=2)
    L.append(f"강세 {up} / 약세 {dn}")
    L.append("")
    L.append("<b>확률</b> 단기 / 스윙(2~4주) · 상방 / 하방 기준")
    for s, t in n["tech"].items():
        tt = d["tech"][s]
        L.append(f"<b>{s}</b> {tt['price']} ({tt['chg_pct']:+.1f}%) {short_label(tt)} {t['short']}{delta(t.get('short_d'))} / "
                 f"{t['swing']}{delta(t.get('swing_d'))} · {e(key_level(t['up']))} / {e(key_level(t['down']))}")
    if n.get("calendar"):
        L.append("")
        L.append("<b>일정</b> " + " · ".join(e(x) for x in n["calendar"][:3]))
    L.append("")
    L.append("<b>요약</b>")
    L += [f"- {e(x)}" for x in n["summary"][:4]]
    L.append("")
    L.append(f'<a href="{REPORT_URL}">전체 리포트(원인·시간외·차트·출처)</a> · 확률은 참고용')
    return "\n".join(L)


def caption(d, n, imgs):
    return (f"<b>미증시 마감 · {dstr(d['session'])}</b> 차트 {len(imgs)}장\n"
            "크로스에셋 · 섹터 · 대형주 · 브레드스 · " + " · ".join(n["tech"].keys()))


# ------------------------------------------------------------------ HTML report
CSS = """
:root{--bg:#1a1a19;--card:#232321;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--line:#33332f;--up:#e66767;--down:#5598e7;--acc:#3987e5}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 'Noto Sans KR',system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:34px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.sub{color:var(--ink2)}.head{font-size:17px;margin:14px 0 0}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}
.tile{background:var(--card);border-radius:10px;padding:10px 12px}.tile b{display:block;font-size:19px}.tile span{color:var(--muted);font-size:12.5px}
.up,.tile .up{color:var(--up)}.dn,.tile .dn{color:var(--down)}ul{padding-left:20px;margin:6px 0}li{margin:4px 0}
figure{margin:16px 0}figure img{width:100%;border-radius:10px;display:block}figcaption{color:var(--muted);font-size:13px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500}.card{background:var(--card);border-radius:10px;padding:14px 16px;margin:10px 0}
.prob{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}a{color:#86b6ef}.foot{color:var(--muted);font-size:13px;margin-top:40px}
.wrap{overflow-x:auto}
"""


def cls(v):
    return "" if v is None else ("up" if v > 0 else "dn" if v < 0 else "")


def td(v, unit="%", nd=2):
    if v is None:
        return "<td>–</td>"
    return f'<td class="{cls(v)}">{v:+.{nd}f}{unit}</td>'


def report(d, n, imgs, img_prefix):
    c = cross_map(d)
    tiles = []
    for s, lab in (("^GSPC", "S&P 500"), ("^NDX", "나스닥100"), ("^DJI", "다우"), ("^RUT", "러셀2000"),
                   ("^SOX", "반도체(SOX)"), ("^VIX", "VIX"), ("^TNX", "미 10년물"), ("USDKRW=X", "원/달러")):
        r = c[s]
        unit = "bp" if r["kind"] == "yld" else ("pt" if r["kind"] == "lvl" else "%")
        val = f"{r['close']:.2f}%" if r["kind"] == "yld" else num(r["close"])
        tiles.append(f'<div class="tile"><span>{lab}</span><b>{val}</b><span class="{cls(r["d1"])}">{r["d1"]:+.2f}{unit}</span></div>')
    H = [f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>미증시 마감 브리핑 {d['session']}</title><style>{CSS}</style></head><body><main>",
         f"<h1>미증시 마감 브리핑</h1><div class='sub'>{dstr(d['session'])} 뉴욕 마감 기준 · 생성 {d['generated_kst']} KST</div>",
         f"<p class='head'>{e(n['headline'])}</p>", "<div class='tiles'>" + "".join(tiles) + "</div>",
         "<h2>요약</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in n["summary"]) + "</ul>",
         f"<div class='card'><b>종합 판단</b><br>{e(n['view'])}</div>",
         "<h2>무엇이 시장을 움직였나</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in n["drivers"]) + "</ul>"]
    if n.get("after_hours"):
        H.append("<h2>장 마감 후·시간외</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in n["after_hours"]) + "</ul>")
    for name in imgs[:4]:
        H.append(f"<figure><img src='{img_prefix}{name}' alt='{IMG_CAPTIONS.get(name, name)}'><figcaption>{IMG_CAPTIONS.get(name, '')}</figcaption></figure>")
        if name == "01_cross.png":
            H.append("<div class='wrap'><table><tr><th>자산</th><th>종가</th><th>1일</th><th>5일</th><th>1개월</th></tr>" + "".join(
                f"<tr><td>{r['label']}</td><td>{r['close']}</td>" +
                (td(r['d1'], 'bp', 1) + td(r['d5'], 'bp', 1) + td(r['m1'], 'bp', 1) if r['kind'] == 'yld' else
                 td(r['d1'], 'pt', 2) + td(r['d5'], 'pt', 2) + td(r['m1'], 'pt', 2) if r['kind'] == 'lvl' else
                 td(r['d1']) + td(r['d5']) + td(r['m1'])) + "</tr>" for r in d["cross"]) + "</table></div>")
        if name == "02_sectors.png":
            H.append("<div class='wrap'><table><tr><th>섹터·테마</th><th>1일</th><th>5일</th><th>1개월</th><th>YTD</th><th>RSI</th><th>일봉 추세</th></tr>" + "".join(
                f"<tr><td>{r['label']} <span class='sub'>{r['sym']}</span></td>{td(r['d1'])}{td(r['d5'])}{td(r['m1'])}{td(r['ytd'], '%', 1)}"
                f"<td>{r['rsi']:.0f}</td><td>{'상승' if r['st_up'] else '하락'}</td></tr>"
                for r in sorted(d["sectors"] + d["themes"], key=lambda r: -r["d1"])) + "</table></div>")
        if name == "03_treemap.png":
            H.append("<div class='wrap'><table><tr><th>상승 상위</th><th>1일</th><th>거래량(20일 대비)</th><th>하락 상위</th><th>1일</th><th>거래량</th></tr>" + "".join(
                f"<tr><td>{u['sym']}</td>{td(u['d1'])}<td>x{u['vol_x']}</td><td>{w['sym']}</td>{td(w['d1'])}<td>x{w['vol_x']}</td></tr>"
                for u, w in zip(d["movers"]["up"], d["movers"]["down"])) + "</table></div>")
        if name == "04_breadth.png":
            b = d["breadth"]
            H.append(f"<p class='sub'>대형주 {b['n']}종: 상승 {b['adv']} / 하락 {b['dec']} · 50일선 위 {b['above50']}% "
                     f"(전일 {b['above50_prev']}%, 5일 전 {b['above50_5d']}%) · 200일선 위 {b['above200']}% · "
                     f"52주 신고가 {b['new_high']} / 신저가 {b['new_low']}</p>"
                     + (f"<p>{e(n['breadth_note'])}</p>" if n.get("breadth_note") else "")
                     + "<div class='wrap'><table><tr><th>팩터 스프레드</th><th>1일</th><th>5일</th><th>1개월</th></tr>" + "".join(
                         f"<tr><td>{f['label']} ({f['long']}/{f['short']})</td>{td(f['d1'])}{td(f['d5'])}{td(f['m1'])}</tr>" for f in d["factors"]) + "</table></div>")
    H.append("<h2>기술적 분석 · 확률</h2><p class='sub'>TradingView 설정과 맞춘 지표(EMA50·EMA120, SuperTrend ATR20×4, RSI14, MACD 12/26/9). "
             "확률은 차트 기반 추정치예요.</p><div class='prob'>")
    for s, t in n["tech"].items():
        tt = d["tech"][s]
        st = " / ".join(f"{k.upper()} {'상승' if tt[k]['supertrend']['dir'] == 'up' else '하락'}" for k in ("4h", "1d", "1w"))
        H.append(f"<div class='card'><b>{s}</b> {tt['price']} <span class='{cls(tt['chg_pct'])}'>{tt['chg_pct']:+.2f}%</span><br>"
                 f"<span class='sub'>SuperTrend {st} · RSI {tt['1d']['rsi']['v']}</span><br>"
                 f"단기 {short_label(tt)} {t['short']}%{delta(t.get('short_d'))} · 스윙 상승 {t['swing']}%{delta(t.get('swing_d'))}<br>"
                 f"{e(t['note'])}<br><span class='up'>상방</span> {e(t['up'])}<br><span class='dn'>하방</span> {e(t['down'])}</div>")
    H.append("</div>")
    for name in imgs[4:]:
        H.append(f"<figure><img src='{img_prefix}{name}' alt='{IMG_CAPTIONS.get(name, name)}'><figcaption>{IMG_CAPTIONS.get(name, '')}</figcaption></figure>")
    if n.get("calendar"):
        H.append("<h2>일정</h2><ul>" + "".join(f"<li>{e(x)}</li>" for x in n["calendar"]) + "</ul>")
    if n.get("sources"):
        H.append("<h2>출처</h2><ul>" + "".join(f"<li><a href='{html.escape(x['url'])}'>{e(x['title'])}</a></li>" for x in n["sources"]) + "</ul>")
    H.append(f"<p class='foot'>데이터 Yahoo Finance·Nasdaq. 색은 한국식(빨강 상승·파랑 하락). 투자 판단의 참고 자료예요. "
             f"<a href='archive.html'>지난 브리핑</a></p></main></body></html>")
    return "\n".join(H)


def main(session, narrative_path):
    sdir = os.path.join(HERE, "out", session)
    d = json.load(open(os.path.join(sdir, "data.json")))
    n = json.load(open(narrative_path))
    imgs = sorted(os.listdir(os.path.join(sdir, "img")))
    tg = telegram(d, n)
    cap = caption(d, n, imgs)
    open(os.path.join(sdir, "telegram.html"), "w").write(tg)
    open(os.path.join(sdir, "caption.html"), "w").write(cap)
    open(os.path.join(sdir, "report.html"), "w").write(report(d, n, imgs, "img/"))
    print(f"telegram {len(tg)} chars · caption {len(cap)} chars · imgs {imgs}")
    if len(tg) > 1500 or len(cap) > 1000:
        sys.exit("too long: trim narrative")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
