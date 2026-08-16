"""주말 모드.

미국장이 닫힌 주말에 일간 브리프를 그대로 재발송하면 '멈춘 시스템'과
구분이 되지 않는다. 그래서 주말은 성격이 다른 두 산출물로 분리한다:

  토요일 — 주간 리뷰   : 지난 5거래일 동안 무엇이 바뀌었는가 (사후)
  일요일 — 주간 워치리스트 : 각 지표가 임계선까지 얼마나 남았는가 (사전)

둘 다 history.json 과 latest.json 에서 결정론적으로 계산한다.
새로 예측하거나 지어내는 값은 없다.
"""

from __future__ import annotations

STATE_ORDER = {"THESIS_BROKEN": 0, "THESIS_STRESSED": 1, "THESIS_INTACT": 2}


def _delta(curr, prev):
    if curr is None or prev is None:
        return None
    return round(curr - prev, 4)


def weekly_review(history: list[dict], report: dict, days: int = 5) -> dict:
    """지난 N거래일 변화 요약."""
    if not history:
        return {"available": False, "reason": "이력 없음 — 첫 주는 다음 주부터 집계됩니다"}

    window = history[-(days + 1):]
    if len(window) < 2:
        return {"available": False, "reason": f"이력 {len(window)}일 — 최소 2거래일 필요"}

    first, last = window[0], window[-1]

    # 이번 주에 켜진 경보를 코드별로 집계
    alert_counts: dict[str, int] = {}
    for record in window[1:]:
        for code in record.get("alerts") or []:
            alert_counts[code] = alert_counts.get(code, 0) + 1

    # 헤지 상태가 며칠 BROKEN 이었는가
    broken_days = sum(1 for r in window[1:] if r.get("hedge") == "BROKEN")
    watch_days = sum(1 for r in window[1:] if r.get("hedge") == "WATCH")

    # 노드 순위 변화 — 이번 주 시작 대비
    nodes = [n for n in (report.get("nodes") or []) if n.get("role") == "long"]
    movers = sorted(nodes, key=lambda n: n.get("rank_delta", 0), reverse=True)

    state_change = None
    if first.get("state") != last.get("state"):
        state_change = {"from": first.get("state"), "to": last.get("state")}

    return {
        "available": True,
        "span": {"from": first.get("d"), "to": last.get("d"), "sessions": len(window) - 1},
        "state": {"from": first.get("state"), "to": last.get("state"), "changed": state_change},
        "score_delta": _delta(last.get("score"), first.get("score")),
        "hedge": {
            "from": first.get("hedge"), "to": last.get("hedge"),
            "broken_days": broken_days, "watch_days": watch_days,
            "corr_delta": _delta(last.get("corr"), first.get("corr")),
            "spread_now": last.get("spread"),
        },
        "crowding": {
            "from": first.get("crowd"), "to": last.get("crowd"),
            "delta": _delta(last.get("crowd"), first.get("crowd")),
            "level": last.get("crowd_lv"),
        },
        "breadth": {"from": first.get("breadth"), "to": last.get("breadth"),
                    "delta": _delta(last.get("breadth"), first.get("breadth"))},
        "bottleneck": {"from": first.get("top"), "to": last.get("top"),
                       "changed": first.get("top") != last.get("top")},
        "alerts": sorted(alert_counts.items(), key=lambda kv: -kv[1]),
        "movers_up": [m for m in movers if m.get("rank_delta", 0) > 0][:3],
        "movers_down": [m for m in reversed(movers) if m.get("rank_delta", 0) < 0][:3],
    }


def watchlist(report: dict, thresholds: dict) -> dict:
    """각 지표가 다음 등급까지 얼마나 남았는지 거리로 표시.

    '무엇이 얼마나 더 나빠지면 경보인가'를 숫자로 못박아,
    다음 주에 무엇을 볼지 미리 고정한다.
    """
    hedge = report.get("hedge") or {}
    crowd = report.get("crowding") or {}
    fund = report.get("funding") or {}
    items: list[dict] = []

    hth = thresholds.get("hedge", {})
    corr = hedge.get("corr20")
    if corr is not None:
        broken_at = hth.get("corr20_broken", -0.40)
        items.append({
            "name": "롱·숏 20일 상관",
            "current": corr,
            "trigger": broken_at,
            "distance": round(corr - broken_at, 4),
            "breached": corr <= broken_at,
            "condition": f"{broken_at} 이하 + 동시손실이면 헤지 BROKEN",
            # 상관 조건을 이미 통과했다면 동시손실만 붙으면 즉시 BROKEN 이다
            "armed": corr <= broken_at or bool(hedge.get("both_legs_lose")),
            "note": "동시손실만 발생하면 즉시 BROKEN" if corr <= broken_at else None,
        })

    cut = crowd.get("cutoffs") or {}
    if crowd.get("score") is not None and cut.get("watch") is not None:
        target = cut.get("alert") if crowd.get("level") == "WATCH" else cut.get("watch")
        label = "ALERT" if crowd.get("level") == "WATCH" else "WATCH"
        items.append({
            "name": "혼잡도",
            "current": crowd.get("score"),
            "trigger": target,
            "distance": round(target - crowd["score"], 2),
            "breached": crowd["score"] >= target,
            "condition": f"{target} 이상이면 {label}",
            "armed": crowd.get("level") != "NORMAL" or crowd["score"] >= target,
            "note": None,
        })

    hy = (fund.get("values") or {}).get("hy_oas") or {}
    fth = thresholds.get("funding", {})
    if hy.get("value") is not None:
        target = fth.get("hy_oas_alert", 5.0) if fund.get("level") == "WATCH" else fth.get("hy_oas_watch", 4.0)
        items.append({
            "name": "HY 크레딧 스프레드",
            "current": hy["value"],
            "trigger": target,
            "distance": round(target - hy["value"], 2),
            "breached": hy["value"] >= target,
            "condition": f"{target}% 이상이면 경보 단계 상승",
            "armed": fund.get("level") != "NORMAL" or hy["value"] >= target,
            "note": None,
        })

    breadth_info = report.get("breadth") or {}
    if breadth_info.get("ratio") is not None:
        bth = thresholds.get("breadth", {})
        target = bth.get("broken", 0.25)
        items.append({
            "name": "논지 폭 (초과수익 노드 비율)",
            "current": breadth_info["ratio"],
            "trigger": target,
            "distance": round(breadth_info["ratio"] - target, 3),
            "breached": breadth_info["ratio"] <= target,
            "condition": f"{target} 이하면 논지 폭 붕괴 판정",
            "armed": breadth_info["ratio"] < bth.get("intact", 0.60),
            "note": None,
        })

    return {"items": items, "armed_count": sum(1 for i in items if i["armed"])}
