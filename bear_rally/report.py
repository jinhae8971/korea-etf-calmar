"""스냅샷 → 텔레그램 본문(짧게) · GitHub Pages 리포트(상세)."""
from __future__ import annotations

import html
from datetime import date

from engine import S_BEAR, S_BULL, S_FAILED, S_RALLY, S_SHIFT, STATE_KO

WD = "월화수목금토일"
HIT_KO = {"rsi_ceil": "RSI 천장", "ma50": "50일선", "ma200": "200일선", "band": "BMSB", "avwap": "고점 AVWAP",
          "fib": "피보 38.2%↑", "narrow": "좁은 반등", "zbt_near": "ZBT 근접 실패"}
EVID_KO = {"zbt": "ZBT", "breadth": "200일선 위 종목 50%↑", "rsi_shift": "RSI 레인지 전환", "weekly": "주봉 안착",
           "ma_flat": "200일선 하락 멈춤", "reclaim": "200일선 회복"}
EV_KO = {"bear_enter": "베어 진입", "rally": "반등 감시 시작", "bear_rally": "베어랠리 판정", "shift_fail": "전환 실패 → 베어랠리",
         "shift": "전환 후보", "bull": "강세 확정", "new_low": "신저점 (반등 실패)", "rearm": "반등 소멸 → 재감시",
         "zbt_fire": "ZBT 발생", "zbt_near": "ZBT 근접 실패"}
EV_ICON = {"bear_enter": "🔻", "bear_rally": "🔻", "shift_fail": "🔻", "new_low": "🔻",
           "shift": "🔺", "bull": "🔺", "rally": "·", "rearm": "·"}
STATE_CLASS = {S_BULL: "bull", S_BEAR: "bear", S_RALLY: "rally", S_FAILED: "failed", S_SHIFT: "shift"}


def pct(x, d=1, sign=True) -> str:
    if x is None:
        return "—"
    s = f"{x * 100:+.{d}f}%" if sign else f"{x * 100:.{d}f}%"
    return s.replace("-", "−")


def price(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"


def md(s: str) -> str:
    d = date.fromisoformat(s)
    return f"{d.month}/{d.day}({WD[d.weekday()]})"


# ----------------------------------------------------------------------------- 자산별 요약 문구
def state_line(a: dict) -> str:
    st = a["state"]
    if st == S_BULL:
        s = f"강세 · 200일선 {pct(a['ma200_gap'])}"
        if a["dd_peak"] is not None and a["dd_peak"] <= -0.10:
            s += f" · 고점 {pct(a['dd_peak'], 0)}"
        return s
    if st == S_BEAR:
        return f"베어 · 고점 {pct(a['dd_peak'], 0)} · 저점 {pct(a['rally_pct'])}"
    if st == S_RALLY:
        return f"반등 감시 · 저점 {pct(a['rally_pct'])} · 근거 {a['bear_hits']}/{a['hits_max']} · 전환 {a['shift_score']}/{a['shift_max']}"
    if st == S_FAILED:
        why = " · ".join(HIT_KO[h] for h in a["hits"][:3]) or "근거 해제"
        return f"베어랠리 판정 · {why}"
    return f"전환 후보 · 점수 {a['shift_score']}/{a['shift_max']}"


def watch_line(a: dict) -> str | None:
    """다음 상태로 넘어가는 조건 — '무엇을 보면 되는가'."""
    st = a["state"]
    if st == S_BULL:
        if a["ma200_gap"] is None or a["ma200_gap"] >= 0:
            return None
        need = []                                   # (짧은 이름, 현재 수치)
        if a["mode"] == "crypto":
            if not a["cond"]["weekly_below_band"]:
                need.append(("주봉 BMSB 하단 이탈", f"하단 {price(a['w_bot'])}"))
        elif not a["cond"]["slope_or_cross"]:
            gap = a["ma50_gap"]
            need.append(("데드크로스", f"50·200일선 간격 {abs(gap) * 100:.1f}%" if gap is not None else ""))
        if not a["cond"]["dd"]:
            need.append((f"{a['dd_len']}일 고점 −{a['dd_min_pct']:.0f}%", f"현재 {pct(a['dd63'])}"))
        if not need:
            return "베어 진입 조건 충족 — 다음 마감에 판정"
        if len(need) == 1:
            return f"베어 진입 대기: {need[0][0]}" + (f" ({need[0][1]})" if need[0][1] else "")
        return "베어 진입 대기: " + " + ".join(n for n, _ in need)
    if st == S_BEAR:
        return f"{price(a['rally_trigger'])} 위 마감 시 반등 감시 (저점 +{a['rally_min_pct']:.0f}%)"
    if st == S_RALLY:
        res = f" · 저항 {a['res_name']} {price(a['res_level'])}" if a.get("res_level") else ""
        return f"{price(a['turn_level'])} 아래 + RSI 60 미만 마감 시 판정 대상{res}"
    if st == S_FAILED:
        return f"{price(a['rearm_level'])} 이탈 시 재감시 · {price(a['rally_base'])} 깨지면 신저점"
    hold = f"주봉 안착 {a['w_up'] or 0}/{a['hold_weeks']}"
    return f"강세 확정까지: {hold} · 50일선 {'상승' if a['ma50_up'] else '하락'} · 200일선 {'위' if a['ma200_gap'] and a['ma200_gap'] > 0 else '아래'}"


def change_line(key: str, e: dict) -> str:
    s = f"{EV_ICON.get(e['type'], '·')} <b>{key}</b> {EV_KO[e['type']]} ({md(e['d'])})"
    if e["type"] in ("bear_rally", "shift_fail") and e["hits"]:
        s += "\n   " + " · ".join(HIT_KO[h] for h in e["hits"])
    elif e["type"] in ("shift", "bull") and e["evid"]:
        s += "\n   " + " · ".join(EVID_KO[k] for k in e["evid"])
    elif e["type"] == "rally" and e.get("rally_pct") is not None:
        s += f"  저점 {pct(e['rally_pct'])}"
    return s


# ----------------------------------------------------------------------------- 텔레그램
def telegram(snap: dict) -> str:
    L = [f"<b>베어랠리 레이더</b>  {md(snap['as_of_date'])}",
         f"<i>{snap['basis']}</i>", ""]
    ch = [(a["key"], e) for a in snap["assets"] for e in a["changes"] if e["type"] in EV_KO and not e["type"].startswith("zbt")]
    L.append("<b>오늘 바뀐 것</b>")
    if ch:
        L += [change_line(k, e) for k, e in ch]
    else:
        L.append("상태 변화 없음")
    L += ["", "<b>현재 상태</b>"]
    for a in snap["assets"]:
        if a.get("error"):
            L.append(f"<b>{a['key']}</b> 판정 불가 — 데이터 수집 실패")
            continue
        L.append(f"<b>{a['key']}</b> {state_line(a)}")
        w = watch_line(a)
        if w:
            L.append(f"   └ {w}")
    if snap.get("notes"):
        L += [""] + [f"<i>{html.escape(n)}</i>" for n in snap["notes"]]
    L += ["", f"리포트 › {snap['pages_url']}"]
    return "\n".join(L)


# ----------------------------------------------------------------------------- 리포트 페이지
CSS = """
:root{color-scheme:light;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;
--bull:#008300;--bear:#2a62ab;--rally:#a86e00;--failed:#c0346b;--shift:#13805a;--chip:#f0efec}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--muted:#898781;--line:#2c2c2a;--bull:#0ca30c;--bear:#5598e7;--rally:#eda100;--failed:#e87ba4;--shift:#1baf7a;--chip:#242422}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:system-ui,-apple-system,"Segoe UI","Apple SD Gothic Neo","Malgun Gothic",sans-serif;line-height:1.55}
main{max-width:1040px;margin:0 auto;padding:22px 16px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:30px 0 10px}h3{font-size:15px;margin:0 0 6px}
.sub{color:var(--ink2);font-size:13.5px}.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:12px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{padding:8px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{color:var(--ink2);font-weight:600;white-space:nowrap}td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.st{display:inline-block;padding:2px 9px;border-radius:999px;font-weight:700;font-size:12.5px;white-space:nowrap;border:1.5px solid currentColor}
.st.bull{color:var(--bull)}.st.bear{color:var(--bear)}.st.rally{color:var(--rally)}.st.failed{color:var(--failed)}.st.shift{color:var(--shift)}
.chips span{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12.5px}
.chips span.on{outline:1.5px solid var(--ink2);font-weight:600}.chips span.off{color:var(--muted)}
img{width:100%;height:auto;border-radius:10px;border:1px solid var(--line);display:block}
.watch{font-size:13.5px;margin:6px 0 0}.flow{font-size:13.5px}.flow li{margin:3px 0}
.scroll{overflow-x:auto}footer{margin-top:36px;font-size:12.5px;color:var(--muted)}
"""


def _chip_list(items: dict, labels: dict) -> str:
    return "".join(f'<span class="{"on" if v else "off"}">{"✓" if v else "·"} {html.escape(labels[k])}</span>'
                   for k, v in items.items() if k in labels)


def page(snap: dict) -> str:
    e = html.escape
    rows = []
    for a in snap["assets"]:
        if a.get("error"):
            rows.append(f"<tr><td><b>{e(a['key'])}</b></td><td colspan=7 class=muted>판정 불가 — {e(a['error'])}</td></tr>")
            continue
        rows.append(
            f"<tr><td><b>{e(a['key'])}</b><div class=muted>{e(a['name'])}</div></td>"
            f"<td><span class='st {STATE_CLASS[a['state']]}'>{STATE_KO[a['state']]}</span>"
            f"<div class=muted>{e(md(a['state_since']))}부터</div></td>"
            f"<td class=num>{price(a['close'])}<div class=muted>{e(md(a['last_date']))}</div></td>"
            f"<td class=num>{pct(a['ma200_gap'])}<div class=muted>{'하락 중' if a['ma_fall'] else '하락 아님'}</div></td>"
            f"<td class=num>{pct(a['dd_peak'], 0)}</td>"
            f"<td class=num>{pct(a['rally_pct']) if a['state'] != S_BULL else '—'}</td>"
            f"<td class=num>{a['rsi']:.0f}</td>"
            f"<td>{e(watch_line(a) or '—')}</td></tr>")
    table = ("<div class=scroll><table><tr><th>자산</th><th>상태</th><th>종가</th><th>200일선 대비</th><th>고점 대비</th>"
             "<th>저점 대비</th><th>RSI</th><th>다음 조건</th></tr>" + "".join(rows) + "</table></div>")

    sections = []
    for a in snap["assets"]:
        if a.get("error"):
            continue
        hits = {k: (k in a["hits"]) for k in a["hits_all"]}
        evid = {k: (k in a["evid"]) for k in a["evid_all"]}
        ev_rows = "".join(
            f"<tr><td class=num>{e(x['d'])}</td><td>{e(EV_KO.get(x['type'], x['type']))}</td><td class=num>{price(x['c'])}</td>"
            f"<td>{e(' · '.join(HIT_KO[h] for h in x['hits']) if x['type'] in ('bear_rally', 'shift_fail') else ' · '.join(EVID_KO[k] for k in x['evid']) if x['type'] in ('shift', 'bull') else '')}</td></tr>"
            for x in reversed(a["events_recent"]))
        br = ""
        if a.get("breadth_on"):
            br = (f"<p class=muted>미국 브레드스(대형주 {snap['breadth']['universe']}종 근사, {e(snap['breadth']['last_date'] or '-')} 기준): "
                  f"200일선 위 {a['br200']:.0f}% · 50일선 위 {a['br50']:.0f}% · ZBT 10일 EMA {a['zbt']:.3f} ({e(a['zbt_state'])})</p>"
                  if a.get("br200") is not None else "<p class=muted>미국 브레드스 데이터 없음</p>")
        sections.append(f"""
<div class=card id="{e(a['key'])}">
<h3>{e(a['name'])} <span class='st {STATE_CLASS[a['state']]}'>{STATE_KO[a['state']]}</span></h3>
<p class=watch>{e(state_line(a))}<br><span class=muted>다음 조건 · {e(watch_line(a) or '—')}</span></p>
<img src="{e(a['chart'])}" alt="{e(a['key'])} 차트: 종가·200일선·상태 구간" loading="lazy">
<p class=muted style="margin:8px 0 2px">베어랠리 근거 {a['bear_hits']}/{a['hits_max']} (판정 기준 {a['min_hits']}개 + 반등 고점 대비 2ATR 반락)</p>
<div class=chips>{_chip_list(hits, HIT_KO)}</div>
<p class=muted style="margin:8px 0 2px">전환 점수 {a['shift_score']}/{a['shift_max']} (전환 후보 기준 {a['shift_min']}점)</p>
<div class=chips>{_chip_list(evid, EVID_KO)}</div>
{br}
<details><summary class=muted>최근 이벤트</summary><div class=scroll><table>
<tr><th>날짜</th><th>이벤트</th><th>종가</th><th>근거</th></tr>{ev_rows}</table></div></details>
<p class=muted style="margin-top:6px">데이터: {e(a['src'])} · {e(a['last_date'])} 마감{' (정규장 마감 시세로 보강)' if a.get('patched') else ''}</p>
</div>""")

    notes = "".join(f"<li>{e(n)}</li>" for n in snap.get("notes", []))
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>베어랠리 레이더</title>
<meta property="og:title" content="베어랠리 레이더 — {e(snap['as_of_date'])}">
<meta property="og:description" content="하락 레짐 판정 → 반등 포착 → 베어랠리/전환 판별 (SPY·QQQ·SOXX·KOSPI·BTC·ETH)">
<style>{CSS}</style></head><body><main>
<h1>베어랠리 레이더</h1>
<div class=sub>{e(snap['as_of_kst'])} KST 갱신 · {e(snap['basis'])}</div>
<div class=card>{table}</div>
<img src="{e(snap['overview'])}" alt="6개 자산 최근 12개월 상태 요약 차트">
<ul class="sub" style="margin-top:10px">{notes}</ul>
<h2>자산별 판정</h2>
{''.join(sections)}
<h2>판정 규칙</h2>
<div class="card flow"><ol>
<li><b>베어 진입</b> — 주식: 종가 &lt; 200일선, 200일선 하락(20봉 전 대비) 또는 데드크로스, 63일 고점 대비 −10% 이상.
크립토: 종가 &lt; 200일선, 직전 확정 주봉 종가 &lt; BMSB 하단(20주 SMA·21주 EMA), 63일 고점 대비 −20% 이상.</li>
<li><b>반등 감시</b> — 베어 레짐 안에서 직전 저점 대비 종가 +10%(크립토 +20%).</li>
<li><b>베어랠리 판정</b> — 저항 근거 3개 이상(RSI 60~70 천장, 50일선·200일선·BMSB·고점 AVWAP에 닿은 뒤 아래 마감, 하락폭 38.2% 이상 되돌림,
좁은 반등, ZBT 근접 실패) + 반등 고점 대비 2ATR 반락 + RSI 60 미만.</li>
<li><b>전환 후보</b> — 전환 점수 기준 이상(ZBT, 200일선 위 종목 50%↑, RSI 70 돌파 후 40 지지, 주봉 안착, 200일선 하락 멈춤, 200일선 회복).
브레드스가 없는 자산(국내·크립토)은 앞의 두 항목을 빼고 4점 만점에 3점.</li>
<li><b>강세 확정</b> — 전환 후보에서 주봉 2주 연속 기준선(주식 40주선, 크립토 BMSB 상단) 위 마감 + 종가 &gt; 200일선 + 50일선 상승.</li>
<li>반등 중 기준 저점을 깨면 베어로 돌아가고, 반등폭의 50%를 되돌리면 다음 반등을 다시 감시합니다.</li>
</ol>
<p class=muted>트레이딩뷰 지표(bear_rally_detector_v6.pine)와 같은 규칙·기본값입니다. 차이: 미국 브레드스는 S5TH·S5FI·NYSE 등락 대신
대형주 {snap['breadth']['universe']}종으로 근사하며, 근사 ZBT는 원본보다 자주 발생해 전환 점수 1점으로만 씁니다(단독 전환 우선권 없음).
상태 전환은 확정된 일봉으로만 판정하고 주봉 값은 직전 확정 주봉만 써서 지난 신호가 바뀌지 않습니다.</p>
</div>
<footer>관측 리포트입니다. 매수·매도 권유가 아니며 상태 판정은 과거 가격 규칙의 서술입니다.
데이터: Nasdaq(미국) · Coinbase/Kraken(크립토) · 네이버 증권(코스피). 매일 크립토 일봉 마감(09:00 KST) 뒤 갱신.</footer>
</main></body></html>"""
