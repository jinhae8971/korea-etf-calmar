# -*- coding: utf-8 -*-
"""Render the weekly close report: two Telegram messages (stock / crypto), a blog-style page and blog.md.

Numbers come from data.json; the narrative only supplies wording (headline, key points, drivers, insights, next week).
"""
import html
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_URL = "https://jinhae8971.github.io/korea-etf-calmar/weekly-close/"
TG_LIMIT, CAP_LIMIT = 1500, 1000
STOCK_IMGS = [("s1_indices.png", "글로벌 지수 주간 등락. 세로 표시는 직전 주 등락입니다."),
              ("s4_path.png", "직전 주 종가를 0%로 둔 요일별 누적 흐름입니다."),
              ("s2_sectors.png", "섹터·테마·원자재의 1주, 4주, 연초 이후 등락률입니다."),
              ("s3_movers.png", "미국 대형주 상·하위 8종목과 한국 대표주의 주간 등락입니다.")]
CRYPTO_IMGS = [("c1_majors.png", "BTC 60일 흐름과 시총 상위 코인의 7일 등락입니다."),
               ("c2_movers.png", "시총 상위 100(스테이블·래핑 제외)의 주간 상승·하락 상위입니다."),
               ("c3_flows.png", "공포탐욕 지수, 현물 BTC ETF 순유입, 스테이블코인 총공급입니다.")]


def md(s):
    return f"{int(s[5:7])}/{int(s[8:10])}"


def e(s):
    return html.escape(str(s), quote=False)


def by(rows, sym):
    return next((r for r in rows if r["sym"] == sym or r["label"] == sym), None)


def sg(v, nd=1, unit="%"):
    return "–" if v is None else f"{v:+.{nd}f}{unit}"


def stock_numbers(s):
    i, m = s["indices"], s["macro"]
    g = lambda k: by(i, k)["w1"]  # noqa: E731
    tnx, vix, dxy, krw, gold, wti = (by(m, k) for k in ("^TNX", "^VIX", "DX-Y.NYB", "USDKRW=X", "GC=F", "CL=F"))
    sec = sorted(s["sectors"], key=lambda r: -r["w1"])
    l1 = (f"S&P {sg(g('^GSPC'))} · 나스닥100 {sg(g('^NDX'))} · SOX {sg(g('^SOX'))} · "
          f"코스피 {sg(g('^KS11'))} · 코스닥 {sg(g('^KQ11'))}")
    l2 = (f"10년 {tnx['close']:.2f}% ({sg(tnx['w1'], 0, 'bp')}) · VIX {vix['close']:.1f} · 달러 {sg(dxy['w1'])} · "
          f"원/달러 {krw['close']:,.0f} · 금 {sg(gold['w1'])} · WTI {sg(wti['w1'])}")
    l3 = (f"강세 {sec[0]['label']} {sg(sec[0]['w1'], 1, '')} · {sec[1]['label']} {sg(sec[1]['w1'], 1, '')} / "
          f"약세 {sec[-1]['label']} {sg(sec[-1]['w1'], 1, '')} · {sec[-2]['label']} {sg(sec[-2]['w1'], 1, '')}")
    return [l1, l2, l3]


def kfmt(v):
    return f"{v / 1000:.1f}K" if v >= 10000 else (f"{v:,.0f}" if v >= 100 else f"{v:.2f}")


def crypto_numbers(c):
    mj = {m["label"]: m for m in c["majors"]}
    l1 = " · ".join(f"{k} {kfmt(mj[k]['close'])} {sg(mj[k]['w1'])}" for k in ("BTC", "ETH", "SOL") if k in mj)
    parts = []
    if c.get("global"):
        parts.append(f"도미넌스 {c['global']['btc_dom']:.1f}%")
    if c.get("fng"):
        parts.append(f"공포탐욕 {c['fng']['now']} (1주 전 {c['fng']['w_ago']})")
    if c.get("etf"):
        parts.append(f"ETF {c['etf']['week_btc']:+,} BTC")
    if c.get("stables"):
        parts.append(f"스테이블 공급 {c['stables']['w1_abs'] / 1e9:+.1f}B달러")
    out = [l1, " · ".join(parts)]
    t = c.get("top100")
    if t:
        out.append(f"상위 {t['n']}종 중 상승 {t['up']}종 · 중앙값 {sg(t['median_w1'])} · ETH/BTC {sg(c['ethbtc']['w1'])}")
    return out


def telegram(kind, d, n):
    blk = n[kind]
    if kind == "stock":
        s = d["stock"]
        title, rng, nums, anchor = "주간 마감 · 주식", f"{md(s['week_start'])}~{md(s['week_end'])}", stock_numbers(s), "stock"
    else:
        c = d["crypto"]
        title, rng, nums, anchor = "주간 마감 · 크립토", f"{md(c['week_start'])}~{md(c['week_end'])} UTC", crypto_numbers(c), "crypto"
    L = [f"<b>{title}</b> {rng}", e(blk["headline"]), ""] + [e(x) for x in nums] + ["", "<b>핵심</b>"]
    L += [f"- {e(x)}" for x in blk["key"][:3]]
    L += ["", "<b>인사이트</b>", e(blk["tg_insight"]), "", "<b>다음 주</b>"]
    L += [f"- {e(x)}" for x in blk["next"][:3]]
    L += ["", f'<a href="{REPORT_URL}{d["week"]}/#{anchor}">전체 글(차트·원인·출처)</a> · 투자 판단은 본인 책임']
    return "\n".join(L)


def caption(kind, d):
    if kind == "stock":
        s = d["stock"]
        return f"<b>주간 마감 · 주식</b> {md(s['week_start'])}~{md(s['week_end'])} 차트 4장\n지수 · 한 주 흐름 · 섹터 · 종목"
    c = d["crypto"]
    return f"<b>주간 마감 · 크립토</b> {md(c['week_start'])}~{md(c['week_end'])} 차트 3장\n주요 코인 · 상위 100 · 심리와 자금"


CSS = """
:root{--bg:#1a1a19;--card:#232321;--ink:#f0efec;--ink2:#c3c2b7;--muted:#8f8e86;--line:#33332f;--up:#e66767;--down:#5598e7;--acc:#86b6ef}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 'Noto Sans KR',system-ui,-apple-system,sans-serif;word-break:keep-all}
main{max-width:820px;margin:0 auto;padding:28px 18px 72px}h1{font-size:28px;line-height:1.35;margin:6px 0 10px}
h2{font-size:22px;margin:48px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}h3{font-size:17.5px;margin:26px 0 6px}
.kicker{color:var(--acc);font-size:13.5px;letter-spacing:.02em}.sub{color:var(--muted);font-size:14px}.lead{font-size:17.5px;color:var(--ink2);margin:14px 0 0}
.card{background:var(--card);border-radius:12px;padding:16px 18px;margin:16px 0}.card h3{margin-top:0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin:16px 0}
.tile{background:var(--card);border-radius:10px;padding:10px 12px}.tile b{display:block;font-size:19px}.tile span{color:var(--muted);font-size:12.5px}
.up{color:var(--up)}.dn{color:var(--down)}ul{padding-left:20px;margin:8px 0}li{margin:6px 0}p{margin:8px 0}
figure{margin:18px 0}figure img{width:100%;border-radius:10px;display:block}figcaption{color:var(--muted);font-size:13px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14.5px;margin:8px 0}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:500}td:first-child{white-space:nowrap}.wrap{overflow-x:auto}a{color:var(--acc)}
.insight{border-left:3px solid var(--acc);padding:2px 0 2px 14px;margin:16px 0}.insight b{display:block;font-size:16.5px;margin-bottom:2px}
nav{display:flex;gap:14px;flex-wrap:wrap;font-size:14px;margin:14px 0 0}.foot{color:var(--muted);font-size:13px;margin-top:44px}
"""


def tile(label, val, chg=None, unit="%", nd=1):
    cl = "" if chg is None else ("up" if chg > 0 else "dn" if chg < 0 else "")
    ch = "" if chg is None else f' <small class="{cl}">{sg(chg, nd, unit)}</small>'
    return f'<div class="tile"><span>{e(label)}</span><b>{e(val)}{ch}</b></div>'


def section(kind, title, blk, tiles, imgs, prefix):
    h = [f'<h2 id="{kind}">{e(title)}</h2>', f'<p class="lead">{e(blk["headline"])}</p>', f'<div class="tiles">{"".join(tiles)}</div>',
         '<div class="card"><h3>이번 주 핵심 3가지</h3><ul>' + "".join(f"<li>{e(x)}</li>" for x in blk["key"]) + "</ul></div>"]
    figs = [f'<figure><img src="{prefix}{f}" alt="{e(cap)}" loading="lazy"><figcaption>{e(cap)}</figcaption></figure>' for f, cap in imgs]
    h.append(figs[0])
    h.append("<h3>무슨 일이 있었나</h3>")
    rest = figs[1:]
    for x in blk["drivers"]:
        h.append(f'<p><b>{e(x["h"])}</b><br>{e(x["b"])}</p>')
        if rest and len(rest) > 1:
            h.append(rest.pop(0))
    h += rest
    h.append("<h3>숫자 뒤의 인사이트</h3>")
    h += [f'<div class="insight"><b>{e(x["h"])}</b>{e(x["b"])}</div>' for x in blk["insights"]]
    h.append("<h3>다음 주 관전 포인트</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in blk.get("next_long", blk["next"])) + "</ul>")
    return "\n".join(h)


def report(d, n, prefix="img/", archive="../archive.html"):
    s, c = d["stock"], d["crypto"]
    i, m = s["indices"], s["macro"]
    st = [tile(lab, f"{by(i, k)['close']:,.0f}", by(i, k)["w1"]) for k, lab in
          (("^GSPC", "S&P 500"), ("^NDX", "나스닥100"), ("^SOX", "반도체(SOX)"), ("^KS11", "코스피"), ("^KQ11", "코스닥"))]
    tnx, vix, krw = by(m, "^TNX"), by(m, "^VIX"), by(m, "USDKRW=X")
    st += [tile("미 10년물", f"{tnx['close']:.2f}%", tnx["w1"], "bp", 0), tile("VIX", f"{vix['close']:.1f}", vix["w1"], "pt", 1),
           tile("원/달러", f"{krw['close']:,.0f}", krw["w1"])]
    ct = [tile(x["label"], f"${x['close']:,.0f}" if x["close"] >= 100 else f"${x['close']:.2f}", x["w1"]) for x in c["majors"][:3]]
    if c.get("global"):
        ct.append(tile("BTC 도미넌스", f"{c['global']['btc_dom']:.1f}%"))
    if c.get("fng"):
        ct.append(tile("공포탐욕", str(c["fng"]["now"]), c["fng"]["now"] - c["fng"]["w_ago"], "", 0))
    if c.get("etf"):
        ct.append(tile("ETF 순유입(주)", f"{c['etf']['week_btc']:+,} BTC"))
    if c.get("stables"):
        ct.append(tile("스테이블 공급", f"${c['stables']['now'] / 1e9:.0f}B", c["stables"]["w1"]))
    if c.get("top100"):
        ct.append(tile("상위 100 상승", f"{c['top100']['up']} / {c['top100']['n']}"))
    cal = "".join(f"<tr><td>{e(x['d'])}</td><td>{e(x['e'])}</td><td>{e(x['w'])}</td></tr>" for x in n.get("calendar", []))
    src = "".join(f'<li><a href="{e(x["u"])}" rel="noopener">{e(x["t"])}</a></li>' for x in n.get("sources", []))
    notes = []
    if c.get("etf") and c["etf"].get("last") and c["etf"]["last"] < c["week_end"]:
        notes.append(f"ETF 순유입은 {md(c['etf']['last'])}까지 집계분({c['etf']['n_days']}거래일)입니다.")
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(n['title'])}</title><meta name="description" content="{e(n['lead'])}">
<meta property="og:title" content="{e(n['title'])}"><meta property="og:description" content="{e(n['lead'])}">
<meta property="og:image" content="{REPORT_URL}{d['week']}/img/s1_indices.png"><style>{CSS}</style></head><body><main>
<div class="kicker">주간 마감 리포트 · 주식 {md(s['week_start'])}~{md(s['week_end'])} · 크립토 {md(c['week_start'])}~{md(c['week_end'])}(UTC)</div>
<h1>{e(n['title'])}</h1><p class="lead">{e(n['lead'])}</p>
<nav><a href="#stock">주식</a><a href="#crypto">크립토</a><a href="#cross">함께 보기</a><a href="#calendar">다음 주 일정</a><a href="blog.md">블로그용 마크다운</a><a href="{archive}">지난 호</a></nav>
<div class="card"><h3>30초 요약</h3><ul>{''.join(f'<li>{e(x)}</li>' for x in n['tldr'])}</ul></div>
{section('stock', '주식: ' + n['stock']['title'], n['stock'], st, STOCK_IMGS, prefix)}
{section('crypto', '크립토: ' + n['crypto']['title'], n['crypto'], ct, CRYPTO_IMGS, prefix)}
<h2 id="cross">주식과 크립토를 함께 보면</h2><div class="insight"><b>{e(n['cross']['h'])}</b>{e(n['cross']['b'])}</div>
<h2 id="calendar">다음 주 일정</h2><div class="wrap"><table><tr><th>일시(KST)</th><th>일정</th><th>왜 중요한가</th></tr>{cal}</table></div>
<h2>출처</h2><ul>{src}</ul>
<p class="foot">시세: Yahoo Finance, Coinbase, CoinGecko, alternative.me, DefiLlama, bitcoin-data.com. 주식은 금요일 뉴욕 종가, 크립토는 UTC 일봉 종가 기준 7일입니다. {' '.join(notes)}
이 글은 정보 제공용이며 투자 권유가 아닙니다. 작성 {e(d['generated_utc'])}.</p></main></body></html>"""


def blog_md(d, n):
    s, c = d["stock"], d["crypto"]
    base = f"{REPORT_URL}{d['week']}/img/"
    L = [f"# {n['title']}", "", f"> {n['lead']}", "", "## 30초 요약"] + [f"- {x}" for x in n["tldr"]]
    for kind, title, imgs, nums in (("stock", "주식", STOCK_IMGS, stock_numbers(s)), ("crypto", "크립토", CRYPTO_IMGS, crypto_numbers(c))):
        b = n[kind]
        L += ["", f"## {title}: {b['title']}", "", b["headline"], ""] + [f"- {x}" for x in nums]
        L += ["", "### 이번 주 핵심 3가지"] + [f"- {x}" for x in b["key"]]
        L += ["", f"![{imgs[0][1]}]({base}{imgs[0][0]})", "", "### 무슨 일이 있었나"]
        for x in b["drivers"]:
            L += ["", f"**{x['h']}**", "", x["b"]]
        for f, cap in imgs[1:]:
            L += ["", f"![{cap}]({base}{f})"]
        L += ["", "### 숫자 뒤의 인사이트"]
        for x in b["insights"]:
            L += ["", f"**{x['h']}**", "", x["b"]]
        L += ["", "### 다음 주 관전 포인트"] + [f"- {x}" for x in b.get("next_long", b["next"])]
    L += ["", "## 주식과 크립토를 함께 보면", "", f"**{n['cross']['h']}**", "", n["cross"]["b"], "", "## 다음 주 일정", "",
          "| 일시(KST) | 일정 | 왜 중요한가 |", "|---|---|---|"]
    L += [f"| {x['d']} | {x['e']} | {x['w']} |" for x in n.get("calendar", [])]
    L += ["", "## 출처"] + [f"- [{x['t']}]({x['u']})" for x in n.get("sources", [])]
    L += ["", "*이 글은 정보 제공용이며 투자 권유가 아닙니다.*", ""]
    return "\n".join(L)


def main(week, narrative):
    odir = os.path.join(HERE, "out", week)
    d = json.load(open(os.path.join(odir, "data.json")))
    n = json.load(open(narrative))
    out = {}
    for kind in ("stock", "crypto"):
        t, cap = telegram(kind, d, n), caption(kind, d)
        if len(t) > TG_LIMIT:
            raise SystemExit(f"{kind} telegram text {len(t)} > {TG_LIMIT}: shorten headline/key/tg_insight/next")
        if len(cap) > CAP_LIMIT:
            raise SystemExit(f"{kind} caption too long")
        open(os.path.join(odir, f"telegram_{kind}.html"), "w").write(t)
        open(os.path.join(odir, f"caption_{kind}.html"), "w").write(cap)
        out[kind] = len(t)
    open(os.path.join(odir, "index.html"), "w").write(report(d, n))
    open(os.path.join(odir, "blog.md"), "w").write(blog_md(d, n))
    print("rendered", out)
    return d, n


if __name__ == "__main__":
    import sys
    main(sys.argv[1], sys.argv[2])
