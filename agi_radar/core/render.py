"""텔레그램 브리프 렌더링.

포매팅은 이 레포에서만 한다. 릴레이 레포는 latest.json 의 `message` 필드를
그대로 전달만 하므로 포맷이 두 곳에서 이중 관리되지 않는다.
"""

from __future__ import annotations

import html
import re

STATE_BADGE = {
    "THESIS_INTACT": "🟢 논지 유지",
    "THESIS_STRESSED": "🟡 논지 압박",
    "THESIS_BROKEN": "🔴 논지 훼손",
}
HEDGE_BADGE = {"OK": "🟢 정상", "WATCH": "🟡 약화", "BROKEN": "🔴 붕괴", "NO_DATA": "⚪ 데이터부족"}
LEVEL_BADGE = {"NORMAL": "🟢", "WATCH": "🟡", "ALERT": "🔴", "NO_DATA": "⚪"}


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:+.1f}%"



# ---------------------------------------------- 가시성 규격 v2 (2026-09-07)
# 글자 수보다 "한눈에 읽히는가"가 기준. 상한은 안전장치이지 목표가 아니다.
#   · 한 줄 폭 LINE_COLS(반각) 이하 — 넘치면 모바일에서 접히고 들여쓰기가
#     사라져 항목 경계가 무너진다. 접느니 줄을 나눈다.
#   · 모든 증감에 색 점을 붙인다(전 트랙 동일 임계).
DIGEST_CAP = 900
LINE_COLS = 40
VIS_FROZEN_AT = "2026-09-07"
_TAGRE = re.compile(r"<[^>]+>")


def vis_width(s):
    """한글·이모지는 2칸으로 세는 표시 폭."""
    return sum(2 if ord(c) > 0x2000 else 1 for c in _TAGRE.sub("", s or ""))


def dot(v, unit="%"):
    """증감 색 점. pp 는 %와 겨루도록 3배 가중(압축 규격과 동일 기준)."""
    if v is None:
        return "\u26aa"
    x = v * 3.0 if unit == "pp" else v
    if x >= 20:
        return "\U0001F7E9"
    if x >= 3:
        return "\U0001F7E2"
    if x > -3:
        return "\u26aa"
    if x > -20:
        return "\U0001F534"
    return "\U0001F7E5"


def sig(v, unit="%", digits=0):
    """색 점 + 부호 있는 값. 예: 🟢+8% / 🔴-12%"""
    if v is None:
        return "\u26aa–"
    return "%s%+.*f%s" % (dot(v, unit), digits, v, "pp" if unit == "pp" else "%")


def rank_arrow(d):
    if not d:
        return ""
    return " \u25b2%d" % d if d > 0 else " \u25bc%d" % abs(d)


def wrap_items(label, items, cols=LINE_COLS, sep=" \u00b7 "):
    """라벨 + 항목들을 폭 상한에 맞춰 여러 줄로. 이어지는 줄은 공백 들여쓰기."""
    out, cur = [], "<b>%s</b> " % label
    pad = " " * (len(label) + 1)
    for it in items:
        cand = cur + (sep if cur.strip() != ("<b>%s</b>" % label) and not cur.endswith(" ") else "") + it
        if vis_width(cand) > cols and vis_width(cur) > vis_width("<b>%s</b> " % label):
            out.append(cur.rstrip())
            cur = pad + it
        else:
            cur = cand if cur.endswith(" ") else cur + sep + it
    if cur.strip():
        out.append(cur.rstrip())
    return out


def clip(s, budget=LINE_COLS):
    """표시 폭 기준으로 자른다. 글자 수로 자르면 한글에서 여전히 넘친다."""
    limit = budget
    out, w = [], 0
    for ch in str(s):
        cw = 2 if ord(ch) > 0x2000 else 1
        if w + cw > limit - 2:   # 말줄임표(…)도 폭 2
            out.append("\u2026")
            break
        out.append(ch)
        w += cw
    return "".join(out)


def cap_lines(lines, tail, cap=DIGEST_CAP):
    budget = cap - sum(len(_TAGRE.sub("", t)) + 1 for t in tail)
    out, used = [], 0
    for ln in lines:
        n = len(_TAGRE.sub("", ln)) + 1
        if used + n > budget:
            out.append("\u2026")
            break
        out.append(ln)
        used += n
    return out + tail


def plain_len(s):
    return len(_TAGRE.sub("", s or ""))


def render_digest(report: dict, dashboard_url: str = "") -> str:
    """가시성 규격 v2 — 한 항목 한 줄, 증감은 색 점으로."""
    verdict = report["verdict"]
    hedge = report["hedge"]
    crowding = report["crowding"]
    nodes = report["nodes"]

    L = [f"🛰 <b>AGI Thesis Radar</b> · {report['date'][5:]}",
         f"{STATE_BADGE.get(verdict['final_state'], verdict['final_state'])} "
         f"<i>(확신 {verdict['confidence_score']}%)</i>",
         ""]

    L.append("<b>지표</b>")
    sp = hedge.get("spread_return")
    L.append("· 헤지 %s <i>· 스프레드 %s</i>" % (
        HEDGE_BADGE.get(hedge.get("status"), "⚪"),
        sig(sp * 100 if sp is not None else None, digits=1)))
    L.append("· 혼잡 %s %s <i>(%s)</i>" % (
        LEVEL_BADGE.get(crowding.get("level"), "⚪"), crowding.get("score"),
        crowding.get("level")))
    vr = hedge.get("spread_vol_ratio")
    if vr is not None:
        L.append("· 변동성비 %.2f" % float(vr))

    longs = [n for n in nodes if n["role"] == "long"][:4]
    if longs:
        L.append("")
        L.append("<b>병목 순위</b> <i>(20일 초과수익)</i>")
        for n in longs:
            r = n.get("rs20")
            L.append("%s %s%s" % (
                sig(r * 100 if r is not None else None, digits=1),
                html.escape(clip(n["label"], 26)),
                rank_arrow(n.get("rank_delta"))))

    shift = report.get("bottleneck_shift")
    if shift and shift.get("shifted"):
        L.append("↳ <i>이동 %s → %s</i>" % (
            html.escape(clip(shift["previous"], 12)),
            html.escape(clip(shift["current"], 12))))

    # 종합(summary)과 인사이트(key_insights)는 같은 문장이라 인사이트만 남긴다.
    ins = (verdict.get("key_insights") or [])[:3]
    if ins:
        L.append("")
        L.append("<b>판정 근거</b>")
        for i in ins:
            L.append("· " + html.escape(clip(str(i), LINE_COLS - 2)))

    shifted = bool(shift and shift.get("shifted"))
    alerts = [a for a in report["rule_verdict"]["alerts"]
              if not (shifted and a.get("code") == "BOTTLENECK_SHIFT")]
    if alerts:
        L.append("")
        for a in alerts[:2]:
            mark = "🔴" if a["severity"] == "HIGH" else "ℹ️"
            L.append("%s %s" % (mark, html.escape(clip(a["text"], LINE_COLS - 3))))

    tail = [""]
    if dashboard_url:
        tail.append(f'📎 <a href="{dashboard_url}">전체 대시보드</a>')
    tail.append("<i>참고 정보 · 매매 권유 아님</i>")
    return "\n".join(cap_lines(L, tail))


def render_brief(report: dict, dashboard_url: str = "") -> str:
    verdict = report["verdict"]
    hedge = report["hedge"]
    crowding = report["crowding"]
    funding = report["funding"]
    nodes = report["nodes"]

    lines = [
        f"<b>🛰 AGI Thesis Radar — {report['date']}</b>",
        "",
        f"{STATE_BADGE.get(verdict['final_state'], verdict['final_state'])} "
        f"(확신도 {verdict['confidence_score']}%)",
        "",
        "<b>① 헤지 유효성</b>  " + HEDGE_BADGE.get(hedge.get("status"), "⚪"),
        f"  20일 상관 {hedge.get('corr20')} · 변동성비 {hedge.get('spread_vol_ratio')}",
        f"  롱 {_pct(hedge.get('long_return_20d'))} vs 숏 {_pct(hedge.get('short_return_20d'))} "
        f"→ 스프레드 {_pct(hedge.get('spread_return'))}",
        "",
        f"<b>② 혼잡도</b>  {LEVEL_BADGE.get(crowding.get('level'), '⚪')} {crowding.get('score')}"
        f" ({crowding.get('level')})",
        f"<b>③ 자금조달</b>  {LEVEL_BADGE.get(funding.get('level'), '⚪')} {funding.get('level')}",
    ]

    hy = (funding.get("values") or {}).get("hy_oas") or {}
    if hy.get("value") is not None:
        stale = " (캐시)" if hy.get("stale") else ""
        lines.append(f"  HY OAS {hy['value']:.2f}%{stale} · 20일 {hy.get('change_20d')}")

    longs = [n for n in nodes if n["role"] == "long"][:4]
    if longs:
        lines += ["", "<b>④ 병목 순위 (20일 초과수익)</b>"]
        for node in longs:
            arrow = "▲" if node["rank_delta"] > 0 else ("▼" if node["rank_delta"] < 0 else "–")
            lines.append(
                f"  {node.get('long_rank', node['rank'])}. {html.escape(node['label'])} {_pct(node['rs20'])} "
                f"{arrow}{abs(node['rank_delta']) or ''}"
            )

    shift = report.get("bottleneck_shift")
    if shift and shift.get("shifted"):
        lines.append(f"  ↳ 병목 이동: {html.escape(shift['previous'])} → {html.escape(shift['current'])}")

    alerts = report["rule_verdict"]["alerts"]
    if alerts:
        lines += ["", "<b>⚠️ 경보</b>"]
        for alert in alerts[:4]:
            mark = "🔴" if alert["severity"] == "HIGH" else "ℹ️"
            lines.append(f"  {mark} {html.escape(alert['text'])}")

    summary = (verdict.get("summary") or "").strip()
    if summary:
        lines += ["", "<b>📌 종합</b>", html.escape(summary[:420])]

    insights = verdict.get("key_insights") or []
    if insights:
        lines += ["", "<b>💡 인사이트</b>"]
        lines += [f"  · {html.escape(str(i))[:160]}" for i in insights[:3]]

    actions = verdict.get("action_items") or []
    if actions:
        lines += ["", "<b>✅ 점검 항목</b>"]
        lines += [f"  · {html.escape(str(a))[:160]}" for a in actions[:3]]

    status = report.get("data_status", {})
    if status.get("mode") not in ("OK", None):
        lines += ["", f"⚠️ 데이터 상태: {status.get('mode')} "
                      f"(신규 {status.get('fresh')} / 캐시 {status.get('from_cache')})"]

    engine = "멀티에이전트 토론" if verdict.get("llm") else "규칙 엔진"
    lines += ["", f"<i>판정 엔진: {engine}</i>"]
    if dashboard_url:
        lines.append(f"📎 {dashboard_url}")

    lines += ["", "<i>투자 판단의 참고 정보이며, 매매 권유가 아닙니다.</i>"]
    return "\n".join(lines)


STATE_SHORT = {"THESIS_INTACT": "논지 유지", "THESIS_STRESSED": "논지 압박",
               "THESIS_BROKEN": "논지 훼손"}
ALERT_LABEL = {
    "HEDGE_BROKEN": "헤지 붕괴", "CROWDING_ALERT": "혼잡도 경보",
    "FUNDING_STRESS": "자금조달 스트레스", "BOTTLENECK_SHIFT": "병목 이동",
}


def _arrow(value) -> str:
    if value is None:
        return "–"
    if value > 0:
        return f"▲{abs(value)}"
    if value < 0:
        return f"▼{abs(value)}"
    return "–"


def render_weekly(report: dict, review: dict, dashboard_url: str = "") -> str:
    """토요일 — 주간 리뷰. 지난 5거래일에 무엇이 바뀌었는가."""
    lines = [f"<b>📅 AGI Thesis Radar — 주간 리뷰 ({report['date']})</b>"]

    if not review.get("available"):
        lines += ["", f"⚠️ {html.escape(review.get('reason', '집계 불가'))}",
                  "", "<i>다음 주 토요일부터 정상 집계됩니다.</i>"]
        return "\n".join(lines)

    span = review["span"]
    lines += ["", f"기간 {span['from']} → {span['to']} ({span['sessions']}거래일)", ""]

    state = review["state"]
    if state.get("changed"):
        lines.append(
            f"<b>🔄 판정 변화</b>  {STATE_SHORT.get(state['from'], state['from'])} → "
            f"<b>{STATE_SHORT.get(state['to'], state['to'])}</b>"
        )
    else:
        lines.append(f"<b>판정</b>  {STATE_SHORT.get(state['to'], state['to'])} 유지 "
                     f"(점수 {_arrow(review.get('score_delta'))})")

    hedge = review["hedge"]
    lines += [
        "",
        f"<b>① 헤지</b>  {hedge['from']} → {hedge['to']}",
        f"  BROKEN {hedge['broken_days']}일 / WATCH {hedge['watch_days']}일 "
        f"(총 {span['sessions']}일 중)",
        f"  20일 상관 변화 {_arrow(hedge.get('corr_delta'))} · "
        f"주간 스프레드 {_pct(hedge.get('spread_now'))}",
    ]

    crowd = review["crowding"]
    lines += ["", f"<b>② 혼잡도</b>  {crowd['from']} → {crowd['to']} "
                  f"({_arrow(crowd.get('delta'))}) · 현재 {crowd.get('level')}"]

    br = review["breadth"]
    if br.get("to") is not None:
        lines.append(f"<b>③ 논지 폭</b>  {br['from']} → {br['to']} ({_arrow(br.get('delta'))})")

    bn = review["bottleneck"]
    if bn.get("changed"):
        lines += ["", f"<b>④ 병목 이동</b>  {html.escape(str(bn['from']))} → "
                      f"<b>{html.escape(str(bn['to']))}</b>"]
    else:
        lines += ["", f"<b>④ 병목</b>  {html.escape(str(bn.get('to') or '–'))} 유지"]

    if review.get("movers_up") or review.get("movers_down"):
        lines.append("")
        for node in review.get("movers_up", []):
            lines.append(f"  ▲ {html.escape(node['label'])} {node['rank_delta']}단계 상승 "
                         f"({_pct(node.get('rs20'))})")
        for node in review.get("movers_down", []):
            lines.append(f"  ▼ {html.escape(node['label'])} {abs(node['rank_delta'])}단계 하락 "
                         f"({_pct(node.get('rs20'))})")

    if review.get("alerts"):
        lines += ["", "<b>⚠️ 이번 주 경보 발생</b>"]
        for code, count in review["alerts"][:4]:
            lines.append(f"  · {ALERT_LABEL.get(code, code)} — {count}일")
    else:
        lines += ["", "<b>⚠️ 경보</b>  이번 주 발생 없음"]

    lines += ["", f"<i>기준 종가 {report.get('as_of_close', '–')} · 미국장 휴장 중</i>"]
    if dashboard_url:
        lines.append(f"📎 {dashboard_url}")
    lines += ["", "<i>투자 판단의 참고 정보이며, 매매 권유가 아닙니다.</i>"]
    return "\n".join(lines)


def render_watchlist(report: dict, watch: dict, dashboard_url: str = "") -> str:
    """일요일 — 다음 주 워치리스트. 각 지표가 임계선까지 얼마나 남았는가."""
    lines = [
        f"<b>🎯 AGI Thesis Radar — 다음 주 워치리스트 ({report['date']})</b>",
        "",
        f"현재 판정: {STATE_SHORT.get((report.get('verdict') or {}).get('final_state'), '–')} · "
        f"발동 임박 지표 {watch.get('armed_count', 0)}개",
        "",
        "<b>임계선까지 남은 거리</b>",
    ]

    if not watch.get("items"):
        lines.append("  데이터 부족 — 산출 불가")
    for item in watch["items"]:
        breached = item.get("breached")
        mark = "🔴" if breached else ("🟠" if item["armed"] else "🟢")
        gap = "<b>이미 통과</b>" if breached else f"여유 {item['distance']:+}"
        lines += [
            f"{mark} <b>{html.escape(item['name'])}</b>",
            f"    현재 {item['current']} → 발동 {item['trigger']} ({gap})",
            f"    <i>{html.escape(item['condition'])}</i>",
        ]
        if item.get("note"):
            lines.append(f"    ⚠️ {html.escape(item['note'])}")

    longs = [n for n in (report.get("nodes") or []) if n.get("role") == "long"][:3]
    if longs:
        lines += ["", "<b>병목 상위 노드 (다음 주 관찰 대상)</b>"]
        for node in longs:
            lines.append(f"  {node.get('long_rank', node['rank'])}. "
                         f"{html.escape(node['label'])} {_pct(node.get('rs20'))}")

    lines += ["", f"<i>기준 종가 {report.get('as_of_close', '–')}</i>"]
    if dashboard_url:
        lines.append(f"📎 {dashboard_url}")
    lines += ["", "<i>투자 판단의 참고 정보이며, 매매 권유가 아닙니다.</i>"]
    return "\n".join(lines)
