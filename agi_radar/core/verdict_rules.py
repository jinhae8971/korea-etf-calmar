"""규칙 기반 판정 엔진.

멀티에이전트 토론(L3)은 ANTHROPIC_API_KEY 가 있을 때만 돈다.
키가 없어도 시스템 전체가 멈추면 안 되므로, 결정론적 규칙 엔진을
항상 먼저 돌리고 LLM 토론은 그 위에 얹는 구조로 둔다.
규칙 엔진 결과는 Moderator 의 '선행 판단'으로도 쓰인다.
"""

from __future__ import annotations

STATES = ("THESIS_INTACT", "THESIS_STRESSED", "THESIS_BROKEN")


def evaluate(
    nodes: list[dict],
    breadth_info: dict,
    hedge: dict,
    crowding: dict,
    funding: dict,
    thresholds: dict,
) -> dict:
    reasons: list[str] = []
    score = 0.0

    # 1) 논지 폭 — 롱 노드 중 몇 개가 60일 초과수익인가 (가중 40)
    ratio = breadth_info.get("ratio")
    bth = thresholds.get("breadth", {})
    if ratio is None:
        reasons.append("노드 폭 산출 불가 (데이터 부족)")
    elif ratio >= bth.get("intact", 0.60):
        score += 40
        reasons.append(f"논지 폭 양호 — 롱 노드 {breadth_info['leading']}/{breadth_info['total']}개 60일 초과수익")
    elif ratio <= bth.get("broken", 0.25):
        score -= 25
        reasons.append(f"논지 폭 붕괴 — 롱 노드 {breadth_info['leading']}/{breadth_info['total']}개만 초과수익")
    else:
        score += 10
        reasons.append(f"논지 폭 혼조 — 롱 노드 {breadth_info['leading']}/{breadth_info['total']}개 초과수익")

    # 2) 헤지 유효성 (가중 30) — SA 가 죽은 지점
    hstatus = hedge.get("status")
    if hstatus == "OK":
        score += 30
        reasons.append("롱/숏 헤지 정상 작동 — 숏 다리가 변동성을 줄이고 있음")
    elif hstatus == "WATCH":
        score += 5
        reasons.append(f"헤지 약화 — 스프레드 변동성비 {hedge.get('spread_vol_ratio')}")
    elif hstatus == "BROKEN":
        score -= 30
        reasons.append(
            f"헤지 붕괴 — 변동성비 {hedge.get('spread_vol_ratio')}, 20일 상관 {hedge.get('corr20')}. "
            "롱·숏 동시 손실 구조 (SA 2026-07 패턴)"
        )

    # 3) 혼잡도 (가중 20, 역방향)
    clevel = crowding.get("level")
    if clevel == "NORMAL":
        score += 20
        reasons.append(f"포지셔닝 혼잡도 정상 ({crowding.get('score')})")
    elif clevel == "WATCH":
        score += 5
        reasons.append(f"혼잡도 상승 ({crowding.get('score')}) — 한 팩터 트레이드화 진행")
    elif clevel == "ALERT":
        score -= 20
        reasons.append(f"혼잡도 경보 ({crowding.get('score')}) — 청산 시 동반 하락 위험")

    # 4) 자금조달 스트레스 (가중 10, 역방향)
    flevel = funding.get("level")
    if flevel == "NORMAL":
        score += 10
        reasons.append("크레딧 스프레드 안정 — 자금조달 경로 정상")
    elif flevel == "WATCH":
        reasons.append("크레딧 스프레드 확대 조짐")
    elif flevel == "ALERT":
        score -= 20
        reasons.append("크레딧 스프레드 경보 — 레버리지 축소 압력")

    if score >= 60:
        state = "THESIS_INTACT"
    elif score >= 20:
        state = "THESIS_STRESSED"
    else:
        state = "THESIS_BROKEN"

    return {
        "state": state,
        "score": round(score, 1),
        "reasons": reasons,
        "alerts": _alerts(hedge, crowding, funding, nodes),
    }


def _alerts(hedge: dict, crowding: dict, funding: dict, nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    if hedge.get("status") == "BROKEN":
        out.append({
            "code": "HEDGE_BROKEN", "severity": "HIGH",
            "text": "롱/숏 헤지가 무력화됐습니다. 페어 구조라면 순노출을 줄이거나 페어를 해체해야 하는 국면입니다.",
        })
    if crowding.get("level") == "ALERT":
        out.append({
            "code": "CROWDING_ALERT", "severity": "HIGH",
            "text": "테마 내부 상관이 극단으로 올라 사실상 단일 팩터입니다. 분산 효과를 기대하지 마세요.",
        })
    if funding.get("level") == "ALERT":
        out.append({
            "code": "FUNDING_STRESS", "severity": "HIGH",
            "text": "크레딧 스프레드가 경보 구간입니다. 레버리지 상품·고베타 익스포저 축소 검토 구간입니다.",
        })
    longs = [n for n in nodes if n["role"] == "long"]
    if longs and longs[0].get("rank_delta", 0) >= 2:
        out.append({
            "code": "BOTTLENECK_SHIFT", "severity": "INFO",
            "text": f"병목 선두가 '{longs[0]['label']}'로 이동했습니다 (순위 {longs[0]['rank_delta']}단계 상승).",
        })
    return out
