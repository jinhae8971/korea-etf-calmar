"""텔레그램 브리프 렌더링.

포매팅은 이 레포에서만 한다. 릴레이 레포는 latest.json 의 `message` 필드를
그대로 전달만 하므로 포맷이 두 곳에서 이중 관리되지 않는다.
"""

from __future__ import annotations

import html

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
